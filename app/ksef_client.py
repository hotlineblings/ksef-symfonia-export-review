"""Klient KSeF 2.0 (produkcja): auth samym tokenem KSeF, query metadanych,
pobieranie pojedynczych faktur oraz eksport wsadowy (paczka ZIP).

Kontrakt wg reference/ksef2-recipe.md + żywego OpenAPI v2.6.1 (2026-07-08):
- auth: challenge -> RSA-OAEP(SHA-256) na "token|timestampMs" -> /auth/ksef-token
  -> poll /auth/{ref} -> /auth/token/redeem (jednorazowy) -> accessToken/refreshToken
- query: POST /invoices/query/metadata (Subject1=sprzedaż, Subject2=koszt),
  max 3 miesiące na zapytanie, paginacja hasMore/isTruncated
- query przyrostowe (spóźnione faktury): ten sam endpoint po dacie trwałego
  zapisu (dateType=PermanentStorage) od punktu kontynuacji do „teraz”,
  z restrictToPermanentStorageHwmDate=true; odpowiedź niesie
  permanentStorageHwmDate = nowy punkt (docs MF: przyrostowe-pobieranie-faktur.md,
  hwm.md; zakres maks. 100 dni w UTC)
- download: GET /invoices/ksef/{ksefNumber} (limit 64/h!), błąd 21165 = retry
- eksport wsadowy: POST /invoices/exports (klucz AES-256 szyfrowany RSA-OAEP
  certyfikatem usage=SymmetricKeyEncryption; IV 16 B; pula 20/h osobna od
  metadanych) -> poll GET /invoices/exports/{ref} (100=w toku, 200=gotowe;
  własna pula 600/h) -> części .zip.aes z presigned URL (bez auth i limitów,
  ważny ok. 1 h) -> każda część AES-256-CBC/PKCS7 osobno -> sklejenie wg
  ordinalNumber -> JEDEN ZIP z XML i `_metadata.json` (InvoiceMetadata).
  Paczka: maks. 10 000 faktur / 1 GB, ucięta -> isTruncated + lastIssueDate
  (`pobierz_paczkami` dociąga kolejnym zleceniem).

Limity są rozliczane po stronie KSeF per NIP i godzinę ZEGAROWĄ, niezależnie
od naszych uruchomień — dlatego znaczniki wywołań trzymamy trwale (QuotaStore),
a blokady narzucone przez serwer (429 Retry-After) zapamiętujemy i zgłaszamy
jako KsefQuotaExceeded zamiast ślepo czekać dziesiątki minut.
"""

from __future__ import annotations

import base64
import binascii
import collections
import email.utils
import hashlib
import inspect
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import zipfile
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import PurePosixPath, PureWindowsPath
from typing import Callable, Iterator

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from dateutil.relativedelta import relativedelta

from app.quota import QuotaStore

logger = logging.getLogger(__name__)

PROD_BASE_URL = "https://api.ksef.mf.gov.pl/v2"
TEST_BASE_URL = "https://api-test.ksef.mf.gov.pl/v2"

MAX_QUERY_WINDOW_MONTHS = 3
PAGE_SIZE = 250

# limity z OpenAPI: (okno_sekund, max_wywołań)
METADATA_LIMITS = [(1, 8), (60, 16), (3600, 20)]
DOWNLOAD_LIMITS = [(1, 8), (60, 16), (3600, 64)]
# POST /invoices/exports — osobna pula od metadanych (docs MF limity-api.md)
EXPORT_LIMITS = [(1, 8), (60, 16), (3600, 20)]
# GET /invoices/exports/{ref} — własna, dużo większa pula; 429 na statusie nie
# może blokować zlecania paczek
EXPORT_STATUS_LIMITS = [(1, 10), (60, 60), (3600, 600)]

RETRY_21165_DELAYS = [5, 15, 30, 60, 120]

# powyżej tylu sekund oczekiwania nie czekamy w pętli, tylko zgłaszamy
# KsefQuotaExceeded z godziną wznowienia (czytelny komunikat zamiast zwisu)
LONG_WAIT_S = 180.0

# zapytanie „od punktu” (PermanentStorage): KSeF przyjmuje zakres maks. 100 dni
# w UTC; okna po 90 dni zostawiają zapas na rozjazd zegara komputera i KSeF
SINCE_WINDOW = timedelta(days=90)
# punkt starszy niż tyle jest przycinany (maks. 4 okna = 4 zapytania z puli
# 20/h) — zapytanie o prawie rok zaległości to i tak sytuacja wyjątkowa
SINCE_MAX_LOOKBACK = timedelta(days=360)

# status paczki: co 5 s przez pierwszą minutę, potem co 15 s (paczka 10 000
# faktur przygotowuje się minutami — częstsze pytanie nic nie daje)
EXPORT_POLL_INTERVAL_S = 5.0
EXPORT_POLL_INTERVAL_POZNIEJ_S = 15.0
EXPORT_POLL_SZYBKO_PRZEZ_S = 60.0
# dokumentacja MF nie podaje czasu przygotowania paczki; status żyje 7 dni
EXPORT_TIMEOUT_S = 1800.0
# zapamiętane zlecenie, którego odbiór w kolejnym eksporcie znów kończy się
# timeoutem, uznajemy za zawieszone dopiero, gdy od zamówienia (zegar KSeF)
# minęło tyle — wolny KSeF (przygotowanie do ok. 3,5 h) zdąży, a zawieszone
# zlecenie blokuje eksporty tego zakresu najwyżej ok. 4 h
ZLECENIE_ZAWIESZONE_PO = timedelta(hours=3.5)

# części paczki czytane strumieniowo (część to do 50 MB); sklejone,
# odszyfrowane archiwum (do 1 GB wg limitu KSeF) — w pliku tymczasowym na dysku
CZESC_KAWALEK = 2**20
# limity paczki KSeF: 10 000 faktur albo 1 GB. Paczka, która je osiąga, jest
# traktowana jak ucięta także BEZ isTruncated (błąd tej klasy KSeF miał
# w onlyMetadata, poprawiony w 2.6.1) — kontynuacja kosztuje najwyżej jedno
# zlecenie, pominięcie oznaczałoby cichą stratę reszty zakresu
PACZKA_PELNA_FAKTUR = 10_000
PACZKA_PELNA_BAJTOW = 900 * 2**20


class KsefError(Exception):
    """Błąd komunikacji z KSeF."""


class KsefAuthError(KsefError):
    """Błąd uwierzytelnienia (415 = token bez InvoiceRead, 450 = krypto/czas)."""


class KsefCancelled(KsefError):
    """Operacja przerwana przez użytkownika."""


class KsefInvoiceNotReady(KsefError):
    """Błąd 21165: faktura przetworzona, ale jeszcze niedostępna do pobrania."""


class KsefExportTimeout(KsefError):
    """Paczka eksportu nie była gotowa w zadanym czasie — zlecenie żyje w KSeF
    (status 7 dni). `do_odbioru` — zlecenie zapamiętane: kolejny eksport tego
    zakresu odbierze je zamiast zamawiać nowe (False: uznane za zawieszone)."""

    do_odbioru = True


class KsefQuotaExceeded(KsefError):
    """Limit API wyczerpany — wznowienie możliwe po `until_ts` (czas lokalny)."""

    def __init__(self, group_label: str, until_ts: float):
        self.until_ts = until_ts
        resume = datetime.fromtimestamp(until_ts).strftime("%H:%M")
        super().__init__(
            f"Limit KSeF ({group_label}) wyczerpany — wznowienie ok. {resume}"
        )


