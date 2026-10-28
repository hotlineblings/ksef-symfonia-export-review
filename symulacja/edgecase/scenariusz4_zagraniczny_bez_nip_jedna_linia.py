"""SCENARIUSZ 4: kontrahent zagraniczny BEZ NIP, adres w JEDNEJ linii (AdresL1),
AdresL2 puste -- stabilnosc klucza MIEDZY eksportami (dwa niezalezne eksporty
tego samego realnego kontrahenta, np. w kolejnych miesiacach)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile

from wspolne import StubClient, ctx, fa3_koszt_od, meta, sep

from app.adres import parse_adres
from app.config import Profile
from app.exporter import Kind, run_export
from app.hmffk import map_fa3_to_hmffk
from app.kontrahenci_nr import RejestrNumerow
from app.ledger import Ledger

sep("SCENARIUSZ 4: kontrahent zagraniczny bez NIP, adres w jednej linii")

NAZWA = "Global Freight Solutions Ltd"
ADRES_JEDNOLINIOWY = "123 Main Street, Suite 400, New York, NY 10001, USA"
print(f"Kontrahent: {NAZWA!r}, kraj US")
print(f"AdresL1 (jedna linia): {ADRES_JEDNOLINIOWY!r}")
print("AdresL2: '' (puste)")

s = parse_adres(ADRES_JEDNOLINIOWY, "")
print(f"\nparse_adres(...) -> {s}")
print(f"s.miejscowosc = {s.miejscowosc!r}  (puste -- brak polskiego formatu kodu pocztowego XX-XXX)")
print("=> klucz liczony jako hash(nazwa + '|' + (s.miejscowosc lub AdresL2)) "
      "== hash(nazwa + '|') -- ZALEZY WYLACZNIE OD SUROWEGO POLA 'nazwa', nie od adresu!")

# --- test 1: DWIE faktury tego samego kontrahenta, IDENTYCZNA nazwa i adres,
# w dwoch oddzielnych eksportach (np. styczen i luty) ---
xml_1 = fa3_koszt_od(NAZWA, nip_sprzedawcy="", adres_l1=ADRES_JEDNOLINIOWY, adres_l2="",
                      kod_kraju="US", numer="INV/2026/001", data="2026-01-10")
xml_2 = fa3_koszt_od(NAZWA, nip_sprzedawcy="", adres_l1=ADRES_JEDNOLINIOWY, adres_l2="",
                      kod_kraju="US", numer="INV/2026/017", data="2026-02-14")

res_1 = map_fa3_to_hmffk(xml_1, ctx("NR-US1"))
res_2 = map_fa3_to_hmffk(xml_2, ctx("NR-US2"))
print(f"\nKlucz eksport 1 (styczen) = {res_1.kontrahent.klucz}")
print(f"Klucz eksport 2 (luty)    = {res_2.kontrahent.klucz}")
print(f"STABILNY miedzy eksportami (identyczna 'nazwa' w obu fakturach): "
      f"{res_1.kontrahent.klucz == res_2.kontrahent.klucz}")

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    rejestr = RejestrNumerow(tmp / "nr.db")
    ledger = Ledger(tmp / "ledger.db")
    profile = Profile("Firma Test", "5252445719", "tok", "firma-test")

    metas1 = [meta("NR-US1", invoice_number="INV/2026/001", day="2026-01-10")]
    client1 = StubClient(metas1, {"NR-US1": xml_1})
    run_export(profile, client1, ledger, date(2026, 1, 1), date(2026, 1, 31),
               {Kind.COST}, tmp / "out", fmt="hmffk", rejestr_nr=rejestr)
    poz_1 = rejestr.pozycja("5252445719", res_1.kontrahent.klucz)

    metas2 = [meta("NR-US2", invoice_number="INV/2026/017", day="2026-02-14")]
    client2 = StubClient(metas2, {"NR-US2": xml_2})
    run_export(profile, client2, ledger, date(2026, 2, 1), date(2026, 2, 28),
               {Kind.COST}, tmp / "out", fmt="hmffk", rejestr_nr=rejestr)
    poz_2 = rejestr.pozycja("5252445719", res_2.kontrahent.klucz)

    sep("Eksport w dwoch niezaleznych przebiegach (styczen, luty)")
    print(f"Pozycja karty w rejestrze -- styczen: {poz_1}, luty: {poz_2}")
    print(f"TA SAMA karta w obu eksportach: {poz_1 == poz_2}")
    rejestr.close()
    ledger.close()

# --- test 2: ryzyko -- drobna, NIEUNIKNIONA roznica w polu 'nazwa' miedzy fakturami
# (np. sprzedawca raz doda kropke/spacje/skrot prawnej formy) LUB w adresie, gdy
# ten wplywalby na klucz (nie wplywa tu, bo miejscowosc==''), rozbija kontrahenta ---
sep("Ryzyko: klucz zalezy WYLACZNIE od 'nazwa' (surowy string, bez normalizacji) -- "
    "adres moze sie zmieniac dowolnie bez wplywu na klucz")

warianty_adresu = [
    "123 Main Street, Suite 400, New York, NY 10001, USA",
    "123 Main Street, Suite 400, New York, NY, USA",       # bez kodu pocztowego
    "123 MAIN STREET, SUITE 400, NEW YORK, NY 10001, USA",  # inna wielkosc liter
    "  123 Main Street, Suite 400, New York, NY 10001, USA  ",  # spacje na brzegach
]
klucze_adresowe = set()
for adr in warianty_adresu:
    xml = fa3_koszt_od(NAZWA, nip_sprzedawcy="", adres_l1=adr, adres_l2="", kod_kraju="US")
    res = map_fa3_to_hmffk(xml, ctx("X"))
    klucze_adresowe.add(res.kontrahent.klucz)
    print(f"  adres={adr!r:65s} -> klucz={res.kontrahent.klucz}")
print(f"\nLiczba ROZNYCH kluczy mimo 4 wariantow tego samego adresu (ta sama firma): "
      f"{len(klucze_adresowe)}  (oczekiwane: 1, bo adres nie wplywa na klucz gdy miejscowosc puste)")

warianty_nazwy = [
    "Global Freight Solutions Ltd",
    "Global Freight Solutions Ltd.",   # kropka na koncu
    "Global Freight Solutions LTD",    # wielkosc liter formy prawnej
    "Global Freight Solutions  Ltd",   # podwojna spacja
]
klucze_nazwy = set()
for nazwa in warianty_nazwy:
    xml = fa3_koszt_od(nazwa, nip_sprzedawcy="", adres_l1=ADRES_JEDNOLINIOWY, adres_l2="", kod_kraju="US")
    res = map_fa3_to_hmffk(xml, ctx("X"))
    klucze_nazwy.add(res.kontrahent.klucz)
    print(f"  nazwa={nazwa!r:35s} -> klucz={res.kontrahent.klucz}")
print(f"\nLiczba ROZNYCH kluczy dla 4 drobnych wariantow zapisu TEJ SAMEJ nazwy: "
      f"{len(klucze_nazwy)}  (kazda roznica w surowym stringu 'nazwa' = NOWY klucz = NOWA karta!)")

print("\n[SCENARIUSZ 4] KONIEC")
