"""Symulator serwera KSeF 2.0 na poziomie HTTP (httpx.MockTransport) — do testów
paczek eksportu i dużych wolumenów. Prawdziwy KsefClient rozmawia z nim jak
z KSeF: logowanie, POST /invoices/query/metadata, POST /invoices/exports
(klucz AES szyfrowany RSA-OAEP), GET /invoices/exports/{ref}, części paczki
pod presigned URL, GET /invoices/ksef/{nr}.

Model wg dokumentacji MF (ksef-docs, open-api.json, 09.2026):
- paczka: faktury rosnąco po dacie z dateRange, ucięcie na `limit_paczki`
  (10 000) -> isTruncated + lastIssueDate (dateType=Issue); `_metadata.json`
  zawsze w paczce (klucz `invoices`, model InvoiceMetadata jak query/metadata);
- części: JEDNO archiwum ZIP cięte na kawałki <= `rozmiar_czesci` (w losowym
  bajcie), każdy kawałek szyfrowany osobno AES-256-CBC/PKCS7 tym samym
  kluczem i IV; URL części generowany przy KAŻDYM GET statusu, ważny 1 h;
- limity (per NIP, okno przesuwne): metadane 8/16/20, zlecenie eksportu
  8/16/20 (osobna pula), status eksportu 10/60/600, pobranie faktury 8/16/64;
  przekroczenie -> 429 z Retry-After;
- zakres dat zapytania i eksportu maks. 100 dni (inaczej 400);
- data wystawienia (typ date) porównywana z granicami date-time jako północ
  czasu polskiego (`interpretacja="pl"`) albo UTC (`"utc"`) — dokumentacja
  tego nie precyzuje, więc testy sprawdzają oba warianty;
- synchronizacja „od punktu”: dateType=PermanentStorage, HWM = teraz − opóźnienie.

Zegar jest wirtualny (`Zegar`): `zainstaluj_zegar` podmienia time/_sleep
w app.ksef_client i app.quota oraz „teraz” eksportera.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import io
import json
import math
import random
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.x509.oid import NameOID

from app.ksef_client import KsefClient, _parse_ksef_datetime, warsaw_midnight_iso

BASE = "https://ksef.sim/v2"
NIP = "5252445719"
XML_FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "fa3_basic_23.xml").read_bytes()

LIMITY = {
    "metadata": [(1, 8), (60, 16), (3600, 20)],
    "export": [(1, 8), (60, 16), (3600, 20)],
    "status": [(1, 10), (60, 60), (3600, 600)],
    "download": [(1, 8), (60, 16), (3600, 64)],
}


# --------------------------------------------------------------------------- #
# zegar wirtualny


class Zegar:
    def __init__(self, start: datetime):
        self.teraz = start

    def time(self) -> float:
        return self.teraz.timestamp()

    def monotonic(self) -> float:
        return self.teraz.timestamp()

    def sleep(self, sekundy: float) -> None:
        self.teraz += timedelta(seconds=sekundy)


def zainstaluj_zegar(zegar: Zegar, monkeypatch=None):
    """Podmienia źródła czasu klienta, licznika limitów i eksportera na zegar
    wirtualny. Z `monkeypatch` (pytest) — cofane automatycznie; bez — zwraca
    funkcję cofającą."""
    import app.exporter as ex
    import app.ksef_client as kc
    import app.quota as q

    fake_time = SimpleNamespace(time=zegar.time, monotonic=zegar.monotonic, sleep=zegar.sleep)

    def fake_sleep(sekundy, cancel=None):
        if cancel is not None and cancel.is_set():
            raise kc.KsefCancelled("Przerwano na żądanie użytkownika")
        zegar.sleep(sekundy)

    zmiany = [
        (kc, "time", fake_time), (q, "time", fake_time), (kc, "_sleep", fake_sleep),
        (ex, "_teraz_utc", lambda: zegar.teraz),
    ]
    if monkeypatch is not None:
        for mod, nazwa, wartosc in zmiany:
            monkeypatch.setattr(mod, nazwa, wartosc)
        return lambda: None
    stare = [(mod, nazwa, getattr(mod, nazwa)) for mod, nazwa, _ in zmiany]
    for mod, nazwa, wartosc in zmiany:
        setattr(mod, nazwa, wartosc)

    def cofnij():
        for mod, nazwa, wartosc in stare:
            setattr(mod, nazwa, wartosc)

    return cofnij


# --------------------------------------------------------------------------- #
# dane


@dataclass
class Faktura:
    ksef: str
    subject: str        # Subject1 = sprzedaż (NIP jest sprzedawcą), Subject2 = koszt
    issue: date         # data wystawienia
    ps: datetime        # data trwałego zapisu (UTC) — od tej chwili faktura jest w KSeF
    kontrahent: str = "1180012345"
    xml: bytes | None = None
    form: str = "FA (3)"


def pl(d: date, h: int, m: int = 0) -> datetime:
    """Czas polski -> UTC (reguła UE, jak w app.ksef_client)."""
    from app.ksef_client import _warsaw_offset

    naiwny = datetime(d.year, d.month, d.day, h, m, tzinfo=timezone.utc)
    # przybliżenie offsetu z chwili UTC tego samego zegara (poza nocą zmiany czasu dokładne)
    return naiwny - _warsaw_offset(naiwny)


def surowe_metadane(f: Faktura, nip: str = NIP) -> dict:
    """InvoiceMetadata (open-api.json) — ten sam kształt w query/metadata
    i w `_metadata.json` paczki."""
    sprzedawca, nabywca = (nip, f.kontrahent) if f.subject == "Subject1" else (f.kontrahent, nip)
    return {
        "ksefNumber": f.ksef,
        "invoiceNumber": f"FV/{f.ksef[-9:]}",
        "issueDate": f.issue.isoformat(),
        "invoicingDate": (f.ps - timedelta(minutes=2)).isoformat(),
        "acquisitionDate": (f.ps - timedelta(minutes=1)).isoformat(),
        "permanentStorageDate": f.ps.isoformat(),
        "seller": {"nip": sprzedawca, "name": "Sprzedawca"},
        "buyer": {"identifier": {"type": "Nip", "value": nabywca}, "name": "Nabywca"},
        "netAmount": 100.0, "grossAmount": 123.0, "vatAmount": 23.0,
        "currency": "PLN", "invoicingMode": "Online", "invoiceType": "Vat",
        "formCode": {"systemCode": f.form, "schemaVersion": "1-0E", "value": "FA"},
        "isSelfInvoicing": False, "hasAttachment": False,
        "invoiceHash": base64.b64encode(hashlib.sha256(f.ksef.encode()).digest()).decode(),
    }


# --------------------------------------------------------------------------- #
# budowanie paczki (także do testów jednostkowych klienta)


def zip_paczki(xml_po_nr: dict[str, bytes], metadane: list[dict] | None,
               *, metadata_json: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for nr, xml in xml_po_nr.items():
            archive.writestr(f"{nr}.xml", xml)
        if metadata_json is not None:
            archive.writestr("_metadata.json", metadata_json)
        elif metadane is not None:
            archive.writestr("_metadata.json", json.dumps({"invoices": metadane}))
    return buf.getvalue()


def szyfruj(dane: bytes, klucz: bytes, iv: bytes) -> bytes:
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(dane) + padder.finalize()
    enc = Cipher(algorithms.AES(klucz), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


def potnij(dane: bytes, maks: int, rnd: random.Random | None = None) -> list[bytes]:
    """Tnie archiwum na kawałki <= `maks` w przypadkowych bajtach (granice nie
    pokrywają się z granicami plików w ZIP)."""
    rnd = rnd or random.Random(0)
    kawalki, poz = [], 0
    while poz < len(dane):
        dl = min(maks, len(dane) - poz)
        if len(dane) - poz > maks:
            dl = rnd.randint(max(1, maks // 2), maks)
        kawalki.append(dane[poz:poz + dl])
        poz += dl
    return kawalki or [b""]


def opis_czesci(blob_zaszyfrowany: bytes, jawny: bytes, nr: int, url: str,
                wygasa: datetime | None = None) -> dict:
    return {
        "ordinalNumber": nr,
        "partName": f"part{nr}.zip.aes",
        "method": "GET",
        "url": url,
        "partSize": len(jawny),
        "partHash": base64.b64encode(hashlib.sha256(jawny).digest()).decode(),
        "encryptedPartSize": len(blob_zaszyfrowany),
        "encryptedPartHash": base64.b64encode(hashlib.sha256(blob_zaszyfrowany).digest()).decode(),
        "expirationDate": (wygasa or datetime(2099, 1, 1, tzinfo=timezone.utc)).isoformat(),
    }


def _fake_jwt() -> str:
    naglowek = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    tresc = base64.urlsafe_b64encode(json.dumps({"exp": 2**33}).encode()).rstrip(b"=").decode()
    return f"{naglowek}.{tresc}.sig"


# --------------------------------------------------------------------------- #
# serwer


@dataclass
class _Eksport:
    ref: str
    nr: int                      # kolejne zlecenie (1, 2, ...)
    subject: str
    od: datetime
    do: datetime
    gotowy_od: datetime
    faktury: list                # włączone do paczki (kolejność)
    obciety: bool
    czesci_jawne: list[bytes]
    czesci: list[bytes]          # zaszyfrowane
    wersje: dict = field(default_factory=dict)   # nr części -> {wersja: wygasa}
    nigdy_gotowy: bool = False


class SerwerKsef:
    def __init__(
        self,
        faktury: list[Faktura],
        zegar: Zegar,
        *,
        nip: str = NIP,
        interpretacja: str = "pl",
        limit_paczki: int = 10_000,
        rozmiar_czesci: int = 50 * 2**20,
        czas_przygotowania: timedelta = timedelta(seconds=40),
        hwm_opoznienie: timedelta = timedelta(minutes=3),
        limit_query: int = 10_000,
        seed: int = 0,
    ):
        self.faktury = faktury
        self.zegar = zegar
        self.nip = nip
        self.interpretacja = interpretacja
        self.limit_paczki = limit_paczki
        self.rozmiar_czesci = rozmiar_czesci
        self.czas_przygotowania = czas_przygotowania
        self.hwm_opoznienie = hwm_opoznienie
        self.limit_query = limit_query
        self.rnd = random.Random(seed)
        self._rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        nazwa = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MF Sim")])
        cert = (
            x509.CertificateBuilder().subject_name(nazwa).issuer_name(nazwa)
            .public_key(self._rsa.public_key()).serial_number(1)
            .not_valid_before(datetime(2020, 1, 1)).not_valid_after(datetime(2099, 1, 1))
            .sign(self._rsa, hashes.SHA256())
        )
        self._cert_b64 = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
        self.eksporty: dict[str, _Eksport] = {}
        self.stemple: dict[str, list[float]] = collections.defaultdict(list)
        self.wywolania: collections.Counter = collections.Counter()
        self.pobrania_pojedyncze: list[str] = []
        self.zapytania_metadanych: list[dict] = []   # (body, params) każdego zapytania
        self.zlecenia: list[dict] = []               # filtry każdego zlecenia eksportu
        # wstrzykiwane awarie:
        self.wygas_raz: set[tuple[int, int]] = set()   # (nr zlecenia, nr części) -> 403 przy 1. pobraniu
        self.nigdy_gotowe: set[int] = set()            # nr zlecenia, które nigdy nie będzie gotowe
        self.limit_eksportu_od: int | None = None      # od tego nr zlecenia 429 z długim Retry-After
        self.po_zleceniu = None                        # hook(nr) po przyjęciu zlecenia
        self.psuj_metadane = None                      # hook(list[dict]) -> list[dict] dla _metadata.json
        self.last_issue_date = None                    # hook(date | None) -> str | None (lastIssueDate w statusie)
        self.ukryj_uciecie = False                     # paczka ucięta, ale bez isTruncated/lastIssueDate
        self.usun_xml: set[str] = set()                # nr KSeF: wpis w _metadata.json jest, pliku XML brak
        self.czas_przygotowania_nr: dict[int, timedelta] = {}   # nr zlecenia -> czas przygotowania
        self.wygasle: set[int] = set()                 # nr zlecenia: status 210 (eksport wygasł)
        self.zerwij_status: set[int] = set()           # nr zlecenia: zerwane połączenie przy statusie
        self.naglowek_date = True                      # nagłówek Date (zegar KSeF) w odpowiedziach

    # -- pomocnicze ------------------------------------------------------------

    def _ts_wystawienia(self, d: date) -> datetime:
        if self.interpretacja == "pl":
            return _parse_ksef_datetime(warsaw_midnight_iso(d)).astimezone(timezone.utc)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)

    def _widoczne(self, subject: str) -> list[Faktura]:
        teraz = self.zegar.teraz
        return [f for f in self.faktury if f.subject == subject and f.ps <= teraz]

    def _hwm(self) -> datetime:
        return self.zegar.teraz - self.hwm_opoznienie

    def _limit(self, grupa: str) -> httpx.Response | None:
        teraz = self.zegar.time()
        st = [t for t in self.stemple[grupa] if t > teraz - 3600]
        self.stemple[grupa] = st
        czekaj = 0.0
        for okno, n in LIMITY[grupa]:
            w_oknie = [t for t in st if t > teraz - okno]
            if len(w_oknie) >= n:
                czekaj = max(czekaj, w_oknie[0] + okno - teraz)
        if czekaj > 0:
            self.wywolania[f"429:{grupa}"] += 1
            return httpx.Response(429, headers={"Retry-After": str(math.ceil(czekaj))},
                                  json={"status": 429})
        st.append(teraz)
        return None

    @staticmethod
    def _400(kod: int, opis: str) -> httpx.Response:
        return httpx.Response(400, json={"exception": {"exceptionDetailList": [
            {"exceptionCode": kod, "exceptionDescription": opis}]}})

    def _sprawdz_zakres(self, od: datetime, do: datetime) -> httpx.Response | None:
        if do - od > timedelta(days=100):
            return self._400(21405, f"Zakres dat przekracza 100 dni: {od} – {do}")
        if do < od:
            return self._400(21405, "Zakres dat odwrócony")
        return None

    # -- transport -------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.obsluz)

    def klient(self, quota=None, cancel=None) -> "KlientSym":
        k = KlientSym(BASE, self.nip, "token-sim", quota=quota, cancel=cancel)
        k._http.close()
        k._http = httpx.Client(base_url=BASE, transport=self.transport(), timeout=60)
        k.zegar = self.zegar
        return k

    def obsluz(self, request: httpx.Request) -> httpx.Response:
        odp = self._obsluz(request)
        if self.naglowek_date:
            from email.utils import format_datetime

            odp.headers["Date"] = format_datetime(self.zegar.teraz, usegmt=True)
        return odp

    def _obsluz(self, request: httpx.Request) -> httpx.Response:
        host, sciezka, metoda = request.url.host, request.url.path, request.method
        if host == "blob.sim":
            return self._blob(request)
        sciezka = sciezka.removeprefix("/v2")
        self.wywolania[f"{metoda} {re.sub(r'/[A-Z]+-[^/]+$', '/{ref}', sciezka)}"] += 1
        if sciezka == "/security/public-key-certificates":
            return httpx.Response(200, json=[{
                "certificate": self._cert_b64, "publicKeyId": "sim-1",
                "usage": ["KsefTokenEncryption", "SymmetricKeyEncryption"],
            }])
        if sciezka == "/auth/challenge":
            return httpx.Response(200, json={"challenge": "ch", "timestampMs": int(self.zegar.time() * 1000)})
        if sciezka == "/auth/ksef-token":
            return httpx.Response(202, json={"referenceNumber": "AUTH-1",
                                             "authenticationToken": {"token": "auth-jwt"}})
        if sciezka == "/auth/AUTH-1":
            return httpx.Response(200, json={"status": {"code": 200}})
        if sciezka == "/auth/token/redeem":
            return httpx.Response(200, json={"accessToken": {"token": _fake_jwt()},
                                             "refreshToken": {"token": "refresh"}})
        if sciezka == "/invoices/query/metadata" and metoda == "POST":
            return self._query(request)
        if sciezka == "/invoices/exports" and metoda == "POST":
            return self._zlec(request)
        if sciezka.startswith("/invoices/exports/") and metoda == "GET":
            return self._status(sciezka.rsplit("/", 1)[1])
        if sciezka.startswith("/invoices/ksef/") and metoda == "GET":
            return self._pobierz(sciezka.rsplit("/", 1)[1])
        return httpx.Response(404, text=f"nieznany endpoint {metoda} {sciezka}")

    # -- endpointy -------------------------------------------------------------

    def _query(self, request: httpx.Request) -> httpx.Response:
        if (r := self._limit("metadata")) is not None:
            return r
        body = json.loads(request.content)
        params = dict(request.url.params)
        self.zapytania_metadanych.append({"body": body, "params": params})
        offset, rozmiar = int(params.get("pageOffset", 0)), int(params.get("pageSize", 10))
        dr = body["dateRange"]
        od = _parse_ksef_datetime(dr["from"])
        do = _parse_ksef_datetime(dr["to"]) if dr.get("to") else self.zegar.teraz
        widoczne = self._widoczne(body["subjectType"])
        hwm = None
        if dr["dateType"] == "PermanentStorage":
            hwm = self._hwm()
            if dr.get("restrictToPermanentStorageHwmDate"):
                if od > hwm:
                    return self._400(21183, "dateRange.from > PermanentStorageHwmDate")
                do = min(do, hwm)
            if (r := self._sprawdz_zakres(od, do)) is not None:
                return r
            wyn = sorted((f for f in widoczne if od <= f.ps <= do), key=lambda f: (f.ps, f.ksef))
        else:
            if (r := self._sprawdz_zakres(od, do)) is not None:
                return r
            wyn = sorted((f for f in widoczne if od <= self._ts_wystawienia(f.issue) <= do),
                         key=lambda f: (f.issue, f.ksef))
        dostepne = wyn[: self.limit_query]
        strona = dostepne[offset * rozmiar:(offset + 1) * rozmiar]
        has_more = (offset + 1) * rozmiar < len(wyn)
        obcieta = has_more and (offset + 1) * rozmiar >= self.limit_query
        odp = {"hasMore": has_more, "isTruncated": obcieta,
               "invoices": [surowe_metadane(f, self.nip) for f in strona]}
        if hwm is not None:
            odp["permanentStorageHwmDate"] = min(do, hwm).isoformat()
        return httpx.Response(200, json=odp)

    def _zlec(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        nr = len(self.zlecenia) + 1
        if self.limit_eksportu_od is not None and nr >= self.limit_eksportu_od:
            self.wywolania["429:export"] += 1
            return httpx.Response(429, headers={"Retry-After": "1800"}, json={"status": 429})
        if (r := self._limit("export")) is not None:
            return r
        self.zlecenia.append(body["filters"])
        enc = body["encryption"]
        klucz = self._rsa.decrypt(
            base64.b64decode(enc["encryptedSymmetricKey"]),
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        iv = base64.b64decode(enc["initializationVector"])
        filtry = body["filters"]
        dr = filtry["dateRange"]
        assert dr["dateType"] == "Issue", dr
        od, do = _parse_ksef_datetime(dr["from"]), _parse_ksef_datetime(dr["to"])
        if (r := self._sprawdz_zakres(od, do)) is not None:
            return r
        kandydaci = sorted(
            (f for f in self._widoczne(filtry["subjectType"])
             if od <= self._ts_wystawienia(f.issue) <= do),
            key=lambda f: (f.issue, f.ksef),
        )
        wlaczone = kandydaci[: self.limit_paczki]
        obciety = len(kandydaci) > self.limit_paczki
        metadane = [surowe_metadane(f, self.nip) for f in wlaczone]
        if self.psuj_metadane is not None:
            metadane = self.psuj_metadane(metadane)
        archiwum = zip_paczki({f.ksef: (f.xml or XML_FIXTURE) for f in wlaczone
                               if f.ksef not in self.usun_xml}, metadane)
        jawne = potnij(archiwum, self.rozmiar_czesci, self.rnd)
        ref = f"EXP-{nr:04d}"
        self.eksporty[ref] = _Eksport(
            ref=ref, nr=nr, subject=filtry["subjectType"], od=od, do=do,
            gotowy_od=self.zegar.teraz + self.czas_przygotowania_nr.get(nr, self.czas_przygotowania),
            faktury=wlaczone,
            obciety=obciety, czesci_jawne=jawne,
            czesci=[szyfruj(c, klucz, iv) for c in jawne],
            nigdy_gotowy=nr in self.nigdy_gotowe,
        )
        if self.po_zleceniu is not None:
            self.po_zleceniu(nr)
        return httpx.Response(201, json={"referenceNumber": ref})

    def _status(self, ref: str) -> httpx.Response:
        if (r := self._limit("status")) is not None:
            return r
        e = self.eksporty.get(ref)
        if e is None:
            return httpx.Response(404, json={"status": 404})
        if e.nr in self.zerwij_status:
            raise httpx.ConnectError("zerwane połączenie (symulacja)")
        if e.nr in self.wygasle:
            return httpx.Response(200, json={"status": {"code": 210, "description": "Eksport wygasł"}})
        if e.nigdy_gotowy or self.zegar.teraz < e.gotowy_od:
            return httpx.Response(200, json={"status": {"code": 100, "description": "Eksport w toku"}})
        wygasa = self.zegar.teraz + timedelta(hours=1)
        czesci = []
        for i, (blob, jawny) in enumerate(zip(e.czesci, e.czesci_jawne), start=1):
            wersje = e.wersje.setdefault(i, {})
            wersja = len(wersje) + 1
            wersje[wersja] = wygasa
            czesci.append(opis_czesci(blob, jawny, i, f"https://blob.sim/{ref}/{i}?v={wersja}", wygasa))
        pakiet = {
            "invoiceCount": sum(1 for f in e.faktury if f.ksef not in self.usun_xml),
            "size": sum(len(c) for c in e.czesci_jawne),
            "isTruncated": e.obciety and not self.ukryj_uciecie,
            "parts": czesci,
            "permanentStorageHwmDate": None,
        }
        if e.obciety and not self.ukryj_uciecie:
            pakiet["lastIssueDate"] = e.faktury[-1].issue.isoformat()
            if self.last_issue_date is not None:
                pakiet["lastIssueDate"] = self.last_issue_date(e.faktury[-1].issue)
                if pakiet["lastIssueDate"] is None:
                    del pakiet["lastIssueDate"]
        return httpx.Response(200, json={
            "status": {"code": 200, "description": "Eksport zakończony"},
            "completedDate": e.gotowy_od.isoformat(), "package": pakiet,
        })

    def _blob(self, request: httpx.Request) -> httpx.Response:
        self.wywolania["GET blob"] += 1
        _, ref, nr = request.url.path.split("/")
        nr = int(nr)
        wersja = int(parse_qs(request.url.query.decode()).get("v", ["0"])[0])
        e = self.eksporty[ref]
        wygasa = e.wersje.get(nr, {}).get(wersja)
        if (e.nr, nr) in self.wygas_raz:
            self.wygas_raz.discard((e.nr, nr))
            return httpx.Response(403, text="AuthenticationFailed: Signed expiry time has passed")
        if wygasa is None or self.zegar.teraz > wygasa:
            return httpx.Response(403, text="AuthenticationFailed")
        return httpx.Response(200, content=e.czesci[nr - 1])

    def _pobierz(self, nr: str) -> httpx.Response:
        if (r := self._limit("download")) is not None:
            return r
        for f in self.faktury:
            if f.ksef == nr and f.ps <= self.zegar.teraz:
                self.pobrania_pojedyncze.append(nr)
                return httpx.Response(200, content=f.xml or XML_FIXTURE)
        return httpx.Response(404, text="brak faktury")


class KlientSym(KsefClient):
    """Prawdziwy klient; jedyna różnica: „teraz” synchronizacji przyrostowej
    z zegara wirtualnego (klient bierze datetime.now, którego nie podmieniamy)."""

    zegar: Zegar

    def query_metadata_since(self, subject_type, since, **kw):
        kw.setdefault("now", self.zegar.teraz)
        return super().query_metadata_since(subject_type, since, **kw)


def generuj_miesiac(rok: int, mies: int, ile: int, *, subject: str = "Subject1", seed: int = 1,
                    prefiks: str = "S", opoznienie_wplywu=None) -> list[Faktura]:
    """`ile` faktur wystawionych w danym miesiącu (rozłożonych po dniach),
    wpływ do KSeF w dniu wystawienia (8–20 czasu polskiego) albo z opóźnieniem."""
    import calendar

    rnd = random.Random(seed)
    dni = calendar.monthrange(rok, mies)[1]
    out = []
    for n in range(ile):
        d = date(rok, mies, 1 + (n * dni) // ile)
        ps = pl(d, rnd.randint(8, 19), rnd.randint(0, 59)) + timedelta(seconds=rnd.randint(0, 59))
        if opoznienie_wplywu is not None:
            ps += opoznienie_wplywu(rnd)
        out.append(Faktura(f"{NIP}-{d:%Y%m%d}-{prefiks}{n:06d}", subject, d, ps))
    return out