@dataclass(frozen=True)
class InvoiceMetadata:
    ksef_number: str
    invoice_number: str
    issue_date: str
    invoicing_date: str | None
    acquisition_date: str | None
    seller_nip: str
    seller_name: str
    buyer_id: str
    buyer_name: str
    net: Decimal | None
    gross: Decimal | None
    vat: Decimal | None
    currency: str
    form_system_code: str
    invoice_type: str
    # data trwałego zapisu w KSeF (PermanentStorage) — od niej liczy się
    # synchronizacja przyrostowa (punkt kontynuacji, spóźnione faktury)
    permanent_storage_date: str | None = None

    @property
    def ksef_date(self) -> str:
        """Data otrzymania faktury -> 'data wpływu' w FK (YYYY-MM-DD).

        Prawnie datą otrzymania jest data nadania numeru KSeF (acquisitionDate;
        docs MF limity-api.md, przyrostowe-pobieranie-faktur.md), liczona jako
        dzień kalendarzowy w Polsce. KSeF zwraca znaczniki w UTC — obcięcie
        pierwszych 10 znaków dawało dzień wcześniej dla faktur przyjętych
        między północą a 02:00 (latem) / 01:00 (zimą), a 1. dnia miesiąca
        przenosiło VAT kosztu do poprzedniego miesiąca."""
        raw = self.acquisition_date or self.invoicing_date or self.issue_date
        return warsaw_date(raw)


@dataclass(frozen=True)
class PermanentStorageQuery:
    """Wynik zapytania „co wpłynęło do KSeF od punktu” (`query_metadata_since`)."""

    invoices: list[InvoiceMetadata]
    # permanentStorageHwmDate z ostatniej odpowiedzi: do tego momentu KSeF
    # gwarantuje kompletność — nowy punkt kontynuacji; None = KSeF nie podał
    hwm: str | None
    since: str      # faktycznie użyty początek (po ewentualnym przycięciu)
    clamped: bool   # początek przycięty do `max_lookback` wstecz od teraz
    # False = sprawdzenie przerwane (budżet stron `przed_strona` albo limit KSeF);
    # `progress` = punkt, od którego trzeba je wznowić: data trwałego zapisu
    # ostatniej odebranej faktury (dateRange.from jest włącznie — faktury z tą
    # samą datą wrócą i odpadną w rejestrze) albo granica ostatniego okna
    # przeczytanego w całości; `hwm` = z ostatniej odebranej strony
    complete: bool = True
    progress: str | None = None


class _CzescNiedostepna(KsefError):
    """URL części paczki wygasł (403/410) albo zerwane połączenie — do ponowienia
    z nowym URL-em z ponownego odczytu statusu."""


class PaczkaEksportu:
    """Paczka eksportu po pobraniu WSZYSTKICH części, odszyfrowaniu każdej
    i sklejeniu wg ordinalNumber: jedno archiwum ZIP (w pamięci do
    PACZKA_W_PAMIECI_MAX, większe na dysku). XML faktur czytany leniwie
    z archiwum; `_metadata.json` (zawsze w paczce od 27.10.2025, model
    InvoiceMetadata jak w query/metadata) — `wszystkie_metadane()`.

    `metadane` wypełnia `KsefClient.pobierz_paczkami`: faktury z tej paczki
    jeszcze niewidziane w poprzednich paczkach tego samego zakresu."""

    def __init__(self, reference: str, package: dict, plik, archiwum: zipfile.ZipFile,
                 *, pusta: bool = False, rozmiar: int = 0):
        self.reference = reference
        self.package = package
        self._plik = plik
        self._archiwum = archiwum
        self._pusta = pusta
        self.rozmiar = rozmiar          # bajty sklejonego archiwum (odszyfrowanego)
        self._xml: dict[str, str] = {}
        self._metadata_json: str | None = None
        self.powtorzone: list[str] = []  # nazwy plików występujące w archiwum > 1 raz
        for name in archiwum.namelist():
            base = PurePosixPath(PureWindowsPath(name).as_posix()).name
            if base == "_metadata.json":
                if self._metadata_json is not None:
                    self.powtorzone.append(base)
                self._metadata_json = name
            elif base.lower().endswith(".xml"):
                if base[:-4] in self._xml:
                    self.powtorzone.append(base)
                self._xml[base[:-4]] = name
        self.metadane: list[InvoiceMetadata] = []
        # wypełnia pobierz_paczkami: w tym przebiegu przyjdą jeszcze kolejne paczki
        self.kolejne = False
        # paczka odebrana z zamówienia z POPRZEDNIEGO eksportu: moment zamówienia
        # (UTC) — jej zawartość to stan KSeF z tamtej chwili; `zamowiona_zegar_ksef`
        # — moment z zegara KSeF (nagłówek Date), inaczej z zegara komputera
        self.zamowiona: datetime | None = None
        self.zamowiona_zegar_ksef = False

    @property
    def obcieta(self) -> bool:
        return bool(self.package.get("isTruncated"))

    def numery_xml(self) -> list[str]:
        return list(self._xml)

    def xml(self, ksef_number: str) -> bytes:
        name = self._xml.get(ksef_number)
        if name is None:
            raise KsefError(f"Brak pliku XML faktury {ksef_number} w paczce eksportu")
        try:
            return self._archiwum.read(name)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError, OSError) as exc:
            raise KsefError(f"Uszkodzony plik {name} w paczce eksportu: {exc}") from exc

    def wszystkie_metadane(self) -> list[InvoiceMetadata]:
        if self._pusta:
            return []
        if self._metadata_json is None:
            raise KsefError("Paczka eksportu bez pliku _metadata.json")
        try:
            dane = json.loads(self._archiwum.read(self._metadata_json))
        except (ValueError, zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError,
                OSError) as exc:
            raise KsefError(f"Nieczytelny _metadata.json w paczce eksportu: {exc}") from exc
        faktury = dane.get("invoices") if isinstance(dane, dict) else dane
        if not isinstance(faktury, list):
            raise KsefError("_metadata.json w paczce eksportu bez listy faktur (invoices)")
        return [KsefClient._parse_metadata(raw) for raw in faktury]

    def close(self) -> None:
        try:
            self._archiwum.close()
        finally:
            self._plik.close()

    def __enter__(self) -> "PaczkaEksportu":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _RateLimiter:
    """Pacing pod limity KSeF; okna sekunda/minuta/godzina liczone TRWALE
    (QuotaStore) per NIP, więc kolejne uruchomienia widzą zużyty budżet."""

    def __init__(
        self,
        limits: list[tuple[int, int]],
        quota: QuotaStore,
        nip: str,
        group: str,
        group_label: str,
        cancel: threading.Event | None,
    ):
        self._limits = limits
        self._quota = quota
        self._nip = nip
        self._group = group
        self._label = group_label
        self._cancel = cancel

    def acquire(self) -> None:
        block = self._quota.block_remaining(self._nip, self._group)
        if block > LONG_WAIT_S:
            raise KsefQuotaExceeded(self._label, time.time() + block)
        if block > 0:
            logger.info("Blokada serwera KSeF (%s): czekam %.0f s", self._label, block)
            _sleep(block, self._cancel)

        announced = False
        last_log = 0.0
        while True:
            now = time.time()
            horizon = max(w for w, _ in self._limits)
            events = self._quota.recent(self._nip, self._group, horizon)
            wait = 0.0
            for window, limit in self._limits:
                recent = [t for t in events if t > now - window]
                if len(recent) >= limit:
                    wait = max(wait, recent[0] + window - now)
            if wait <= 0:
                self._quota.record(self._nip, self._group)
                return
            if wait > LONG_WAIT_S:
                raise KsefQuotaExceeded(self._label, now + wait)
            if wait > 60:
                if not announced or now - last_log >= 60:
                    logger.info(
                        "Limit %s wyczerpany — wznowienie za ~%d min (czekam "
                        "automatycznie; Przerwij zatrzymuje eksport)",
                        self._label, int(wait / 60) + 1,
                    )
                    announced, last_log = True, now
                _sleep(min(wait, 30), self._cancel)
            else:
                if wait > 5:
                    logger.info("Limit %s: czekam %.0f s", self._label, wait)
                _sleep(wait, self._cancel)

    def remaining_in_window(self, window: int) -> int:
        if self._quota.block_remaining(self._nip, self._group) > 0:
            return 0
        used = len(self._quota.recent(self._nip, self._group, window))
        limit = dict(self._limits)[window]
        return max(0, limit - used)

    def block_remaining(self) -> float:
        return self._quota.block_remaining(self._nip, self._group)

    def set_block(self, seconds: float) -> None:
        self._quota.set_block(self._nip, self._group, time.time() + seconds)


