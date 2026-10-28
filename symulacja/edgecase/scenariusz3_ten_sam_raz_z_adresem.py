"""SCENARIUSZ 3: TA SAMA osoba raz z adresem, raz bez -- dwa klucze -> duplikat.

Pytania:
- czy fuzzy (podobni()) to wylapuje?
- czy scalenie aliasem (dodaj_alias) naprawia konto w KOLEJNYM eksporcie?
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

sep("SCENARIUSZ 3: ta sama osoba raz z adresem, raz bez -- duplikat karty")

NAZWA = "Anna Nowak"
print(f"Kontrahent: {NAZWA!r} (bez NIP -- osoba fizyczna) -- TA SAMA osoba na obu fakturach")
print("Faktura 1: BEZ adresu (AdresL1/L2 puste)")
print("Faktura 2: Z adresem (ul. Lipowa 8, 30-001 Krakow) -- np. sprzedawca dopisal go pozniej")

xml_bez = fa3_koszt_od(NAZWA, nip_sprzedawcy="", adres_l1="", adres_l2="",
                        numer="FV/BEZ-ADR/0001", data="2026-01-05")
xml_z = fa3_koszt_od(NAZWA, nip_sprzedawcy="", adres_l1="ul. Lipowa 8",
                      adres_l2="30-001 Krakow", numer="FV/Z-ADR/0001", data="2026-02-05")

res_bez = map_fa3_to_hmffk(xml_bez, ctx("NR-BEZ"))
res_z = map_fa3_to_hmffk(xml_z, ctx("NR-Z"))
print(f"\nKlucz (bez adresu) = {res_bez.kontrahent.klucz}")
print(f"Klucz (z adresem)  = {res_z.kontrahent.klucz}")
print(f"Klucze ROZNE (duplikat karty w FK): {res_bez.kontrahent.klucz != res_z.kontrahent.klucz}")

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    rejestr = RejestrNumerow(tmp / "nr.db")
    ledger = Ledger(tmp / "ledger.db")
    profile = Profile("Firma Test", "5252445719", "tok", "firma-test")

    # --- eksport 1: tylko faktura BEZ adresu ---
    metas1 = [meta("NR-BEZ", invoice_number="FV/BEZ-ADR/0001", day="2026-01-05")]
    client1 = StubClient(metas1, {"NR-BEZ": xml_bez})
    report1 = run_export(
        profile, client1, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    poz_bez = rejestr.pozycja("5252445719", res_bez.kontrahent.klucz)
    sep("Eksport 1 (styczen): faktura BEZ adresu")
    print(f"pozycja karty 'bez adresu' = {poz_bez}")
    print(f"warnings = {report1.warnings}")

    # --- eksport 2: faktura Z adresem (ten sam czlowiek, inny klucz) ---
    metas2 = [meta("NR-Z", invoice_number="FV/Z-ADR/0001", day="2026-02-05")]
    client2 = StubClient(metas2, {"NR-Z": xml_z})
    report2 = run_export(
        profile, client2, ledger, date(2026, 2, 1), date(2026, 2, 28),
        {Kind.COST}, tmp / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    poz_z = rejestr.pozycja("5252445719", res_z.kontrahent.klucz)
    sep("Eksport 2 (luty): TA SAMA osoba, ale Z adresem -- czy widac ostrzezenie?")
    print(f"pozycja karty 'z adresem' = {poz_z}")
    print(f"warnings = {report2.warnings}")
    ostrzezenia = [w for w in report2.warnings if "podobny" in w]
    print(f"Ostrzezenia o podobienstwie: {len(ostrzezenia)}")
    for w in ostrzezenia:
        print(f"  -> {w}")
    print(f"\n=> DUPLIKAT: dwie rozne karty ({poz_bez} i {poz_z}) dla JEDNEJ osoby: "
          f"{poz_bez != poz_z}")

    # --- scalanie aliasem: ksiegowa wykrywa duplikat w GUI (RejestrDialog) i woła
    # dodaj_alias(klucz_alias=nowszy/duplikat, klucz_kanoniczny=starszy/kanoniczny) ---
    sep("Scalanie aliasem: dodaj_alias(duplikat -> kanoniczny)")
    rejestr.dodaj_alias("5252445719", res_z.kontrahent.klucz, res_bez.kontrahent.klucz,
                         zwolnij_numer=True)
    print(f"Alias zapisany: {res_z.kontrahent.klucz} -> {res_bez.kontrahent.klucz}")
    print(f"kanoniczny(klucz 'z adresem') teraz = "
          f"{rejestr.kanoniczny('5252445719', res_z.kontrahent.klucz)}")
    print(f"Pozycja klucza 'z adresem' po scaleniu (zwolniona): "
          f"{rejestr.pozycja('5252445719', res_z.kontrahent.klucz)}")

    # --- eksport 3 (marzec): NOWA faktura z tym samym kluczem 'z adresem' --
    # czy trafi teraz na kanoniczne konto (poz_bez)?
    xml_z2 = fa3_koszt_od(NAZWA, nip_sprzedawcy="", adres_l1="ul. Lipowa 8",
                           adres_l2="30-001 Krakow", numer="FV/Z-ADR/0002", data="2026-03-05")
    metas3 = [meta("NR-Z2", invoice_number="FV/Z-ADR/0002", day="2026-03-05")]
    client3 = StubClient(metas3, {"NR-Z2": xml_z2})
    report3 = run_export(
        profile, client3, ledger, date(2026, 3, 1), date(2026, 3, 31),
        {Kind.COST}, tmp / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    out_dir = tmp / "out" / "firma-test"
    pliki = sorted(out_dir.glob("hmffk_*.TXT"), key=lambda p: p.stat().st_mtime)
    tresc3 = pliki[-1].read_bytes().decode("cp1250")
    sep("Eksport 3 (marzec): kolejna faktura tego samego klucza 'z adresem' PO scaleniu")
    print(f"warnings = {report3.warnings}")
    import re
    konta3 = re.findall(r"Konto =([\w-]+)", tresc3)
    khid3 = re.findall(r"khid =([\w]+)", tresc3)
    print(f"khid w pliku eksportu 3 = {khid3}")
    print(f"Konta w pliku eksportu 3 = {konta3}")
    print(f"\n=> Po scaleniu aliasem konto w KOLEJNYM eksporcie wskazuje na kanoniczna "
          f"pozycje {poz_bez}: {f'202-2-{poz_bez}' in konta3}")

    rejestr.close()
    ledger.close()

print("\n[SCENARIUSZ 3] KONIEC")
