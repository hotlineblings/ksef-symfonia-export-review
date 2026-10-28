"""Generator TESTOWEGO pliku symulacji do RĘCZNEGO importu w Symfonii FK.

Cel: zbudować realistyczny plik hmffk (.TXT, format "Dane z programu Handel,
format FK 3.0") z 4 syntetycznymi fakturami FA(3) -- 1 od kontrahenta już
istniejącego w kartotece (MERIDOL, poz. 5) + 3 od nowych kontrahentów (2 firmy
+ 1 osoba fizyczna bez NIP) -- i sprawdzić, że:

  * kartoteka jest wczytywana z SUROWEGO oryginału (bez żadnej normalizacji
    danych wejściowych) -- test regresyjny naprawy _wczytaj_xlsx/_digits w
    app/kontrahenci_nr.py (NIP zapisany w Excelu jako LICZBA, np. 9100010014.0,
    musi dać klucz "9100010014", a nie zepsuty 11-cyfrowy "91000100140"),
  * karta MERIDOL NIE dubluje się (dostaje to samo konto rozrachunkowe co
    istniejąca pozycja z kartoteki),
  * trzy nowe karty dostają pierwsze wolne numery (28, 29, 30) w kolejności
    sekcji Kontrahent{} w pliku,
  * konta rozrachunkowe "<prefiks>-<pozycja>" trafiają na właściwą stronę
    zapisu (WN dla sprzedaży, MA dla kosztu), zawsze na kwocie brutto --
    prefiks zależy od rodzaju dokumentu (201-2 sprzedaż / 202-2 koszt);
    dokumenty sprzedażowe dostają dodatkowo pełny dekret: konto na linii
    MA netto (703) i MA VAT (222-1); dokumenty kosztowe dostają dekret
    analogicznie -- konto na linii WN VAT (221-1, VAT naliczony) -- linia
    WN netto zostaje bez konta (dekretuje księgowa) -- kolejność zapisów
    kosztu: WN netto -> MA brutto (rozrachunek) -> WN VAT,
  * każdy zapis rozrachunkowy (linia brutto z kontem) niesie NumerDok/
    IdDlaRozliczen i BEZPOŚREDNIO po nim jest sekcja transakcja{} z terminem
    płatności (ISO, z Fa/Platnosc/TerminPlatnosci/Termin) -- przepis
    reference/rozrachunki-hmffk-E2E.md,
  * gdy faktura NIE ma sekcji Platnosc/TerminPlatnosci w FA(3) (na żywo
    dotyczyło to 505/547 faktur), termin płatności i tak powstaje --
    map_fa3_to_hmffk liczy go jako data wystawienia (P_1) + termin_fallback_dni
    (domyślnie 14) -- test TEST-SYM-0002 celowo nie ma tej sekcji,
  * plik jest CP1250 + CRLF, tak jak oczekuje tego szablon hmffk.AMS.

WAŻNE: skrypt tworzy WYŁĄCZNIE tymczasowe bazy w katalogu symulacja/
(rejestr_symulacja.db, ledger_symulacja.db) i NIGDY nie dotyka
%APPDATA%\\KsefSymfoniaExport (tam są bazy produkcyjne). Nie importuje
app_data_dir() i wszędzie przekazuje jawne db_path.

Uruchomienie:
    .venv\\Scripts\\python.exe symulacja\\generuj_symulacje.py

Kod wyjścia 0 = wszystkie weryfikacje przeszły; != 0 = niezgodność (patrz
podsumowanie na stdout) -- kod 3 = regres normalizacji NIP-ów z Excela
wykryty zaraz po seedzie (patrz _wczytaj_xlsx/_digits w app/kontrahenci_nr.py).
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SIM_DIR = Path(__file__).resolve().parent               # ...\ksef-symfonia-export\symulacja
ROOT = SIM_DIR.parent                                     # ...\ksef-symfonia-export
sys.path.insert(0, str(ROOT))

from app.config import Profile                              # noqa: E402
from app.exporter import Kind, run_export                    # noqa: E402
from app.kontrahenci_nr import RejestrNumerow                 # noqa: E402
from app.ksef_client import InvoiceMetadata                   # noqa: E402
from app.ledger import Ledger                                 # noqa: E402

# --------------------------------------------------------------------------- #
# konfiguracja symulacji -- WYŁĄCZNIE pliki w symulacja\, nigdy %APPDATA%

# SUROWY oryginał -- BEZ żadnej normalizacji/kopii pośredniej. Kolumna NIP w
# tym pliku jest zapisana jako LICZBA Excela (np. 9100010014.0 dla MERIDOL,
# poz. 5) -- dokładnie ten przypadek, dla którego naprawiono
# app/kontrahenci_nr.py::_wartosc_komorki/_digits (patrz gate w main()).
KARTOTEKA_XLSX = Path(r"C:\Users\Anna\Downloads\AGTLABO - kontrahenci.xlsx")

PROFIL_NIP = "5555555555"          # placeholder "własnej firmy" -- NIE trafia do pliku wynikowego
PROFIL_NAZWA = "AGTLABO SYMULACJA"
PROFIL_SUBFOLDER = "agtlabo-symulacja"

# prefiksy/konta wg aktualnej sygnatury app/exporter.py::run_export
KONTO_PREFIX_SPRZEDAZ = "201-2"
KONTO_PREFIX_KOSZT = "202-2"
KONTO_SPRZEDAZ_NETTO = "703"
KONTO_SPRZEDAZ_VAT = "222-1"
KONTO_KOSZT_VAT = "221-1"          # WN VAT naliczony (dekret kosztowy)
KONTO_KOSZT_NETTO = ""             # WN netto -- celowo puste, dekretuje księgowa
TERMIN_FALLBACK_DNI = 14           # brak TerminPlatnosci w FA(3) -> P_1 + tyle dni

REJESTR_DB = SIM_DIR / "rejestr_symulacja.db"
LEDGER_DB = SIM_DIR / "ledger_symulacja.db"
WYNIK_DIR = SIM_DIR / "wynik"

# relikt poprzedniej wersji skryptu (workaround normalizujący NIP-y) -- już
# nie generowany, ale sprzątany na starcie, gdyby został z wcześniejszego
# przebiegu (patrz reset_symulacja)
_STARY_SEED_NORMALIZED = SIM_DIR / "kartoteka_seed_normalized.xlsx"

# strona "nasza" na każdej z 4 faktur -- NIP placeholder, adres bez znaczenia
# (nigdy nie trafia do pliku: hmffk.py ekstrahuje tylko stronę PRZECIWNĄ)
WLASNA = dict(nip=PROFIL_NIP, nazwa="FIRMA SYMULACYJNA SP. Z O.O.",
              adres_l1="Siedziba 1", adres_l2="00-001 Warszawa")

DATA_FAKTURY = "2026-07-25"
TERMIN_PLATNOSCI = "2026-08-08"   # Fa/Platnosc/TerminPlatnosci/Termin -- wszystkie 4 faktury


def _assert_bezpieczne_sciezki() -> None:
    """Twardy strażnik: nigdy nie wolno przypadkiem trafić do %APPDATA%."""
    import os

    appdata = Path(os.environ.get("APPDATA") or "___brak___").resolve()
    for p in (REJESTR_DB, LEDGER_DB, WYNIK_DIR):
        rp = p.resolve()
        if appdata != Path("___brak___") and appdata in rp.parents:
            raise RuntimeError(f"BEZPIECZEŃSTWO: {rp} leży pod %APPDATA% -- przerywam!")
        if SIM_DIR.resolve() not in rp.parents and rp != SIM_DIR.resolve():
            raise RuntimeError(f"BEZPIECZEŃSTWO: {rp} poza katalogiem symulacja\\ -- przerywam!")


# --------------------------------------------------------------------------- #
# walidacja sumy kontrolnej NIP (wagi 6,5,7,2,3,4,5,6,7; suma%11 != 10)

_NIP_WAGI = (6, 5, 7, 2, 3, 4, 5, 6, 7)


def nip_checksum_ok(nip: str) -> bool:
    if len(nip) != 10 or not nip.isdigit():
        return False
    suma = sum(int(d) * w for d, w in zip(nip[:9], _NIP_WAGI))
    kontrolna = suma % 11
    return kontrolna != 10 and kontrolna == int(nip[9])


# dwa NIP-y syntetyczne, POPRAWNA suma kontrolna, nieobecne w kartotece AGTLABO
NIP_DOSTAWCA = "9000000016"
NIP_ODBIORCA = "9000000022"
assert nip_checksum_ok(NIP_DOSTAWCA), NIP_DOSTAWCA
assert nip_checksum_ok(NIP_ODBIORCA), NIP_ODBIORCA
assert NIP_DOSTAWCA != NIP_ODBIORCA


# --------------------------------------------------------------------------- #
# budowa syntetycznych FA(3) -- ściśle wg tests/fixtures/fa3_basic_23.xml,
# rozszerzone o Platnosc/TerminPlatnosci/Termin (rozrachunki)

_FA3_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<Faktura xmlns="http://crd.gov.pl/wzor/2025/06/25/13775/">
  <Naglowek>
    <KodFormularza kodSystemowy="FA (3)" wersjaSchemy="1-0E">FA</KodFormularza>
    <WariantFormularza>3</WariantFormularza>
    <DataWytworzeniaFa>{data}T09:00:00Z</DataWytworzeniaFa>
    <SystemInfo>Symulacja</SystemInfo>
  </Naglowek>
  <Podmiot1>
{podmiot1_dane}
    <Adres>
      <KodKraju>PL</KodKraju>
      <AdresL1>{p1_adres_l1}</AdresL1>
      <AdresL2>{p1_adres_l2}</AdresL2>
    </Adres>
  </Podmiot1>
  <Podmiot2>
{podmiot2_dane}
    <Adres>
      <KodKraju>PL</KodKraju>
      <AdresL1>{p2_adres_l1}</AdresL1>
      <AdresL2>{p2_adres_l2}</AdresL2>
    </Adres>
  </Podmiot2>
  <Fa>
    <KodWaluty>PLN</KodWaluty>
    <P_1>{data}</P_1>
    <P_2>{numer}</P_2>
    <P_6>{data}</P_6>
    <P_13_1>100.00</P_13_1>
    <P_14_1>23.00</P_14_1>
    <P_15>123.00</P_15>
    <RodzajFaktury>VAT</RodzajFaktury>
    <FaWiersz>
      <NrWierszaFa>1</NrWierszaFa>
      <P_7>Usluga testowa - symulacja</P_7>
      <P_8A>szt.</P_8A>
      <P_8B>1</P_8B>
      <P_9A>100.00</P_9A>
      <P_11>100.00</P_11>
      <P_12>23</P_12>
    </FaWiersz>{platnosc_sekcja}
  </Fa>
</Faktura>
"""