def _sleep(seconds: float, cancel: threading.Event | None) -> None:
    if cancel is None:
        time.sleep(seconds)
    elif cancel.wait(seconds):
        raise KsefCancelled("Przerwano na żądanie użytkownika")


def _decode_jwt_exp(jwt_token: str) -> int | None:
    """Odczyt `exp` z JWT bez weryfikacji podpisu (tylko do planowania odświeżenia)."""
    try:
        payload_b64 = jwt_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload["exp"])
    except (IndexError, KeyError, ValueError, binascii.Error):
        return None


def split_date_range(date_from: date, date_to: date) -> list[tuple[date, date]]:
    """Dzieli zakres na okna <= 3 miesiące (wymóg API)."""
    if date_from > date_to:
        raise ValueError("data 'od' późniejsza niż 'do'")
    windows: list[tuple[date, date]] = []
    start = date_from
    while start <= date_to:
        end = min(start + relativedelta(months=MAX_QUERY_WINDOW_MONTHS) - timedelta(days=1), date_to)
        windows.append((start, end))
        start = end + timedelta(days=1)
    return windows


def _last_sunday_1utc(year: int, month: int) -> datetime:
    """Ostatnia niedziela marca/października, 01:00 UTC (moment zmiany czasu w UE)."""
    last = date(year, month, 31)
    last -= timedelta(days=(last.weekday() - 6) % 7)
    return datetime(last.year, last.month, last.day, 1, 0, tzinfo=timezone.utc)


def _warsaw_offset(utc: datetime) -> timedelta:
    """Przesunięcie czasu polskiego względem UTC (reguła UE, obowiązuje w Polsce
    od 1996 r.). Liczone w kodzie, bo zoneinfo na Windows wymaga pakietu tzdata,
    którego nie ma w requirements."""
    summer = _last_sunday_1utc(utc.year, 3) <= utc < _last_sunday_1utc(utc.year, 10)
    return timedelta(hours=2 if summer else 1)


_KSEF_DATETIME = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?"
    r"\s*(Z|[+-]\d{2}:?\d{2})?"
)


def _parse_ksef_datetime(s: str) -> datetime | None:
    """Znacznik czasu z KSeF -> datetime (ze strefą, jeśli podana). Własny
    parser zamiast datetime.fromisoformat: na Pythonie 3.10 ten nie przyjmuje
    'Z', '+0000' ani ułamka innego niż 3 lub 6 cyfr (KSeF zwraca 1–7 cyfr),
    a wynik nie może zależeć od wersji interpretera, z którą zbudowano exe."""
    m = _KSEF_DATETIME.fullmatch(s)
    if m is None:
        return None
    rok, mies, dzien, godz, minuta, sek, ulamek, strefa = m.groups()
    mikro = int((ulamek or "0").ljust(6, "0")[:6])
    tz = None
    if strefa:
        if strefa == "Z":
            tz = timezone.utc
        else:
            znak = -1 if strefa[0] == "-" else 1
            cyfry = strefa[1:].replace(":", "")
            tz = timezone(znak * timedelta(hours=int(cyfry[:2]), minutes=int(cyfry[2:])))
    return datetime(int(rok), int(mies), int(dzien), int(godz), int(minuta),
                    int(sek or 0), mikro, tzinfo=tz)


def _data_z_naglowka(wartosc: str | None) -> datetime | None:
    """Nagłówek HTTP Date (zegar serwera KSeF) -> datetime UTC albo None."""
    if not wartosc:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(wartosc)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None or dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _moment_zamowienia(wpis) -> tuple[datetime, bool]:
    """Moment zlecenia zapamiętanej paczki: wg zegara KSeF (True), a gdy go
    nie zapisano — wg zegara komputera (False; wołający bierze większy zapas)."""
    if wpis.ordered_ksef:
        dt = _parse_ksef_datetime(wpis.ordered_ksef)
        if dt is not None and dt.tzinfo is not None:
            return dt.astimezone(timezone.utc), True
    return datetime.fromtimestamp(wpis.ordered_at, timezone.utc), False


def _przyjmuje_argument(fn, nazwa: str) -> bool:
    """Czy funkcja przyjmuje argument `nazwa` (wprost albo przez **kwargs) —
    nadpisane metody w symulatorach nie muszą znać nowych argumentów."""
    try:
        parametry = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return nazwa in parametry or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parametry.values()
    )


def _od_pozniejszy_niz_hwm(exc: Exception) -> bool:
    """Czy błąd KSeF to 21183 (HTTP 400: dateRange.from > PermanentStorageHwmDate)."""
    tekst = str(exc)
    return "HTTP 400" in tekst and re.search(r"\b21183\b", tekst) is not None


def warsaw_date(raw: str) -> str:
    """Dzień kalendarzowy w Polsce (YYYY-MM-DD) dla znacznika czasu z KSeF.

    Wartość z offsetem (+00:00, Z, +02:00) przeliczana na czas polski; wartość
    bez offsetu to już czas polski (tak KSeF interpretuje wartości bez strefy);
    sama data zostaje bez zmian."""
    s = (raw or "").strip()
    if len(s) <= 10:
        return s
    dt = _parse_ksef_datetime(s)
    if dt is None:
        logger.warning("Nierozpoznany format daty z KSeF: %r — biorę pierwsze 10 znaków", raw)
        return s[:10]
    if dt.tzinfo is None:
        return dt.date().isoformat()
    utc = dt.astimezone(timezone.utc)
    return (utc + _warsaw_offset(utc)).date().isoformat()


