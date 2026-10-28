"""MINIMALNY test całego flow (import kartoteki -> eksport hmffk -> weryfikacja)
dla DWÓCH kontrahentów na bazie BETALABO -- odchudzona wersja generuj_symulacje.py
(przeczytaj ten plik najpierw, ten skrypt wzoruje się na jego strukturze).

Cel -- sprawdzić RAZEM, jedną ścieżką, całe flow:

  1) Rejestr numerów jest seedowany SUROWYM (bez żadnej wstępnej normalizacji)
     plikiem CSV wyeksportowanym z Arkuszy Google
     (`C:\\Users\\Anna\\Downloads\\Arkusz kalkulacyjny bez tytułu - Arkusz1.csv`)
     -- plik ma śmieciowy pierwszy wiersz (same przecinki, nagłówek dopiero w
     drugim) i dwie nadmiarowe kolumny bez nazwy na końcu każdego wiersza --
     to jednocześnie test odporności importera naprawionego w fazie 1
     (`app/kontrahenci_nr.py::_wczytaj_csv` / `_czy_wiersz_naglowka` /
     `_mapuj_wiersze_danych`). Bramka zaraz po seedzie: dokładnie 33 wpisy,
     a klucz "9100010014" (MERIDOL JAN KOWALSKI) ląduje na pozycji 5.
  2) Dwie syntetyczne faktury FA(3) idą przez PRODUKCYJNY run_export naraz
     (kinds={Kind.COST, Kind.SALES}):
       a) KOSZT od MERIDOL (ISTNIEJĄCY kontrahent, poz. 5 w seedzie) -- CELOWO
          bez sekcji Fa/Platnosc/TerminPlatnosci: test ścieżki fallback
          (termin = P_1 + termin_fallback_dni = 2026-07-28 + 14 dni =
          2026-08-11) -- na żywo dotyczyło to 505/547 faktur.
       b) SPRZEDAŻ do NOWEGO odbiorcy (NIP poprawny, nieobecny w seedzie) --
          termin PODANY WPROST w FA(3) (też 2026-08-11, żeby oba dokumenty
          miały ten sam termin rozrachunku mimo dwóch różnych ścieżek jego
          wyznaczenia) -- nowa karta dostaje max+1: 34.
  3) Weryfikacja PEŁNEGO dekretu obu dokumentów (konta, kolejność zapisów,
     rozrachunki NumerDok/IdDlaRozliczen + transakcja{}), kart Kontrahent{}
     (MERIDOL bez duplikatu, nowa karta poz. 34) i stanu rejestru po eksporcie
     (34 wpisy).

WAŻNE: skrypt tworzy WYŁĄCZNIE tymczasowe bazy w katalogu symulacja/
(rejestr_flow.db, ledger_flow.db) i NIGDY nie dotyka
%APPDATA%\\KsefSymfoniaExport (tam są bazy produkcyjne). Nie importuje
app_data_dir() i wszędzie przekazuje jawne db_path.

Uruchomienie:
    .venv\\Scripts\\python.exe symulacja\\generuj_flow_betalabo.py

Kod wyjścia 0 = wszystkie weryfikacje przeszły; != 0 = niezgodność (patrz
podsumowanie na stdout) -- kod 3 = bramka importera (33 wpisy / pozycja 5)
nie przeszła zaraz po seedzie.
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
# konfiguracja -- WYŁĄCZNIE pliki w symulacja\, nigdy %APPDATA%

# SUROWY oryginał -- BEZ żadnej normalizacji/kopii pośredniej. CSV eksportu z
# Arkuszy Google: śmieciowy pierwszy wiersz (same przecinki) + nagłówek w
# drugim + 2 nadmiarowe kolumny bez nazwy na końcu -- patrz bramka w main().
# Trwała kopia w tests/fixtures/ (oryginał w Downloads bywa usuwany) -- ścieżka
# względna od położenia tego skryptu, żeby działała niezależnie od maszyny.
KARTOTEKA_CSV = ROOT / "tests" / "fixtures" / "kartoteka_google_sheets.csv"

PROFIL_NIP = "5555555555"          # placeholder "własnej firmy" -- NIE trafia do pliku wynikowego
PROFIL_NAZWA = "BETALABO FLOW SYMULACJA"
PROFIL_SUBFOLDER = "betalabo-flow"

# konta -- DOMYŚLNE (nie nadpisujemy w wywołaniu run_export), patrz
# app/config.py::AppConfig / app/exporter.py::run_export sygnatura:
# konto_prefix_sprzedaz="201-2", konto_prefix_koszt="202-2",
# konto_sprzedaz_netto="703", konto_sprzedaz_vat="222-1",
# konto_koszt_vat="221-1", konto_koszt_netto="", termin_fallback_dni=14
KONTO_PREFIX_SPRZEDAZ = "201-2"
KONTO_PREFIX_KOSZT = "202-2"
KONTO_SPRZEDAZ_NETTO = "703"
KONTO_SPRZEDAZ_VAT = "222-1"
KONTO_KOSZT_VAT = "221-1"
TERMIN_FALLBACK_DNI = 14           # brak TerminPlatnosci w FA(3) -> P_1 + tyle dni

REJESTR_DB = SIM_DIR / "rejestr_flow.db"
LEDGER_DB = SIM_DIR / "ledger_flow.db"
WYNIK_DIR = SIM_DIR / "wynik_flow"

# strona "nasza" na obu fakturach -- NIP placeholder, adres bez znaczenia
# (nigdy nie trafia do pliku: hmffk.py ekstrahuje tylko stronę PRZECIWNĄ)
WLASNA = dict(nip=PROFIL_NIP, nazwa="FIRMA FLOW SP. Z O.O.",
              adres_l1="Siedziba 1", adres_l2="00-001 Warszawa")

DATA_FAKTURY = "2026-07-28"        # Fa/P_1 -- obie faktury
TERMIN_PLATNOSCI = "2026-08-11"    # termin docelowy obu dokumentów (jawny + fallback)


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


# NIP syntetyczny, POPRAWNA suma kontrolna, nieobecny w kartotece BETALABO
# (33 pozycje z pliku CSV -- patrz KARTOTEKA_CSV -- żadna nie ma tego NIP-u)
NIP_ODBIORCA = "9000000016"
assert nip_checksum_ok(NIP_ODBIORCA), NIP_ODBIORCA


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
      <P_7>Usluga testowa - flow</P_7>
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
# termin_platnosci=None (faktura kosztowa: test fallbacku termin_fallback_dni)
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
ODBIORCA = dict(nip=NIP_ODBIORCA, nazwa="TESTOWY ODBIORCA FLOW SP. Z O.O.",
                adres_l1="Handlowa 5", adres_l2="30-001 Krakow")


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

    # a) KOSZT: sprzedawca = MERIDOL (ISTNIEJĄCY kontrahent, poz. 5 w seedzie) --
    # CELOWO bez Platnosc/TerminPlatnosci: test ścieżki fallback (termin =
    # P_1 + termin_fallback_dni dni -- patrz stałe DATA_FAKTURY/TERMIN_FALLBACK_DNI)
    xml = build_fa3_xml(
        numer="TEST-FLOW/1/2026", sprzedawca=MERIDOL, nabywca=WLASNA, termin_platnosci=None,
    )
    faktury.append(Faktura(
        Kind.COST, "TEST-FLOW-0001", "TEST-FLOW/1/2026",
        _meta("TEST-FLOW-0001", "TEST-FLOW/1/2026", MERIDOL["nip"], MERIDOL["nazwa"],
              PROFIL_NIP, WLASNA["nazwa"]),
        xml, "koszt od istniejącego kontrahenta MERIDOL (poz. 5) -- BEZ Platnosc (fallback terminu)",
    ))

    # b) SPRZEDAZ: nabywca = nowy odbiorca (NIP poprawny, nieobecny w seedzie) --
    # termin PODANY WPROST (ten sam co fallback powyżej -- oba dokumenty kończą
    # z jednym wspólnym terminem rozrachunku, ale dwiema różnymi ścieżkami)
    xml = build_fa3_xml(numer="TEST-FLOW/2/2026", sprzedawca=WLASNA, nabywca=ODBIORCA)
    faktury.append(Faktura(
        Kind.SALES, "TEST-FLOW-0002", "TEST-FLOW/2/2026",
        _meta("TEST-FLOW-0002", "TEST-FLOW/2/2026", PROFIL_NIP, WLASNA["nazwa"],
              ODBIORCA["nip"], ODBIORCA["nazwa"]),
        xml, "sprzedaż do NOWEGO kontrahenta (firma, NIP) -> nowa karta poz. 34",
    ))
    return faktury


# --------------------------------------------------------------------------- #
# stub klienta KSeF -- WAŻNE: serwuje metadane PER KIND (subject_type), inaczej
# niż najprostszy StubClient z tests/test_exporter.py (który po prostu zwraca tę
# samą listę niezależnie od subject_type -- działa tam tylko dlatego, że testy
# wołają run_export() z JEDNYM kind na raz). Tu wołamy run_export RAZEM z
# kinds={Kind.SALES, Kind.COST}, więc filtrowanie po subject_type jest konieczne
# (dokładnie ten sam powód co w symulacja\\generuj_symulacje.py).

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

def reset_flow() -> None:
    """Czyści stare artefakty poprzedniego przebiegu (idempotencja)."""
    for db in (REJESTR_DB, LEDGER_DB):
        for suffix in ("", "-wal", "-shm"):
            p = db.with_name(db.name + suffix)
            if p.exists():
                p.unlink()
    if WYNIK_DIR.exists():
        shutil.rmtree(WYNIK_DIR)


def seed_rejestr(rejestr: RejestrNumerow, kartoteka_path: Path) -> tuple[int, list[str]]:
    return rejestr.importuj_kartoteke(PROFIL_NIP, kartoteka_path)


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

    # (ii) rejestr po eksporcie: 34 wpisy (33 seed + 1 nowa karta odbiorcy);
    # MERIDOL pozostaje na pozycji 5 (bez nowego przydziału); nowa karta -> 34
    wpisy = rejestr.lista(PROFIL_NIP)
    nowe = [w for w in wpisy if w.pozycja >= 34]
    nowe_pozycje = sorted(w.pozycja for w in nowe)
    wyniki.append(Weryfikacja(
        len(wpisy) == 34, f"Rejestr ma 34 wpisy po eksporcie (33 seed + 1 nowa); ma {len(wpisy)}"
    ))
    wyniki.append(Weryfikacja(
        nowe_pozycje == [34],
        f"Nowa karta (odbiorca) dostała pozycję [34]; dostała {nowe_pozycje}",
    ))
    wyniki.append(Weryfikacja(
        rejestr.pozycja(PROFIL_NIP, MERIDOL["nip"]) == 5,
        f"MERIDOL (klucz {MERIDOL['nip']}, {len(MERIDOL['nip'])} cyfr) pozostał na pozycji 5 "
        "(z seedu, bez nowego przydziału) -- karta NIE zduplikowała się",
    ))

    klucz_do_pozycji = {w.klucz: w.pozycja for w in wpisy}

    # dokładnie 1 karta Kontrahent{} MERIDOL (odróżnij od "khid =" w dokument{})
    wyniki.append(Weryfikacja(
        content.count(f'Kontrahent{{\r\nid ={MERIDOL["nip"]}\r\n') == 1,
        "Karta MERIDOL występuje w pliku dokładnie raz (bez duplikatu, dane z faktury)",
    ))

    # (iii) konta rozrachunkowe + pełny dekret (202-2-5 koszt / 201-2-34 sprzedaż)
    wyniki += _sprawdz_konta_dokumentu(chunks.get("TEST-FLOW-0001", ""), "TEST-FLOW-0001", "koszt", 5)
    wyniki += _sprawdz_konta_dokumentu(
        chunks.get("TEST-FLOW-0002", ""), "TEST-FLOW-0002", "sprzedaz",
        klucz_do_pozycji.get(ODBIORCA["nip"]),
    )

    # (iv) sanity WEJŚCIA: TEST-FLOW-0001 (koszt) celowo BEZ sekcji
    # Platnosc/TerminPlatnosci w FA(3) (test ścieżki fallback); TEST-FLOW-0002
    # (sprzedaż) ma ją jawnie -- gwarantuje to, że test rzeczywiście ćwiczy obie
    # ścieżki wyznaczenia terminu.
    for f in faktury:
        ma_platnosc = b"<Platnosc>" in f.xml
        oczekiwana = f.ksef_number != "TEST-FLOW-0001"
        opis_sciezki = "termin jawny (Fa/Platnosc/TerminPlatnosci/Termin)" if oczekiwana \
            else "BRAK Platnosc -- termin liczony fallbackiem (P_1 + termin_fallback_dni)"
        wyniki.append(Weryfikacja(
            ma_platnosc == oczekiwana,
            f"Dokument {f.ksef_number}: FA(3) wejściowe -- {opis_sciezki}",
        ))

    # (v) rozrachunki: zapis{} rozrachunkowy (NumerDok/IdDlaRozliczen) tuż przed
    # transakcja{} z tym samym idDlaRozliczen, terminem ISO i kwotą brutto
    # (przepis: reference/rozrachunki-hmffk-E2E.md) -- OBA dokumenty mają
    # transakcja{} z tym samym terminem: TEST-FLOW-0002 jawnie z FA(3),
    # TEST-FLOW-0001 fallbackiem (P_1 + termin_fallback_dni dni = ta sama data)
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
    wyniki.append(Weryfikacja(content.count("Kontrahent{") == 2,
                               f"2 sekcje Kontrahent{{}} w pliku (ma {content.count('Kontrahent{')})"))
    wyniki.append(Weryfikacja(content.count("dokument{") == 2,
                               f"2 sekcje dokument{{}} w pliku (ma {content.count('dokument{')})"))
    wyniki.append(Weryfikacja(PROFIL_NIP not in content,
                               f"NIP własnej firmy ({PROFIL_NIP}) NIE występuje w pliku wynikowym"))

    return wyniki


# --------------------------------------------------------------------------- #

def zapisz_readme(txt_path: Path, rejestr: RejestrNumerow) -> Path:
    poz_meridol = rejestr.pozycja(PROFIL_NIP, MERIDOL["nip"])
    poz_odbiorca = rejestr.pozycja(PROFIL_NIP, ODBIORCA["nip"])

    tabela = f"""