# sekcja Platnosc/TerminPlatnosci -- POMIJANA, gdy build_fa3_xml dostanie
# termin_platnosci=None (test TEST-SYM-0002: fallback termin_fallback_dni)
_PLATNOSC_TEMPLATE = """
    <Platnosc>
      <TerminPlatnosci>
        <Termin>{termin}</Termin>
      </TerminPlatnosci>
    </Platnosc>"""


def _dane_identyfikacyjne(strona: dict) -> str:
    if strona.get("brak_id"):
        return (
            "    <DaneIdentyfikacyjne>\n"
            "      <BrakID>1</BrakID>\n"
            f"      <Nazwa>{strona['nazwa']}</Nazwa>\n"
            "    </DaneIdentyfikacyjne>"
        )
    return (
        "    <DaneIdentyfikacyjne>\n"
        f"      <NIP>{strona['nip']}</NIP>\n"
        f"      <Nazwa>{strona['nazwa']}</Nazwa>\n"
        "    </DaneIdentyfikacyjne>"
    )


def build_fa3_xml(
    *, numer: str, sprzedawca: dict, nabywca: dict,
    data: str = DATA_FAKTURY, termin_platnosci: str | None = TERMIN_PLATNOSCI,
) -> bytes:
    """termin_platnosci=None -> FA(3) BEZ sekcji Platnosc/TerminPlatnosci (test
    ścieżki fallback w map_fa3_to_hmffk: termin = P_1 + termin_fallback_dni)."""
    platnosc_sekcja = (
        _PLATNOSC_TEMPLATE.format(termin=termin_platnosci) if termin_platnosci else ""
    )
    xml = _FA3_TEMPLATE.format(
        data=data,
        numer=numer,
        platnosc_sekcja=platnosc_sekcja,
        podmiot1_dane=_dane_identyfikacyjne(sprzedawca),
        p1_adres_l1=sprzedawca["adres_l1"],
        p1_adres_l2=sprzedawca["adres_l2"],
        podmiot2_dane=_dane_identyfikacyjne(nabywca),
        p2_adres_l1=nabywca["adres_l1"],
        p2_adres_l2=nabywca["adres_l2"],
    )
    return xml.encode("utf-8")


# strony syntetycznych faktur ------------------------------------------------

MERIDOL = dict(nip="9100010014", nazwa="MERIDOL JAN KOWALSKI",
              adres_l1="Testowa 4", adres_l2="40-158 Katowice")
DOSTAWCA = dict(nip=NIP_DOSTAWCA, nazwa="TESTOWY DOSTAWCA SYMULACJA SP. Z O.O.",
                adres_l1="Przykladowa 10", adres_l2="00-001 Warszawa")
ODBIORCA = dict(nip=NIP_ODBIORCA, nazwa="TESTOWY ODBIORCA SYMULACJA SP. Z O.O.",
                adres_l1="Handlowa 5", adres_l2="30-001 Krakow")
