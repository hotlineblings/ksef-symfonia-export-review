"""SCENARIUSZ 5: faktura uproszczona ZUPELNIE bez nazwy nabywcy -- jaki klucz/karta
powstaje?"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile

from wspolne import StubClient, ctx, fa3_sprzedaz_uproszczona_bez_nabywcy, meta, sep

from app.config import Profile
from app.exporter import Kind, run_export
from app.hmffk import map_fa3_to_hmffk
from app.kontrahenci_nr import RejestrNumerow
from app.ledger import Ledger

sep("SCENARIUSZ 5: faktura uproszczona bez nazwy nabywcy (Podmiot2/DaneIdentyfikacyjne puste)")

xml_1 = fa3_sprzedaz_uproszczona_bez_nabywcy(numer="PAR/2026/01/0001", data="2026-01-10")
xml_2 = fa3_sprzedaz_uproszczona_bez_nabywcy(numer="PAR/2026/01/0002", data="2026-01-12")

res_1 = map_fa3_to_hmffk(xml_1, ctx("NR-PAR1", subject_type="Subject1"))
res_2 = map_fa3_to_hmffk(xml_2, ctx("NR-PAR2", subject_type="Subject1"))

k1, k2 = res_1.kontrahent, res_2.kontrahent
print(f"nazwa nabywcy 1 = {k1.nazwa!r}")
print(f"nip nabywcy 1 = {k1.nip!r}")
print(f"kraj nabywcy 1 = {k1.kraj!r}")
print(f"klucz nabywcy 1 = {k1.klucz}")
print(f"osoba_fizyczna = {k1.osoba_fizyczna}, imie={k1.imie!r}, nazwisko={k1.nazwisko!r}")
print(f"ostrzezenia mapowania faktury 1: {res_1.warnings}")

print(f"\nklucz nabywcy 2 (kolejny paragon, rowniez bez nazwy) = {k2.klucz}")
print(f"TEN SAM klucz co faktura 1 (mimo ze to mogą byc RÓŻNI klienci detaliczni): "
      f"{k1.klucz == k2.klucz}")

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    rejestr = RejestrNumerow(tmp / "nr.db")
    ledger = Ledger(tmp / "ledger.db")
    profile = Profile("Firma Test", "5252445719", "tok", "firma-test")

    metas = [meta("NR-PAR1", invoice_number="PAR/2026/01/0001", day="2026-01-10"),
             meta("NR-PAR2", invoice_number="PAR/2026/01/0002", day="2026-01-12")]
    client = StubClient(metas, {"NR-PAR1": xml_1, "NR-PAR2": xml_2})

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.SALES}, tmp / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    sep("run_export -- dwie faktury uproszczone bez nazwy nabywcy, ten sam eksport")
    print(f"report.kontrahenci = {report.kontrahenci}  (oczekiwane 1 -- oba paragony scalaja sie)")
    print(f"report.warnings = {report.warnings}")

    out_dir = tmp / "out" / "firma-test"
    plik = next(out_dir.glob("hmffk_*.TXT"))
    tresc = plik.read_bytes().decode("cp1250")

    # wyciagnij sekcje Kontrahent{} zeby pokazac jak wyglada karta z pusta nazwa
    start = tresc.index("Kontrahent{")
    koniec = tresc.index("}", start) + 1
    sep("Sekcja Kontrahent{} wygenerowana dla nabywcy bez nazwy")
    print(tresc[start:koniec])

    poz = rejestr.pozycja("5252445719", k1.klucz)
    print(f"Pozycja karty w rejestrze = {poz}")

    rejestr.close()
    ledger.close()

sep("KONSEKWENCJE KSIEGOWE (do raportu)")
print("""
1. Klucz nabywcy bez nazwy = hash("" + "|" + "") = STALA wartosc (zawsze ten sam
   12-znakowy skrot SHA1 pustego stringu) -- WSZYSTKIE faktury uproszczone bez
   nazwy nabywcy z KAZDEGO eksportu i KAZDEGO okresu trafiaja na JEDNA kartę
   "kontrahenta" w FK.
2. Karta ma puste pole 'nazwa' (sekcja Kontrahent{} bez linii "nazwa =" tresci --
   patrz wydruk powyzej) -- w FK bedzie to karta bez czytelnej nazwy, kod (skrot)
   spada na klucz K_xxxxxxxxxxxx (patrc _unikalne_skroty: nazwa pusta -> nip pusty
   -> uzyty klucz).
3. Wszystkie takie utargi (paragony/faktury do paragonu bez danych nabywcy)
   ksiegowo spadaja na WSPOLNE saldo rozrachunkowe jednego zbiorczego 'klienta' --
   to w praktyce OK dla sprzedazy detalicznej (jeden zbiorczy odbiorca "brak
   danych" jest akceptowalny księgowo, o ile firma i tak nie sledzi rozrachunkow
   z anonimowymi nabywcami), ale ryzykowne jesli te faktury MIALYBY oddzielne
   naleznosci (np. terminy platnosci) -- rozrachunek jednego takiego nabywcy
   pomiesza sie z rozrachunkiem innego.
4. Ostrzezenie z map_fa3_to_hmffk ("Nabywca bez nazwy...") trafia do raportu
   przy KAZDEJ takiej fakturze -- ksiegowa dostaje sygnal, ale przy duzej liczbie
   paragonow to duzo powtarzalnych ostrzezen w UI/logu.
""")
print("[SCENARIUSZ 5] KONIEC")
