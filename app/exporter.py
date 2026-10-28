"""Orkiestracja eksportu per profil: query -> dedup -> download -> map -> transform -> zapis."""

from __future__ import annotations

import calendar
import contextlib
import inspect
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Callable

from lxml import etree

from app.ams_transform import AmsTransformer, encode_cp1250, write_txt_atomic
from app.config import Profile
from app.fa_mapper import Fa3MappingError, InvoiceContext, map_fa3_to_ksef_invoice
from app.hmffk import KontrahenciRejestr, build_hmffk_text, map_fa3_to_hmffk, opis_dokumentu
from app.kontrahenci import (
    TEMPLATE_AMS,
    TEMPLATE_FILENAME,
    Kontrahent,
    build_kontrahenci_ams,
    kontrahenci_from_invoice,
    merge_kontrahent,
)
from app.kontrahenci_nr import RejestrNumerow, _digits
from app.ksef_client import (
    PAGE_SIZE,
    SINCE_MAX_LOOKBACK,
    InvoiceMetadata,
    KsefCancelled,
    KsefClient,
    KsefError,
    KsefExportTimeout,
    KsefQuotaExceeded,
    _parse_ksef_datetime,
    split_date_range,
    warsaw_date,
    warsaw_midnight_iso,
)
from app.ledger import STATUS_DO_RECZNEJ, STATUS_W_PLIKU, LateRecord, Ledger

logger = logging.getLogger(__name__)

# powyżej tylu faktur do pobrania używamy eksportu wsadowego (paczka ZIP z KSeF):
# pojedyncze GET-y mają limit 64/h, paczka nie ma limitu na liczbę faktur
BATCH_DOWNLOAD_THRESHOLD = 10

# KSeF filtruje datę wystawienia (typ date) granicami typu date-time, a sposób
# porównania nie jest udokumentowany — granica może się przesunąć o dzień w obie
# strony. Dlatego pytamy z zapasem 1 dnia i sami odcinamy faktury spoza zakresu.
QUERY_MARGIN = timedelta(days=1)

# spóźnione faktury: przy PIERWSZYM sprawdzeniu dla firmy i rodzaju (brak punktu
# kontynuacji w rejestrze) nie sięgamy dalej wstecz niż tyle — bez ściągania
# historii sprzed wprowadzenia mechanizmu
PIERWSZE_SPRAWDZENIE_MAX_WSTECZ = timedelta(days=90)

# bezpiecznik W2: faktura z miesiąca jeszcze niedomkniętego czeka na jego
# domknięcie (punkt kontynuacji jej nie przeskakuje), ale najwyżej tyle —
# potem dołącza jako spóźniona z osobnym ostrzeżeniem. Czas liczony od wpływu
# do KSeF (data trwałego zapisu), nie wcześniej niż od początku obserwacji
# (pierwszy punkt kontynuacji) — patrz `_czeka_za_dlugo`.
CZEKANIE_NA_OKRES_MAX = timedelta(days=60)

# paczka odebrana z zamówienia z poprzedniego eksportu to stan KSeF z chwili
# zamówienia — wpływy od tej chwili (z tym zapasem na zegary i opóźnienie
# zapisu w KSeF) sprawdzamy osobnym zapytaniem „od punktu”
ZAPAS_OD_ZAMOWIENIA = timedelta(hours=1)
# gdy moment zamówienia znamy tylko z zegara komputera (brak nagłówka Date
# w odpowiedzi KSeF) — zapas na rozjazd zegara komputera
ZAPAS_OD_ZAMOWIENIA_ZEGAR_KOMPUTERA = timedelta(hours=12)

# dopiski do błędów mapowania faktur z zakresu (report.failed): „zaksięguj
# ręcznie” tylko przy zapisie na listę do ręcznej obsługi, inaczej — wróci
DOPISEK_NA_LISCIE = (
    " — zapisana na liście do ręcznej obsługi, zaksięguj ją ręcznie "
    "(kolejne eksporty nie zgłoszą jej ponownie)"
)
DOPISEK_NIEPELNY = (
    " — eksport niepełny: nie księguj jej jeszcze ręcznie, kolejny kompletny eksport "
    "zgłosi ją ponownie"
)
DOPISEK_BEZ_ZAKRESU = (
    " — zakres nie został zapisany (dane KSeF kompletne dopiero do wczoraj): nie księguj "
    "jej jeszcze ręcznie, kompletny eksport jej zakresu (domknięcie miesiąca) zgłosi ją ponownie"
)
DOPISEK_WYSZUKIWANIE = (
    " — wyszukiwanie nie zapisuje jej na liście do ręcznej obsługi: nie księguj jej jeszcze "
    "ręcznie, kompletny eksport jej zakresu (domknięcie miesiąca) zgłosi ją ponownie"
)
DOPISEK_JUZ_NA_LISCIE = (
    " — jest już na liście do ręcznej obsługi (zgłoszona wcześniej do ręcznego zaksięgowania)"
)

# N3-LIMIT: tyle zapytań o metadane (z 20/h per NIP) sprawdzenie „od punktu”
# zostawia nietknięte — na kolejny eksport tej samej firmy w tej godzinie
REZERWA_ZAPYTAN = 2

_MIESIACE = (
    "styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec", "lipiec", "sierpień",
    "wrzesień", "październik", "listopad", "grudzień",
)


def _w_zakresie(issue_date: str, date_from: date, date_to: date) -> bool:
    """Czy data wystawienia z metadanych KSeF mieści się w wybranym zakresie.
    Nierozpoznana data -> True (nie gubimy faktury po cichu)."""
    try:
        d = date.fromisoformat((issue_date or "")[:10])
    except ValueError:
        logger.warning("Nierozpoznana data wystawienia z KSeF: %r", issue_date)
        return True
    return date_from <= d <= date_to


def _polozenie(issue_date: str, date_from: date, date_to: date) -> int:
    """-1 = data wystawienia przed zakresem, 0 = w zakresie, 1 = po zakresie.
    Nierozpoznana data -> 0 (jak `_w_zakresie`: nie gubimy faktury po cichu)."""
    try:
        d = date.fromisoformat((issue_date or "")[:10])
    except ValueError:
        logger.warning("Nierozpoznana data wystawienia z KSeF: %r", issue_date)
        return 0
    return -1 if d < date_from else (1 if d > date_to else 0)


def _norm_numer(s: str) -> str:
    """Normalizacja numeru faktury (nadanego przez wystawcę) do porównania:
    bez wielkości liter i bez białych znaków — "FV 12/2026" == "fv12/2026"."""
    return re.sub(r"\s+", "", s).casefold()


def _ostatnie_cyfry(numer_znorm: str) -> int | None:
    """Ostatni ciąg cyfr w znormalizowanym numerze faktury, jako int (do
    dopasowania po „końcówce” — np. "FV/07/2026/00247" -> 247, więc
    "247" == "00247"). None, gdy numer nie zawiera żadnych cyfr."""
    znalezione = re.findall(r"\d+", numer_znorm)
    return int(znalezione[-1]) if znalezione else None


@dataclass
class _WantedNumber:
    """Jeden wpis użytkownika w trybie szukania po numerze faktury.

    Forma wpisu:
    - pełny numer -> `exact_norm` ustawiony, dopasowanie dokładne po `_norm_numer`;
    - sama końcówka ("247") albo zakres ("247-251") -> `suffix_values` (jedna
      albo więcej wartości), dopasowanie liczbowe po ostatnim ciągu cyfr numeru.

    `matches`: klucz dopasowania (znorm. numer albo końcówka) -> lista trafień
    (meta); osobny klucz per wartość, żeby zakres trafiający po 1 fakturze na
    każdą końcówkę nie generował fałszywego ostrzeżenia o duplikacie."""

    original: str
    exact_norm: str | None = None
    suffix_values: frozenset[int] = field(default_factory=frozenset)
    matches: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SpoznionaFaktura:
    """Faktura z miesiąca już domkniętego (eksport całego miesiąca) dołączona do
    bieżącego pliku, bo wpłynęła do KSeF po domknięciu (tryb offline, spóźniony
    wystawca). `bezpiecznik=True` — faktura z okresu, którego aplikacja nie
    eksportowała, dołączona po CZEKANIE_NA_OKRES_MAX oczekiwania."""

    ksef_number: str
    invoice_number: str
    issue_date: str  # YYYY-MM-DD
    rodzaj: str      # "sprzedaż" / "koszt"
    bezpiecznik: bool = False


def _liczebnik(n: int, jedna: str, kilka: str, wiele: str) -> str:
    if n == 1:
        return f"1 {jedna}"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} {kilka}"
    return f"{n} {wiele}"


def _lista_spoznionych(spoznione: list[SpoznionaFaktura]) -> str:
    return "; ".join(
        f"{s.invoice_number} z {s.issue_date} ({s.rodzaj}, nr KSeF {s.ksef_number})"
        for s in spoznione
    )


def _ostrzezenie_spoznione(spoznione: list[SpoznionaFaktura]) -> str:
    ile = _liczebnik(len(spoznione), "spóźnioną fakturę", "spóźnione faktury", "spóźnionych faktur")
    return (
        f"Dołączono {ile} z wcześniejszych okresów (wpłynęły do KSeF po "
        f"domknięciu swojego miesiąca): {_lista_spoznionych(spoznione)}"
    )


def _ostrzezenie_bezpiecznik(spoznione: list[SpoznionaFaktura]) -> str:
    # D4: bez twierdzenia „aplikacja tego okresu nie eksportowała” — miesiąc
    # mógł domknąć eksport starszej wersji, poza aplikacją, albo domknięcie się
    # spóźnia; pewne jest tylko to, że faktura czekała ponad limit
    ile = _liczebnik(len(spoznione), "fakturę, która czekała", "faktury, które czekały",
                     "faktur, które czekały")
    czy = "czy nie jest już zaksięgowana" if len(spoznione) == 1 else "czy nie są już zaksięgowane"
    dni = CZEKANIE_NA_OKRES_MAX.days
    return (
        f"Dołączono {ile} ponad {dni} dni od wpływu do KSeF na domknięcie swojego "
        f"miesiąca (eksport całego miesiąca) — sprawdź, {czy}: "
        f"{_lista_spoznionych(spoznione)}"
    )


def _ostrzezenie_do_recznej(pominiete: list) -> str:
    ile = _liczebnik(len(pominiete), "fakturę oznaczoną", "faktury oznaczone",
                     "faktur oznaczonych")
    lista = "; ".join(
        f"{m.invoice_number} z {(m.issue_date or '')[:10]} ({rodzaj.label}, nr KSeF {m.ksef_number})"
        for m, rodzaj in pominiete
    )
    powod = (
        "wcześniej nie dało się jej przetworzyć i miała zostać zaksięgowana ręcznie — "
        "bez dubla nie trafia do pliku" if len(pominiete) == 1 else
        "wcześniej nie dało się ich przetworzyć i miały zostać zaksięgowane ręcznie — "
        "bez dubla nie trafiają do pliku"
    )
    return f"Pominięto {ile} do ręcznej obsługi ({powod}): {lista}"


def _teraz_utc() -> datetime:
    return datetime.now(timezone.utc)


def _granica_zakresu(spr: "_Spoznione | None") -> date:
    """N2: ostatni dzień, który zapisany zakres może objąć — dzień przed
    momentem kompletności danych w czasie polskim (HWM tego sprawdzenia; bez
    HWM — dziś według zegara). Eksport „na bieżąco” z „Do” = koniec miesiąca,
    zrobiony w trakcie miesiąca, nie może domknąć miesiąca przed czasem."""
    for surowy in ((spr.hwm if spr is not None else None), _teraz_utc().isoformat()):
        if not surowy:
            continue
        try:
            return date.fromisoformat(warsaw_date(surowy)) - timedelta(days=1)
        except ValueError:
            continue
    return date.today() - timedelta(days=1)


def _czas_ksef(s: str | None) -> datetime | None:
    """Znacznik czasu KSeF ze strefą albo None (brak / nieczytelny / bez strefy)."""
    dt = _parse_ksef_datetime(s) if s else None
    return dt if dt is not None and dt.tzinfo is not None else None


