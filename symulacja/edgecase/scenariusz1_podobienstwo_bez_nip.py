"""SCENARIUSZ 1: dwaj ROZNI kontrahenci bez NIP o PODOBNYCH nazwach (literowka).

Pytania:
- czy dostaja osobne karty/numery w rejestrze?
- czy pojawia sie ostrzezenie o podobienstwie nazwy?
"""

from __future__ import annotations

from datetime import date

from wspolne import FIXTURES, StubClient, fa3_koszt_od, meta, sep

from app.config import Profile
from app.exporter import Kind, run_export
from app.hmffk import map_fa3_to_hmffk
from app.kontrahenci_nr import RejestrNumerow, _norm, _rdzen
from app.ledger import Ledger
from difflib import SequenceMatcher

sep("SCENARIUSZ 1: dwaj rozni kontrahenci bez NIP, podobne nazwy (literowka)")

NAZWA_A = "Kowalski Transport Drogowy"
NAZWA_B = "Kowalksi Transport Drogowy"  # literowka: s/k odwrocone
ADRES_L1 = "ul. Polna 5"
ADRES_L2 = "62-800 Kalisz"

print(f"Kontrahent A: {NAZWA_A!r}")
print(f"Kontrahent B: {NAZWA_B!r}  (literowka wzgledem A)")

rdzen_a, rdzen_b = _rdzen(NAZWA_A), _rdzen(NAZWA_B)
ratio = SequenceMatcher(None, rdzen_a, rdzen_b).ratio()
print(f"\n_rdzen(A) = {rdzen_a!r}")
print(f"_rdzen(B) = {rdzen_b!r}")
print(f"SequenceMatcher(rdzen_a, rdzen_b).ratio() = {ratio:.4f}  (prog domyslny podobni() = 0.87)")

# --- klucze wygenerowane przez map_fa3_to_hmffk (bez rejestru numerow) ---
xml_a = fa3_koszt_od(NAZWA_A, nip_sprzedawcy="", adres_l1=ADRES_L1, adres_l2=ADRES_L2, numer="FV/A/0001")
xml_b = fa3_koszt_od(NAZWA_B, nip_sprzedawcy="", adres_l1=ADRES_L1, adres_l2=ADRES_L2, numer="FV/B/0001")

from wspolne import ctx  # noqa: E402

res_a = map_fa3_to_hmffk(xml_a, ctx("NR-A"))
res_b = map_fa3_to_hmffk(xml_b, ctx("NR-B"))
print(f"\nKlucz A (map_fa3_to_hmffk) = {res_a.kontrahent.klucz}")
print(f"Klucz B (map_fa3_to_hmffk) = {res_b.kontrahent.klucz}")
print(f"Klucze rozne: {res_a.kontrahent.klucz != res_b.kontrahent.klucz}")

# --- pelny przebieg run_export: najpierw A (rejestr pusty), potem osobno B ---
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    rejestr = RejestrNumerow(tmp / "nr.db")
    ledger = Ledger(tmp / "ledger.db")
    profile = Profile("Firma Test", "5252445719", "tok", "firma-test")

    metas_a = [meta("NR-A", invoice_number="FV/A/0001")]
    client_a = StubClient(metas_a, {"NR-A": xml_a})
    report_a = run_export(
        profile, client_a, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    poz_a = rejestr.pozycja("5252445719", res_a.kontrahent.klucz)
    sep("Eksport 1: tylko kontrahent A (rejestr pusty)")
    print(f"raport.warnings = {report_a.warnings}")
    print(f"pozycja A w rejestrze = {poz_a}")

    metas_b = [meta("NR-B", invoice_number="FV/B/0001")]
    client_b = StubClient(metas_b, {"NR-B": xml_b})
    report_b = run_export(
        profile, client_b, ledger, date(2026, 2, 1), date(2026, 2, 28),
        {Kind.COST}, tmp / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    poz_b = rejestr.pozycja("5252445719", res_b.kontrahent.klucz)
    sep("Eksport 2: kontrahent B (A juz w rejestrze) -- czy jest ostrzezenie?")
    print(f"raport.warnings = {report_b.warnings}")
    print(f"pozycja B w rejestrze = {poz_b}")
    print(f"\nA i B maja RÓŻNE pozycje (osobne karty): {poz_a != poz_b}  (A={poz_a}, B={poz_b})")

    ostrzezenia_podobienstwa = [w for w in report_b.warnings if "podobny" in w]
    print(f"Liczba ostrzezen o podobienstwie przy eksporcie B: {len(ostrzezenia_podobienstwa)}")
    for w in ostrzezenia_podobienstwa:
        print(f"  -> {w}")

    # test progu: co jesli literowka jest wieksza (rdzen mniej podobny)?
    sep("Kontrola: literowka WIEKSZA (mniejsze podobienstwo) -- czy ostrzezenie znika?")
    NAZWA_C = "Nowaczyk Uslugi Budowlane"
    NAZWA_D = "Nowak Handel Materialami"
    rdzen_c, rdzen_d = _rdzen(NAZWA_C), _rdzen(NAZWA_D)
    ratio2 = SequenceMatcher(None, rdzen_c, rdzen_d).ratio()
    print(f"'{NAZWA_C}' vs '{NAZWA_D}': ratio={ratio2:.4f} (< 0.87 -> BEZ ostrzezenia, mimo ze oba to inne firmy budowlane)")

    rejestr.close()
    ledger.close()

print("\n[SCENARIUSZ 1] KONIEC")