| Kontrahent (faktura)                                        | Klucz w rejestrze   | Numer karty (oczekiwany)              | Konto rozrachunkowe (brutto) | Inne konta dekretu             | Termin płatności |
|---------------------------------------------------------------|----------------------|-----------------------------------------|-------------------------------|----------------------------------|--------------------|
| MERIDOL JAN KOWALSKI (TEST-FLOW/1/2026, koszt)                  | NIP {MERIDOL['nip']} | {poz_meridol} (ISTNIEJĄCA, z kartoteki, BEZ duplikatu) | {KONTO_PREFIX_KOSZT}-{poz_meridol} (MA) | {KONTO_KOSZT_VAT} (WN vat); netto bez konta | **fallback** (brak Platnosc w FA(3), P_1+{TERMIN_FALLBACK_DNI}dni) = {TERMIN_PLATNOSCI} |
| TESTOWY ODBIORCA FLOW SP. Z O.O. (TEST-FLOW/2/2026, sprzedaż)  | NIP {ODBIORCA['nip']} | {poz_odbiorca} (NOWA)                  | {KONTO_PREFIX_SPRZEDAZ}-{poz_odbiorca} (WN) | {KONTO_SPRZEDAZ_NETTO} (MA) / {KONTO_SPRZEDAZ_VAT} (MA) | jawny w FA(3) = {TERMIN_PLATNOSCI} |
"""

    tresc = f"""# Plik flow (BETALABO) -- minimalny test całego przebiegu importu do Symfonii FK