JAN_TESTOWY = dict(brak_id=True, nazwa="Jan Testowy",
                    adres_l1="Testowa 1", adres_l2="00-001 Warszawa")


def _meta(ksef_number: str, invoice_number: str, seller_nip: str, seller_name: str,
          buyer_id: str, buyer_name: str, day: str = DATA_FAKTURY) -> InvoiceMetadata:
    return InvoiceMetadata(
        ksef_number=ksef_number,
        invoice_number=invoice_number,
        issue_date=f"{day}T00:00:00+00:00",
        invoicing_date=f"{day}T10:00:00+00:00",
        acquisition_date=None,
        seller_nip=seller_nip,
        seller_name=seller_name,
        buyer_id=buyer_id,
        buyer_name=buyer_name,
        net=None, gross=None, vat=None,
        currency="PLN",
        form_system_code="FA (3)",
        invoice_type="Vat",
    )


@dataclass
class Faktura:
    kind: Kind
    ksef_number: str
    numer: str
    meta: InvoiceMetadata
    xml: bytes
    opis: str


def build_faktury() -> list[Faktura]:
    faktury = []

    # a) KOSZT: sprzedawca = MERIDOL (ISTNIEJĄCY kontrahent, poz. 5 w seedzie)
    xml = build_fa3_xml(numer="TEST/1/2026", sprzedawca=MERIDOL, nabywca=WLASNA)
    faktury.append(Faktura(
        Kind.COST, "TEST-SYM-0001", "TEST/1/2026",
        _meta("TEST-SYM-0001", "TEST/1/2026", MERIDOL["nip"], MERIDOL["nazwa"],
              PROFIL_NIP, WLASNA["nazwa"]),
        xml, "koszt od istniejącego kontrahenta MERIDOL (poz. 5)",
    ))

    # b) KOSZT: sprzedawca = nowy dostawca (NIP poprawny, nieobecny w seedzie)
    # -- CELOWO bez Platnosc/TerminPlatnosci: test ścieżki fallback (termin =
    # P_1 + termin_fallback_dni dni, patrz stałe DATA_FAKTURY/TERMIN_FALLBACK_DNI)
    xml = build_fa3_xml(
        numer="TEST/2/2026", sprzedawca=DOSTAWCA, nabywca=WLASNA, termin_platnosci=None,
    )
    faktury.append(Faktura(
        Kind.COST, "TEST-SYM-0002", "TEST/2/2026",
        _meta("TEST-SYM-0002", "TEST/2/2026", DOSTAWCA["nip"], DOSTAWCA["nazwa"],
              PROFIL_NIP, WLASNA["nazwa"]),
        xml, "koszt od NOWEGO kontrahenta -- BEZ Platnosc/TerminPlatnosci (fallback terminu)",
    ))

    # c) SPRZEDAZ: nabywca = nowy odbiorca (NIP poprawny, nieobecny w seedzie)
    xml = build_fa3_xml(numer="TEST/3/2026", sprzedawca=WLASNA, nabywca=ODBIORCA)
    faktury.append(Faktura(
        Kind.SALES, "TEST-SYM-0003", "TEST/3/2026",
        _meta("TEST-SYM-0003", "TEST/3/2026", PROFIL_NIP, WLASNA["nazwa"],
              ODBIORCA["nip"], ODBIORCA["nazwa"]),
        xml, "sprzedaż do NOWEGO kontrahenta (firma, NIP)",
    ))

    # d) SPRZEDAZ: nabywca = osoba fizyczna bez NIP (BrakID=1) -> klucz K_...
    xml = build_fa3_xml(numer="TEST/4/2026", sprzedawca=WLASNA, nabywca=JAN_TESTOWY)
    faktury.append(Faktura(
        Kind.SALES, "TEST-SYM-0004", "TEST/4/2026",
        _meta("TEST-SYM-0004", "TEST/4/2026", PROFIL_NIP, WLASNA["nazwa"],
              "", JAN_TESTOWY["nazwa"]),
        xml, "sprzedaż do osoby fizycznej bez NIP (ścieżka K_<hash>)",
    ))
    return faktury


# --------------------------------------------------------------------------- #
# stub klienta KSeF -- WAŻNE: serwuje metadane PER KIND (subject_type), inaczej
# niż najprostszy StubClient z tests/test_exporter.py (który po prostu zwraca tę
# samą listę niezależnie od subject_type -- działa tam tylko dlatego, że testy
# wołają run_export() z JEDNYM kind na raz). Tu wołamy run_export RAZEM z
# kinds={Kind.SALES, Kind.COST}, więc filtrowanie po subject_type jest konieczne.

class StubClient:
    def __init__(self, faktury: list[Faktura]):
        self._metas_by_kind: dict[str, list[InvoiceMetadata]] = {
            Kind.SALES.value: [f.meta for f in faktury if f.kind is Kind.SALES],
            Kind.COST.value: [f.meta for f in faktury if f.kind is Kind.COST],
        }
        self._xml = {f.ksef_number: f.xml for f in faktury}
        self.auth_called = False

    def authenticate(self) -> None:
        self.auth_called = True

    def query_metadata(self, subject_type, date_from, date_to):
        yield from self._metas_by_kind.get(subject_type, [])

    def download_invoice(self, ksef_number: str) -> bytes:
        return self._xml[ksef_number]

    def download_budget_remaining(self) -> int:
        return 64


# --------------------------------------------------------------------------- #

def reset_symulacja() -> None:
    """Czyści stare artefakty poprzedniego przebiegu / poprzedniej wersji skryptu
    (idempotencja) -- w tym kartotekę znormalizowaną z wersji z workaroundem
    (kartoteka_seed_normalized.xlsx), której już nie generujemy: test ma
    świadomie użyć SUROWEGO oryginału (patrz KARTOTEKA_XLSX)."""
    for db in (REJESTR_DB, LEDGER_DB):
        for suffix in ("", "-wal", "-shm"):
            p = db.with_name(db.name + suffix)
            if p.exists():
                p.unlink()
    if WYNIK_DIR.exists():
        shutil.rmtree(WYNIK_DIR)
    if _STARY_SEED_NORMALIZED.exists():
        _STARY_SEED_NORMALIZED.unlink()
    for stary_txt in SIM_DIR.glob("*.TXT"):
        stary_txt.unlink()


def seed_rejestr(rejestr: RejestrNumerow, kartoteka_path: Path) -> tuple[int, list[str]]:
    dodane, ostrzezenia = rejestr.importuj_kartoteke(PROFIL_NIP, kartoteka_path)
    return dodane, ostrzezenia


# --------------------------------------------------------------------------- #
# weryfikacja pliku wynikowego

@dataclass
class Weryfikacja:
    ok: bool
    opis: str


