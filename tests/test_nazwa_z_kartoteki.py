"""Nazwa kontrahenta z kartoteki (rejestru numerów), nie z faktury.

Zgłoszenie z produkcji (29.09): dostawca EKO-PAK (karta FK nr 727) wpisał na
fakturze w polu Nazwa także numer BDO („… sp. z o.o<nowa linia>BDO: 000588677”).
Aplikacja przepisywała nazwę z faktury do sekcji Kontrahent{} i do „Dane
sprzedawcy{ khnazwa }”, więc Symfonia zgłaszała błąd dostawcy (nazwa na
dokumencie ≠ nazwa na karcie). Dla kontrahenta, który ma już kartę, nazwa idzie
z kartoteki; nazwa z faktury zostaje tylko dla nowej karty.
"""

from datetime import date
from pathlib import Path

from app.config import Profile
from app.exporter import Kind, run_export
from app.kontrahenci_nr import RejestrNumerow
from app.ledger import Ledger

from tests.test_exporter import FIXTURES, StubClient, _meta

PROFIL = "5252445719"
NAZWA_KARTY = "EKO-PAK PRODUCENT OPAKOWAŃ SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ"
NAZWA_Z_FAKTURY = "EKO-PAK PRODUCENT OPAKOWAŃ sp. z o.o\nBDO: 000588677"


def _xml_z_nazwa_sprzedawcy(nazwa: str) -> bytes:
    xml = (FIXTURES / "fa3_basic_23.xml").read_text(encoding="utf-8")
    xml = xml.replace(
        "<Nazwa>Dostawca Testowy Sp. z o.o.</Nazwa>", f"<Nazwa>{nazwa}</Nazwa>", 1
    )
    return xml.encode("utf-8")