Ten katalog zawiera WYŁĄCZNIE dane testowe wygenerowane skryptem
`generuj_flow_betalabo.py`, do jednorazowego, RĘCZNEGO wypróbowania importu w
Symfonii FK na kopii/testowej bazie firmy. Nic tu nie dotyka bazy
produkcyjnej (`%APPDATA%\\KsefSymfoniaExport`) -- rejestr numerów i ledger są
osobnymi, tymczasowymi plikami `.db` w tym katalogu (`rejestr_flow.db`,
`ledger_flow.db`), ODRĘBNYMI od tych używanych przez `generuj_symulacje.py`.

Kartoteka źródłowa (`{KARTOTEKA_CSV}`) jest wczytywana w POSTACI SUROWEJ, bez
żadnej normalizacji -- plik ma śmieciowy pierwszy wiersz (same przecinki,
nagłówek dopiero w drugim wierszu) i dwie nadmiarowe kolumny bez nazwy na
końcu każdego wiersza (eksport z Arkuszy Google). To jednocześnie test
odporności importera (`app/kontrahenci_nr.py::_wczytaj_csv` /
`_czy_wiersz_naglowka` / `_mapuj_wiersze_danych`) -- bramka w skrypcie
sprawdza tuż po seedzie: dokładnie 33 wpisy w rejestrze, a MERIDOL TOMASZ
KOWALSKI (NIP {MERIDOL['nip']}) ląduje na pozycji 5.