def _czeka_za_dlugo(meta, teraz: datetime | None, czuwa_od: datetime | None) -> bool:
    """Bezpiecznik W2: czy faktura czeka na domknięcie swojego miesiąca dłużej niż
    CZEKANIE_NA_OKRES_MAX.

    Liczone od daty trwałego zapisu w KSeF (permanentStorageDate), bo:
    (1) punkt kontynuacji jest kursorem po tej właśnie dacie — bezpiecznik ma
    ograniczyć, jak daleko punkt może zostać w tyle, a czas od wpływu mierzy to
    wprost; (2) data wystawienia bywa dowolnie stara (tryb offline, faktura
    wystawiona dawno i wysłana późno — zostałaby dołączona od razu po wpływie)
    albo z przyszłości (pomyłka wystawcy — nie zostałaby dołączona nigdy);
    (3) data wpływu i „teraz” (HWM z tej samej odpowiedzi) to ten sam zegar
    KSeF — bez rozjazdu z zegarem komputera.
    Nie wcześniej jednak niż od początku obserwacji (pierwszy punkt): pierwszy
    eksport po instalacji sięga do 90 dni wstecz, a faktury okresów jeszcze
    nieeksportowanych (np. nadrabianie miesięcy po kolei) nie mogą od razu
    wpaść do bieżącego pliku. Brak „teraz”, początku obserwacji albo daty
    wpływu -> False (czeka dalej)."""
    if teraz is None or czuwa_od is None:
        return False
    wplyw = _czas_ksef(getattr(meta, "permanent_storage_date", None))
    if wplyw is None:
        return False
    return teraz - max(wplyw, czuwa_od) > CZEKANIE_NA_OKRES_MAX


def _meta_z_rejestru(rec: LateRecord) -> InvoiceMetadata:
    """Metadane spóźnionej odtworzone z rejestru (W3: ponowny pełny eksport
    zakresu, do którego ją dołączono) — tyle, ile potrzeba do pobrania,
    mapowania (data wpływu) i raportu."""
    return InvoiceMetadata(
        ksef_number=rec.ksef_number, invoice_number=rec.invoice_number,
        issue_date=rec.issue_date, invoicing_date=None,
        acquisition_date=rec.acquisition_date, seller_nip="", seller_name="",
        buyer_id="", buyer_name="", net=None, gross=None, vat=None, currency="",
        form_system_code="", invoice_type="",
        permanent_storage_date=rec.permanent_storage_date,
    )


@dataclass
class _Spoznione:
    """Wynik zapytania „co wpłynęło do KSeF od punktu” dla jednego rodzaju."""

    od: str                 # faktyczny początek sprawdzenia (punkt albo start pierwszego)
    hwm: str | None         # permanentStorageHwmDate z KSeF
    # przed zakresem, z miesiąca już domkniętego, bez eksportu — do dołączenia
    # (+ te z bezpiecznika 60 dni, patrz `bezpiecznik`)
    spoznione: list = field(default_factory=list)
    w_zakresie: list = field(default_factory=list)  # z zakresu, a nie było ich w głównym zapytaniu
    # bez eksportu, a nie do tego pliku: z miesiąca niedomkniętego albo po
    # zakresie — punkt kontynuacji nie może ich przeskoczyć
    wstrzymane: list = field(default_factory=list)
    bezpiecznik: set = field(default_factory=set)  # nr KSeF dołączone z bezpiecznika
    # N3-LIMIT: None = sprawdzenie pełne (do HWM); inaczej przerwane (budżet
    # stron / limit) — punkt wznowienia (wszystko przed nim sprawdzone)
    postep: str | None = None


class _Rodzaj:
    """Stan jednego rodzaju (sprzedaż/koszt) w przebiegu `run_export`."""

    def __init__(self, kind: "Kind"):
        self.kind = kind
        self.glowne_nr: set[str] = set()   # numery z głównego zapytania (przed D1)
        self.batch: list = []              # sksef + paczka: (meta, ksefInvoice, ponowna?)
        self.niepelny = False
        self.spr: _Spoznione | None = None
        self.spoznione_nr: set[str] = set()  # spóźnione (i ponowne W3) — błąd mapowania = lista ręczna
        # błędy mapowania faktur z zakresu: nr KSeF -> (meta, błąd); na listę do
        # ręcznej obsługi tylko wtedy, gdy przebieg zapisuje zakres tego rodzaju
        self.bledy_glowne: dict[str, tuple[object, str]] = {}


def _sprawdz_spoznione(
    client, ledger: Ledger, nip: str, kind: "Kind", date_from: date, date_to: date,
    biezace, report: "ExportReport", pomijane: set[str] = frozenset(),
    przed_strona=None,
) -> _Spoznione | None:
    """Pyta KSeF (data trwałego zapisu), co wpłynęło od punktu kontynuacji
    (NIP, rodzaj), i dzieli to na (W2, decyzja 28.09):
    - spóźnione — wystawione przed `date_from`, z miesiąca DOMKNIĘTEGO dla
      tego NIP-u i rodzaju (`Ledger.is_covered`: jeden zapisany zakres
      obejmuje cały miesiąc; zakresy „na bieżąco” go nie domykają), bez wpisu
      w rejestrze — do dołączenia;
    - z zakresu nieobecne w głównym zapytaniu (zapisane w KSeF między oboma
      zapytaniami) — dołączane jak zwykłe;
    - wstrzymane — z miesiąca jeszcze niedomkniętego (czeka na jego
      domknięcie, które zabierze ją głównym zapytaniem) albo wystawione po
      `date_to` (spóźnione dołączamy tylko do eksportu późniejszego okresu) —
      nie do tego pliku, ale punkt nie może ich przeskoczyć; wyjątek:
      bezpiecznik — czekająca dłużej niż CZEKANIE_NA_OKRES_MAX dołącza jako
      spóźniona (`_czeka_za_dlugo`), żeby punkt nie stał w miejscu.
    Już wyeksportowane i `pomijane` (lista do ręcznej obsługi) pomija.

    Pierwsze sprawdzenie (brak punktu): start = północ `date_from` w czasie
    polskim, ale nie dalej niż PIERWSZE_SPRAWDZENIE_MAX_WSTECZ — wyłapuje
    faktury z wcześniejszych okresów, które wpłynęły od początku wybranego
    zakresu (np. wystawione 30.06, przyjęte 01.07), bez ściągania historii
    sprzed wprowadzenia mechanizmu. Wyjątek N3 — przejście ze starszej wersji
    (rejestr ma jej eksporty, `Ledger.legacy_start`): start = najstarszy
    eksport starszej wersji dla tej firmy (też nie dalej niż 90 dni wstecz) —
    inaczej spóźnione z miesięcy domkniętych starszą wersją, które wpłynęły
    między starym domknięciem a pierwszym eksportem nowej wersji, nie
    zostałyby nigdy wykryte. Faktury z miesięcy sprzed najstarszego zakresu
    starszej wersji (`Ledger.legacy_horizon` — czas sprzed pracy aplikacji)
    spoza zakresu są pomijane: nie do pliku, nie wstrzymują punktu, nie
    wpadają przez bezpiecznik; jeden zbiorczy wpis w logu, bez ostrzeżenia
    w raporcie. Stary punkt starszy niż SINCE_MAX_LOOKBACK jest przycinany
    przez klienta — wtedy ostrzeżenie.

    Błąd zapytania (limit, HTTP) nie przerywa eksportu zakresu: ostrzeżenie,
    punkt bez zmian, następny eksport ponowi sprawdzenie. Zwraca None."""
    punkt = ledger.sync_point(nip, kind.value)
    pierwszy = punkt is None
    since = punkt
    if pierwszy:
        stara = _czas_ksef(ledger.legacy_start(nip))
        # D-N3: do KSeF pełne sekundy z offsetem (bez mikrosekund zapisu rejestru)
        since = (
            stara.replace(microsecond=0).isoformat() if stara is not None
            else warsaw_midnight_iso(date_from)
        )
    argumenty = {
        "max_lookback": PIERWSZE_SPRAWDZENIE_MAX_WSTECZ if pierwszy else SINCE_MAX_LOOKBACK,
    }
    if przed_strona is not None and _przyjmuje(client.query_metadata_since, "przed_strona"):
        argumenty["przed_strona"] = przed_strona
    try:
        wynik = client.query_metadata_since(kind.value, since, **argumenty)
    except KsefCancelled:
        raise
    except KsefError as exc:  # także KsefQuotaExceeded
        report.warnings.append(
            f"Nie udało się sprawdzić spóźnionych faktur ({kind.label}) — {exc}. "
            "Sprawdzenie zostanie powtórzone przy następnym eksporcie."
        )
        logger.warning("%s", report.warnings[-1])
        return None
    if wynik.clamped and not pierwszy:
        report.warnings.append(
            f"Spóźnione faktury ({kind.label}): poprzednie sprawdzenie było dawno — "
            f"sprawdzono faktury, które wpłynęły do KSeF od {warsaw_date(wynik.since)}; "
            "spóźnione faktury, które wpłynęły wcześniej, mogły zostać pominięte."
        )
        logger.warning("%s", report.warnings[-1])

    wynik_sp = _Spoznione(od=wynik.since, hwm=wynik.hwm)
    if not getattr(wynik, "complete", True):
        wynik_sp.postep = getattr(wynik, "progress", None) or wynik.since
        report.warnings.append(
            f"Sprawdzanie spóźnionych faktur ({kind.label}) przerwane — kończy się godzinny "
            "limit zapytań KSeF o metadane; sprawdzono faktury, które wpłynęły do KSeF do "
            f"{warsaw_date(wynik_sp.postep)}. Zakres wybrany do eksportu jest kompletny; "
            "sprawdzanie spóźnionych dokończy się przy następnym eksporcie."
        )
        logger.warning("%s", report.warnings[-1])
    teraz = _czas_ksef(wynik.hwm)
    czuwa_od = _czas_ksef(ledger.watch_from(nip, kind.value))
    # N3-H: horyzont „sprzed pracy aplikacji” tylko dla zaległości — faktur,
    # które wpłynęły przed początkiem obserwacji (przy pierwszym sprawdzeniu:
    # przed „teraz”); późniejsze idą zwykłą ścieżką (czekanie, bezpiecznik)
    granica_zaleglosci = czuwa_od or teraz
    horyzont = ledger.legacy_horizon(nip, kind.value)
    sprzed_pracy = 0
    # numery z głównego zapytania (zbiór numerów albo lista metadanych)
    biezace_nr = {m if isinstance(m, str) else m.ksef_number for m in biezace}
    for meta in wynik.invoices:
        if meta.ksef_number in biezace_nr:
            continue  # już w głównym zapytaniu (zakres)
        polozenie = _polozenie(meta.issue_date, date_from, date_to)
        if polozenie == 0:
            wynik_sp.w_zakresie.append(meta)
        elif ledger.is_done(nip, meta.ksef_number) or meta.ksef_number in pomijane:
            continue  # wcześniej wyeksportowana / na liście do ręcznej obsługi
        elif (
            horyzont is not None and granica_zaleglosci is not None
            and date.fromisoformat(meta.issue_date[:10]) < horyzont
            and (_czas_ksef(meta.permanent_storage_date) or granica_zaleglosci) < granica_zaleglosci
        ):
            sprzed_pracy += 1  # N3: zaległość z miesiąca sprzed pracy aplikacji
        elif polozenie < 0 and ledger.is_covered(
            nip, kind.value, date.fromisoformat(meta.issue_date[:10])
        ):
            wynik_sp.spoznione.append(meta)
        elif _czeka_za_dlugo(meta, teraz, czuwa_od):
            wynik_sp.spoznione.append(meta)
            wynik_sp.bezpiecznik.add(meta.ksef_number)
        else:
            wynik_sp.wstrzymane.append(meta)
    if sprzed_pracy:
        logger.info(
            "%s: pominięto %d faktur z miesięcy sprzed pracy aplikacji (wystawione przed "
            "%s — przed najstarszym eksportem starszej wersji), które wpłynęły do KSeF od "
            "%s; aplikacja ich nie eksportuje", kind.label, sprzed_pracy,
            horyzont.strftime("%m.%Y"), warsaw_date(wynik.since),
        )
    return wynik_sp