def _doc_chunks(content: str) -> dict[str, str]:
    """dokument{...} bloki zindeksowane po KsefNumber (proste dzielenie po
    delimiterze 'dokument{' -- bezpieczne, bo ten literał nie występuje nigdzie
    indziej w pliku)."""
    parts = content.split("dokument{")
    chunks: dict[str, str] = {}
    for part in parts[1:]:
        m = re.search(r"KsefNumber =(\S+)", part)
        if m:
            chunks[m.group(1)] = part
    return chunks


def _zapisy(chunk: str) -> list[dict[str, str]]:
    wyniki = []
    for blok in re.findall(r"zapis\{(.*?)\}", chunk, re.S):
        strona = re.search(r"strona =(\w+)", blok)
        kwota = re.search(r"kwota =([\d.]+)", blok)
        konto = re.search(r"Konto =([^\r\n]+)", blok)
        wyniki.append({
            "strona": strona.group(1) if strona else "",
            "kwota": kwota.group(1) if kwota else "",
            "konto": konto.group(1) if konto else "",
        })
    return wyniki


# zapis{...} bezpośrednio (bez pustej linii) followed by transakcja{...} --
# dokładnie taki układ wymaga przepis rozrachunków (żadna z dwóch sekcji nie
# zawiera zagnieżdżonych nawiasów klamrowych, więc [^{}]* jest bezpieczne)
_ZAPIS_TRANSAKCJA_RE = re.compile(r"zapis\{([^{}]*)\}\r\ntransakcja\{([^{}]*)\}")


def _zapis_transakcja_pary(chunk: str) -> list[tuple[str, str]]:
    return _ZAPIS_TRANSAKCJA_RE.findall(chunk)


def _sprawdz_konta_dokumentu(
    chunk: str, nr_ksef: str, rodzaj: str, poz: int | None,
) -> list[Weryfikacja]:
    """Konta na liniach zapis{} jednego dokumentu wg rodzaju (koszt/sprzedaz).

    koszt (dekret zakupu, kolejność zapisów: WN netto -> MA brutto -> WN vat):
      - WN netto (100.00): BEZ konta -- dekretuje księgowa (konto_koszt_netto=""),
      - MA brutto (123.00): "Konto =<KONTO_PREFIX_KOSZT>-<poz>" + rozrachunek
        (NumerDok/IdDlaRozliczen, sprawdzane osobno w pętli rozrachunków),
      - WN vat (23.00): "Konto =<KONTO_KOSZT_VAT>" (VAT naliczony, domyślnie 221-1).
    sprzedaz: WYŁĄCZNIE WN brutto dostaje "Konto =<KONTO_PREFIX_SPRZEDAZ>-<poz>"
      (kwota 123.00); linie MA netto/VAT dostają dodatkowo KONTO_SPRZEDAZ_NETTO
      (100.00) / KONTO_SPRZEDAZ_VAT (23.00) -- pełny dekret sprzedaży.
    """
    zapisy = _zapisy(chunk)

    if rodzaj == "koszt":
        oczekiwane_brutto = f"{KONTO_PREFIX_KOSZT}-{poz}" if poz else None
        netto_z = next((z for z in zapisy if z["strona"] == "WN" and z["kwota"] == "100.00"), None)
        brutto_z = next((z for z in zapisy if z["strona"] == "MA" and z["kwota"] == "123.00"), None)
        vat_z = next((z for z in zapisy if z["strona"] == "WN" and z["kwota"] == "23.00"), None)

        ok_brutto = (
            poz is not None and brutto_z is not None and brutto_z["konto"] == oczekiwane_brutto
        )
        ok_vat = vat_z is not None and vat_z["konto"] == KONTO_KOSZT_VAT
        ok_netto_bez_konta = netto_z is not None and not netto_z["konto"]
        kolejnosc = [(z["strona"], z["kwota"]) for z in zapisy]
        ok_kolejnosc = kolejnosc == [("WN", "100.00"), ("MA", "123.00"), ("WN", "23.00")]
        ok_liczba_kont = sum(1 for z in zapisy if z["konto"]) == 2  # tylko MA brutto + WN vat

        return [
            Weryfikacja(
                ok_brutto,
                f"Dokument {nr_ksef} (koszt): 'Konto ={oczekiwane_brutto}' na zapisie MA brutto, "
                "kwota 123.00 (rozrachunek)",
            ),
            Weryfikacja(
                ok_vat,
                f"Dokument {nr_ksef} (koszt): 'Konto ={KONTO_KOSZT_VAT}' na zapisie WN vat, "
                "kwota 23.00",
            ),
            Weryfikacja(
                ok_netto_bez_konta,
                f"Dokument {nr_ksef} (koszt): zapis WN netto (100.00) BEZ konta "
                "(dekretuje księgowa)",
            ),
            Weryfikacja(
                ok_kolejnosc,
                f"Dokument {nr_ksef} (koszt): kolejność zapisów WN netto -> MA brutto -> "
                "WN vat (dekret zakupu Symfonii); ma "
                + str(kolejnosc),
            ),
            Weryfikacja(
                ok_liczba_kont,
                f"Dokument {nr_ksef} (koszt): dokładnie 2 linie z 'Konto =' (MA brutto + WN vat)",
            ),
        ]

    wn_konto = [z for z in zapisy if z["strona"] == "WN" and z["konto"]]
    ma_konto = [z for z in zapisy if z["strona"] == "MA" and z["konto"]]
    oczekiwane = f"{KONTO_PREFIX_SPRZEDAZ}-{poz}" if poz else None
    ok_brutto = poz is not None and any(
        z["konto"] == oczekiwane and z["kwota"] == "123.00" for z in wn_konto
    )
    ok_netto = any(
        z["konto"] == KONTO_SPRZEDAZ_NETTO and z["kwota"] == "100.00" for z in ma_konto
    )
    ok_vat = any(
        z["konto"] == KONTO_SPRZEDAZ_VAT and z["kwota"] == "23.00" for z in ma_konto
    )
    return [
        Weryfikacja(
            ok_brutto,
            f"Dokument {nr_ksef} (sprzedaż): 'Konto ={oczekiwane}' na zapisie WN, "
            "kwota 123.00",
        ),
        Weryfikacja(
            ok_netto,
            f"Dokument {nr_ksef}: 'Konto ={KONTO_SPRZEDAZ_NETTO}' na zapisie MA netto, "
            "kwota 100.00",
        ),
        Weryfikacja(
            ok_vat,
            f"Dokument {nr_ksef}: 'Konto ={KONTO_SPRZEDAZ_VAT}' na zapisie MA VAT, "
            "kwota 23.00",
        ),
        Weryfikacja(
            len(wn_konto) == 1 and len(ma_konto) == 2,
            f"Dokument {nr_ksef}: dokładnie 3 linie z 'Konto =' (1 WN brutto + 2 MA netto/VAT)",
        ),
    ]