## Co zawiera plik wynikowy

Plik: `{txt_path.relative_to(ROOT)}`

Format: AMS "Dane z programu Handel, format FK 3.0" (szablon `hmffk.AMS`),
CP1250 + CRLF. Zawiera:

- 2 sekcje `Kontrahent{{}}` -- 1 istniejący (MERIDOL, emitowany z danymi z tej
  faktury) + 1 nowy (Testowy Odbiorca Flow),
- 2 sekcje `dokument{{}}` -- 1 faktura kosztowa (FVZ) + 1 sprzedażowa (FVS),
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
  - **Fallback terminu płatności**: faktura kosztowa (TEST-FLOW/1/2026) NIE
    niesie `Fa/Platnosc/TerminPlatnosci/Termin` (na żywo dotyczyło to
    505/547 faktur) -- termin = data wystawienia (P_1 = {DATA_FAKTURY}) +
    `termin_fallback_dni` ({TERMIN_FALLBACK_DNI} dni) = {TERMIN_PLATNOSCI}.
    Faktura sprzedażowa ma ten sam termin PODANY WPROST -- oba dokumenty
    kończą z jednym wspólnym terminem rozrachunku mimo dwóch różnych ścieżek
    jego wyznaczenia.

## Tabela oczekiwań
{tabela}
Uwaga: numer karty MERIDOL ({poz_meridol}) pochodzi z zaimportowanej kartoteki
BETALABO (33 pozycje, 1-33, z CSV). Nowa karta dostaje **max+1** -- tu
{poz_odbiorca} -- zgodnie z ustaleniem empirycznym z produkcji (FK NIE
zasypuje dziur po usuniętych kartach), które odwzorowuje rejestr numerów w
tej aplikacji (`app/kontrahenci_nr.py::RejestrNumerow.nastepny_numer`).

