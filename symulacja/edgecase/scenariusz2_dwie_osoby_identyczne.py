"""SCENARIUSZ 2: dwie ROZNE osoby fizyczne o IDENTYCZNYM imieniu i nazwisku,
bez zadnych innych danych (bez NIP, bez adresu).

Teza do sprawdzenia: sklejaja sie w JEDEN klucz/karte (hash z nazwa|puste).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile

from wspolne import StubClient, ctx, fa3_koszt_od, meta, sep

from app.config import Profile
from app.exporter import Kind, run_export
from app.hmffk import map_fa3_to_hmffk
from app.kontrahenci_nr import RejestrNumerow
from app.ledger import Ledger

sep("SCENARIUSZ 2: dwie rozne osoby fizyczne, identyczne imie+nazwisko, bez NIP/adresu")

NAZWA = "Jan Kowalski"
print(f"Osoba 1: {NAZWA!r} -- mieszka w Krakowie, brak NIP, brak adresu na fakturze")
print(f"Osoba 2: {NAZWA!r} -- INNA osoba, mieszka w Gdansku, brak NIP, brak adresu na fakturze")
print("(w realnym KSeF: osoba fizyczna nieprowadzaca dzialalnosci moze nie podac adresu")
print(" na fakturze zakupowej / sprzedawca nie zawsze wypelnia AdresL1/L2 nabywcy)")

# obie faktury: ten sam Podmiot1.Nazwa, ZUPELNIE bez adresu (AdresL1/L2 puste), bez NIP
xml_1 = fa3_koszt_od(NAZWA, nip_sprzedawcy="", adres_l1="", adres_l2="", numer="FV/OSOBA1/0001", data="2026-01-10")
xml_2 = fa3_koszt_od(NAZWA, nip_sprzedawcy="", adres_l1="", adres_l2="", numer="FV/OSOBA2/0001", data="2026-01-20")

res_1 = map_fa3_to_hmffk(xml_1, ctx("NR-OSOBA1"))
res_2 = map_fa3_to_hmffk(xml_2, ctx("NR-OSOBA2"))

print(f"\nKlucz osoby 1 = {res_1.kontrahent.klucz}")
print(f"Klucz osoby 2 = {res_2.kontrahent.klucz}")
print(f"IDENTYCZNY klucz: {res_1.kontrahent.klucz == res_2.kontrahent.klucz}")
print(f"osoba_fizyczna flag: 1={res_1.kontrahent.osoba_fizyczna}, 2={res_2.kontrahent.osoba_fizyczna}")
print(f"imie/nazwisko wykryte: 1=({res_1.kontrahent.imie!r}, {res_1.kontrahent.nazwisko!r}), "
      f"2=({res_2.kontrahent.imie!r}, {res_2.kontrahent.nazwisko!r})")
print(f"ostrzezenia mapowania faktury 1: {res_1.warnings}")
print(f"ostrzezenia mapowania faktury 2: {res_2.warnings}")

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    rejestr = RejestrNumerow(tmp / "nr.db")
    ledger = Ledger(tmp / "ledger.db")
    profile = Profile("Firma Test", "5252445719", "tok", "firma-test")

    metas = [meta("NR-OSOBA1", invoice_number="FV/OSOBA1/0001", day="2026-01-10"),
             meta("NR-OSOBA2", invoice_number="FV/OSOBA2/0001", day="2026-01-20")]
    client = StubClient(metas, {"NR-OSOBA1": xml_1, "NR-OSOBA2": xml_2})

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    sep("Wynik run_export -- oba dokumenty w JEDNYM eksporcie")
    print(f"report.kontrahenci (liczba KART kontrahenta w pliku) = {report.kontrahenci}")
    print(f"report.exported = {report.exported}")
    print(f"report.warnings = {report.warnings}")

    out_dir = tmp / "out" / "firma-test"
    plik = next(out_dir.glob("hmffk_*.TXT"))
    tresc = plik.read_bytes().decode("cp1250")
    print(f"\nLiczba sekcji 'Kontrahent{{' w pliku wynikowym: {tresc.count('Kontrahent{')}")
    print(f"Liczba sekcji 'dokument{{' w pliku wynikowym: {tresc.count('dokument{')}")

    poz = rejestr.pozycja("5252445719", res_1.kontrahent.klucz)
    print(f"Pozycja karty w rejestrze (WSPOLNA dla obu dokumentow) = {poz}")

    # oba dokumenty musza wskazywac na TA SAMA karte kontrahenta (khid) i TO SAMO konto
    import re
    khidy = re.findall(r"khid =([\w]+)", tresc)
    konta = re.findall(r"Konto =([\w-]+)", tresc)
    print(f"khid w obu dokumentach: {khidy}")
    print(f"Konta rozrachunkowe uzyte w obu dokumentach: {konta}")

    rejestr.close()
    ledger.close()

sep("KONSEKWENCJE KSIEGOWE (do raportu)")
print("""
1. Faktura od "Jana Kowalskiego nr 1" i faktura od "Jana Kowalskiego nr 2"
   (dwoch ROZNYCH, niepowiazanych ze soba dostawcow-osob fizycznych) trafiaja
   na WSPOLNA karte kontrahenta i WSPOLNE konto rozrachunkowe (np. 202-2-7).
2. Saldo tego konta w FK to SUMA zobowiazan wobec DWOCH roznych osob -- ksiegowa
   widzi jedno saldo zamiast dwoch odrebnych rozrachunkow.
3. Jesli obu placi sie w roznych terminach / kwotach, rozliczenie przelewu
   bankowego (sekcja rozliczenie{}, poza zakresem hmffk) moze rozliczyc
   platnosc dla OSOBY 1 z rozrachunkiem OSOBY 2 (ten sam idDlaRozliczen/konto) --
   ryzyko bledu przy wyciagu bankowym.
4. Dane kontaktowe/adresowe karty (jesli w ogole podane na ktorejs fakturze)
   NADPISUJA sie wzajemnie w KontrahenciRejestr.add() -- "pierwszy wygrywa,
   puste pola uzupelnia kolejny rekord" (patrz app/hmffk.py: KontrahenciRejestr.add) --
   czyli np. telefon osoby 2 moze pojawic sie na karcie podpisanej danymi osoby 1.
""")
print("[SCENARIUSZ 2] KONIEC")