def _eksport(tmp_path: Path, xml: bytes, rejestr: RejestrNumerow, kinds: set[Kind]):
    client = StubClient([_meta("NR-0001")], {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", PROFIL, "tok", "firma-a")
    try:
        report = run_export(
            profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
            kinds, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
        )
    finally:
        ledger.close()
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    return report, content


def test_istniejaca_karta_nazwa_i_kod_z_kartoteki_nie_z_faktury(tmp_path):
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.ustaw(PROFIL, "1180012345", 727, nazwa=NAZWA_KARTY, nip="1180012345", status="auto")

    report, content = _eksport(
        tmp_path, _xml_z_nazwa_sprzedawcy(NAZWA_Z_FAKTURY), rejestr, {Kind.COST}
    )

    assert report.exported == 1
    assert f"khnazwa ={NAZWA_KARTY}" in content          # Dane sprzedawcy{}
    assert f"\nnazwa ={NAZWA_KARTY}" in content          # Kontrahent{}
    # bez kodu z FK kod jak przed zmianą — z nazwy z faktury
    assert "kod =EKO-PAK PRODUCENT OPAKOWAŃ sp. z o.o BDO" in content
    assert [ln for ln in content.splitlines() if "BDO" in ln and not ln.startswith("kod =")] == []
    assert "Konto =202-2-727" in content
    assert report.nowi_kontrahenci == []
    assert any(
        "nr 727" in w and "kartoteki" in w and "BDO: 000588677" in w for w in report.warnings
    )
    rejestr.close()


def test_dwie_faktury_tego_samego_dostawcy_rozne_nazwy_obie_z_kartoteki(tmp_path):
    """Pierwsza faktura z nazwą zgodną z kartą, druga z dopiskiem BDO — karta
    w pliku ma nazwę pierwszej, ale dokument drugiej też musi dostać nazwę karty."""
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.ustaw(PROFIL, "1180012345", 727, nazwa=NAZWA_KARTY, nip="1180012345", status="auto")
    client = StubClient(
        [
            _meta("NR-0001", invoice_number="FV/1"),
            _meta("NR-0002", invoice_number="FV/2"),
        ],
        {
            "NR-0001": _xml_z_nazwa_sprzedawcy(NAZWA_KARTY),
            "NR-0002": _xml_z_nazwa_sprzedawcy(NAZWA_Z_FAKTURY).replace(
                b"FV/2026/01/0007", b"FV/2026/01/0008"
            ),
        },
    )
    ledger = Ledger(tmp_path / "ledger.db")
    report = run_export(
        Profile("Firma A", PROFIL, "tok", "firma-a"), client, ledger,
        date(2026, 1, 1), date(2026, 1, 31), {Kind.COST}, tmp_path / "out",
        fmt="hmffk", rejestr_nr=rejestr,
    )
    ledger.close()
    content = next((tmp_path / "out" / "firma-a").glob("hmffk_*.TXT")).read_bytes().decode("cp1250")

    assert report.exported == 2
    assert "BDO" not in content
    assert content.count(f"khnazwa ={NAZWA_KARTY}") == 2
    assert sum("kartoteki" in w for w in report.warnings) == 1
    rejestr.close()


def test_istniejaca_karta_nazwa_zgodna_bez_ostrzezenia(tmp_path):
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.ustaw(PROFIL, "1180012345", 5, nazwa="Dostawca Testowy Sp. z o.o.", status="auto")

    report, content = _eksport(
        tmp_path, (FIXTURES / "fa3_basic_23.xml").read_bytes(), rejestr, {Kind.COST}
    )

    assert "khnazwa =Dostawca Testowy Sp. z o.o." in content
    assert not any("kartoteki" in w for w in report.warnings)
    rejestr.close()


def test_istniejaca_karta_bez_nazwy_w_rejestrze_zostaje_nazwa_z_faktury(tmp_path):
    """Wpis w rejestrze bez nazwy (np. seed z samym NIP-em) — nie ma czym
    zastąpić, zostaje nazwa z faktury."""
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.ustaw(PROFIL, "1180012345", 5, nazwa="", status="auto")

    _, content = _eksport(
        tmp_path, (FIXTURES / "fa3_basic_23.xml").read_bytes(), rejestr, {Kind.COST}
    )

    assert "khnazwa =Dostawca Testowy Sp. z o.o." in content
    rejestr.close()


def test_nowa_karta_dostaje_nazwe_z_faktury(tmp_path):
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    report, content = _eksport(
        tmp_path, (FIXTURES / "fa3_basic_23.xml").read_bytes(), rejestr, {Kind.COST}
    )

    assert "khnazwa =Dostawca Testowy Sp. z o.o." in content
    assert report.nowi_kontrahenci and report.nowi_kontrahenci[0][0] == "Dostawca Testowy Sp. z o.o."
    rejestr.close()


def test_sprzedaz_dane_nabywcy_z_kartoteki(tmp_path):
    """To samo dla sprzedaży: kontrahentem jest nabywca (Dane nabywcy{})."""
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.ustaw(PROFIL, "5252445719", 12, nazwa="NASZA FIRMA SPÓŁKA Z O.O.", status="auto")

    report, content = _eksport(
        tmp_path, (FIXTURES / "fa3_basic_23.xml").read_bytes(), rejestr, {Kind.SALES}
    )

    assert "khnazwa =NASZA FIRMA SPÓŁKA Z O.O." in content
    assert [ln for ln in content.splitlines()
            if "Nasza Firma Sp. z o.o." in ln and not ln.startswith("kod =")] == []
    assert any("nr 12" in w and "kartoteki" in w for w in report.warnings)
    rejestr.close()


def test_kontrahent_bez_nip_po_poprawce_nazwy_w_tabeli(tmp_path):
    """Kontrahent bez polskiego NIP (klucz z nazwy): księgowa poprawia nazwę
    w tabeli kontrahentów (zmien_dane -> nowy klucz + alias ze starego).
    Kolejny eksport tej samej faktury idzie na tę samą kartę, z nową nazwą."""
    xml = (FIXTURES / "fa3_no_nip.xml").read_bytes()
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    _eksport(tmp_path / "pierwszy", xml, rejestr, {Kind.SALES})
    (wpis,) = [w for w in rejestr.lista(PROFIL) if w.nazwa == "Beispiel GmbH"]

    rejestr.zmien_dane(
        PROFIL, wpis.klucz, pozycja=wpis.pozycja, nip=wpis.nip,
        nazwa="BEISPIEL GMBH (KARTA FK)", miejscowosc=wpis.miejscowosc, status=wpis.status,
    )

    report, content = _eksport(tmp_path / "drugi", xml, rejestr, {Kind.SALES})

    assert "khnazwa =BEISPIEL GMBH (KARTA FK)" in content
    assert [ln for ln in content.splitlines()
            if "Beispiel GmbH" in ln and not ln.startswith("kod =")] == []
    assert f"Konto =201-2-{wpis.pozycja}" in content
    assert report.nowi_kontrahenci == []
    rejestr.close()


def test_opis_dokumentu_z_nazwa_z_kartoteki(tmp_path):
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.ustaw(PROFIL, "1180012345", 727, nazwa=NAZWA_KARTY, nip="1180012345", status="auto")

    _, content = _eksport(
        tmp_path, _xml_z_nazwa_sprzedawcy(NAZWA_Z_FAKTURY), rejestr, {Kind.COST}
    )

    assert f"opis FK =Faktura FV/2026/01/0007 - {NAZWA_KARTY}"[:128] in content
    rejestr.close()


def test_roznica_tylko_wielkosci_liter_bez_ostrzezenia(tmp_path):
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.ustaw(PROFIL, "1180012345", 5, nazwa="DOSTAWCA TESTOWY SP. Z O.O.", status="auto")

    report, content = _eksport(
        tmp_path, (FIXTURES / "fa3_basic_23.xml").read_bytes(), rejestr, {Kind.COST}
    )

    assert "khnazwa =DOSTAWCA TESTOWY SP. Z O.O." in content
    assert not any("kartoteki" in w for w in report.warnings)
    rejestr.close()


# --------------------------------------------------------------------------- #
# synchronizacja z Symfonią: nazwa kontrahenta z NIP-em idzie z FK zawsze —
# inaczej rejestr trzyma nazwę z pierwszej faktury (np. z dopiskiem BDO) i
# eksport dalej wysyła nazwę niezgodną z kartą

import app.kontrahenci_nr as kontrahenci_nr_mod  # noqa: E402


def _wiersz(pozycja, nazwa, nip, miejscowosc=""):
    return {"pozycja": pozycja, "kod": nazwa[:40], "nazwa": nazwa, "nip": nip,
            "miejscowosc": miejscowosc}


def test_sync_sql_nadpisuje_nazwe_z_faktury_nazwa_z_fk(tmp_path, monkeypatch):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.przydziel(PROFIL, "9100010379", nazwa=NAZWA_Z_FAKTURY, nip="9100010379")
    poz = rej.pozycja(PROFIL, "9100010379")
    monkeypatch.setattr(
        kontrahenci_nr_mod, "_czytaj_kartoteke_sql",
        lambda baza: [_wiersz(poz, NAZWA_KARTY, "9100010379", "Niegosław")],
    )

    raport = rej.synchronizuj_z_sql(PROFIL, "BAZA")

    assert rej.wpis(PROFIL, "9100010379").nazwa == NAZWA_KARTY
    assert rej.pozycja(PROFIL, "9100010379") == poz
    assert raport.zaktualizowane == 1
    # idempotencja
    assert rej.synchronizuj_z_sql(PROFIL, "BAZA").zaktualizowane == 0
    rej.close()


def test_sync_sql_nie_nadpisuje_nazwy_wpisu_manual(tmp_path, monkeypatch):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "9100010379", 727, nazwa="NAZWA OD KSIĘGOWEJ", nip="9100010379",
              status="manual")
    monkeypatch.setattr(
        kontrahenci_nr_mod, "_czytaj_kartoteke_sql",
        lambda baza: [_wiersz(727, NAZWA_KARTY, "9100010379")],
    )

    rej.synchronizuj_z_sql(PROFIL, "BAZA")

    assert rej.wpis(PROFIL, "9100010379").nazwa == "NAZWA OD KSIĘGOWEJ"
    rej.close()


# --------------------------------------------------------------------------- #
# ustalenia recenzji Codexa (29.09)

def _xml(nip: str, nazwa: str, numer: str) -> bytes:
    xml = (FIXTURES / "fa3_basic_23.xml").read_text(encoding="utf-8")
    xml = xml.replace("<NIP>1180012345</NIP>", f"<NIP>{nip}</NIP>", 1)
    xml = xml.replace(
        "<Nazwa>Dostawca Testowy Sp. z o.o.</Nazwa>", f"<Nazwa>{nazwa}</Nazwa>", 1
    )
    return xml.replace("FV/2026/01/0007", numer).encode("utf-8")


def _eksport_wielu(tmp_path, xmle: dict[str, bytes], rejestr):
    client = StubClient(
        [_meta(nr, invoice_number=f"FV/{i}") for i, nr in enumerate(xmle)], xmle
    )
    ledger = Ledger(tmp_path / "ledger.db")
    try:
        report = run_export(
            Profile("Firma A", PROFIL, "tok", "firma-a"), client, ledger,
            date(2026, 1, 1), date(2026, 1, 31), {Kind.COST}, tmp_path / "out",
            fmt="hmffk", rejestr_nr=rejestr,
        )
    finally:
        ledger.close()
    content = next((tmp_path / "out" / "firma-a").glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    return report, content


def test_kody_unikalne_po_transliteracji_cp1250(tmp_path):
    """„Møller” w CP1250 zapisze się jako „Moller” — dwa różne kody w Pythonie,
    jeden w pliku. Unikalność sprawdzana na postaci końcowej (kody liczone
    z nazw nowych kart)."""
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.ustaw(PROFIL, "INNY", 726, nazwa="Inny", status="auto")

    _, content = _eksport_wielu(tmp_path, {
        "NR-1": _xml("1180012345", "Møller", "FV/A"),
        "NR-2": _xml("1180012346", "Moller", "FV/B"),
    }, rejestr)

    kody = [ln for ln in content.splitlines() if ln.startswith("kod =")]
    assert len(kody) == 2 and len(set(k.upper() for k in kody)) == 2
    assert "Konto =202-2-727" in content and "Konto =202-2-728" in content
    assert "kod =Moller~2" in content
    rejestr.close()


def test_kody_unikalne_po_usunieciu_nowej_linii(tmp_path):
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.ustaw(PROFIL, "INNY", 726, nazwa="Inny", status="auto")

    _, content = _eksport_wielu(tmp_path, {
        "NR-1": _xml("1180012345", "ACME&#10;Oddzial", "FV/A"),
        "NR-2": _xml("1180012346", "ACME Oddzial", "FV/B"),
    }, rejestr)

    kody = [ln for ln in content.splitlines() if ln.startswith("kod =")]
    assert len(set(k.upper() for k in kody)) == 2
    rejestr.close()


def test_opis_gdy_pierwsza_faktura_bez_nazwy(tmp_path):
    """Pierwsza faktura bez nazwy sprzedawcy, druga z nazwą — obiekt karty
    współdzielony z pierwszym dokumentem zostaje uzupełniony przy scalaniu;
    opis obu dokumentów musi mimo to dostać nazwę z kartoteki."""
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.ustaw(PROFIL, "1180012345", 727, nazwa="ACME FK", nip="1180012345", status="auto")

    _, content = _eksport_wielu(tmp_path, {
        "NR-1": _xml("1180012345", "", "FV/A"),
        "NR-2": _xml("1180012345", "ACME One", "FV/B"),
    }, rejestr)

    assert "opis FK =Faktura FV/A - ACME FK" in content
    assert "opis FK =Faktura FV/B - ACME FK" in content
    rejestr.close()


def test_sync_sql_zmiana_pozycji_i_nazwy_w_jednym_przebiegu(tmp_path, monkeypatch):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1180012345", 5, nazwa="ACME BDO: 123", nip="1180012345", status="seed")
    monkeypatch.setattr(
        kontrahenci_nr_mod, "_czytaj_kartoteke_sql",
        lambda baza: [_wiersz(727, "ACME", "1180012345")],
    )

    rej.synchronizuj_z_sql(PROFIL, "BAZA")
    po_pierwszym = rej.wpis(PROFIL, "1180012345")
    rej.synchronizuj_z_sql(PROFIL, "BAZA")

    assert (po_pierwszym.pozycja, po_pierwszym.nazwa) == (727, "ACME")
    assert rej.wpis(PROFIL, "1180012345") == po_pierwszym   # idempotencja
    rej.close()


def test_sync_sql_zmiana_pozycji_wpis_manual_nietkniety(tmp_path, monkeypatch):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1180012345", 5, nazwa="OD KSIĘGOWEJ", nip="1180012345", status="manual")
    monkeypatch.setattr(
        kontrahenci_nr_mod, "_czytaj_kartoteke_sql",
        lambda baza: [_wiersz(727, "ACME", "1180012345")],
    )

    rej.synchronizuj_z_sql(PROFIL, "BAZA")

    w = rej.wpis(PROFIL, "1180012345")
    assert (w.pozycja, w.nazwa) == (5, "OD KSIĘGOWEJ")
    rej.close()


def test_nowa_karta_numer_max_plus_1(tmp_path):
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.ustaw(PROFIL, "INNY", 40, nazwa="Inny", status="auto")

    report, content = _eksport(
        tmp_path, (FIXTURES / "fa3_basic_23.xml").read_bytes(), rejestr, {Kind.COST}
    )

    assert report.nowi_kontrahenci[0][2] == 41
    assert "Konto =202-2-41" in content
    rejestr.close()


# --------------------------------------------------------------------------- #
# kod karty z FK (skrot) — decyzja Bartka 29.09: wysyłać prawdziwy kod karty
# z Symfonii; gdy nieznany, kod jak przed zmianą (z nazwy z faktury)

def test_kod_karty_z_fk_ma_pierwszenstwo(tmp_path):
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.ustaw(PROFIL, "1180012345", 727, nazwa="ACME POLSKA SP. Z O.O.",
                  nip="1180012345", status="auto")
    rejestr._conn.execute("UPDATE numery SET skrot='ACME', skrot_poz=727 WHERE klucz='1180012345'")

    _, content = _eksport(tmp_path, _xml_z_nazwa_sprzedawcy("ACME"), rejestr, {Kind.COST})

    assert "kod =ACME\r" in content or "kod =ACME\n" in content
    assert "khnazwa =ACME POLSKA SP. Z O.O." in content
    rejestr.close()


def test_kod_z_fk_zachowany_przy_kolizji_z_nowa_karta(tmp_path):
    """Nowa karta, której liczony kod koliduje z kodem istniejącej karty z FK —
    sufiks dostaje nowa karta, kod z FK zostaje bez zmian."""
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.ustaw(PROFIL, "1180012346", 728, nazwa="ACME FK", nip="1180012346", status="auto")
    rejestr._conn.execute("UPDATE numery SET skrot='ACME', skrot_poz=728 WHERE klucz='1180012346'")

    _, content = _eksport_wielu(tmp_path, {
        "NR-1": _xml("1180012345", "ACME", "FV/A"),        # nowa karta, kod liczony „ACME”
        "NR-2": _xml("1180012346", "ACME inna", "FV/B"),   # istniejąca, kod FK „ACME”
    }, rejestr)

    kody = [ln for ln in content.splitlines() if ln.startswith("kod =")]
    assert sorted(kody) == ["kod =ACME", "kod =ACME~2"]
    blok_fk = content[content.index("id =1180012346"):]
    assert blok_fk.splitlines()[1] == "kod =ACME"
    rejestr.close()


def test_sync_sql_zapisuje_kod_karty(tmp_path, monkeypatch):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1180012345", 727, nazwa="ACME", nip="1180012345", status="seed")
    monkeypatch.setattr(
        kontrahenci_nr_mod, "_czytaj_kartoteke_sql",
        lambda baza: [{"pozycja": 727, "kod": "ACME-KOD", "nazwa": "ACME",
                       "nip": "1180012345", "miejscowosc": ""}],
    )

    rej.synchronizuj_z_sql(PROFIL, "BAZA")

    assert rej.wpis(PROFIL, "1180012345").skrot == "ACME-KOD"
    rej.close()


def test_sync_nie_zapisuje_kodu_wpisowi_manual_z_inna_pozycja(tmp_path, monkeypatch):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1180012345", 5, nazwa="ACME", nip="1180012345", status="manual")
    monkeypatch.setattr(
        kontrahenci_nr_mod, "_czytaj_kartoteke_sql",
        lambda baza: [{"pozycja": 727, "kod": "ACME-KOD", "nazwa": "ACME",
                       "nip": "1180012345", "miejscowosc": ""}],
    )

    rej.synchronizuj_z_sql(PROFIL, "BAZA")

    assert rej.wpis(PROFIL, "1180012345").skrot == ""
    rej.close()


def test_rejestr_sprzed_zmiany_dostaje_kolumne_skrot(tmp_path):
    import sqlite3
    db = tmp_path / "stary.db"
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE numery(profil_nip TEXT, klucz TEXT, pozycja INTEGER,
                 nazwa TEXT DEFAULT '', nip TEXT DEFAULT '', miejscowosc TEXT DEFAULT '',
                 status TEXT DEFAULT 'auto', added_at TEXT, PRIMARY KEY (profil_nip, klucz))""")
    c.execute("INSERT INTO numery VALUES (?,?,?,?,?,?,?,?)",
              (PROFIL, "1180012345", 727, "ACME", "1180012345", "", "auto", "x"))
    c.commit()
    c.close()

    rej = RejestrNumerow(db)

    w = rej.wpis(PROFIL, "1180012345")
    assert (w.pozycja, w.nazwa, w.skrot) == (727, "ACME", "")
    assert [x.klucz for x in rej.lista(PROFIL)] == ["1180012345"]
    rej.close()
    RejestrNumerow(db).close()   # drugi start: kolumna już jest, bez błędu



def _sync(rej, monkeypatch, wiersze):
    monkeypatch.setattr(kontrahenci_nr_mod, "_czytaj_kartoteke_sql", lambda baza: wiersze)
    return rej.synchronizuj_z_sql(PROFIL, "BAZA")


def _w(poz, kod, nazwa="ACME", nip="1180012345"):
    return {"pozycja": poz, "kod": kod, "nazwa": nazwa, "nip": nip, "miejscowosc": ""}


def test_sync_nie_zapisuje_kodu_wpisowi_manual_na_tej_samej_pozycji(tmp_path, monkeypatch):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1180012345", 7, nazwa="ACME", nip="1180012345", status="manual")

    _sync(rej, monkeypatch, [_w(7, "NOWY")])

    assert rej.wpis(PROFIL, "1180012345").skrot == ""
    rej.close()


def test_sync_pusty_kod_w_fk_czysci_zapamietany(tmp_path, monkeypatch):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1180012345", 7, nazwa="ACME", nip="1180012345", status="seed")
    _sync(rej, monkeypatch, [_w(7, "STARY")])
    assert rej.wpis(PROFIL, "1180012345").skrot == "STARY"

    _sync(rej, monkeypatch, [_w(7, "")])

    assert rej.wpis(PROFIL, "1180012345").skrot == ""
    rej.close()


def test_zmiana_numeru_karty_uniewaznia_kod(tmp_path, monkeypatch):
    """Kod należy do karty o danej pozycji — po zmianie numeru w tabeli (ustaw)
    zapamiętany kod nie jest już używany."""
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1180012345", 7, nazwa="ACME", nip="1180012345", status="seed")
    _sync(rej, monkeypatch, [_w(7, "STARY")])

    rej.ustaw(PROFIL, "1180012345", 9, nazwa="ACME", nip="1180012345", status="manual")

    assert rej.wpis(PROFIL, "1180012345").skrot == ""
    rej.close()


def test_sync_liczy_zmiane_samego_kodu(tmp_path, monkeypatch):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.ustaw(PROFIL, "1180012345", 7, nazwa="ACME", nip="1180012345", status="seed")

    assert _sync(rej, monkeypatch, [_w(7, "KOD")]).zaktualizowane == 1
    assert _sync(rej, monkeypatch, [_w(7, "KOD")]).zaktualizowane == 0
    assert _sync(rej, monkeypatch, [_w(7, "INNY")]).zaktualizowane == 1
    rej.close()


def test_kody_fk_nigdy_nie_dostaja_sufiksu(tmp_path):
    from app.hmffk import HKontrahent, _unikalne_skroty
    wynik = _unikalne_skroty([
        HKontrahent(klucz="1", kod="Møller"),
        HKontrahent(klucz="2", kod="Moller"),
        HKontrahent(klucz="3", nazwa="Moller"),
    ])
    assert wynik["1"] == "Møller" and wynik["2"] == "Moller"
    assert wynik["3"] == "Moller~2"