## Instrukcja importu w Symfonii FK

1. Otwórz Symfonię FK na profilu/firmie TESTOWEJ (NIE na produkcyjnej bazie
   BETALABO!).
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

- **Karta MERIDOL** (NIP {MERIDOL['nip']}) NIE powinna się zduplikować -- system
  powinien rozpoznać istniejącą kartę nr {poz_meridol} po kluczu (id = NIP) i
  dopisać do niej fakturę, a nie założyć nowej.
- **Nowa karta** (Testowy Odbiorca Flow) powinna dostać numer **{poz_odbiorca}**
  -- max+1 po 33 z kartoteki BETALABO.
- **Bufor dokumentów** (2 dokumenty w buforze, przed zatwierdzeniem/
  zaksięgowaniem): na zapisie z kontem rozrachunkowym sprawdź, że konto
  zgadza się z numerem karty kontrahenta:
    - TEST-FLOW/1/2026 (MERIDOL, koszt -- termin fallbackiem): konto
      **{KONTO_PREFIX_KOSZT}-{poz_meridol}** na stronie MA (brutto 123,00),
      plus **{KONTO_KOSZT_VAT}** na WN VAT (23,00); WN netto (100,00) BEZ
      konta; kolejność zapisów: WN netto -> MA brutto -> WN VAT,
    - TEST-FLOW/2/2026 (Testowy Odbiorca Flow, sprzedaż -- termin jawny): konto
      **{KONTO_PREFIX_SPRZEDAZ}-{poz_odbiorca}** na stronie WN, plus
      **{KONTO_SPRZEDAZ_NETTO}** (MA netto) i **{KONTO_SPRZEDAZ_VAT}**
      (MA VAT).
  Konto netto kosztu zostaje puste -- dekretuje je księgowa ręcznie w
  buforze wg zasad tej firmy; konto WN VAT ({KONTO_KOSZT_VAT}) przychodzi
  już wypełnione.