def _przyjmuje(fn, nazwa: str) -> bool:
    """Czy funkcja przyjmuje argument `nazwa` (wprost albo przez **kwargs) —
    klienci-zaślepki i starsze symulatory nie muszą znać nowych argumentów."""
    try:
        parametry = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return nazwa in parametry or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parametry.values()
    )


def _budzet_stron(client, pozostale_rodzaje: int, kind: "Kind", report: "ExportReport"):
    """N3-LIMIT (2): sprawdzenia „od punktu” biorą zapytania z tej samej
    godzinnej puli co główne zapytania (20/h per NIP). Główne zapytania są już
    zrobione (faza 1), ale zostawiamy REZERWA_ZAPYTAN na kolejny eksport w tej
    godzinie; z reszty każdy z pozostałych rodzajów czekających na sprawdzenie
    dostaje co najmniej jedną stronę, bieżący — resztę. Zwraca: None — klient bez licznika
    (bez ograniczenia); False — brak budżetu (ostrzeżenie, sprawdzenie
    pominięte, punkt bez zmian); funkcja „czy wolno zadać kolejne zapytanie”
    dla `query_metadata_since(przed_strona=...)`."""
    budzet = getattr(client, "metadata_budget_remaining", None)
    if budzet is None:
        return None
    zostalo = budzet()
    dostepne = zostalo - REZERWA_ZAPYTAN
    if dostepne <= 0:
        report.warnings.append(
            f"Pominięto sprawdzenie spóźnionych faktur ({kind.label}) — kończy się godzinny "
            f"limit zapytań KSeF o metadane (zostało {zostalo}). Zakres wybrany do eksportu "
            "jest kompletny; sprawdzanie spóźnionych dokończy się przy następnym eksporcie."
        )
        logger.warning("%s", report.warnings[-1])
        return False
    # pierwszy rodzaj (sprzedaż, zwykle największa) bierze wszystko poza jedną
    # stroną dla każdego z pozostałych — koszty zwykle mieszczą się na jednej
    # stronie, a równy podział marnował połowę budżetu
    limit = max(1, dostepne - (pozostale_rodzaje - 1))
    uzyte = [0]

    def przed_strona() -> bool:
        if uzyte[0] >= limit or budzet() <= REZERWA_ZAPYTAN:
            return False
        uzyte[0] += 1
        return True

    return przed_strona


def _nowy_punkt(spr: _Spoznione, wstrzymane: list) -> str:
    """Nowy punkt kontynuacji: HWM z KSeF, ale nie dalej niż data trwałego
    zapisu najwcześniejszej wstrzymanej faktury (następne sprawdzenie zobaczy
    ją ponownie) i nigdy wcześniej niż początek bieżącego sprawdzenia. Brak HWM
    albo nieczytelna data wstrzymanej -> zostaje początek sprawdzenia (bez
    utraty: następny eksport sprawdzi ten sam odcinek jeszcze raz)."""
    od_dt = _parse_ksef_datetime(spr.od)
    kandydat, kandydat_dt = spr.od, od_dt
    # N3-LIMIT (3): przerwane sprawdzenie -> punkt = postęp (wznowienie)
    cel = spr.postep if spr.postep is not None else spr.hwm
    hwm_dt = _parse_ksef_datetime(cel) if cel else None
    if hwm_dt is not None and hwm_dt.tzinfo is not None:
        kandydat, kandydat_dt = cel, hwm_dt
    else:
        logger.warning(
            "KSeF nie zwrócił czytelnego permanentStorageHwmDate (%r) — punkt "
            "kontynuacji bez przesunięcia", spr.hwm,
        )
    for meta in wstrzymane:
        ps = meta.permanent_storage_date
        ps_dt = _parse_ksef_datetime(ps) if ps else None
        if ps_dt is None or ps_dt.tzinfo is None:
            return spr.od
        if kandydat_dt is None or ps_dt < kandydat_dt:
            kandydat, kandydat_dt = ps, ps_dt
    if od_dt is not None and kandydat_dt is not None and kandydat_dt < od_dt:
        return spr.od
    return kandydat


def _zglos_niedomkniete(report: "ExportReport", date_from: date, date_to: date,
                        granice: list[date]) -> None:
    """D-N2: eksport objął CAŁY miesiąc kalendarzowy, ale zapisany zakres został
    przycięty (dane KSeF kompletne dopiero do `granica`) — miesiąc nie jest
    domknięty; księgowa musi to wiedzieć, bo inaczej uzna domknięcie za zrobione."""
    if not granice:
        return
    do_dnia = min(granice)
    m = date_from.replace(day=1)
    while m <= date_to:
        koniec = m.replace(day=calendar.monthrange(m.year, m.month)[1])
        if m >= date_from and koniec <= date_to and koniec > do_dnia:
            co_dalej = (
                "powtórz domknięcie jutro" if (koniec - do_dnia).days <= 1
                else "domknij go eksportem całego miesiąca po jego zakończeniu"
            )
            tekst = (
                f"Miesiąc {_MIESIACE[m.month - 1]} {m.year} nie został jeszcze domknięty "
                f"(dane KSeF kompletne do {do_dnia:%d.%m.%Y}) — {co_dalej}."
            )
            report.niedomkniete.append(tekst)
            report.warnings.append(tekst)
            logger.warning("%s", tekst)
        m = (koniec + timedelta(days=1))


class Kind(Enum):
    SALES = "Subject1"  # sprzedaż (jesteśmy sprzedawcą)
    COST = "Subject2"   # koszt (jesteśmy nabywcą)

    @property
    def label(self) -> str:
        return "sprzedaż" if self is Kind.SALES else "koszt"

    @property
    def file_label(self) -> str:
        return "sprzedaz" if self is Kind.SALES else "koszt"


@dataclass
class ExportReport:
    found: int = 0
    exported: int = 0
    re_exported: int = 0      # w tym: pobrane ponownie (były już w rejestrze)
    skipped_dedup: int = 0    # pominięte w trybie "tylko nowe"
    kontrahenci: int = 0
    # błędy do ręcznej obsługi (mapowanie FA(3), nietrafione wyszukiwanie):
    # (nr KSeF lub nr faktury, komunikat) — okno mówi „zaksięguj ręcznie”
    failed: list[tuple[str, str]] = field(default_factory=list)
    # faktury NIEPOBRANE z KSeF (błąd paczki eksportu, brak w paczce, błąd KSeF
    # przy pobraniu): (nr KSeF, powód) — nie do ręcznego księgowania, tylko do
    # dociągnięcia ponownym eksportem z „Pomiń już pobrane”; zawsze z `niepelny`
    niepobrane: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False
    # nie wszystkie faktury z zakresu trafiły do pliku z powodu innego niż błąd
    # mapowania konkretnej faktury (limit KSeF, nieudana paczka, niepobrane) —
    # liczby found/exported mogą się wtedy zgadzać, bo niepobrany rodzaj w ogóle
    # nie został policzony
    niepelny: bool = False
    numbers_searched: int = 0  # tryb szukania po numerze faktury: ile numerów szukano (0 = pełny eksport)
    numbers_found: int = 0     # ile z szukanych numerów trafiono (>=1 dopasowaną fakturą)
    contractors_searched: int = 0  # tryb szukania po kontrahencie: ile identyfikatorów szukano
    contractors_found: int = 0     # ile z szukanych kontrahentów trafiono (>=1 dopasowaną fakturą)
    # faktury z miesięcy już domkniętych dołączone do pliku, bo wpłynęły do
    # KSeF po domknięciu (wliczone w found/exported). Lista także
    # jako pierwsze ostrzeżenia eksportu („Dołączono N spóźnionych…”, osobno
    # dołączone z bezpiecznika 60 dni) — pierwsze wśród ostrzeżeń run_export;
    # GUI dokleja PRZED nimi ostrzeżenia synchronizacji z Symfonią, więc nie
    # zakładaj, że to warnings[0] (pełna lista: to pole)
    spoznione: list[SpoznionaFaktura] = field(default_factory=list)
    # kontrahenci, którym TEN eksport nadał nowy numer karty (max+1) — aplikacja
    # nie ma łączności z Symfonią, więc księgowa musi po imporcie sprawdzić, czy
    # Symfonia nadała ten sam numer: (nazwa, NIP, numer karty, konto/konta)
    nowi_kontrahenci: list[tuple[str, str, int, str]] = field(default_factory=list)
    # D-N2: eksport objął cały miesiąc, ale dane KSeF nie są jeszcze kompletne
    # do jego końca — miesiąc NIE jest domknięty (komunikaty także w warnings)
    niedomkniete: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.numbers_searched:
            parts.append(f"szukano {self.numbers_searched} numerów, znaleziono {self.numbers_found}")
        if self.contractors_searched:
            parts.append(
                f"szukano {self.contractors_searched} kontrahentów, znaleziono {self.contractors_found}"
            )
        if not parts:
            parts.append(f"znaleziono {self.found}")
        parts.append(f"wyeksportowano {self.exported}")
        if self.re_exported:
            parts.append(f"w tym ponownie {self.re_exported}")
        if self.spoznione:
            parts.append(f"w tym spóźnione z wcześniejszych okresów {len(self.spoznione)}")
        if self.skipped_dedup:
            parts.append(f"pominięto (tylko nowe) {self.skipped_dedup}")
        if self.kontrahenci:
            parts.append(f"kontrahentów {self.kontrahenci}")
        if self.nowi_kontrahenci:
            parts.append(f"w tym nowych (sprawdź numery kart) {len(self.nowi_kontrahenci)}")
        if self.failed:
            parts.append(f"BŁĘDY {len(self.failed)}")
        if self.niepobrane:
            parts.append(f"nie pobrano {len(self.niepobrane)}")
        if self.cancelled:
            parts.append("PRZERWANO")
        elif self.niepelny:
            parts.append("NIEPEŁNY")
        return ", ".join(parts)