def weryfikuj(txt_path: Path, rejestr: RejestrNumerow, faktury: list[Faktura]) -> list[Weryfikacja]:
    wyniki: list[Weryfikacja] = []

    raw = txt_path.read_bytes()

    # (i) CRLF: każdy \n poprzedzony \r, każdy \r bezpośrednio przed \n
    crlf_ok = raw.count(b"\n") == raw.count(b"\r\n") and raw.count(b"\r") == raw.count(b"\r\n")
    wyniki.append(Weryfikacja(crlf_ok, "Plik ma spójne zakończenia linii CRLF"))

    content = raw.decode("cp1250")
    chunks = _doc_chunks(content)

    # (ii) mapowanie klucz -> pozycja z rejestru; trzy nowe karty = 28,29,30
    wpisy = rejestr.lista(PROFIL_NIP)
    nowe = [w for w in wpisy if w.pozycja >= 28]
    nowe_pozycje = sorted(w.pozycja for w in nowe)
    wyniki.append(Weryfikacja(
        len(wpisy) == 30, f"Rejestr ma 30 wpisów po eksporcie (27 seed + 3 nowe); ma {len(wpisy)}"
    ))
    wyniki.append(Weryfikacja(
        nowe_pozycje == [28, 29, 30],
        f"Trzy nowe karty dostały pozycje [28, 29, 30]; dostały {nowe_pozycje}",
    ))
    wyniki.append(Weryfikacja(
        rejestr.pozycja(PROFIL_NIP, MERIDOL["nip"]) == 5,
        f"MERIDOL (klucz {MERIDOL['nip']}, {len(MERIDOL['nip'])} cyfr) pozostał na pozycji 5 "
        "(z seedu, bez nowego przydziału) -- potwierdza naprawę _wczytaj_xlsx/_digits",
    ))

    klucz_do_pozycji = {w.klucz: w.pozycja for w in wpisy}

    # dokładnie 1 karta Kontrahent{} MERIDOL (odróżnij od "khid =" w dokument{})
    wyniki.append(Weryfikacja(
        content.count(f'Kontrahent{{\r\nid ={MERIDOL["nip"]}\r\n') == 1,
        "Karta MERIDOL występuje w pliku dokładnie raz (bez duplikatu)",
    ))

    # (iii) konta rozrachunkowe + pełny dekret sprzedaży (201-2/202-2/703/222-1)
    wyniki += _sprawdz_konta_dokumentu(chunks.get("TEST-SYM-0001", ""), "TEST-SYM-0001", "koszt", 5)
    wyniki += _sprawdz_konta_dokumentu(
        chunks.get("TEST-SYM-0002", ""), "TEST-SYM-0002", "koszt",
        klucz_do_pozycji.get(DOSTAWCA["nip"]),
    )
    wyniki += _sprawdz_konta_dokumentu(
        chunks.get("TEST-SYM-0003", ""), "TEST-SYM-0003", "sprzedaz",
        klucz_do_pozycji.get(ODBIORCA["nip"]),
    )
    # Jan Testowy (K_...) -- znajdź jego klucz przez wpis bez NIP o statusie 'auto'
    jan_wpisy = [w for w in wpisy if w.pozycja >= 28 and not w.nip]
    wyniki.append(Weryfikacja(
        len(jan_wpisy) == 1,
        f"Dokładnie jeden wpis w rejestrze bez NIP (Jan Testowy, K_...); znaleziono {len(jan_wpisy)}",
    ))
    jan_poz = jan_wpisy[0].pozycja if len(jan_wpisy) == 1 else None
    wyniki += _sprawdz_konta_dokumentu(chunks.get("TEST-SYM-0004", ""), "TEST-SYM-0004", "sprzedaz", jan_poz)

    # (iv) sanity WEJŚCIA: TEST-SYM-0002 celowo BEZ sekcji Platnosc/TerminPlatnosci
    # w FA(3) (test ścieżki fallback termin_fallback_dni); pozostałe 3 faktury mają
    # ją jawnie -- to gwarantuje, że test rzeczywiście ćwiczy dwie różne ścieżki.
    for f in faktury:
        ma_platnosc = b"<Platnosc>" in f.xml
        oczekiwana = f.ksef_number != "TEST-SYM-0002"
        opis_sciezki = "termin jawny (Fa/Platnosc/TerminPlatnosci/Termin)" if oczekiwana \
            else "BRAK Platnosc -- termin liczony fallbackiem (P_1 + termin_fallback_dni)"
        wyniki.append(Weryfikacja(
            ma_platnosc == oczekiwana,
            f"Dokument {f.ksef_number}: FA(3) wejściowe -- {opis_sciezki}",
        ))

    # (v) rozrachunki: zapis{} rozrachunkowy (NumerDok/IdDlaRozliczen) tuż przed
    # transakcja{} z tym samym idDlaRozliczen, terminem ISO i kwotą brutto
    # (przepis: reference/rozrachunki-hmffk-E2E.md) -- WSZYSTKIE 4 dokumenty mają
    # transakcja{} z tym samym terminem: 3 jawnie z FA(3), TEST-SYM-0002 fallbackiem
    # (P_1 + termin_fallback_dni dni = ta sama data -- patrz stałe u góry pliku)
    for f in faktury:
        chunk = chunks.get(f.ksef_number, "")
        pary = _zapis_transakcja_pary(chunk)
        ok = len(pary) == 1
        if ok:
            zapis_body, trans_body = pary[0]
            ok = (
                f"NumerDok ={f.numer}" in zapis_body
                and "IdDlaRozliczen =1" in zapis_body
                and "idDlaRozliczen =1" in trans_body
                and f"termin ={TERMIN_PLATNOSCI}" in trans_body
                and "kwota =123.00" in trans_body
            )
        wyniki.append(Weryfikacja(
            ok,
            f"Dokument {f.ksef_number} ({f.numer}): zapis rozrachunkowy z "
            f"'NumerDok ={f.numer}' i 'IdDlaRozliczen =1' bezpośrednio poprzedza "
            f"'transakcja{{ idDlaRozliczen =1, termin ={TERMIN_PLATNOSCI}, kwota =123.00 }}'",
        ))
        wyniki.append(Weryfikacja(
            chunk.count("transakcja{") == 1,
            f"Dokument {f.ksef_number}: dokładnie 1 sekcja transakcja{{}} w dokumencie",
        ))

    # struktura ogólna pliku
    wyniki.append(Weryfikacja(content.startswith("INFO{\r\nWersja szablonu =4\r\n"),
                               "Plik zaczyna się nagłówkiem INFO{ Wersja szablonu =4"))
    wyniki.append(Weryfikacja(content.count("Kontrahent{") == 4,
                               f"4 sekcje Kontrahent{{}} w pliku (ma {content.count('Kontrahent{')})"))
    wyniki.append(Weryfikacja(content.count("dokument{") == 4,
                               f"4 sekcje dokument{{}} w pliku (ma {content.count('dokument{')})"))
    wyniki.append(Weryfikacja(PROFIL_NIP not in content,
                               f"NIP własnej firmy ({PROFIL_NIP}) NIE występuje w pliku wynikowym"))

    return wyniki