- **Rozrachunki (2/2)**: na obu dokumentach powinien automatycznie powstać
  otwarty rozrachunek z terminem płatności **{TERMIN_PLATNOSCI}** (bez
  klikania "rozrachunków" ręcznie) -- to efekt sekcji `transakcja{{}}`
  towarzyszącej zapisowi brutto (patrz `reference/rozrachunki-hmffk-E2E.md`).
  Na TEST-FLOW/1/2026 ten termin powstaje mimo braku sekcji
  Platnosc/TerminPlatnosci w źródłowej FA(3) -- to efekt fallbacku
  `termin_fallback_dni` ({TERMIN_FALLBACK_DNI} dni od daty wystawienia)
  w `map_fa3_to_hmffk`.

## Jak POSPRZĄTAĆ po teście

Test jest odwracalny -- żeby nie zaśmiecać bazy testowej:

1. W buforze dokumentów usuń 2 zaimportowane dokumenty: TEST-FLOW/1/2026,
   TEST-FLOW/2/2026 (numery KSeF TEST-FLOW-0001, TEST-FLOW-0002) -- wraz z
   rozrachunkami, które do nich powstały.
2. W kartotece kontrahentów usuń TESTOWĄ kartę: TESTOWY ODBIORCA FLOW SP. Z
   O.O. (nr {poz_odbiorca}). NIE usuwaj karty MERIDOL (nr {poz_meridol}) -- ona
   była w kartotece PRZED testem.
3. Po usunięciu tej karty FK **NIE** odzyska numeru {poz_odbiorca} dla
   kolejnego NOWEGO kontrahenta -- FK nie zasypuje dziur po usuniętych
   kartach, więc kolejna nowa karta i tak dostanie max(pozycja)+1 (dziura po
   {poz_odbiorca} zostanie pusta) -- ustalenie empiryczne z produkcji, które
   odwzorowuje też rejestr numerów w tej aplikacji
   (`app/kontrahenci_nr.py::RejestrNumerow.nastepny_numer`).

## Pliki tymczasowe tego testu

- `rejestr_flow.db` -- rejestr numerów kart (seed z kartoteki BETALABO CSV +
  1 nowy przydział). Możesz go bezpiecznie usunąć po teście.
- `ledger_flow.db` -- rejestr wyeksportowanych faktur (dedup). Możesz go
  bezpiecznie usunąć po teście.
- `wynik_flow/{PROFIL_SUBFOLDER}/*.TXT` -- plik wynikowy.