def run_export(
    profile: Profile,
    client: KsefClient,
    ledger: Ledger,
    date_from: date,
    date_to: date,
    kinds: set[Kind],
    target_dir: Path,
    *,
    fmt: str = "sksef",
    combined: bool = False,
    only_new: bool = False,
    invoice_numbers: list[str] | None = None,
    contractor_filter: list[str] | None = None,
    kontrahenci_file: bool = False,
    structured_address: bool = False,
    transformer: AmsTransformer | None = None,
    on_progress: Callable[[int, int, str], None] = lambda done, total, nr: None,
    cancel: threading.Event | None = None,
    rejestr_nr: RejestrNumerow | None = None,
    konto_prefix_sprzedaz: str = "201-2",
    konto_prefix_koszt: str = "202-2",
    konto_sprzedaz_netto: str = "703",
    konto_sprzedaz_vat: str = "222-1",
    konto_koszt_vat: str = "221-1",
    konto_koszt_netto: str = "",
    termin_fallback_dni: int = 14,
) -> ExportReport:
    """Eksport per profil.

    `fmt`:
    - "hmffk" — JEDEN plik AMS "format FK 3.0" pod standardowy szablon hmffk.AMS
      ("Dane z programu Handel, format FK 3.0"): sekcje Kontrahent{} + dokument{}
      razem (kontrahenci wbudowani, bez osobnej kartoteki i bez kopiowania
      szablonów do Symfonii). Opcje `combined`/`kontrahenci_file`/
      `structured_address` nie mają tu zastosowania.
    - "sksef" — dotychczasowy tryb: pliki pod SKsefInvoiceImport.AMS (3.1),
      1 plik per faktura albo paczka (`combined`), opcjonalna kartoteka.

    Domyślnie pobierane są WSZYSTKIE faktury z zakresu (KSeF mówi X -> zapisujemy X).
    `only_new=True` pomija faktury zarejestrowane w rejestrze (tryb przyrostowy).
    `kontrahenci_file=True` (tylko sksef) zapisuje dodatkowo plik kart kontrahentów
    AMS (podfolder kartoteka\\) wraz z własnym szablonem importu.

    `rejestr_nr` (tylko hmffk) — trwały rejestr numerów kart kontrahentów w FK
    (per NIP profilu); gdy nie podany, tworzony i zamykany lokalnie na czas
    eksportu. Każdy dokument dostaje konto rozrachunkowe
    "{konto_prefix_sprzedaz|konto_prefix_koszt}-<pozycja karty>" w linii brutto
    sekcji zapis{} — prefiks zależy od rodzaju dokumentu (sprzedaż/koszt).
    Dokumenty sprzedażowe dostają dodatkowo pełny dekret: `konto_sprzedaz_netto`
    na linii MA netto i `konto_sprzedaz_vat` na linii MA VAT. Dokumenty kosztowe
    dostają dekret analogicznie: `konto_koszt_vat` na linii WN VAT (domyślnie
    "221-1" — VAT naliczony) i `konto_koszt_netto` na linii WN netto (domyślnie
    puste — konto kosztowe przypisuje księgowa ręcznie, pole tylko do dyspozycji
    stałego ustawienia).

    `termin_fallback_dni` (tylko hmffk) — gdy faktura nie ma TerminPlatnosci
    w FA(3), map_fa3_to_hmffk liczy termin jako data wystawienia + tyle dni,
    żeby rozrachunek (sekcja transakcja{}) i tak powstał automatycznie w FK.

    `invoice_numbers` — tryb wyszukiwania po numerze faktury WYSTAWCY (nie
    numerze KSeF). KSeF nie umie szukać po tym numerze, więc pobierane są
    metadane w zadanym `date_from`–`date_to`/`kinds` (jak w pełnym eksporcie),
    a następnie filtrowane lokalnie. Każdy wpis może mieć jedną z trzech form:
    - pełny numer (np. "FV/07/2026/00247") — dopasowanie dokładne, znormalizowane
      (bez wielkości liter i białych znaków);
    - sama końcówka, same cyfry (np. "247") — dopasowanie po OSTATNIM ciągu cyfr
      numeru faktury, porównanie liczbowe ("247" == "00247"); numer faktury bez
      żadnych cyfr nigdy nie pasuje;
    - zakres końcówek (np. "247-251") — jw., rozwinięty lokalnie do listy
      końcówek 247..251; początek musi być <= końca, a rozpiętość <= 2000,
      inaczej wpis od razu trafia do `report.failed` (bez żadnego zapytania
      do KSeF).

    Pobierane i eksportowane są tylko trafienia. Wpis bez ŻADNEGO trafienia
    (w żadnym z `kinds`) trafia do `report.failed`; pojedyncza wartość
    (numer albo konkretna końcówka z zakresu) trafiająca więcej niż jedną
    fakturę (różni wystawcy/serie pod tym samym numerem/końcówką) trafia do
    `report.warnings`, a WSZYSTKIE tak dopasowane faktury zostają pobrane —
    zakres, w którym każda końcówka trafia dokładnie 1 fakturę, warningu nie
    generuje. `report.numbers_searched` liczy WPISY użytkownika (zakres to
    zawsze 1 wpis, niezależnie od liczby końcówek, które obejmuje). `None`/
    pusta lista — zachowanie jak dotąd (pełny eksport zakresu).

    `contractor_filter` — tryb wyszukiwania po KONTRAHENCIE: lista
    identyfikatorów, każdy albo NIP (10 cyfr po `_digits` — toleruje prefiks
    "PL" i separatory), albo numer karty z rejestru numerów (liczba całkowita
    < 10 cyfr, tłumaczona na NIP przez `rejestr_nr.lista(profile.nip)`; karta
    bez przypisanego NIP w rejestrze daje ostrzeżenie i jest pomijana — bez
    rejestru numerów wcale, np. `fmt="sksef"` bez `rejestr_nr`, numer karty
    też nie może zostać przetłumaczony). Podobnie jak `invoice_numbers`,
    metadane są pobierane z całego `date_from`–`date_to`/`kinds`, a następnie
    filtrowane: dla `Kind.SALES` kontrahentem jest nabywca (`buyer_id`), dla
    `Kind.COST` — sprzedawca (`seller_nip`); porównanie po cyfrach (`_digits`).
    Może współistnieć z `invoice_numbers` — wtedy działa KONIUNKCJA (faktura
    musi pasować do obu filtrów). Identyfikator bez trafienia w żadnym z
    `kinds` trafia do `report.failed` jako `("kontrahent <id>", "brak faktur
    w KSeF w zakresie <od>–<do> dla wybranych rodzajów")`. Nazwa pliku
    wynikowego dostaje infiks `_kontrahenci` (obok `_numery`, gdy oba filtry
    aktywne naraz: `_numery_kontrahenci`). `None`/pusta lista — bez zmian.

    Spóźnione faktury (pełny eksport zakresu, nie tryby wyszukiwania): główne
    zapytanie idzie po dacie wystawienia, więc faktura z wcześniejszego okresu,
    która dostała numer KSeF PO eksporcie tamtego okresu (tryb offline, spóźniony
    wystawca), nie trafiłaby już do żadnego eksportu. Dlatego po głównym
    zapytaniu każdego rodzaju pytamy KSeF (data trwałego zapisu, PermanentStorage,
    z HWM), co wpłynęło od punktu kontynuacji zapisanego w rejestrze dla
    (NIP profilu, rodzaj) — `_sprawdz_spoznione`.

    W2 (decyzja 28.09, „do domknięcia miesiąca”): rejestr pamięta zakresy dat
    wystawienia, które wyeksportowano (per NIP × rodzaj — `Ledger.record_range`,
    zapis po przebiegu dla rodzaju przetworzonego w całości). Miesiąc jest
    domknięty, gdy JEDEN zakres obejmuje go w całości (domknięcie = osobny
    eksport całego miesiąca; zakres wielomiesięczny domyka każdy w pełni
    objęty miesiąc; zakresy „na bieżąco” — nie, nawet gdy razem pokrywają
    miesiąc). Spóźniona to faktura wystawiona przed `date_from`, z miesiąca
    domkniętego, bez wpisu w rejestrze pobranych — dokładana do TEGO
    SAMEGO pliku (pobierana zawsze pojedynczo: paczka eksportu idzie po dacie
    wystawienia z zakresu, a spóźnionych jest zwykle 0–kilka), liczona
    w found/exported, wypisywana w `report.spoznione` i jako pierwsze
    ostrzeżenie eksportu (GUI dokleja przed nim ostrzeżenia synchronizacji
    z Symfonią — pełna lista to `report.spoznione`, nie `warnings[0]`).
    Faktura z miesiąca jeszcze niedomkniętego czeka: nie trafia do pliku,
    a punkt jej nie przeskakuje — zabierze ją główne zapytanie domknięcia jej
    miesiąca (także z „Pomiń już pobrane” — nie ma jej w rejestrze). Wystawione po
    `date_to` też czekają (spóźnione dołączamy tylko do eksportu późniejszego
    okresu). Bezpiecznik: czekająca dłużej niż CZEKANIE_NA_OKRES_MAX od wpływu
    do KSeF (nie wcześniej niż od początku obserwacji) dołącza jako spóźniona
    z osobnym ostrzeżeniem. Przykład: faktura z 03.09, która wpłynęła po
    eksporcie 01–07.09, nie trafia do eksportu 08–15.09 — bierze ją domknięcie
    01–30.09 jako zwykłą; dopiero faktura z września, która wpłynie PO
    domknięciu, jest spóźniona i dołącza do najbliższego eksportu.

    W3: rejestr pamięta, do którego zakresu dołączono spóźnioną
    (`Ledger.record_late`); PEŁNY eksport (bez `only_new`) zakresu, który
    obejmuje zakres dołączenia (ten sam albo np. cały miesiąc), dokłada ją
    znowu — plik taki sam jak za pierwszym razem. Z `only_new` — nie (jest
    w rejestrze pobranych). Spóźnionych z listy do ręcznej obsługi — nie
    (za pierwszym razem ich w pliku nie było).

    Spóźniona z błędem mapowania (Fa3MappingError): `report.failed` (do
    ręcznego zaksięgowania) + trwała lista „do ręcznej obsługi” w rejestrze
    (`Ledger.manual_list`); punkt ją przeskakuje (błąd mapowania jest trwały —
    wstrzymany punkt stałby w miejscu), a kolejne sprawdzenia jej nie zgłaszają
    ponownie (bez ryzyka podwójnego ręcznego księgowania). D1: w pełnym
    eksporcie jest wycinana także z głównego zapytania (eksport JEJ miesiąca,
    z `only_new` i bez) — ostrzeżenie „Pominięto N faktur oznaczonych do
    ręcznej obsługi”; tryby wyszukiwania tej listy nie stosują. Faktura
    z ZAKRESU z błędem mapowania trafia na tę samą listę, ale tylko gdy
    przebieg zapisuje zakres jej rodzaju (`r.bledy_glowne`, zapis na końcu
    przebiegu) — inaczej zostaje zwykłym błędem, a kolejny kompletny eksport
    zgłosi ją ponownie.

    Nowy punkt (HWM) i zakres zapisywane są na końcu przebiegu, po zapisie
    plików, tylko dla rodzaju przetworzonego bez przerwania i bez eksportu
    niepełnego (limit, błąd paczki, niepobrane) — liczone osobno per rodzaj;
    zakres najwyżej do dnia przed dniem HWM w czasie polskim (N2,
    `_granica_zakresu`; bez HWM — do wczoraj), a gdy to przed `date_from` —
    wcale;
    inaczej zostaje stary i następny eksport ponawia sprawdzenie. Koszt: +1
    zapytanie o metadane na rodzaj (więcej przy >250 nowych fakturach albo
    punkcie starszym niż 90 dni) z puli 20/h per NIP. Klient bez
    `query_metadata_since` (stuby w testach) — bez sprawdzania.

    N3-LIMIT — kolejność i budżet: faza 1 = główne zapytania i pobieranie
    WSZYSTKICH rodzajów; faza 2 = sprawdzenia „od punktu” z budżetem stron
    (`_budzet_stron`: rezerwa REZERWA_ZAPYTAN, reszta po równo między rodzaje).
    Przerwane sprawdzenie (budżet albo limit KSeF) NIE czyni eksportu
    niepełnym — ostrzeżenie „dokończy się przy następnym eksporcie”, punkt =
    min(postęp, wpływ najwcześniejszej wstrzymanej) (`_nowy_punkt`), kolejny
    eksport kontynuuje. Bez żadnej odebranej strony punkt się nie zmienia.
    D-N2: eksport całego miesiąca przycięty przez N2 -> `report.niedomkniete`
    (miesiąc nie jest domknięty — okno końcowe ostrzega).
    """
    report = ExportReport()
    hmffk_mode = fmt == "hmffk"
    transformer = transformer if (transformer or hmffk_mode) else AmsTransformer()
    out_dir = Path(target_dir) / profile.subfolder
    zebrani: dict[str, Kontrahent] = {}  # NIP -> karta (unikalnie w całym przebiegu)
    hmffk_kh = KontrahenciRejestr()
    hmffk_docs: list[tuple[object, object, bool]] = []  # (meta, HDokument, ponowna?)

    # tryb szukania po numerze faktury: trzy formy wpisu (patrz docstring wyżej).
    # `numbers_mode` opiera się na SUROWYM parametrze (nie na `wanted`), żeby wpis
    # z samymi nieprawidłowymi zakresami nie "cichł" do pełnego eksportu zakresu.
    wanted: list[_WantedNumber] = []
    widziane_wpisy: set[str] = set()  # dedup wg postaci wpisu, pierwsze wystąpienie wygrywa
    numbers_mode = bool(invoice_numbers)
    if invoice_numbers:
        for raw in invoice_numbers:
            numer = (raw or "").strip()
            if not numer:
                continue
            compact = re.sub(r"\s+", "", numer)
            zakres = re.fullmatch(r"(\d+)-(\d+)", compact)
            if zakres:
                if compact in widziane_wpisy:
                    continue
                widziane_wpisy.add(compact)
                lo, hi = int(zakres.group(1)), int(zakres.group(2))
                etykieta = f"zakres {compact}"
                if lo > hi or hi - lo > 2000:
                    report.failed.append((
                        etykieta,
                        "nieprawidłowy zakres numerów — początek nie może być większy "
                        "od końca, a zakres nie może obejmować więcej niż 2000 numerów",
                    ))
                    report.numbers_searched += 1
                    continue
                wanted.append(_WantedNumber(
                    original=etykieta, suffix_values=frozenset(range(lo, hi + 1)),
                ))
            elif compact.isdigit():
                if compact in widziane_wpisy:
                    continue
                widziane_wpisy.add(compact)
                wanted.append(_WantedNumber(original=numer, suffix_values=frozenset({int(compact)})))
            else:
                norm = _norm_numer(numer)
                if norm in widziane_wpisy:
                    continue
                widziane_wpisy.add(norm)
                wanted.append(_WantedNumber(original=numer, exact_norm=norm))
    report.numbers_searched += len(wanted)

    rejestr = rejestr_nr
    wlasny_rejestr = False
    if rejestr is None and hmffk_mode:
        rejestr = RejestrNumerow()
        wlasny_rejestr = True

    # tryb szukania po kontrahencie: identyfikator (NIP LUB numer karty) ->
    # znormalizowany NIP (cyfry). Numer karty tłumaczymy na NIP przez rejestr
    # numerów (dostępny tylko gdy hmffk_mode albo jawnie podano rejestr_nr).
    # Identyfikator, którego NIE da się przełożyć na NIP (karta bez NIP, karta
    # nieznana, tekst niebędący ani NIP-em ani numerem), i tak "uczestniczy"
    # w trybie szukania — z góry wiadomo, że nie znajdzie żadnej faktury, więc
    # trafia prosto do nietrafionych (patrz niżej), a nie cichnie do pełnego
    # eksportu.
    contractor_ids: dict[str, str] = {}  # znorm. NIP -> oryginalny identyfikator
    kontrahent_nierozwiazany: list[str] = []  # identyfikatory bez przełożenia na NIP
    if contractor_filter:
        karty_do_nip: dict[int, str] | None = None
        widziane: set[str] = set()
        for raw in contractor_filter:
            ident = (raw or "").strip()
            if not ident or ident in widziane:
                continue
            widziane.add(ident)
            cyfry = _digits(ident)
            if len(cyfry) == 10:
                contractor_ids.setdefault(cyfry, ident)
                continue
            try:
                numer_karty = int(ident)
            except ValueError:
                report.warnings.append(
                    f"kontrahent {ident}: nieprawidłowy identyfikator (nie jest NIP-em "
                    "ani numerem karty) — pominięto"
                )
                kontrahent_nierozwiazany.append(ident)
                continue
            if karty_do_nip is None:
                if rejestr is None:
                    report.warnings.append(
                        "filtr po numerze karty wymaga rejestru numerów (dostępny dla "
                        "formatu hmffk albo gdy podano rejestr_nr) — pominięto"
                    )
                    karty_do_nip = {}
                else:
                    karty_do_nip = {w.pozycja: w.nip for w in rejestr.lista(profile.nip)}
            if numer_karty not in karty_do_nip:
                report.warnings.append(f"karta {numer_karty} nie istnieje w rejestrze — pominięto")
                kontrahent_nierozwiazany.append(ident)
                continue
            nip_karty = karty_do_nip[numer_karty]
            if not nip_karty:
                report.warnings.append(
                    f"karta {numer_karty} nie ma NIP — filtr po NIP niemożliwy, pominięto"
                )
                kontrahent_nierozwiazany.append(ident)
                continue
            contractor_ids.setdefault(_digits(nip_karty), ident)
    contractors_mode = bool(contractor_ids) or bool(kontrahent_nierozwiazany)
    contractor_found: dict[str, bool] = {nip: False for nip in contractor_ids}
    report.contractors_searched = len(contractor_ids) + len(kontrahent_nierozwiazany)

    plik_suffix = ("_numery" if numbers_mode else "") + ("_kontrahenci" if contractors_mode else "")

    # spóźnione faktury, zakresy (W2) i ponowne dołączanie (W3): tylko pełny
    # eksport zakresu (nie wyszukiwania)
    pelny_tryb = not numbers_mode and not contractors_mode
    sprawdzaj_spoznione = pelny_tryb and hasattr(client, "query_metadata_since")
    # subject_type -> (punkt, moment sprawdzenia = HWM) do zapisania po przebiegu
    nowe_punkty: dict[str, tuple[str, str]] = {}
    # zakres przetworzony w całości -> rejestr zakresów: (rodzaj, ostatni dzień do zapisu)
    kompletne_rodzaje: list[tuple[Kind, date]] = []
    pominiete_reczne: list[tuple[object, Kind]] = []  # D1: z listy do ręcznej obsługi
    wykryte_spoznione: list[tuple[object, Kind]] = []  # (meta, rodzaj)
    z_bezpiecznika: set[str] = set()  # nr KSeF spóźnionych dołączonych z bezpiecznika
    bledy_mapowania: dict[str, str] = {}  # nr KSeF -> Fa3MappingError (pobrane pojedynczo)
    zapisane: set[str] = set()  # nr KSeF faktycznie zapisane do pliku w tym przebiegu
    do_recznej = (
        {r.ksef_number for r in ledger.manual_list(profile.nip)} if pelny_tryb else set()
    )

    logger.info(
        "Profil %s (NIP %s…): eksport %s, zakres %s – %s",
        profile.name, profile.nip[:3],
        "+".join(k.label for k in sorted(kinds, key=lambda k: k.value)),
        date_from, date_to,
    )

    try:
        try:
            client.authenticate()
        except KsefError as exc:
            report.failed.append(("(auth)", str(exc)))
            logger.error("Logowanie do KSeF nieudane: %s", exc)
            return report

        kolejnosc = sorted(kinds, key=lambda k: k.value)
        # rodzaje po fazie 1 (główne zapytanie przetworzone) — stan per rodzaj
        rodzaje: list[_Rodzaj] = []

        def oznacz_niepelny(r: _Rodzaj) -> None:
            # niepełny liczony per rodzaj (11): błąd paczki sprzedaży nie może
            # blokować punktu/zakresu kosztów; report.niepelny = suma rodzajów
            r.niepelny = True
            report.niepelny = True

        def process(r: _Rodzaj, meta, fa_xml: bytes) -> None:
            """Wspólna obróbka jednej faktury: map -> (hmffk | paczka | plik per faktura)."""
            ctx = InvoiceContext(
                ksef_number=meta.ksef_number,
                ksef_date=meta.ksef_date,
                subject_type=r.kind.value,
            )
            if hmffk_mode:
                res = map_fa3_to_hmffk(fa_xml, ctx, termin_fallback_dni=termin_fallback_dni)
                for w in res.warnings:
                    report.warnings.append(f"{meta.ksef_number}: {w}")
                    logger.warning("%s: %s", meta.ksef_number, w)
                hmffk_kh.add(res.kontrahent)
                hmffk_docs.append(
                    (meta, res.dokument, ledger.is_done(profile.nip, meta.ksef_number))
                )
                logger.info("OK %s (%s)", meta.ksef_number, res.dokument.numer)
                return
            result = map_fa3_to_ksef_invoice(fa_xml, ctx, structured_address=structured_address)
            for w in result.warnings:
                report.warnings.append(f"{meta.ksef_number}: {w}")
                logger.warning("%s: %s", meta.ksef_number, w)
            if kontrahenci_file:
                for karta in kontrahenci_from_invoice(result.element, profile.nip):
                    zebrani[karta.klucz] = merge_kontrahent(zebrani.get(karta.klucz), karta)
            already = ledger.is_done(profile.nip, meta.ksef_number)
            if combined:
                r.batch.append((meta, result.element, already))
            else:
                data = transformer.to_txt_bytes(result.element, context=meta.ksef_number)
                txt_path = out_dir / f"{meta.ksef_number}.TXT"
                write_txt_atomic(txt_path, data)
                ledger.mark_done(profile.nip, meta.ksef_number, str(txt_path))
                zapisane.add(meta.ksef_number)
                report.exported += 1
                if already:
                    report.re_exported += 1
                logger.info("OK %s -> %s", meta.ksef_number, txt_path.name)

        def blad_mapowania(r: _Rodzaj, meta, exc: Fa3MappingError) -> None:
            """Faktura, której nie da się przetworzyć -> report.failed (zaksięgować
            ręcznie). Spóźniona -> od razu trwała lista do ręcznej obsługi (punkt
            ją przeskakuje); z zakresu -> na listę dopiero na końcu przebiegu,
            jeśli zakres tego rodzaju zostanie zapisany (`r.bledy_glowne`)."""
            komunikat = str(exc)
            if meta.ksef_number in r.spoznione_nr:
                komunikat += (
                    f" — spóźniona faktura z {(meta.issue_date or '')[:10]}: "
                    "zapisana na liście do ręcznej obsługi, zaksięguj ją ręcznie "
                    "(kolejne eksporty nie zgłoszą jej ponownie)"
                )
                bledy_mapowania[meta.ksef_number] = str(exc)
            else:
                r.bledy_glowne[meta.ksef_number] = (meta, str(exc))
            report.failed.append((meta.ksef_number, komunikat))
            logger.error("BŁĄD %s: %s", meta.ksef_number, komunikat)

        def pobierz_pojedynczo(r: _Rodzaj, lista, start: int, razem: int) -> None:
            """Pojedyncze GET-y (limit 64/h). Błąd mapowania -> failed (ręcznie);
            błąd KSeF przy pobraniu -> niepobrane + niepełny (ponów eksport);
            limit/przerwanie -> wyżej."""
            for i, meta in enumerate(lista, start=start):
                if cancel is not None and cancel.is_set():
                    report.cancelled = True
                    return
                on_progress(i, razem, meta.ksef_number)
                try:
                    fa_xml = client.download_invoice(meta.ksef_number)
                    process(r, meta, fa_xml)
                except (KsefCancelled, KsefQuotaExceeded):
                    raise
                except Fa3MappingError as exc:
                    blad_mapowania(r, meta, exc)
                except KsefError as exc:
                    report.niepobrane.append((meta.ksef_number, str(exc)))
                    oznacz_niepelny(r)
                    logger.error("NIE POBRANO %s: %s", meta.ksef_number, exc)

        def pobierz(r: _Rodzaj, todo: list, pojedynczo: list) -> None:
            """`todo` — z zakresu (paczką eksportu przy > progu, inaczej pojedynczo);
            `pojedynczo` — zawsze pojedynczo (spóźnione, ponowne W3, z zakresu
            dopisane przez sprawdzenie „od punktu”). Limit pobierania -> ostrzeżenie
            „Pozostało N”, rodzaj niepełny."""
            razem = len(todo) + len(pojedynczo)
            exported_before = report.exported
            failed_before = len(report.failed)
            niepobrane_before = len(report.niepobrane)
            hmffk_before = len(hmffk_docs)
            batch_before = len(r.batch)
            try:
                if len(todo) > BATCH_DOWNLOAD_THRESHOLD and hasattr(client, "request_export"):
                    # eksport wsadowy: jedna paczka ZIP zamiast N pojedynczych GET-ów
                    logger.info(
                        "%s: %d faktur — pobieram paczką eksportu (bez limitu 64/h)",
                        r.kind.label, len(todo),
                    )
                    by_nr = {m.ksef_number: m for m in todo}
                    done = 0
                    blad_paczki = False
                    # paczka per okno <= 3 mies. (+ zapas): KSeF przyjmuje
                    # zakres maks. 100 dni; nakładki okien usuwa by_nr.pop
                    for win_from, win_to in split_date_range(date_from, date_to):
                        if not by_nr or report.cancelled:
                            break
                        try:
                            reference = client.request_export(
                                r.kind.value, win_from - QUERY_MARGIN, win_to + QUERY_MARGIN
                            )
                            package = client.wait_for_export(reference)
                            for nr, fa_xml in client.iter_export_invoices(reference, package):
                                if cancel is not None and cancel.is_set():
                                    report.cancelled = True
                                    break
                                meta = by_nr.pop(nr, None)
                                if meta is None:
                                    continue  # spoza listy (spoza zakresu, "tylko nowe")
                                done += 1
                                on_progress(done, razem, nr)
                                try:
                                    process(r, meta, fa_xml)
                                except Fa3MappingError as exc:
                                    blad_mapowania(r, meta, exc)
                        except (KsefCancelled, KsefQuotaExceeded):
                            raise
                        except KsefError as exc:
                            # błąd jednej paczki nie może kasować tego, co już
                            # przetworzono (plik zapisuje się na końcu przebiegu)
                            blad_paczki = True
                            oznacz_niepelny(r)
                            report.warnings.append(
                                f"Paczka eksportu {win_from.isoformat()}–{win_to.isoformat()} "
                                f"({r.kind.label}) nie powiodła się: {exc}"
                            )
                            logger.error("%s", report.warnings[-1])
                    if not report.cancelled and by_nr:
                        # W6a: to NIE są błędy do ręcznego księgowania — faktury
                        # nie zostały pobrane; dociągnie je ponowny eksport
                        # z „Pomiń już pobrane” (bez dubli)
                        powod = (
                            "nie pobrano — błąd paczki eksportu (szczegóły w ostrzeżeniach)"
                            if blad_paczki else "nie pobrano — faktury nie było w paczce eksportu"
                        )
                        for nr in by_nr:
                            report.niepobrane.append((nr, powod))
                        oznacz_niepelny(r)
                        logger.error(
                            "%s: nie pobrano %d faktur — uruchom eksport ponownie z „Pomiń "
                            "już pobrane”", r.kind.label, len(by_nr),
                        )
                    start = len(todo) + 1
                else:
                    pobierz_pojedynczo(r, todo, 1, razem)
                    start = len(todo) + 1
                if pojedynczo and not report.cancelled:
                    # spóźnionych nie ma w paczce (ta idzie po dacie wystawienia
                    # z zakresu) — zawsze pojedynczo; zwykle to 0–kilka faktur
                    pobierz_pojedynczo(r, pojedynczo, start, razem)
            except KsefQuotaExceeded as exc:
                remaining = (
                    razem
                    - (report.exported - exported_before)
                    - (len(r.batch) - batch_before)
                    - (len(hmffk_docs) - hmffk_before)
                    - (len(report.failed) - failed_before)
                    - (len(report.niepobrane) - niepobrane_before)
                )
                report.warnings.append(
                    f"{exc} Pozostało {max(remaining, 0)} faktur ({r.kind.label}) — "
                    "uruchom eksport ponownie po tej godzinie; tryb „tylko nowe” "
                    "dociągnie brakujące bez dublowania."
                )
                logger.warning("%s", report.warnings[-1])
                report.cancelled = False  # to nie przerwanie użytkownika
                oznacz_niepelny(r)

        def w_zakresie(metadane, poza_zakresem: list[int]) -> list:
            """Odcina faktury z datą wystawienia spoza zakresu (zapas zapytania do
            KSeF; `poza_zakresem[0]` liczy pominięte) i ostrzega o schemacie
            innym niż FA (3)."""
            metas = []
            for meta in metadane:
                if not _w_zakresie(meta.issue_date, date_from, date_to):
                    poza_zakresem[0] += 1
                    continue
                if meta.form_system_code and meta.form_system_code != "FA (3)":
                    report.warnings.append(
                        f"{meta.ksef_number}: schemat {meta.form_system_code} (nie FA (3))"
                    )
                metas.append(meta)
            return metas

        def filtruj_wyszukiwanie(kind: Kind, metas: list) -> list:
            """Tryby wyszukiwania (numer faktury, kontrahent) — filtr po fakcie,
            ten sam dla listy metadanych i dla paczek eksportu."""
            if numbers_mode:
                trafione = []
                for meta in metas:
                    meta_norm = _norm_numer(meta.invoice_number)
                    meta_koncowka = _ostatnie_cyfry(meta_norm)
                    dopasowano = False
                    for entry in wanted:
                        if entry.exact_norm is not None:
                            if meta_norm == entry.exact_norm:
                                entry.matches.setdefault(entry.exact_norm, []).append(meta)
                                dopasowano = True
                        elif meta_koncowka is not None and meta_koncowka in entry.suffix_values:
                            entry.matches.setdefault(meta_koncowka, []).append(meta)
                            dopasowano = True
                    if dopasowano:
                        trafione.append(meta)
                metas = trafione

            if contractors_mode:
                # SALES: kontrahentem jest nabywca (buyer_id); COST: sprzedawca (seller_nip)
                trafione = []
                for meta in metas:
                    kontrahent = meta.buyer_id if kind is Kind.SALES else meta.seller_nip
                    cyfry_k = _digits(kontrahent)
                    if cyfry_k in contractor_ids:
                        contractor_found[cyfry_k] = True
                        trafione.append(meta)
                metas = trafione
            return metas

        def bez_recznej(kind: Kind, metas: list) -> list:
            if do_recznej:
                # D1: faktura z listy do ręcznej obsługi nie trafia do pliku także
                # w eksporcie JEJ miesiąca (z „Pomiń” i bez) — zaksięgowana
                # ręcznie, więc po poprawce mappera byłby dubel
                pominiete_reczne.extend((m, kind) for m in metas if m.ksef_number in do_recznej)
                metas = [m for m in metas if m.ksef_number not in do_recznej]
            return metas

        def do_pobrania(metas: list) -> list:
            if only_new:
                todo = [m for m in metas if not ledger.is_done(profile.nip, m.ksef_number)]
                report.skipped_dedup += len(metas) - len(todo)
                return todo
            return metas  # pełny eksport: KSeF mówi X -> pobieramy dokładnie X

        def ponowne_w3(r: _Rodzaj) -> list:
            ponowne: list = []
            if pelny_tryb and not only_new:
                # W3: pełny eksport zakresu dokłada spóźnione, które wcześniej
                # dołączono do eksportu mieszczącego się w tym zakresie — plik
                # taki sam jak za pierwszym razem (z „Pomiń już pobrane” nie:
                # są w rejestrze pobranych). Bez listy „do ręcznej obsługi”:
                # za pierwszym razem ich w pliku nie było (dubel z ręcznym
                # księgowaniem, gdyby teraz dało się je przetworzyć)
                ponowne = [
                    _meta_z_rejestru(rec)
                    for rec in ledger.late_within(profile.nip, r.kind.value, date_from, date_to)
                    if rec.status == STATUS_W_PLIKU
                    and rec.ksef_number not in r.glowne_nr
                    and _polozenie(rec.issue_date, date_from, date_to) != 0
                ]
                if ponowne:
                    logger.info(
                        "%s: %d spóźnionych faktur dołączonych wcześniej do tego zakresu — "
                        "dołączam ponownie (pełny eksport)", r.kind.label, len(ponowne),
                    )
            wykryte_spoznione.extend((m, r.kind) for m in ponowne)
            r.spoznione_nr.update(m.ksef_number for m in ponowne)
            return ponowne

        def faza1_paczkami(r: _Rodzaj, poza_zakresem: list[int]) -> int:
            """Duży zakres (sonda: więcej niż jedna strona metadanych): rodzaj
            pobierany paczkami eksportu zamiast listy — lista całego zakresu
            (pageSize 250, limit 20 zapytań/h) padała na limicie przy > ~5000
            faktur, zanim cokolwiek pobrano. Metadane z `_metadata.json`, XML
            z tej samej paczki (bez ponownego pobierania); te same filtry co na
            liście. Przetwarzanie per paczka: błąd, limit albo timeout kolejnej
            paczki -> rodzaj niepełny z ostrzeżeniem (bez zapisu zakresu
            i punktu), a to, co przetworzone z wcześniejszych paczek, zostaje.
            Zwraca liczbę faktur z zakresu (po filtrach)."""
            kind = r.kind
            logger.info(
                "%s: ponad %d faktur w zakresie — pobieram paczkami eksportu KSeF "
                "(bez listowania metadanych)", kind.label, PAGE_SIZE,
            )
            w_zakresie_razem = 0
            przetworzone = 0
            najstarsze_zamowienie: datetime | None = None   # początek sprawdzenia wpływów
            # (7) mianownik postępu nie może pokazać 100%, gdy przyjdą jeszcze
            # kolejne paczki; czekanie na paczkę opisane w treści postępu
            on_progress(0, 1, "czekam na paczkę eksportu z KSeF…")
            try:
                with contextlib.closing(client.pobierz_paczkami(
                    kind.value, date_from, date_to, margin=QUERY_MARGIN,
                )) as paczki:
                    for paczka in paczki:
                        zamowiona = getattr(paczka, "zamowiona", None)
                        if zamowiona is not None:
                            od_wplywow = zamowiona - (
                                ZAPAS_OD_ZAMOWIENIA if getattr(paczka, "zamowiona_zegar_ksef", False)
                                else ZAPAS_OD_ZAMOWIENIA_ZEGAR_KOMPUTERA
                            )
                            najstarsze_zamowienie = min(od_wplywow, najstarsze_zamowienie or od_wplywow)
                        metas = filtruj_wyszukiwanie(kind, w_zakresie(paczka.metadane, poza_zakresem))
                        r.glowne_nr.update(m.ksef_number for m in metas)
                        w_zakresie_razem += len(metas)
                        metas = bez_recznej(kind, metas)
                        report.found += len(metas)
                        todo = do_pobrania(metas)
                        kolejne = bool(getattr(paczka, "kolejne", False))
                        razem = przetworzone + len(todo) + (1 if kolejne else 0)
                        for meta in todo:
                            if cancel is not None and cancel.is_set():
                                report.cancelled = True
                                break
                            przetworzone += 1
                            on_progress(przetworzone, razem, meta.ksef_number)
                            try:
                                process(r, meta, paczka.xml(meta.ksef_number))
                            except Fa3MappingError as exc:
                                blad_mapowania(r, meta, exc)
                            except KsefError as exc:
                                report.niepobrane.append((meta.ksef_number, str(exc)))
                                oznacz_niepelny(r)
                                logger.error("NIE POBRANO %s: %s", meta.ksef_number, exc)
                        if report.cancelled:
                            break
                        if kolejne:
                            on_progress(przetworzone, przetworzone + 1,
                                        "czekam na kolejną paczkę eksportu z KSeF…")
            except KsefCancelled:
                raise
            except KsefError as exc:  # także KsefQuotaExceeded, timeout paczki
                oznacz_niepelny(r)
                odbior = (
                    " (zamówiona paczka zostaje w KSeF — kolejny eksport odbierze ją bez "
                    "ponownego zamawiania)"
                    if isinstance(exc, KsefExportTimeout) and getattr(exc, "do_odbioru", True)
                    else ""
                )
                report.warnings.append(
                    f"Pobieranie paczkami ({kind.label}, {date_from.isoformat()}–"
                    f"{date_to.isoformat()}) przerwane: {str(exc).rstrip('.')}{odbior}. "
                    f"Przetworzono {przetworzone} faktur — uruchom eksport ponownie "
                    "z „Pomiń już pobrane”, żeby dociągnąć resztę bez dublowania."
                )
                logger.error("%s", report.warnings[-1])
            if najstarsze_zamowienie is not None and not r.niepelny and not report.cancelled:
                w_zakresie_razem += dociagnij_od_zamowienia(r, najstarsze_zamowienie)
            return w_zakresie_razem

        def dociagnij_od_zamowienia(r: _Rodzaj, od: datetime) -> int:
            """Paczka odebrana z zamówienia z poprzedniego eksportu nie zna faktur,
            które wpłynęły po zamówieniu. Zapytanie „od punktu” (data trwałego
            zapisu) od `od` (chwila zamówienia wg zegara KSeF minus
            ZAPAS_OD_ZAMOWIENIA; bez zegara KSeF — komputera minus
            ZAPAS_OD_ZAMOWIENIA_ZEGAR_KOMPUTERA): faktury z zakresu spoza paczek —
            dociągane pojedynczo. Sprawdzenie niepełne (limit) albo początek
            późniejszy niż „teraz” KSeF (HWM; zegar komputera w przyszłości) ->
            rodzaj niepełny, bez domknięcia: nie wiadomo, czego brakuje. Zwraca
            liczbę dołożonych faktur z zakresu."""
            kind = r.kind
            kiedy = od.astimezone().strftime("%d.%m %H:%M")
            wynik, blad = None, ""
            try:
                wynik = client.query_metadata_since(
                    kind.value, od.replace(microsecond=0).isoformat(),
                    max_lookback=timedelta(days=3),
                )
            except KsefCancelled:
                raise
            except KsefError as exc:
                blad = f" ({exc})"
            hwm = _czas_ksef(getattr(wynik, "hwm", None)) if wynik is not None else None
            if wynik is not None and hwm is not None and hwm <= od:
                wynik, blad = None, " (początek sprawdzenia późniejszy niż czas KSeF)"
            if wynik is None or not getattr(wynik, "complete", True):
                oznacz_niepelny(r)
                report.warnings.append(
                    f"Nie sprawdzono faktur ({kind.label}), które wpłynęły po zamówieniu "
                    f"odebranej paczki (od {kiedy}){blad} — eksport niepełny; uruchom go "
                    "ponownie z „Pomiń już pobrane” po tej godzinie."
                )
                logger.warning("%s", report.warnings[-1])
                return 0
            nowe = [m for m in wynik.invoices if m.ksef_number not in r.glowne_nr]
            metas = filtruj_wyszukiwanie(kind, w_zakresie(nowe, [0]))
            r.glowne_nr.update(m.ksef_number for m in metas)
            metas = bez_recznej(kind, metas)
            report.found += len(metas)
            todo = do_pobrania(metas)
            if todo:
                logger.info(
                    "%s: %d faktur z zakresu wpłynęło po zamówieniu odebranej paczki — "
                    "dociągam pojedynczo", kind.label, len(todo),
                )
                pobierz(r, [], todo)
            return len(metas)

        # --- faza 1 (N3-LIMIT): GŁÓWNE zapytania i pobieranie wszystkich rodzajów.
        # Zakres wybrany przez księgową ma pierwszeństwo przed sprawdzeniem
        # „od punktu” — to nie może zjeść limitu zapytań głównemu zapytaniu kosztów.
        # Sonda: pierwsza strona metadanych; więcej niż jedna strona -> rodzaj
        # paczkami eksportu (faza1_paczkami), inaczej lista jak dotąd (wynik
        # sondy to pełna lista — bez ponownego zapytania o tę samą stronę).
        sonda_mozliwa = hasattr(client, "pobierz_paczkami") and _przyjmuje(
            client.query_metadata, "sonda"
        )
        try:
            for kind in kolejnosc:
                poza_zakresem = [0]
                sonda: dict | None = {} if sonda_mozliwa else None
                argumenty: dict = {"margin": QUERY_MARGIN}
                if sonda is not None:
                    argumenty["sonda"] = sonda
                lista = list(client.query_metadata(kind.value, date_from, date_to, **argumenty))

                def log_poza_zakresem() -> None:
                    if poza_zakresem[0]:
                        logger.info(
                            "%s: pominięto %d faktur z datą wystawienia spoza zakresu "
                            "(zapas zapytania do KSeF)", kind.label, poza_zakresem[0],
                        )

                r = _Rodzaj(kind)
                rodzaje.append(r)
                if sonda is not None and sonda.get("wiecej"):
                    ile = faza1_paczkami(r, poza_zakresem)
                    ponowne = [] if report.cancelled else ponowne_w3(r)
                    report.found += len(ponowne)
                    logger.info("%s: %d faktur w zakresie (paczki eksportu)", kind.label, ile)
                    log_poza_zakresem()
                    if ponowne:
                        pobierz(r, [], ponowne)
                else:
                    metas = filtruj_wyszukiwanie(kind, w_zakresie(lista, poza_zakresem))
                    r.glowne_nr = {m.ksef_number for m in metas}
                    metas = bez_recznej(kind, metas)
                    ponowne = ponowne_w3(r)
                    report.found += len(metas) + len(ponowne)
                    logger.info("%s: %d faktur w zakresie", kind.label, len(metas))
                    log_poza_zakresem()
                    pobierz(r, do_pobrania(metas), ponowne)
                if report.cancelled:
                    break
        except KsefCancelled:
            report.cancelled = True
        except KsefQuotaExceeded as exc:
            report.niepelny = True
            report.warnings.append(
                f"{exc} — uruchom eksport ponownie po tej godzinie (tryb „tylko nowe” "
                "dociągnie brakujące bez dublowania)."
            )
            logger.warning("%s", report.warnings[-1])

        # --- faza 2: sprawdzenia „od punktu” (spóźnione), z budżetem stron
        # (N3-LIMIT). Przerwane sprawdzenie nie czyni eksportu niepełnym —
        # zakres jest kompletny, a punkt zapisuje postęp (wznowienie).
        if sprawdzaj_spoznione and not report.cancelled:
            do_sprawdzenia = [r for r in rodzaje if not r.niepelny]
            try:
                for idx, r in enumerate(do_sprawdzenia):
                    if report.cancelled:
                        break
                    kind = r.kind
                    przed_strona = _budzet_stron(client, len(do_sprawdzenia) - idx, kind, report)
                    if przed_strona is False:
                        continue
                    r.spr = _sprawdz_spoznione(
                        client, ledger, profile.nip, kind, date_from, date_to, r.glowne_nr,
                        report, pomijane=do_recznej, przed_strona=przed_strona,
                    )
                    if r.spr is None:
                        continue
                    for meta in r.spr.w_zakresie + r.spr.spoznione:
                        if meta.form_system_code and meta.form_system_code != "FA (3)":
                            report.warnings.append(
                                f"{meta.ksef_number}: schemat {meta.form_system_code} (nie FA (3))"
                            )
                    w_zakresie = r.spr.w_zakresie
                    if do_recznej:
                        pominiete_reczne.extend(
                            (m, kind) for m in w_zakresie if m.ksef_number in do_recznej
                        )
                        w_zakresie = [m for m in w_zakresie if m.ksef_number not in do_recznej]
                    late = list(r.spr.spoznione)
                    z_bezpiecznika.update(r.spr.bezpiecznik)
                    wykryte_spoznione.extend((m, kind) for m in late)
                    r.spoznione_nr.update(m.ksef_number for m in late)
                    if late:
                        logger.info(
                            "%s: %d spóźnionych faktur z miesięcy już domkniętych "
                            "(wpłynęły do KSeF po domknięciu) — dołączam",
                            kind.label, len(late),
                        )
                    if r.spr.wstrzymane:
                        logger.info(
                            "%s: %d faktur spoza zakresu czeka na domknięcie swojego miesiąca "
                            "(punkt kontynuacji ich nie przeskakuje)", kind.label,
                            len(r.spr.wstrzymane),
                        )
                    report.found += len(w_zakresie) + len(late)
                    if only_new:
                        dopisane = [m for m in w_zakresie if not ledger.is_done(profile.nip, m.ksef_number)]
                        report.skipped_dedup += len(w_zakresie) - len(dopisane)
                    else:
                        dopisane = w_zakresie
                    pobierz(r, [], dopisane + late)
            except KsefCancelled:
                report.cancelled = True

        for r in rodzaje:
            if r.batch:
                # paczka zbiorcza: wiele ksefInvoice pod wspólnym korzeniem -> XSLT
                # emituje INFO{} raz i po jednej sekcji Dokument{} per faktura
                root = etree.Element("ksefInvoices")
                for _, element, _already in r.batch:
                    root.append(element)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                txt_path = (
                    out_dir
                    / f"paczka_{r.kind.file_label}{plik_suffix}_{date_from.isoformat()}_{date_to.isoformat()}_{stamp}.TXT"
                )
                data = transformer.to_txt_bytes(root, context=txt_path.name)
                write_txt_atomic(txt_path, data)
                for meta, _, already in r.batch:
                    ledger.mark_done(profile.nip, meta.ksef_number, str(txt_path))
                    zapisane.add(meta.ksef_number)
                    if already:
                        report.re_exported += 1
                report.exported += len(r.batch)
                logger.info(
                    "OK paczka %s: %d faktur -> %s", r.kind.label, len(r.batch), txt_path.name
                )
            if not report.cancelled and not r.niepelny:
                # rodzaj przetworzony w całości -> zakres (W2) i nowy punkt: zapis na
                # końcu przebiegu, po plikach. Niepobrane (błąd KSeF) oznaczają
                # niepełny rodzaj, więc punkt ich nie przeskakuje; spóźnione
                # z błędem mapowania przeskakuje — trafiają na trwałą listę
                # do ręcznej obsługi (patrz niżej)
                if pelny_tryb:
                    # N2: najwyżej do dnia przed momentem kompletności danych
                    kompletne_rodzaje.append((r.kind, min(date_to, _granica_zakresu(r.spr))))
                if r.spr is not None and (r.spr.postep is None or r.spr.hwm is not None):
                    # przerwane sprawdzenie bez ani jednej odebranej strony — nic
                    # nowego nie wiadomo, punkt zostaje
                    nowe_punkty[r.kind.value] = (
                        _nowy_punkt(r.spr, r.spr.wstrzymane), r.spr.hwm or r.spr.od,
                    )

        if numbers_mode and not report.cancelled:
            # który z szukanych wpisów w ogóle nie trafił (w żadnym z kinds) i która
            # KONKRETNA wartość (pełny numer albo pojedyncza końcówka) trafiła więcej
            # niż jedną fakturę (np. ten sam numer/końcówka u dwóch wystawców/serii) —
            # osobno per wartość, żeby trafiony zakres (1 faktura na końcówkę) nie
            # generował fałszywego ostrzeżenia o duplikacie
            for entry in wanted:
                if not entry.matches:
                    report.failed.append((
                        entry.original,
                        "nie znaleziono w KSeF w zakresie "
                        f"{date_from.isoformat()}–{date_to.isoformat()} dla wybranych rodzajów",
                    ))
                    continue
                for wartosc, trafienia in entry.matches.items():
                    if len(trafienia) > 1:
                        opis = entry.original if entry.exact_norm is not None else str(wartosc)
                        report.warnings.append(
                            f"numer {opis} pasuje do {len(trafienia)} faktur "
                            "(różni wystawcy) — sprawdź"
                        )
            report.numbers_found = sum(1 for entry in wanted if entry.matches)

        if contractors_mode and not report.cancelled:
            nietrafione = list(kontrahent_nierozwiazany) + [
                oryginal for nip, oryginal in contractor_ids.items() if not contractor_found.get(nip)
            ]
            for oryginal in nietrafione:
                report.failed.append((
                    f"kontrahent {oryginal}",
                    "brak faktur w KSeF w zakresie "
                    f"{date_from.isoformat()}–{date_to.isoformat()} dla wybranych rodzajów",
                ))
            report.contractors_found = sum(1 for v in contractor_found.values() if v)

        if hmffk_mode and hmffk_docs:
            # alias -> klucz kanoniczny: kanoniczny() rozwiązuje cały łańcuch aliasów
            # do punktu stałego (per klucz), potem scalamy karty o tym samym kluczu
            for k in hmffk_kh.lista():
                k.klucz = rejestr.kanoniczny(profile.nip, k.klucz)
            for _, d, _ in hmffk_docs:
                d.kontrahent_klucz = rejestr.kanoniczny(profile.nip, d.kontrahent_klucz)
            swiezy = KontrahenciRejestr()
            for k in hmffk_kh.lista():
                swiezy.add(k)
            hmffk_kh = swiezy

            # numeracja kart FK: max+1 (jak FK), w kolejności sekcji Kontrahent{}
            for karta in hmffk_kh.lista():
                nr = rejestr.pozycja(profile.nip, karta.klucz)
                nowy = nr is None
                konta_karty: list[str] = []
                if not nowy:
                    # karta już istnieje: nazwa z kartoteki (rejestru), nie z faktury —
                    # dopisek na fakturze (np. „BDO: …” w polu Nazwa) dawał w FK
                    # błąd dostawcy (nazwa dokumentu ≠ nazwa karty); kod liczony
                    # dalej z nazwy, więc też wychodzi z kartoteki
                    wpis = rejestr.wpis(profile.nip, karta.klucz)
                    nazwa_karty = wpis.nazwa.strip() if wpis else ""
                    # kod: uzgodniony kod karty z FK (z synchronizacji); bez niego
                    # jak przed tą zmianą — z nazwy z faktury, nie z kartoteki
                    karta.kod = wpis.skrot.strip() if wpis else ""
                    karta.nazwa_do_kodu = karta.nazwa
                    if nazwa_karty:
                        # każdy dokument osobno — kilka faktur od tego samego
                        # kontrahenta w pliku może nieść różne nazwy
                        z_faktur = [karta] + [
                            d.kontrahent for _, d, _ in hmffk_docs
                            if d.kontrahent_klucz == karta.klucz
                        ]
                        inne = sorted({
                            " ".join(k.nazwa.split()) for k in z_faktur
                            if k.nazwa.strip() != nazwa_karty
                        })
                        for _, d, _ in hmffk_docs:
                            if d.kontrahent_klucz != karta.klucz:
                                continue
                            if d.opis_auto:
                                d.opis = opis_dokumentu(
                                    d.symbol in ("FKS", "FKZ"), d.numer, nazwa_karty
                                )
                        for k in z_faktur:
                            k.nazwa = nazwa_karty
                        # ostrzeżenie tylko przy różnicy w treści (nie w wielkości
                        # liter/odstępach), jedno na kartę
                        inne = [n for n in inne if n.casefold() != " ".join(nazwa_karty.split()).casefold()]
                        if inne:
                            report.warnings.append(
                                f"Nazwa kontrahenta na fakturze ({'; '.join(repr(n) for n in inne)}) "
                                f"różni się od kartoteki — użyto nazwy z kartoteki nr {nr} "
                                f"'{nazwa_karty}'"
                            )
                if nowy:
                    for wpis, ratio in rejestr.podobni(profile.nip, karta.nazwa):
                        report.warnings.append(
                            f"Nowy kontrahent '{karta.nazwa}' podobny ({ratio:.0%}) do "
                            f"istniejącego nr {wpis.pozycja} '{wpis.nazwa}' — jeśli to ten "
                            "sam, scal w rejestrze kontrahentów (GUI) i popraw dekret w FK"
                        )
                    nr = rejestr.przydziel(
                        profile.nip, karta.klucz, nazwa=karta.nazwa, nip=karta.nip,
                        miejscowosc=karta.miejscowosc,
                    )
                for _, d, _ in hmffk_docs:
                    if d.kontrahent_klucz != karta.klucz:
                        continue
                    prefiks = (
                        konto_prefix_sprzedaz if d.rodzaj_dok == "sprzedaży" else konto_prefix_koszt
                    )
                    d.konto_rozrachunkowe = f"{prefiks}-{nr}"
                    if d.konto_rozrachunkowe not in konta_karty:
                        konta_karty.append(d.konto_rozrachunkowe)
                    if d.rodzaj_dok == "sprzedaży":
                        d.konto_netto = konto_sprzedaz_netto
                        d.konto_vat = konto_sprzedaz_vat
                    else:
                        d.konto_netto = konto_koszt_netto
                        d.konto_vat = konto_koszt_vat
                    brutto = sum(
                        (Decimal(r.netto) + Decimal(r.vat) for r in d.rejestry), Decimal("0")
                    )
                    if brutto == 0:
                        report.warnings.append(
                            f"dokument {d.numer}: suma brutto = 0 — linia zapisu z kontem "
                            "nie zostanie wyemitowana, zadekretuj ręcznie"
                        )
                if nowy:
                    report.nowi_kontrahenci.append(
                        (karta.nazwa, karta.nip, nr, ", ".join(konta_karty))
                    )
            if report.nowi_kontrahenci:
                # (10) numery kart już nadane (przydziel zapisał je w rejestrze
                # numerów) — ostrzeżenie czeka w rejestrze pobranych, aż trafi do
                # raportu UDANEGO eksportu; padnięcie zapisu pliku niżej go nie gubi
                # (przy następnym eksporcie kontrahent nie jest już „nowy”)
                ledger.add_pending_contractors(profile.nip, report.nowi_kontrahenci)

            # JEDEN plik pod hmffk.AMS: Kontrahent{} (wbudowani) + dokument{}
            text = build_hmffk_text(hmffk_kh.lista(), [d for _, d, _ in hmffk_docs])
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            data, bad = encode_cp1250(text)
            if bad:
                report.warnings.append(
                    "Plik hmffk: znaki spoza CP1250 zastąpione transliteracją: "
                    + " ".join(repr(c) for c in bad)
                )
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            txt_path = (
                out_dir
                / f"hmffk_{profile.subfolder}{plik_suffix}_{date_from.isoformat()}_{date_to.isoformat()}_{stamp}.TXT"
            )
            try:
                write_txt_atomic(txt_path, data)
            except OSError:
                if report.nowi_kontrahenci:
                    logger.warning(
                        "Zapis pliku nie powiódł się, a nowym kontrahentom nadano już numery "
                        "kart — ostrzeżenie pojawi się przy następnym udanym eksporcie"
                    )
                raise
            for meta, _, already in hmffk_docs:
                ledger.mark_done(profile.nip, meta.ksef_number, str(txt_path))
                zapisane.add(meta.ksef_number)
                if already:
                    report.re_exported += 1
            report.exported += len(hmffk_docs)
            report.kontrahenci = len(hmffk_kh)
            # nowi z tego przebiegu + zaległe z przebiegu, którego plik nie powstał
            biezacy = {nr for _, _, nr, _ in report.nowi_kontrahenci}
            report.nowi_kontrahenci += [
                z for z in ledger.take_pending_contractors(profile.nip) if z[2] not in biezacy
            ]
            if report.nowi_kontrahenci:
                report.warnings.append(
                    "Nowi kontrahenci w pliku — sprawdź numery kart w Symfonii po imporcie "
                    "i popraw w oknie Kontrahenci…: "
                    + "; ".join(
                        f"{nazwa} (NIP {nip or 'brak'}) — nr karty {nr}, konto {konta}"
                        for nazwa, nip, nr, konta in report.nowi_kontrahenci
                    )
                )
            logger.info(
                "OK plik hmffk: %d dokumentów, %d kontrahentów -> %s | import w FK: "
                "Import specjalny, szablon „Dane z programu Handel, format FK 3.0”",
                len(hmffk_docs), len(hmffk_kh), txt_path.name,
            )

        if not hmffk_mode and kontrahenci_file and zebrani:
            # plik danych: sekcje AMS czytane WŁASNYM szablonem SKsefKontrahenciImport.AMS
            text = build_kontrahenci_ams(list(zebrani.values()))
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            data, bad = encode_cp1250(text)
            if bad:
                report.warnings.append(
                    "Plik kontrahentów: znaki spoza CP1250 zastąpione transliteracją"
                )
            bez_nip = sum(1 for k in zebrani.values() if not k.nip)
            if bez_nip:
                report.warnings.append(
                    f"Kartoteka: {bez_nip} kontrahent(ów) bez NIP — klucz zastępczy K_<hash>"
                )
            stamp = datetime.now().strftime("%Y%m%d")
            karty_dir = out_dir / "kartoteka"
            karty_path = karty_dir / f"kontrahenci_{profile.subfolder}_{stamp}.TXT"
            write_txt_atomic(karty_path, data)
            # szablon importu — do JEDNORAZOWEGO skopiowania do folderu szablonów
            # Symfonii (obok SKsefInvoiceImport.AMS); okno wyboru listuje tylko stamtąd
            szablon_path = karty_dir / "szablon" / TEMPLATE_FILENAME
            write_txt_atomic(szablon_path, TEMPLATE_AMS.replace("\n", "\r\n").encode("cp1250"))
            report.kontrahenci = len(zebrani)
            logger.info(
                "OK kartoteka: %d kontrahentów -> %s | szablon %s — skopiuj go raz "
                "do folderu szablonów Symfonii („Import kontrahentow z KSeF”)",
                len(zebrani), karty_path.name, szablon_path,
            )

        # punkt kontynuacji i zakresy dopiero teraz: pliki zapisane, rejestr
        # uzupełniony (hmffk zapisuje plik na końcu — wcześniejszy zapis punktu
        # zgubiłby spóźnione przy błędzie zapisu pliku)
        zapisane_zakresy: set[str] = set()
        if not report.cancelled:
            for subject_type, (punkt, sprawdzono) in nowe_punkty.items():
                ledger.set_sync_point(profile.nip, subject_type, punkt, observed_at=sprawdzono)
            for k, do_dnia in kompletne_rodzaje:
                if do_dnia >= date_from:
                    ledger.record_range(profile.nip, k.value, date_from, do_dnia)
                    zapisane_zakresy.add(k.value)
            _zglos_niedomkniete(report, date_from, date_to, [d for _, d in kompletne_rodzaje])
        # (9) błąd mapowania faktury z zakresu w przebiegu, który zapisał zakres
        # rodzaju (księgowa dostała „zaksięguj ręcznie” w kompletnym przebiegu):
        # trwała lista do ręcznej obsługi, jak spóźnione — kolejne eksporty jej
        # nie zgłaszają ani nie dołączają (D1, faza 2), także po poprawce mappera
        na_liscie: set[str] = set()
        for r in rodzaje:
            if r.kind.value not in zapisane_zakresy:
                continue
            for nr, (meta, blad) in r.bledy_glowne.items():
                ledger.record_late(
                    profile.nip, r.kind.value, nr, status=STATUS_DO_RECZNEJ, error=blad,
                    invoice_number=meta.invoice_number,
                    issue_date=(meta.issue_date or "")[:10],
                    acquisition_date=meta.acquisition_date,
                    permanent_storage_date=meta.permanent_storage_date,
                    range_from=date_from, range_to=date_to,
                )
                na_liscie.add(nr)
                logger.info("%s: zapisana na liście do ręcznej obsługi", nr)
        # (R-b, P2) „zaksięguj ręcznie” pada TYLKO raz — gdy faktura trafia na listę
        # do ręcznej obsługi. Każda inna faktura z błędem mapowania wróci
        # w kompletnym eksporcie jej zakresu (domknięciu), więc teraz nie wolno jej
        # księgować ręcznie (dubel): przebieg niepełny, zakres niezapisany (dane
        # KSeF kompletne dopiero do wczoraj), wyszukiwanie
        dopiski: dict[str, str] = {nr: DOPISEK_NA_LISCIE for nr in na_liscie}
        juz_na_liscie = (
            {rec.ksef_number for rec in ledger.manual_list(profile.nip)}
            if not pelny_tryb and any(r.bledy_glowne for r in rodzaje) else set()
        )
        for r in rodzaje:
            if r.kind.value in zapisane_zakresy:
                continue
            for nr in r.bledy_glowne:
                if not pelny_tryb:
                    dopiski[nr] = DOPISEK_JUZ_NA_LISCIE if nr in juz_na_liscie else DOPISEK_WYSZUKIWANIE
                elif r.niepelny or report.cancelled:
                    dopiski[nr] = DOPISEK_NIEPELNY
                else:
                    dopiski[nr] = DOPISEK_BEZ_ZAKRESU
        if dopiski:
            report.failed = [
                (nr, komunikat + dopiski[nr]) if nr in dopiski else (nr, komunikat)
                for nr, komunikat in report.failed
            ]
        widziane_sp: set[str] = set()
        for meta, rodzaj in wykryte_spoznione:
            nr = meta.ksef_number
            if nr in widziane_sp:
                continue
            widziane_sp.add(nr)
            dane = dict(
                invoice_number=meta.invoice_number, issue_date=(meta.issue_date or "")[:10],
                acquisition_date=meta.acquisition_date,
                permanent_storage_date=meta.permanent_storage_date,
                range_from=date_from, range_to=date_to,
            )
            if nr in zapisane:  # faktycznie w pliku tego przebiegu
                ledger.record_late(profile.nip, rodzaj.value, nr, status=STATUS_W_PLIKU, **dane)
                report.spoznione.append(SpoznionaFaktura(
                    ksef_number=nr,
                    invoice_number=meta.invoice_number,
                    issue_date=(meta.issue_date or "")[:10],
                    rodzaj=rodzaj.label,
                    bezpiecznik=nr in z_bezpiecznika,
                ))
            elif nr in bledy_mapowania:
                # (9) trwały ślad: punkt ją przeskakuje, więc lista do ręcznej obsługi
                ledger.record_late(
                    profile.nip, rodzaj.value, nr, status=STATUS_DO_RECZNEJ,
                    error=bledy_mapowania[nr], **dane,
                )
        zwykle = [s for s in report.spoznione if not s.bezpiecznik]
        bezp = [s for s in report.spoznione if s.bezpiecznik]
        naglowki = ([_ostrzezenie_spoznione(zwykle)] if zwykle else []) + (
            [_ostrzezenie_bezpiecznik(bezp)] if bezp else []
        ) + ([_ostrzezenie_do_recznej(pominiete_reczne)] if pominiete_reczne else [])
        report.warnings[0:0] = naglowki
        for w in naglowki:
            logger.warning("%s", w)
        return report
    finally:
        if wlasny_rejestr:
            rejestr.close()