# --------------------------------------------------------------------------- #

def zapisz_readme(txt_path: Path, rejestr: RejestrNumerow) -> Path:
    wpisy = {w.klucz: w for w in rejestr.lista(PROFIL_NIP)}
    poz_meridol = rejestr.pozycja(PROFIL_NIP, MERIDOL["nip"])
    poz_dostawca = rejestr.pozycja(PROFIL_NIP, DOSTAWCA["nip"])
    poz_odbiorca = rejestr.pozycja(PROFIL_NIP, ODBIORCA["nip"])
    jan_wpis = next((w for w in wpisy.values() if w.pozycja >= 28 and not w.nip), None)
    poz_jan = jan_wpis.pozycja if jan_wpis else "?"

    tabela = f"""
| Kontrahent (faktura)                              | Klucz w rejestrze      | Numer karty (oczekiwany) | Konto rozrachunkowe (brutto) | Inne konta dekretu | Termin płatności |
|----------------------------------------------------|-------------------------|--------------------------|-------------------------------|---------------------|--------------------|
| MERIDOL JAN KOWALSKI (TEST/1/2026, koszt)            | NIP {MERIDOL['nip']}     | {poz_meridol} (ISTNIEJĄCA, z kartoteki) | {KONTO_PREFIX_KOSZT}-{poz_meridol} (MA) | {KONTO_KOSZT_VAT} (WN vat); netto bez konta | jawny w FA(3) |
| TESTOWY DOSTAWCA SYMULACJA SP. Z O.O. (TEST/2/2026, koszt) | NIP {DOSTAWCA['nip']} | {poz_dostawca} (NOWA)    | {KONTO_PREFIX_KOSZT}-{poz_dostawca} (MA) | {KONTO_KOSZT_VAT} (WN vat); netto bez konta | **fallback** (brak Platnosc w FA(3), P_1+{TERMIN_FALLBACK_DNI}dni) |
| TESTOWY ODBIORCA SYMULACJA SP. Z O.O. (TEST/3/2026, sprzedaż) | NIP {ODBIORCA['nip']} | {poz_odbiorca} (NOWA) | {KONTO_PREFIX_SPRZEDAZ}-{poz_odbiorca} (WN) | {KONTO_SPRZEDAZ_NETTO} (MA) / {KONTO_SPRZEDAZ_VAT} (MA) | jawny w FA(3) |
| Jan Testowy (TEST/4/2026, sprzedaż, bez NIP)        | K_... (hash nazwa+miasto) | {poz_jan} (NOWA)       | {KONTO_PREFIX_SPRZEDAZ}-{poz_jan} (WN) | {KONTO_SPRZEDAZ_NETTO} (MA) / {KONTO_SPRZEDAZ_VAT} (MA) | jawny w FA(3) |
"""

    tresc = f"""# Plik symulacji hmffk -- test importu do Symfonii FK

Ten katalog zawiera WYŁĄCZNIE dane testowe wygenerowane skryptem
`generuj_symulacje.py`, do jednorazowego, RĘCZNEGO wypróbowania importu w
Symfonii FK na kopii/testowej bazie firmy. Nic tu nie dotyka bazy
produkcyjnej (`%APPDATA%\\KsefSymfoniaExport`) -- rejestr numerów i ledger są
osobnymi, tymczasowymi plikami `.db` w tym katalogu.

Kartoteka źródłowa (`{KARTOTEKA_XLSX}`) jest wczytywana w POSTACI SUROWEJ,
bez żadnej normalizacji -- to jednocześnie test regresyjny naprawy
`app/kontrahenci_nr.py::_wczytaj_xlsx`/`_digits` (kolumna NIP zapisana w
Excelu jako liczba, np. `9100010014.0` dla MERIDOL, musi dać klucz
`9100010014`, a nie zepsuty 11-cyfrowy `91000100140`).

## Co zawiera plik wynikowy

Plik: `{txt_path.relative_to(ROOT)}`

Format: AMS "Dane z programu Handel, format FK 3.0" (szablon `hmffk.AMS`),
CP1250 + CRLF. Zawiera:

- 4 sekcje `Kontrahent{{}}` -- karty kontrahentów (1 istniejący + 3 nowi),
- 4 sekcje `dokument{{}}` -- 2 faktury kosztowe (FVZ) + 2 sprzedażowe (FVS),
  każda z numerem KSeF, rejestrem VAT (23%, netto 100,00 / VAT 23,00 /
  brutto 123,00) i PEŁNYM dekretem rozrachunkowym (sekcje `zapis{{}}` +
  `transakcja{{}}`): linia brutto kontrahenta dostaje konto rozrachunkowe,
  `NumerDok`, `IdDlaRozliczen` i sekcję `transakcja{{}}` z terminem płatności
  ({TERMIN_PLATNOSCI}) -- FK zakłada rozrachunek automatycznie, bez ręcznego
  wypełniania.
  - **Dekret sprzedaży** (WN brutto / MA netto / MA VAT): konto rozrachunkowe
    na WN brutto, dodatkowo `{KONTO_SPRZEDAZ_NETTO}` na MA netto i
    `{KONTO_SPRZEDAZ_VAT}` na MA VAT.
  - **Dekret kosztowy** (WN netto / MA brutto / WN VAT -- kolejność jak w
    oryginalnym dekrecie zakupu Symfonii): WN netto zostaje BEZ konta
    (dekretuje księgowa), MA brutto dostaje konto rozrachunkowe (+
    rozrachunek), WN VAT dostaje `{KONTO_KOSZT_VAT}` (VAT naliczony).
  - **Fallback terminu płatności**: gdy FA(3) nie niesie
    `Fa/Platnosc/TerminPlatnosci/Termin` (na żywo dotyczyło to 505/547
    faktur), termin = data wystawienia (P_1) + `termin_fallback_dni`
    ({TERMIN_FALLBACK_DNI} dni) -- dzięki temu rozrachunek i tak powstaje.
    W tej symulacji ćwiczy to TEST/2/2026 (TEST-SYM-0002): FA(3) BEZ sekcji
    Platnosc, termin liczony fallbackiem wychodzi tak samo jak jawny termin
    pozostałych trzech faktur ({TERMIN_PLATNOSCI}).

## Tabela: kontrahent -> oczekiwany numer karty -> oczekiwane konta
{tabela}
Uwaga: numer karty MERIDOL (5) pochodzi z zaimportowanej kartoteki AGTLABO
(27 pozycji, 1-27). Trzy nowe karty dostają PIERWSZE WOLNE numery -- tu 28,
29, 30 -- w kolejności sekcji `Kontrahent{{}}` w pliku (klucze posortowane:
najpierw NIP-y jako stringi, potem `K_...`). Prefiks konta rozrachunkowego
zależy od rodzaju dokumentu: `{KONTO_PREFIX_SPRZEDAZ}-<pozycja>` dla
sprzedaży (strona WN), `{KONTO_PREFIX_KOSZT}-<pozycja>` dla kosztu
(strona MA). Konto WN VAT kosztu (`{KONTO_KOSZT_VAT}`) i konto WN netto
kosztu (puste -- `konto_koszt_netto=""`) są takie same dla wszystkich
dokumentów kosztowych (nie zależą od pozycji karty).

## Instrukcja importu w Symfonii FK

1. Otwórz Symfonię FK na profilu/firmie TESTOWEJ (NIE na produkcyjnej bazie
   AGTLABO!).
2. Menu: **Import/Eksport -> Katalogi wymiany danych** (albo "Import
   specjalny", zależnie od wersji).
3. Wskaż szablon **"Dane z programu Handel, format FK 3.0"** (to jest
   standardowy szablon `hmffk.AMS`, dostępny w każdej instalacji FK -- nic nie
   trzeba dokopiowywać do folderu szablonów).
4. Jako plik danych wskaż `{txt_path.name}`.
5. Przy PIERWSZYM imporcie tego typu dokumentów FK poprosi o **uzgodnienie
   typów dokumentów** (FVS dla sprzedaży, FVZ dla kosztu) **i rejestrów VAT**
   -- wskaż istniejące typy/rejestry w tej bazie (albo pozwól założyć nowe, wg
   polityki księgowej).
6. Zatwierdź import.

## Co sprawdzić PO imporcie

- **Kartoteka kontrahentów**: nowe karty powinny dostać numery **28, 29,
  30** (dokładnie w tej kolejności: TESTOWY DOSTAWCA=28, TESTOWY ODBIORCA=29,
  Jan Testowy=30) -- to pierwsze wolne numery po 27 z kartoteki AGTLABO.
- **Karta MERIDOL** (NIP {MERIDOL['nip']}) NIE powinna się zduplikować --
  system powinien rozpoznać istniejącą kartę nr {poz_meridol} po kluczu (id
  = NIP) i dopisać do niej fakturę, a nie założyć nowej.
- **Bufor dokumentów** (4 dokumenty w buforze, przed zatwierdzeniem/
  zaksięgowaniem): na zapisie z kontem rozrachunkowym sprawdź, że konto
  zgadza się z numerem karty kontrahenta, i że kolejność zapisów kosztu to
  WN netto (bez konta) -> MA brutto (konto rozrachunkowe) -> WN VAT (konto
  {KONTO_KOSZT_VAT}):
    - TEST/1/2026 (MERIDOL, koszt): konto **{KONTO_PREFIX_KOSZT}-{poz_meridol}**
      na stronie MA (brutto 123,00), plus **{KONTO_KOSZT_VAT}** na WN VAT
      (23,00); WN netto (100,00) BEZ konta,
    - TEST/2/2026 (Dostawca, koszt -- termin fallbackiem, patrz niżej): konto
      **{KONTO_PREFIX_KOSZT}-{poz_dostawca}** na stronie MA, plus
      **{KONTO_KOSZT_VAT}** na WN VAT; WN netto BEZ konta,
    - TEST/3/2026 (Odbiorca, sprzedaż): konto
      **{KONTO_PREFIX_SPRZEDAZ}-{poz_odbiorca}** na stronie WN, plus
      **{KONTO_SPRZEDAZ_NETTO}** (MA netto) i **{KONTO_SPRZEDAZ_VAT}**
      (MA VAT),
    - TEST/4/2026 (Jan Testowy, sprzedaż): konto
      **{KONTO_PREFIX_SPRZEDAZ}-{poz_jan}** na stronie WN, plus
      **{KONTO_SPRZEDAZ_NETTO}** / **{KONTO_SPRZEDAZ_VAT}** jak wyżej.
  Konto netto kosztu zostaje puste -- dekretuje je księgowa ręcznie w
  buforze wg zasad tej firmy; konto WN VAT ({KONTO_KOSZT_VAT}) przychodzi
  już wypełnione.
- **Rozrachunki**: na każdym z 4 dokumentów powinien automatycznie powstać
  otwarty rozrachunek z terminem płatności **{TERMIN_PLATNOSCI}** (bez
  klikania "rozrachunków" ręcznie) -- to efekt sekcji `transakcja{{}}`
  towarzyszącej zapisowi brutto (patrz `reference/rozrachunki-hmffk-E2E.md`).
  Na TEST/2/2026 (TEST-SYM-0002) ten sam termin powstaje mimo braku sekcji
  Platnosc/TerminPlatnosci w źródłowej FA(3) -- to efekt fallbacku
  `termin_fallback_dni` ({TERMIN_FALLBACK_DNI} dni od daty wystawienia)
  w `map_fa3_to_hmffk`.

## Jak POSPRZĄTAĆ po teście

Test jest odwracalny -- żeby nie zaśmiecać bazy testowej:

1. W buforze dokumentów usuń 4 zaimportowane dokumenty: TEST/1/2026,
   TEST/2/2026, TEST/3/2026, TEST/4/2026 (numery KSeF TEST-SYM-0001 ..
   TEST-SYM-0004) -- wraz z rozrachunkami, które do nich powstały.
2. W kartotece kontrahentów usuń 3 TESTOWE karty: TESTOWY DOSTAWCA
   SYMULACJA SP. Z O.O. (nr {poz_dostawca}), TESTOWY ODBIORCA SYMULACJA
   SP. Z O.O. (nr {poz_odbiorca}), Jan Testowy (nr {poz_jan}). NIE usuwaj
   karty MERIDOL (nr {poz_meridol}) -- ona była w kartotece PRZED testem.
3. Po usunięciu tych 3 kart FK odzyska numery 28-30 dla kolejnych NOWYCH
   kontrahentów -- to zgodne z zasadą "pierwszy wolny numer" (FK zasypuje
   dziury po usuniętych kartach), którą odwzorowuje też rejestr numerów w tej
   aplikacji (`app/kontrahenci_nr.py::RejestrNumerow.pierwszy_wolny`).

## Pliki tymczasowe tego testu

- `rejestr_symulacja.db` -- rejestr numerów kart (seed z kartoteki AGTLABO +
  3 nowe przydziały). Możesz go bezpiecznie usunąć po teście.
- `ledger_symulacja.db` -- rejestr wyeksportowanych faktur (dedup). Możesz go
  bezpiecznie usunąć po teście.
- `wynik/{PROFIL_SUBFOLDER}/*.TXT` -- plik(i) wynikowe.

Żaden z powyższych plików NIE jest używany przez aplikację produkcyjną --
produkcyjne bazy są w `%APPDATA%\\KsefSymfoniaExport\\`.
"""
    readme_path = SIM_DIR / "CZYTAJ-SYMULACJA.md"
    readme_path.write_text(tresc, encoding="utf-8")
    return readme_path