Żaden z powyższych plików NIE jest używany przez aplikację produkcyjną --
produkcyjne bazy są w `%APPDATA%\\KsefSymfoniaExport\\`.
"""
    readme_path = SIM_DIR / "CZYTAJ-FLOW-BETALABO.md"
    readme_path.write_text(tresc, encoding="utf-8")
    return readme_path


# --------------------------------------------------------------------------- #

def main() -> int:
    _assert_bezpieczne_sciezki()
    print("=" * 78)
    print("FLOW (BETALABO) -- minimalny test całego przebiegu: import kartoteki -> "
          "eksport hmffk -> weryfikacja")
    print("=" * 78)
    print(f"Katalog symulacji : {SIM_DIR}")
    print(f"Rejestr (tymczas.): {REJESTR_DB}")
    print(f"Ledger  (tymczas.): {LEDGER_DB}")
    print(f"Wynik             : {WYNIK_DIR}")
    print()

    reset_flow()

    rejestr = RejestrNumerow(REJESTR_DB)
    ledger = Ledger(LEDGER_DB)

    try:
        # 1) seed kartoteki BETALABO -- SUROWY oryginał CSV, bez normalizacji --
        print(f"-- Źródło kartoteki (SUROWY oryginał CSV): {KARTOTEKA_CSV}")
        dodane, ostrzezenia = seed_rejestr(rejestr, KARTOTEKA_CSV)
        print(f"   dodano wpisów: {dodane}")
        if ostrzezenia:
            print(f"   ostrzeżenia ({len(ostrzezenia)}):")
            for w in ostrzezenia:
                print(f"     - {w}")
        else:
            print("   brak ostrzeżeń")

        # bramka regresyjna: dokładnie 33 wpisy, MERIDOL na pozycji 5. Bez naprawy
        # detekcji nagłówka (_czy_wiersz_naglowka/_mapuj_wiersze_danych w
        # app/kontrahenci_nr.py) śmieciowy pierwszy wiersz CSV zostałby wzięty za
        # nagłówek i import padłby / dał zupełnie inne wyniki.
        wpisy_gate = rejestr.lista(PROFIL_NIP)
        meridol_poz = rejestr.pozycja(PROFIL_NIP, MERIDOL["nip"])
        print(
            f"-- Bramka: {len(wpisy_gate)} wpisów w rejestrze po seedzie, "
            f"pozycja MERIDOL ({MERIDOL['nip']}) = {meridol_poz}"
        )
        if len(wpisy_gate) != 33 or meridol_poz != 5:
            print(
                "BŁĄD KRYTYCZNY: bramka importera nie przeszła (oczekiwano 33 wpisy i "
                "pozycję MERIDOL = 5) -- sprawdź app/kontrahenci_nr.py::_wczytaj_csv / "
                "_czy_wiersz_naglowka / _mapuj_wiersze_danych."
            )
            return 3
        print("   OK: 33 wpisy, MERIDOL == pozycja 5")
        print()

        # 2) budowa 2 syntetycznych faktur ------------------------------------
        faktury = build_faktury()
        print("-- Syntetyczne faktury FA(3):")
        for f in faktury:
            print(f"   {f.ksef_number}  {f.numer:20s} [{f.kind.label:8s}] {f.opis}")
        print(f"   termin płatności (docelowo, obie): {TERMIN_PLATNOSCI}")
        print()

        # 3) run_export produkcyjną ścieżką, ze stubem KSeF, DOMYŚLNE konta ----
        client = StubClient(faktury)
        print("-- StubClient: metadane per kind (subject_type)")
        for kind in (Kind.SALES, Kind.COST):
            nrs = [m.ksef_number for m in client._metas_by_kind.get(kind.value, [])]
            print(f"   {kind.value} ({kind.label:8s}): {nrs}")
        print()

        profile = Profile(name=PROFIL_NAZWA, nip=PROFIL_NIP, token="FLOW-TOKEN-NIEUZYWANY",
                           subfolder=PROFIL_SUBFOLDER)

        print(
            "-- run_export(fmt='hmffk', kinds={SALES, COST}, rejestr_nr=<seedowany>, "
            "DOMYŚLNE konta -- brak nadpisania konto_prefix_*/konto_*/termin_fallback_dni)"
        )
        report = run_export(
            profile, client, ledger,
            date(2026, 7, 1), date(2026, 7, 31),
            {Kind.SALES, Kind.COST},
            WYNIK_DIR,
            fmt="hmffk",
            rejestr_nr=rejestr,
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