def warsaw_midnight_iso(d: date) -> str:
    """Północ dnia `d` w czasie polskim jako date-time z offsetem, np.
    "2026-07-01T00:00:00+02:00". Zmiana czasu w Polsce jest o 02:00/03:00, nigdy
    o północy, więc offset z 22:00 UTC poprzedniego dnia jest offsetem północy."""
    przed = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - timedelta(hours=2)
    godziny = int(_warsaw_offset(przed).total_seconds() // 3600)
    return f"{d.isoformat()}T00:00:00+{godziny:02d}:00"


def _iso_start(d: date) -> str:
    return f"{d.isoformat()}T00:00:00+00:00"


def _iso_end(d: date) -> str:
    return f"{d.isoformat()}T23:59:59+00:00"


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except ArithmeticError:
        return None


class KsefClient:
    def __init__(
        self,
        base_url: str,
        nip: str,
        ksef_token: str,
        *,
        timeout: float = 60.0,
        cancel: threading.Event | None = None,
        quota: QuotaStore | None = None,
    ):
        self._nip = nip
        self._ksef_token = ksef_token
        self._cancel = cancel
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self._access_token: str | None = None
        self._access_exp: int | None = None
        self._refresh_token: str | None = None
        self._quota = quota or QuotaStore()
        self._meta_limiter = _RateLimiter(
            METADATA_LIMITS, self._quota, nip, "metadata", "zapytań o metadane", cancel
        )
        self._dl_limiter = _RateLimiter(
            DOWNLOAD_LIMITS, self._quota, nip, "download", "pobierania faktur", cancel
        )
        self._export_limiter = _RateLimiter(
            EXPORT_LIMITS, self._quota, nip, "export", "eksportu wsadowego", cancel
        )
        self._status_limiter = _RateLimiter(
            EXPORT_STATUS_LIMITS, self._quota, nip, "export_status",
            "sprawdzania statusu paczki", cancel,
        )
        self._export_keys: dict[str, tuple[bytes, bytes]] = {}  # ref -> (klucz AES, IV)
        # ref -> moment zlecenia wg zegara KSeF (nagłówek Date odpowiedzi) albo None
        self._export_ordered: dict[str, datetime | None] = {}
        # „teraz” wg zegara KSeF z ostatniej odpowiedzi o status paczki (nagłówek Date)
        self._czas_ksef_statusu: datetime | None = None

    # -- cykl życia -----------------------------------------------------------

    def close(self) -> None:
        self._http.close()
        self._quota.close()

    def __enter__(self) -> "KsefClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- smoke test (bez auth) ------------------------------------------------

    def smoke_test(self) -> list[dict]:
        """GET /security/public-key-certificates — potwierdza base URL, bez tokena."""
        resp = self._http.get("/security/public-key-certificates")
        resp.raise_for_status()
        certs = resp.json()
        if not isinstance(certs, list) or not certs:
            raise KsefError(f"Nieoczekiwana odpowiedź certyfikatów: {certs!r}")
        return certs

    # -- auth -----------------------------------------------------------------

    def authenticate(self) -> None:
        public_key, public_key_id = self._fetch_encryption_key("KsefTokenEncryption")
        challenge, timestamp_ms = self._fetch_challenge()
        encrypted = self._rsa_encrypt(
            f"{self._ksef_token}|{timestamp_ms}".encode(), public_key
        )

        resp = self._http.post(
            "/auth/ksef-token",
            json={
                "challenge": challenge,
                "contextIdentifier": {"type": "Nip", "value": self._nip},
                "encryptedToken": encrypted,
                "publicKeyId": public_key_id,
            },
        )
        if resp.status_code not in (200, 202):
            raise KsefAuthError(
                f"/auth/ksef-token: HTTP {resp.status_code}: {resp.text[:500]}"
            )
        body = resp.json()
        reference = body["referenceNumber"]
        auth_jwt = body["authenticationToken"]["token"]

        self._poll_auth(reference, auth_jwt)
        self._redeem(auth_jwt)
        logger.info("Uwierzytelniono NIP %s…", self._nip[:3])

    def _fetch_encryption_key(self, usage: str) -> tuple[rsa.RSAPublicKey, str]:
        certs = self.smoke_test()
        for entry in certs:
            usages = entry.get("usage") or []
            if any(usage in str(u) for u in usages):
                der = base64.b64decode(entry["certificate"])
                cert = x509.load_der_x509_certificate(der)
                key = cert.public_key()
                if not isinstance(key, rsa.RSAPublicKey):
                    raise KsefAuthError(f"Certyfikat {usage} nie jest RSA")
                return key, entry["publicKeyId"]
        raise KsefAuthError(f"Brak certyfikatu z usage={usage}")

    def _fetch_challenge(self) -> tuple[str, int]:
        resp = self._http.post("/auth/challenge")
        resp.raise_for_status()
        body = resp.json()
        challenge = body["challenge"]
        if "timestampMs" in body:
            ts_ms = int(body["timestampMs"])
        elif "timestamp" in body:  # [EMP] wariant: ISO timestamp zamiast ms
            ts = _parse_ksef_datetime(str(body["timestamp"]))
            if ts is None or ts.tzinfo is None:
                raise KsefAuthError(f"Nierozpoznany znacznik czasu challenge: {body!r}")
            ts_ms = int(ts.timestamp() * 1000)
        else:
            raise KsefAuthError(f"Challenge bez znacznika czasu: {body!r}")
        return challenge, ts_ms

    @staticmethod
    def _rsa_encrypt(plaintext: bytes, public_key: rsa.RSAPublicKey) -> str:
        ciphertext = public_key.encrypt(
            plaintext,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return base64.b64encode(ciphertext).decode()

    def _poll_auth(self, reference: str, auth_jwt: str, timeout_s: float = 60.0) -> None:
        deadline = time.monotonic() + timeout_s
        delay = 1.0
        while True:
            resp = self._http.get(
                f"/auth/{reference}", headers={"Authorization": f"Bearer {auth_jwt}"}
            )
            resp.raise_for_status()
            status = resp.json().get("status", {})
            code = int(status.get("code", 0))
            if code == 200:
                return
            if code == 415:
                raise KsefAuthError(
                    "Token KSeF nie ma uprawnienia do odczytu faktur (InvoiceRead). "
                    "Wygeneruj nowy token w Aplikacji Podatnika z zaznaczonym "
                    "przeglądaniem faktur. (kod 415)"
                )
            if code == 450:
                raise KsefAuthError(
                    f"Błąd tokenu/kryptografii/czasu (kod 450): {status.get('description')}"
                )
            if code not in (0, 100):
                raise KsefAuthError(f"Nieoczekiwany status auth {code}: {status!r}")
            if time.monotonic() > deadline:
                raise KsefAuthError("Timeout oczekiwania na uwierzytelnienie (60 s)")
            _sleep(delay, self._cancel)
            delay = 2.0

    def _redeem(self, auth_jwt: str) -> None:
        resp = self._http.post(
            "/auth/token/redeem", headers={"Authorization": f"Bearer {auth_jwt}"}
        )
        if resp.status_code != 200:
            raise KsefAuthError(f"redeem: HTTP {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        self._access_token = body["accessToken"]["token"]
        self._refresh_token = body["refreshToken"]["token"]
        self._access_exp = _decode_jwt_exp(self._access_token)
        if self._access_exp:
            ttl = self._access_exp - time.time()
            logger.info("accessToken ważny ~%.0f min", ttl / 60)

    def _ensure_access_token(self) -> str:
        if self._access_token is None:
            self.authenticate()
        elif self._access_exp and time.time() > self._access_exp - 60:
            self._refresh()
        assert self._access_token
        return self._access_token

    def _refresh(self) -> None:
        if not self._refresh_token:
            self.authenticate()
            return
        resp = self._http.post(
            "/auth/token/refresh",
            headers={"Authorization": f"Bearer {self._refresh_token}"},
        )
        if resp.status_code != 200:
            logger.info("Refresh nieudany (HTTP %s) — pełne ponowne logowanie", resp.status_code)
            self._access_token = None
            self.authenticate()
            return
        body = resp.json()
        self._access_token = body["accessToken"]["token"]
        self._access_exp = _decode_jwt_exp(self._access_token)
        if "refreshToken" in body:
            self._refresh_token = body["refreshToken"]["token"]

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure_access_token()}"}

    # -- query metadanych -----------------------------------------------------

    def query_metadata(
        self,
        subject_type: str,
        date_from: date,
        date_to: date,
        *,
        date_type: str = "Issue",
        margin: timedelta = timedelta(0),
        sonda: dict | None = None,
    ) -> Iterator[InvoiceMetadata]:
        """Iteruje metadane faktur; przezroczyście dzieli zakres >3 mies. i paginuje.
        `margin` poszerza KAŻDE okno z obu stron (okna liczone z zakresu bez
        zapasu — zapas nie zwiększa liczby zapytań); duplikaty z nakładek
        usuwa `seen`.

        `sonda` (słownik) — z każdego okna tylko PIERWSZA strona: gdy okno ma
        więcej (hasMore), iteracja kończy się bez kolejnych zapytań
        i `sonda["wiecej"] = True` (wołający porzuca listę i pobiera zakres
        paczkami eksportu). Bez `wiecej` wynik jest kompletny — te same
        zapytania co bez sondy. Nadpisane `_query_window` bez obsługi sondy
        (symulatory) — pełna lista jak dotąd."""
        seen: set[str] = set()
        uzyj_sondy = sonda is not None and _przyjmuje_argument(self._query_window, "tylko_pierwsza")
        for win_from, win_to in split_date_range(date_from, date_to):
            dodatkowe: dict = {}
            stan: dict = {}
            if uzyj_sondy:
                dodatkowe = {"tylko_pierwsza": True, "stan": stan}
            yield from self._query_window(
                subject_type, _iso_start(win_from - margin), _iso_end(win_to + margin),
                date_type, seen, **dodatkowe,
            )
            if stan.get("wiecej"):
                sonda["wiecej"] = True
                return

    def query_metadata_since(
        self,
        subject_type: str,
        since: str,
        *,
        max_lookback: timedelta = SINCE_MAX_LOOKBACK,
        now: datetime | None = None,
        przed_strona: Callable[[], bool] | None = None,
    ) -> PermanentStorageQuery:
        """Metadane faktur, które trafiły do KSeF (data trwałego zapisu,
        PermanentStorage) od punktu `since` do „teraz”, z nowym punktem
        kontynuacji (HWM) — synchronizacja przyrostowa wg docs MF.

        `since` — date-time z offsetem (punkt z rejestru, wysyłany bez zmian).
        Ostatnie okno idzie bez `dateRange.to` i z
        `restrictToPermanentStorageHwmDate=true`: KSeF bierze „teraz” (UTC)
        przycięte do HWM, a `permanentStorageHwmDate` z odpowiedzi to moment,
        do którego dane są kompletne (następny punkt). Punkt starszy niż
        SINCE_WINDOW jest dzielony na przylegające okna (koniec okna = początek
        następnego; KSeF przyjmuje maks. 100 dni), a starszy niż `max_lookback`
        — przycinany (`clamped=True`, decyzję o ostrzeżeniu podejmuje wołający).
        Paginacja i isTruncated jak w zwykłym zapytaniu (`_query_window`);
        każda strona to jedno zapytanie z puli limitu metadanych (20/h).

        `przed_strona` — pytany przed każdym zapytaniem (stroną); False kończy
        sprawdzenie bez zapytania. Takie przerwanie albo limit KSeF
        (KsefQuotaExceeded) w trakcie daje wynik częściowy (`complete=False`,
        `progress` = punkt wznowienia) zamiast wyjątku — wyniki idą rosnąco po
        dacie trwałego zapisu, więc wszystko przed `progress` jest sprawdzone."""
        start = _parse_ksef_datetime((since or "").strip())
        if start is None or start.tzinfo is None:
            raise KsefError(
                f"Nierozpoznany punkt kontynuacji synchronizacji: {since!r} "
                "(wymagany date-time ze strefą czasową)"
            )
        now = now or datetime.now(timezone.utc)
        clamped = False
        if start < now - max_lookback:
            start = (now - max_lookback).astimezone(timezone.utc)
            since = start.isoformat()
            clamped = True
            logger.warning(
                "Punkt kontynuacji starszy niż %d dni — sprawdzam od %s",
                max_lookback.days, since,
            )

        windows: list[tuple[str, str | None]] = []
        current_dt, current = start, since
        while now - current_dt > SINCE_WINDOW:
            end_dt = (current_dt + SINCE_WINDOW).astimezone(timezone.utc)
            windows.append((current, end_dt.isoformat()))
            current_dt, current = end_dt, end_dt.isoformat()
        windows.append((current, None))

        seen: set[str] = set()
        invoices: list[InvoiceMetadata] = []
        stan: dict[str, str | None] = {}
        ostatni_hwm: str | None = None
        dodatkowe = {} if przed_strona is None else {"przed_zapytaniem": przed_strona}
        for iso_from, iso_to in windows:
            stan = {}
            z_okna: list[InvoiceMetadata] = []
            przerwano = False
            try:
                for meta in self._query_window(
                    subject_type, iso_from, iso_to, "PermanentStorage", seen,
                    restrict_hwm=True, stan=stan, **dodatkowe,
                ):
                    z_okna.append(meta)
                przerwano = bool(stan.get("przerwano"))
            except KsefQuotaExceeded as exc:
                logger.warning("Sprawdzenie „od punktu” przerwane limitem KSeF: %s", exc)
                przerwano = True
            except KsefCancelled:
                raise
            except KsefError as exc:
                # 21183: dateRange.from > PermanentStorageHwmDate (np. pierwszy
                # eksport tuż po północy z „Od” = dziś) — po `from` nic jeszcze
                # nie wpłynęło: pusty wynik, nowy punkt = początek okna
                if iso_to is not None or z_okna or not _od_pozniejszy_niz_hwm(exc):
                    raise
                logger.info(
                    "KSeF: początek sprawdzenia %s jest późniejszy niż HWM — nic nowego",
                    iso_from,
                )
                stan = {"hwm": iso_from}
            invoices.extend(z_okna)
            if stan.get("hwm"):
                ostatni_hwm = stan["hwm"]
            if przerwano:
                postep = iso_from
                najdalej = None
                for meta in z_okna:
                    ps = _parse_ksef_datetime(meta.permanent_storage_date or "")
                    if ps is not None and ps.tzinfo is not None and (najdalej is None or ps > najdalej[0]):
                        najdalej = (ps, meta.permanent_storage_date)
                if najdalej is not None:
                    postep = najdalej[1]
                return PermanentStorageQuery(
                    invoices=invoices, hwm=ostatni_hwm, since=since, clamped=clamped,
                    complete=False, progress=postep,
                )
        return PermanentStorageQuery(
            invoices=invoices, hwm=stan.get("hwm"), since=since, clamped=clamped
        )

    def _query_window(
        self,
        subject_type: str,
        iso_from: str,
        iso_to: str | None,
        date_type: str,
        seen: set[str],
        *,
        restrict_hwm: bool = False,
        stan: dict | None = None,
        przed_zapytaniem: Callable[[], bool] | None = None,
        tylko_pierwsza: bool = False,
    ) -> Iterator[InvoiceMetadata]:
        """Jedno okno zapytania z paginacją. `iso_to=None` — bez górnej granicy
        (KSeF bierze „teraz”); `restrict_hwm` — tylko dla PermanentStorage;
        `stan["hwm"]` dostaje permanentStorageHwmDate OSTATNIEJ odpowiedzi.
        `przed_zapytaniem` False -> koniec bez zapytania, `stan["przerwano"]`.
        `tylko_pierwsza` — po pierwszej stronie z hasMore koniec bez kolejnych
        zapytań, `stan["wiecej"] = True` (sonda wolumenu)."""
        page_offset = 0
        current_from = iso_from
        while True:
            if przed_zapytaniem is not None and not przed_zapytaniem():
                if stan is not None:
                    stan["przerwano"] = True
                return
            self._meta_limiter.acquire()
            date_range: dict = {"dateType": date_type, "from": current_from}
            if iso_to is not None:
                date_range["to"] = iso_to
            if restrict_hwm:
                date_range["restrictToPermanentStorageHwmDate"] = True
            resp = self._request_with_retry(
                "POST",
                "/invoices/query/metadata",
                params={
                    "pageOffset": page_offset,
                    "pageSize": PAGE_SIZE,
                    "sortOrder": "Asc",
                },
                json={
                    "subjectType": subject_type,
                    "dateRange": date_range,
                },
                headers=self._auth_headers(),
                quota_group=("metadata", "zapytań o metadane"),
            )
            if resp.status_code != 200:
                raise KsefError(
                    f"query metadata: HTTP {resp.status_code}: {resp.text[:500]}"
                )
            body = resp.json()
            if stan is not None:
                stan["hwm"] = body.get("permanentStorageHwmDate")
            invoices = body.get("invoices") or []
            last_date: str | None = None
            for raw in invoices:
                meta = self._parse_metadata(raw)
                last_date = raw.get("issueDate") if date_type == "Issue" else raw.get(
                    "permanentStorageDate"
                )
                if meta.ksef_number in seen:
                    continue
                seen.add(meta.ksef_number)
                yield meta

            has_more = bool(body.get("hasMore"))
            is_truncated = bool(body.get("isTruncated"))
            if not has_more:
                return
            if tylko_pierwsza:
                if stan is not None:
                    stan["wiecej"] = True
                return
            if is_truncated:
                if not last_date:
                    raise KsefError("isTruncated bez daty ostatniego rekordu")
                # zawężenie okna, dedup przez `seen`; issueDate przychodzi jako
                # sama data (format: date), a dateRange.from wymaga date-time
                current_from = (
                    _iso_start(date.fromisoformat(last_date[:10]))
                    if date_type == "Issue" else last_date
                )
                page_offset = 0
            else:
                page_offset += 1

    @staticmethod
    def _parse_metadata(raw: dict) -> InvoiceMetadata:
        seller = raw.get("seller") or {}
        buyer = raw.get("buyer") or {}
        buyer_ident = buyer.get("identifier") or {}
        form = raw.get("formCode") or {}
        return InvoiceMetadata(
            ksef_number=raw.get("ksefNumber", ""),
            invoice_number=raw.get("invoiceNumber", ""),
            issue_date=raw.get("issueDate", ""),
            invoicing_date=raw.get("invoicingDate"),
            acquisition_date=raw.get("acquisitionDate"),
            seller_nip=seller.get("nip", ""),
            seller_name=seller.get("name") or "",
            buyer_id=str(buyer_ident.get("value") or ""),
            buyer_name=buyer.get("name") or "",
            net=_dec(raw.get("netAmount")),
            gross=_dec(raw.get("grossAmount")),
            vat=_dec(raw.get("vatAmount")),
            currency=raw.get("currency", "PLN"),
            form_system_code=form.get("systemCode", ""),
            invoice_type=raw.get("invoiceType", ""),
            permanent_storage_date=raw.get("permanentStorageDate"),
        )

    # -- download pojedynczych faktur ------------------------------------------

    def metadata_budget_remaining(self) -> int:
        """Ile zapytań o metadane zostało w bieżącej godzinie (limit 20/h),
        z uwzględnieniem wcześniejszych uruchomień i blokad serwera."""
        return self._meta_limiter.remaining_in_window(3600)

    def download_budget_remaining(self) -> int:
        """Ile faktur można jeszcze pobrać w bieżącej godzinie (limit 64/h),
        z uwzględnieniem wcześniejszych uruchomień i blokad serwera."""
        return self._dl_limiter.remaining_in_window(3600)

    def download_block_remaining(self) -> float:
        return self._dl_limiter.block_remaining()

    def download_invoice(self, ksef_number: str) -> bytes:
        """GET /invoices/ksef/{nr} -> surowy XML FA. Retry na 429 i 21165."""
        for delay_21165 in [0, *RETRY_21165_DELAYS]:
            if delay_21165:
                logger.info(
                    "Faktura %s jeszcze niedostępna (21165) — ponawiam za %d s",
                    ksef_number, delay_21165,
                )
                _sleep(delay_21165, self._cancel)
            self._dl_limiter.acquire()
            resp = self._request_with_retry(
                "GET",
                f"/invoices/ksef/{ksef_number}",
                headers={**self._auth_headers(), "Accept": "application/xml"},
                quota_group=("download", "pobierania faktur"),
            )
            if resp.status_code == 200:
                return resp.content
            if resp.status_code == 400 and "21165" in resp.text:
                continue
            raise KsefError(
                f"Pobranie {ksef_number}: HTTP {resp.status_code}: {resp.text[:300]}"
            )
        raise KsefInvoiceNotReady(
            f"Faktura {ksef_number} wciąż niedostępna po {len(RETRY_21165_DELAYS)} próbach (21165)"
        )

    # -- eksport wsadowy (paczka ZIP) ------------------------------------------

    def request_export(
        self, subject_type: str, date_from: date, date_to: date, *, date_type: str = "Issue"
    ) -> str:
        """POST /invoices/exports — zleca przygotowanie zaszyfrowanej paczki.
        Zwraca numer referencyjny; klucz AES trzymany w kliencie do odszyfrowania.
        Zlecenie liczy się do puli 20/h per NIP (osobnej od metadanych)."""
        self._export_limiter.acquire()
        aes_key = os.urandom(32)
        iv = os.urandom(16)
        public_key, public_key_id = self._fetch_encryption_key("SymmetricKeyEncryption")
        resp = self._request_with_retry(
            "POST",
            "/invoices/exports",
            json={
                "encryption": {
                    "encryptedSymmetricKey": self._rsa_encrypt(aes_key, public_key),
                    "initializationVector": base64.b64encode(iv).decode(),
                    "publicKeyId": public_key_id,
                },
                "filters": {
                    "subjectType": subject_type,
                    "dateRange": {
                        "dateType": date_type,
                        "from": _iso_start(date_from),
                        "to": _iso_end(date_to),
                    },
                },
            },
            headers=self._auth_headers(),
            quota_group=("export", "eksportu wsadowego"),
        )
        if resp.status_code not in (200, 201, 202):
            raise KsefError(
                f"eksport wsadowy: HTTP {resp.status_code}: {resp.text[:500]}"
            )
        body = resp.json()
        reference = body.get("referenceNumber") or body.get("operationReferenceNumber")
        if not reference:
            raise KsefError(f"eksport wsadowy: brak numeru referencyjnego: {body!r}")
        self._export_keys[reference] = (aes_key, iv)
        self._export_ordered[reference] = _data_z_naglowka(resp.headers.get("Date"))
        logger.info("Zlecono paczkę eksportu %s — czekam na przygotowanie", reference)
        return reference

    def wait_for_export(self, reference: str, *, timeout_s: float = EXPORT_TIMEOUT_S) -> dict:
        """Poll statusu paczki aż code==200. Zwraca sekcję `package` (z URL-ami
        części wygenerowanymi przy TYM odczycie — ważne ok. godziny). Status ma
        własną pulę limitów (`export_status`); odpytywanie co 5 s przez pierwszą
        minutę, potem co 15 s; po `timeout_s` — KsefError (paczka nie była gotowa,
        eksport niepełny — do ponowienia)."""
        start = time.monotonic()
        deadline = start + timeout_s
        last_log = start
        while True:
            self._status_limiter.acquire()
            resp = self._request_with_retry(
                "GET",
                f"/invoices/exports/{reference}",
                headers=self._auth_headers(),
                quota_group=("export_status", "sprawdzania statusu paczki"),
            )
            if resp.status_code != 200:
                raise KsefError(
                    f"status paczki: HTTP {resp.status_code}: {resp.text[:300]}"
                )
            self._czas_ksef_statusu = _data_z_naglowka(resp.headers.get("Date"))
            body = resp.json()
            code = int(body.get("status", {}).get("code", 0))
            if code == 200:
                package = body.get("package")
                if not package:
                    raise KsefError("Paczka gotowa, ale bez sekcji package")
                if package.get("isTruncated"):
                    logger.warning(
                        "Paczka ucięta (limit rozmiaru/liczby) — zakres trzeba dokończyć "
                        "od %s", package.get("lastIssueDate"),
                    )
                return package
            if code in (100, 0):
                teraz = time.monotonic()
                if teraz >= deadline:
                    raise KsefExportTimeout(
                        f"Paczka eksportu {reference} nie była gotowa w ciągu "
                        f"{int(timeout_s // 60)} min (KSeF nadal ją przygotowuje)"
                    )
                if teraz - last_log >= 60:
                    logger.info(
                        "Paczka eksportu w przygotowaniu w KSeF — czekam już %d min "
                        "(najwyżej %d min)", int((teraz - start) // 60), int(timeout_s // 60),
                    )
                    last_log = teraz
                interwal = (
                    EXPORT_POLL_INTERVAL_S if teraz - start < EXPORT_POLL_SZYBKO_PRZEZ_S
                    else EXPORT_POLL_INTERVAL_POZNIEJ_S
                )
                _sleep(min(interwal, max(deadline - teraz, 0.0)), self._cancel)
                continue
            raise KsefError(
                f"Eksport wsadowy: status {code}: {body.get('status', {}).get('description')}"
            )

    def pobierz_paczkami(
        self,
        subject_type: str,
        date_from: date,
        date_to: date,
        *,
        margin: timedelta = timedelta(0),
    ) -> Iterator[PaczkaEksportu]:
        """Faktury z zakresu dat wystawienia (dateType=Issue) paczkami eksportu —
        generator PER PACZKA (wołający przetwarza paczkę, zanim zamówimy
        następną; to, co przetworzone, zostaje przy błędzie kolejnej).

        Okna jak `split_date_range` (<= 3 mies.) poszerzone o `margin` z obu
        stron (KSeF przyjmuje maks. 100 dni). Paczka ma maks. 10 000 faktur /
        1 GB, faktury rosnąco po dacie wystawienia; ucięta (isTruncated) ->
        kolejne zlecenie od daty ostatniej faktury (`lastIssueDate`, nie
        później niż najpóźniejsza data wystawienia w `_metadata.json`) MINUS
        `margin` do tego samego końca okna — zapas na niepewną interpretację
        strefy daty wystawienia w KSeF; nakładki usuwa dedup po numerze KSeF
        (docs MF: przyrostowe-pobieranie-faktur.md). Bezpiecznik: kolejne
        zlecenie musi zaczynać się później niż poprzednie — inaczej (ponad
        limit paczki z jednego-dwóch dni) KsefError.

        Każda oddana paczka ma `metadane` (z `_metadata.json`) — tylko faktury
        niewidziane w poprzednich paczkach; XML przez `paczka.xml(nr)`. Paczka
        jest zamykana (plik tymczasowy usuwany) przy przejściu do następnej."""
        seen: set[str] = set()
        okna = split_date_range(date_from, date_to)
        for win_from, win_to in okna:
            od, do = win_from - margin, win_to + margin
            while True:
                paczka = self._zamow_i_otworz(subject_type, od, do)
                reference, package = paczka.reference, paczka.package
                try:
                    wszystkie = paczka.wszystkie_metadane()
                    numery = {m.ksef_number for m in wszystkie}
                    if len(numery) != len(wszystkie):
                        powtorzone = sorted(
                            n for n, c in collections.Counter(
                                m.ksef_number for m in wszystkie).items() if c > 1
                        )
                        raise KsefError(
                            f"Paczka {reference}: powtórzony numer KSeF w _metadata.json "
                            f"({', '.join(powtorzone[:3])}) — paczka niespójna"
                        )
                    bez_metadanych = set(paczka.numery_xml()) - numery
                    if bez_metadanych:
                        raise KsefError(
                            f"Paczka {reference}: {len(bez_metadanych)} plików XML bez wpisu "
                            f"w _metadata.json (np. {sorted(bez_metadanych)[0]})"
                        )
                    paczka.metadane = [m for m in wszystkie if m.ksef_number not in seen]
                    seen.update(numery)
                    obcieta = paczka.obcieta
                    if not obcieta and self._paczka_pelna(paczka, wszystkie):
                        logger.warning(
                            "Paczka %s osiągnęła limit KSeF (%d faktur, %.0f MB), a nie jest "
                            "oznaczona jako ucięta — dociągam dalszą część zakresu",
                            reference, len(wszystkie), paczka.rozmiar / 2**20,
                        )
                        obcieta = True
                    paczka.kolejne = obcieta or (win_from, win_to) != okna[-1]
                    nastepny_od = (
                        self._ostatnia_data_paczki(reference, package, wszystkie) - margin
                        if obcieta else None
                    )
                    logger.info(
                        "Paczka eksportu %s–%s: %d faktur (nowych %d)%s",
                        od, do, len(wszystkie), len(paczka.metadane),
                        f", ucięta — dociągam od {nastepny_od}" if obcieta else "",
                    )
                    yield paczka
                finally:
                    paczka.close()
                if not obcieta:
                    break
                if nastepny_od <= od:
                    raise KsefError(
                        f"Paczka eksportu ucięta bez postępu (od {od}): ponad limit paczki "
                        "KSeF (10 000 faktur albo 1 GB) z jednego dnia wystawienia i dnia "
                        "poprzedniego — tego zakresu nie da się pobrać paczkami"
                    )
                od = nastepny_od

    def _zamow_i_otworz(self, subject_type: str, od: date, do: date) -> PaczkaEksportu:
        """Jedna paczka okna od–do. Zlecenie jest zapamiętywane (QuotaStore) do
        odebrania: timeout przygotowania, limit albo przerwanie zostawiają je
        kolejnemu eksportowi tego samego okna, który najpierw pyta o STARE
        zlecenie (bez nowego — inaczej paczka przygotowywana dłużej niż
        EXPORT_TIMEOUT_S nigdy by nie przyszła). Stare zlecenie nie do
        odebrania (wygasło, błąd) -> usunięte i nowe zamówienie w tym przebiegu."""
        zakres = f"{subject_type}|Issue|{od.isoformat()}|{do.isoformat()}"
        wpis = self._quota.pending_export(self._nip, zakres)
        if wpis is not None:
            self._export_keys[wpis.reference] = (wpis.aes_key, wpis.iv)
            zamowiona, zegar_ksef = _moment_zamowienia(wpis)
            logger.info(
                "Odbieram paczkę %s zamówioną %s w poprzednim eksporcie (nie była wtedy gotowa)",
                wpis.reference, zamowiona.astimezone().strftime("%d.%m %H:%M"),
            )
            try:
                paczka = self._czekaj_i_otworz(wpis.reference, zakres)
            except KsefExportTimeout as exc:
                if self._wiek_zlecenia(wpis, zamowiona, zegar_ksef) < ZLECENIE_ZAWIESZONE_PO:
                    raise          # wolny KSeF — kolejny eksport znów je odbierze
                # (R7) stare zlecenie znów niegotowe po ZLECENIE_ZAWIESZONE_PO —
                # uznane za zawieszone: kolejny eksport zamówi nową paczkę
                self._quota.drop_pending_export(self._nip, zakres)
                blad = KsefExportTimeout(
                    f"{str(exc).rstrip('.')} — to zlecenie z poprzedniego eksportu, uznane za "
                    "zawieszone: kolejny eksport zamówi nową paczkę"
                )
                blad.do_odbioru = False
                raise blad from exc
            except (KsefQuotaExceeded, KsefCancelled):
                raise
            except KsefError as exc:
                logger.warning(
                    "Paczki %s z poprzedniego eksportu nie da się odebrać (%s) — zamawiam nową",
                    wpis.reference, exc,
                )
            else:
                paczka.zamowiona, paczka.zamowiona_zegar_ksef = zamowiona, zegar_ksef
                return paczka
        reference = self.request_export(subject_type, od, do)
        aes_key, iv = self._export_keys[reference]
        zamowiono_ksef = self._export_ordered.get(reference)
        self._quota.save_pending_export(
            self._nip, zakres, reference, aes_key, iv,
            zamowiono_ksef.isoformat() if zamowiono_ksef is not None else None,
        )
        return self._czekaj_i_otworz(reference, zakres)

    def _wiek_zlecenia(self, wpis, zamowiona: datetime, zegar_ksef: bool) -> timedelta:
        """Ile minęło od zamówienia: wg zegara KSeF (moment zlecenia i ostatni
        odczyt statusu z nagłówków Date), a bez nich — wg zegara komputera."""
        if zegar_ksef and self._czas_ksef_statusu is not None:
            return self._czas_ksef_statusu - zamowiona
        return timedelta(seconds=time.time() - wpis.ordered_at)

    def _czekaj_i_otworz(self, reference: str, zakres: str) -> PaczkaEksportu:
        """Wpis zlecenia usuwany po odebraniu paczki i po błędzie PACZKI
        (KsefError: wygasła, uszkodzona, 404…); zostaje przy timeoucie, limicie,
        przerwaniu i błędzie sieci — zlecenie żyje w KSeF."""
        try:
            package = self.wait_for_export(reference)
            paczka = self.otworz_paczke(reference, package)
        except KsefError as exc:
            if not isinstance(exc, (KsefExportTimeout, KsefQuotaExceeded, KsefCancelled)):
                self._quota.drop_pending_export(self._nip, zakres)
            raise
        self._quota.drop_pending_export(self._nip, zakres)
        return paczka

    @staticmethod
    def _paczka_pelna(paczka: PaczkaEksportu, metadane) -> bool:
        """Paczka osiągnęła limit KSeF (liczba faktur albo rozmiar)."""
        try:
            deklarowane = int(paczka.package.get("invoiceCount") or 0)
        except (TypeError, ValueError):
            deklarowane = 0
        return (
            max(len(metadane), deklarowane) >= PACZKA_PELNA_FAKTUR
            or paczka.rozmiar >= PACZKA_PELNA_BAJTOW
        )

    @staticmethod
    def _ostatnia_data_paczki(reference: str, package: dict, metadane) -> date:
        """Data wystawienia ostatniej faktury uciętej paczki: `lastIssueDate`
        z KSeF, ale nie później niż najpóźniejsza data wystawienia
        w `_metadata.json` (wcześniejsza = większa nakładka, bezpieczniej)."""
        kandydaci: list[date] = []
        surowa = package.get("lastIssueDate")
        if surowa:
            try:
                kandydaci.append(date.fromisoformat(str(surowa)[:10]))
            except ValueError:
                logger.warning("Nierozpoznane lastIssueDate paczki %s: %r", reference, surowa)
        z_metadanych = []
        for m in metadane:
            try:
                z_metadanych.append(date.fromisoformat((m.issue_date or "")[:10]))
            except ValueError:
                pass
        if z_metadanych:
            kandydaci.append(max(z_metadanych))
        if not kandydaci:
            raise KsefError(
                f"Paczka {reference} ucięta bez daty ostatniej faktury (lastIssueDate)"
            )
        return min(kandydaci)

    def iter_export_invoices(
        self, reference: str, package: dict
    ) -> Iterator[tuple[str, bytes]]:
        """Pobiera paczkę (`otworz_paczke`) i iteruje (nrKSeF, XML)."""
        with self.otworz_paczke(reference, package) as paczka:
            for ksef_number in paczka.numery_xml():
                yield ksef_number, paczka.xml(ksef_number)

    def otworz_paczke(self, reference: str, package: dict) -> PaczkaEksportu:
        """Pobiera WSZYSTKIE części paczki (presigned URL — bez auth i limitów
        API), odszyfrowuje każdą osobno (AES-256-CBC/PKCS7, ten sam klucz i IV),
        skleja w kolejności ordinalNumber i rozpakowuje raz — części to kawałki
        jednego archiwum ciętego co <= 50 MB w dowolnym bajcie (docs MF:
        przyrostowe-pobieranie-faktur.md). URL części jest ważny ok. godziny
        i generowany przy każdym odczycie statusu: przy 403/410 (albo zerwanym
        połączeniu) ponowny GET statusu daje nowe URL-e — jedna ponowna próba
        na część."""
        aes_key, iv = self._export_keys[reference]
        lista = package.get("parts") or []
        deklarowane = package.get("invoiceCount")
        # zwykły plik tymczasowy (nie SpooledTemporaryFile: ten przed Pythonem
        # 3.11 nie ma seekable(), którego wymaga zipfile)
        plik = tempfile.TemporaryFile()
        try:
            if not lista:
                if int(deklarowane or 0) != 0:
                    raise KsefError(
                        f"Paczka {reference}: brak części przy invoiceCount={deklarowane}"
                    )
                # okno bez faktur — puste archiwum (sam koniec katalogu ZIP)
                plik.write(b"PK\x05\x06" + b"\x00" * 18)
                plik.seek(0)
                return PaczkaEksportu(reference, package, plik, zipfile.ZipFile(plik), pusta=True)
            czesci = {p.get("ordinalNumber", 0): p for p in lista}
            numery = sorted(czesci)
            if len(numery) != len(lista) or numery != list(range(numery[0], numery[0] + len(numery))):
                raise KsefError(
                    f"Paczka {reference}: niepełna lub zdublowana lista części {numery}"
                )
            for nr in numery:
                poczatek = plik.tell()
                try:
                    self._pobierz_czesc(czesci[nr], plik, aes_key, iv)
                except _CzescNiedostepna as exc:
                    logger.info("%s — odświeżam adresy części paczki %s", exc, reference)
                    plik.seek(poczatek)
                    plik.truncate()
                    odswiezona = self.wait_for_export(reference)
                    nowe = {p.get("ordinalNumber", 0): p for p in odswiezona.get("parts") or []}
                    if sorted(nowe) != sorted(czesci):
                        raise KsefError(
                            f"Paczka {reference}: po odświeżeniu inna lista części"
                        ) from exc
                    czesci = nowe
                    try:
                        self._pobierz_czesc(czesci[nr], plik, aes_key, iv)
                    except _CzescNiedostepna as exc2:
                        raise KsefError(
                            f"Paczka {reference}: część {nr} niedostępna także po "
                            f"odświeżeniu adresu ({exc2})"
                        ) from exc2
            rozmiar = plik.tell()
            plik.seek(0)
            try:
                archiwum = zipfile.ZipFile(plik)
                # pełna kontrola CRC: brak/przestawienie części nie zawsze psuje
                # katalog ZIP (zipfile toleruje przesunięcie), ale psuje pliki
                zly = archiwum.testzip()
            except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError) as exc:
                zly = exc
            if zly is not None:
                raise KsefError(
                    f"Paczka {reference}: sklejone części nie tworzą poprawnego archiwum "
                    f"ZIP ({zly})"
                )
            paczka = PaczkaEksportu(reference, package, plik, archiwum, rozmiar=rozmiar)
            if paczka.powtorzone:
                paczka.close()
                raise KsefError(
                    f"Paczka {reference}: powtórzone pliki w archiwum "
                    f"({', '.join(sorted(set(paczka.powtorzone))[:3])})"
                )
            if deklarowane is not None and int(deklarowane) != len(paczka.numery_xml()):
                paczka.close()
                raise KsefError(
                    f"Paczka {reference}: invoiceCount={deklarowane}, a w archiwum "
                    f"{len(paczka.numery_xml())} plików XML"
                )
            return paczka
        except BaseException:
            plik.close()
            raise

    def _pobierz_czesc(self, part: dict, plik, aes_key: bytes, iv: bytes) -> None:
        """Jedna część: pobranie strumieniowe, suma kontrolna zaszyfrowanej
        części, odszyfrowanie i dopisanie jawnej treści do `plik`."""
        nazwa = part.get("partName") or f"część {part.get('ordinalNumber')}"
        oczekiwany = part.get("encryptedPartHash")
        sha = hashlib.sha256()
        decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
        unpadder = sym_padding.PKCS7(128).unpadder()
        try:
            with self._http.stream(part.get("method", "GET"), part["url"]) as resp:
                if resp.status_code in (403, 410):
                    raise _CzescNiedostepna(
                        f"Adres części {nazwa} wygasł (HTTP {resp.status_code})"
                    )
                if resp.status_code != 200:
                    raise KsefError(
                        f"Pobranie części paczki {nazwa}: HTTP {resp.status_code}"
                    )
                for kawalek in resp.iter_bytes(CZESC_KAWALEK):
                    if self._cancel is not None and self._cancel.is_set():
                        raise KsefCancelled("Przerwano na żądanie użytkownika")
                    sha.update(kawalek)
                    plik.write(unpadder.update(decryptor.update(kawalek)))
        except httpx.TransportError as exc:
            raise _CzescNiedostepna(f"Zerwane pobieranie części {nazwa}: {exc}") from exc
        if oczekiwany and base64.b64encode(sha.digest()).decode() != oczekiwany:
            raise KsefError(f"Suma kontrolna części {nazwa} nie zgadza się")
        try:
            plik.write(unpadder.update(decryptor.finalize()))
            plik.write(unpadder.finalize())
        except ValueError as exc:
            raise KsefError(f"Błąd odszyfrowania części paczki {nazwa}: {exc}") from exc

    # -- HTTP z retry ---------------------------------------------------------

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        quota_group: tuple[str, str] | None = None,
        **kwargs,
    ) -> httpx.Response:
        """Retry na 429 (Retry-After/backoff) i 5xx; 400 zwraca do decyzji wołającego.
        Długi Retry-After -> zapamiętana blokada + KsefQuotaExceeded (bez zwisu)."""
        backoff = 1.0
        for attempt in range(6):
            resp = self._http.request(method, url, **kwargs)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else backoff
                except ValueError:
                    wait = backoff
                if quota_group:
                    group, label = quota_group
                    self._quota.set_block(self._nip, group, time.time() + wait)
                    if wait > LONG_WAIT_S:
                        logger.warning(
                            "Serwer KSeF zgłasza wyczerpany limit (%s) — blokada %.0f min",
                            label, wait / 60,
                        )
                        raise KsefQuotaExceeded(label, time.time() + wait)
                logger.info("HTTP 429 — czekam %.0f s", wait)
                _sleep(min(wait, 120), self._cancel)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code >= 500:
                logger.warning("HTTP %s na %s — retry za %.0f s", resp.status_code, url, backoff)
                _sleep(backoff, self._cancel)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code == 401 and self._access_token:
                # accessToken wygasł szybciej niż `exp` — odśwież i ponów raz
                self._refresh()
                kwargs["headers"] = {**kwargs.get("headers", {}), **self._auth_headers()}
                continue
            if resp.status_code >= 400 and resp.status_code not in (400,):
                raise KsefError(f"{method} {url}: HTTP {resp.status_code}: {resp.text[:500]}")
            return resp
        raise KsefError(f"{method} {url}: wyczerpano ponowienia")