# --------------------------------------------------------------------------- #

def main() -> int:
    _assert_bezpieczne_sciezki()
    print("=" * 78)
    print("SYMULACJA eksportu hmffk -- test importu do Symfonii FK")
    print("=" * 78)
    print(f"Katalog symulacji : {SIM_DIR}")
    print(f"Rejestr (tymczas.): {REJESTR_DB}")
    print(f"Ledger  (tymczas.): {LEDGER_DB}")
    print(f"Wynik             : {WYNIK_DIR}")
    print()

    reset_symulacja()

    rejestr = RejestrNumerow(REJESTR_DB)
    ledger = Ledger(LEDGER_DB)

    try:
        # 1) seed kartoteki AGTLABO -- SUROWY oryginał, bez normalizacji ------
        print(f"-- Źródło kartoteki (SUROWY oryginał): {KARTOTEKA_XLSX}")
        dodane, ostrzezenia = seed_rejestr(rejestr, KARTOTEKA_XLSX)
        print(f"   dodano wpisów: {dodane}")
        if ostrzezenia:
            print(f"   ostrzeżenia ({len(ostrzezenia)}):")
            for w in ostrzezenia:
                print(f"     - {w}")
        else:
            print("   brak ostrzeżeń")

        # gate regresyjny: klucz MERIDOL musi być DOKŁADNIE 10-cyfrowym NIP-em.
        # Kolumna NIP w surowym pliku jest liczbą Excela (9100010014.0) --
        # bez naprawy _wczytaj_xlsx/_digits klucz wyszedłby 11-cyfrowy
        # ("91000100140"), rejestr.pozycja(...) zwróciłby None i poniższy
        # test przerwałby się natychmiast (kod 3).
        meridol_poz = rejestr.pozycja(PROFIL_NIP, MERIDOL["nip"])
        print(
            f"-- Gate: klucz MERIDOL = {MERIDOL['nip']!r} ({len(MERIDOL['nip'])} cyfr) "
            f"-> pozycja w rejestrze: {meridol_poz}"
        )
        if meridol_poz is None:
            print(
                "BŁĄD KRYTYCZNY: klucz MERIDOL (NIP 10-cyfrowy) nie znaleziony w rejestrze "
                "po seedzie -- regres _wczytaj_xlsx/_digits (NIP jako liczba Excela, "
                "np. '9100010014.0' -> zepsuty 11-cyfrowy klucz) NIE jest naprawiony "
                "w app/kontrahenci_nr.py."
            )
            return 3
        print("   OK: klucz MERIDOL == 10-cyfrowy NIP (bez sufiksu '.0' / bez 11. cyfry)")
        print()

        # 2) budowa 4 syntetycznych faktur ----------------------------------
        faktury = build_faktury()
        print("-- Syntetyczne faktury FA(3):")
        for f in faktury:
            print(f"   {f.ksef_number}  {f.numer:15s} [{f.kind.label:8s}] {f.opis}")
        print(
            f"   termin płatności (docelowo, wszystkie 4): {TERMIN_PLATNOSCI} -- 3 jawnie "
            f"z FA(3), TEST-SYM-0002 fallbackiem (brak Platnosc, P_1+{TERMIN_FALLBACK_DNI}dni)"
        )
        print()

        # 3) run_export produkcyjną ścieżką, ze stubem KSeF -----------------
        client = StubClient(faktury)
        print("-- StubClient: metadane per kind (subject_type)")
        for kind in (Kind.SALES, Kind.COST):
            nrs = [m.ksef_number for m in client._metas_by_kind.get(kind.value, [])]
            print(f"   {kind.value} ({kind.label:8s}): {nrs}")
        print()

        profile = Profile(name=PROFIL_NAZWA, nip=PROFIL_NIP, token="SYMULACJA-TOKEN-NIEUZYWANY",
                           subfolder=PROFIL_SUBFOLDER)

        print(
            "-- run_export(fmt='hmffk', kinds={SALES, COST}, rejestr_nr=<seedowany>, "
            f"konto_prefix_sprzedaz='{KONTO_PREFIX_SPRZEDAZ}', "
            f"konto_prefix_koszt='{KONTO_PREFIX_KOSZT}', "
            f"konto_sprzedaz_netto='{KONTO_SPRZEDAZ_NETTO}', "
            f"konto_sprzedaz_vat='{KONTO_SPRZEDAZ_VAT}', "
            f"konto_koszt_vat='{KONTO_KOSZT_VAT}', "
            f"konto_koszt_netto='{KONTO_KOSZT_NETTO}', "
            f"termin_fallback_dni={TERMIN_FALLBACK_DNI})"
        )
        report = run_export(
            profile, client, ledger,
            date(2026, 7, 1), date(2026, 7, 31),
            {Kind.SALES, Kind.COST},
            WYNIK_DIR,
            fmt="hmffk",
            rejestr_nr=rejestr,
            konto_prefix_sprzedaz=KONTO_PREFIX_SPRZEDAZ,
            konto_prefix_koszt=KONTO_PREFIX_KOSZT,
            konto_sprzedaz_netto=KONTO_SPRZEDAZ_NETTO,
            konto_sprzedaz_vat=KONTO_SPRZEDAZ_VAT,
            konto_koszt_vat=KONTO_KOSZT_VAT,
            konto_koszt_netto=KONTO_KOSZT_NETTO,
            termin_fallback_dni=TERMIN_FALLBACK_DNI,
        )
        print(f"   {report.summary()}")
        if report.warnings:
            print(f"   ostrzeżenia raportu ({len(report.warnings)}):")
            for w in report.warnings:
                print(f"     - {w}")
        print()

        out_dir = WYNIK_DIR / PROFIL_SUBFOLDER
        pliki = sorted(out_dir.glob("hmffk_*.TXT"))
        if len(pliki) != 1:
            print(f"BŁĄD: oczekiwano dokładnie 1 pliku hmffk_*.TXT w {out_dir}, znaleziono {len(pliki)}")
            return 2
        txt_path = pliki[0]
        print(f"-- Plik wynikowy: {txt_path}")
        print()

        # 4) weryfikacja ------------------------------------------------------
        wyniki = weryfikuj(txt_path, rejestr, faktury)

        print("-" * 78)
        print("PODSUMOWANIE WERYFIKACJI")
        print("-" * 78)
        ok_all = True
        for w in wyniki:
            znacznik = "OK " if w.ok else "!! "
            print(f"[{znacznik}] {w.opis}")
            ok_all = ok_all and w.ok
        print("-" * 78)

        readme_path = zapisz_readme(txt_path, rejestr)
        print(f"Zapisano instrukcję: {readme_path}")
        print()

        if ok_all:
            print("WYNIK: wszystkie weryfikacje OK.")
            return 0
        else:
            print("WYNIK: NIEZGODNOŚĆ -- patrz pozycje '!!' powyżej.")
            return 1
    finally:
        rejestr.close()
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
