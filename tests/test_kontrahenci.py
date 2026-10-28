"""Testy importu kontrahentów — format sekcyjny AMS + własny szablon."""

from datetime import date
from pathlib import Path

from app.config import Profile
from app.exporter import Kind, run_export
from app.fa_mapper import InvoiceContext, map_fa3_to_ksef_invoice
from app.kontrahenci import (
    TEMPLATE_AMS,
    WERSJA_SZABLONU,
    Kontrahent,
    build_kontrahenci_ams,
    kontrahenci_from_invoice,
    merge_kontrahent,
)
from app.ledger import Ledger
from tests.test_exporter import StubClient, _meta

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CTX_KOSZT = InvoiceContext(
    ksef_number="5252445719-20260115-A1B2C3D4E5F6-7A",
    ksef_date="2026-01-15",
    subject_type="Subject2",
)


def _karta(**kwargs) -> Kontrahent:
    base = dict(
        nip="1180012345", nazwa="Dostawca Testowy Sp. z o.o.", kod_kraju="PL",
        adres_l1="ul. Przykladowa 12 lok. 3", adres_l2="00-001 Warszawa",
        email="biuro@x.pl", telefon="221234567",
        ulica="Przykladowa", nr_domu="12", nr_lokalu="3",
        kod_pocztowy="00-001", miejscowosc="Warszawa",
    )
    base.update(kwargs)
    return Kontrahent(**base)


def test_obie_strony_faktury_bez_wlasnego_nip():
    result = map_fa3_to_ksef_invoice((FIXTURES / "fa3_basic_23.xml").read_bytes(), CTX_KOSZT)
    # nasz NIP = 5252445719 (nabywca) -> zostaje tylko sprzedawca
    karty = kontrahenci_from_invoice(result.element, "5252445719")
    assert [k.nip for k in karty] == ["1180012345"]
    k = karty[0]
    assert (k.ulica, k.nr_domu, k.nr_lokalu) == ("Przykladowa", "12", "3")
    assert (k.kod_pocztowy, k.miejscowosc) == ("00-001", "Warszawa")
    # bez filtra własnego NIP: obie strony
    assert len(kontrahenci_from_invoice(result.element, "9999999999")) == 2


def test_klucz_fallback_hash_nigdy_pusty_ani_zero():
    bez_nip = _karta(nip="", nazwa="Beispiel GmbH", miejscowosc="Berlin")
    assert bez_nip.klucz.startswith("K_")
    assert len(bez_nip.klucz) == 14
    # stabilny między przebiegami
    assert bez_nip.klucz == _karta(nip="", nazwa="Beispiel GmbH", miejscowosc="Berlin").klucz
    zero = _karta(nip="0", nazwa="Zerowy")
    assert zero.klucz != "0" and zero.klucz.startswith("K_")


def test_dedup_preferuje_pelniejszy_rekord():
    pelny = _karta()
    goly = _karta(ulica="", nr_domu="", kod_pocztowy="", miejscowosc="", email="", telefon="")
    assert merge_kontrahent(goly, pelny) is pelny
    assert merge_kontrahent(pelny, goly) is pelny
    text = build_kontrahenci_ams([goly, pelny, pelny])
    assert text.count("Kontrahent{") == 1        # 1 sekcja per unikalny klucz
    assert "Ulica =Przykladowa" in text


def test_build_ams_struktura_i_wersja():
    text = build_kontrahenci_ams([_karta(nazwa="Firma {X} Sp. z o.o.")])
    assert text.startswith("INFO{")
    assert f"Wersja szablonu ={WERSJA_SZABLONU}" in text
    assert "klucz =1180012345" in text
    assert "NIP =1180012345" in text
    assert "Miejscowosc =Warszawa" in text
    assert "KodPocztowy =00-001" in text
    assert "NrDomu =12" in text and "NrLokalu =3" in text
    assert "aktywny =1" in text
    # klamry w nazwie nie psują struktury sekcji
    assert "Firma (X) Sp. z o.o." in text
    assert text.count("{") == text.count("}")


def test_template_ams_konwencja():
    assert TEMPLATE_AMS.startswith("#opis")               # nazwa na liście szablonów
    assert '#format  "AMS"' in TEMPLATE_AMS               # listuje się jak inne szablony
    assert '#DANEKH  "1"' in TEMPLATE_AMS                 # tworzenie kart jak w hmffk
    assert 'Section("Kontrahent", "Kontrahent")' in TEMPLATE_AMS
    assert f'equal([Wersja szablonu], "{WERSJA_SZABLONU}")' in TEMPLATE_AMS
    # każde pole z pliku danych ma odbiorcę w szablonie
    for field in ("[klucz]", "[skrot]", "[nazwa]", "[NIP]", "[KodKraju]",
                  "[Miejscowosc]", "[KodPocztowy]", "[Ulica]", "[NrDomu]",
                  "[NrLokalu]", "[Email]", "[Telefon]", "[AdresL1]", "[AdresL2]"):
        assert field in TEMPLATE_AMS, field


def test_export_writes_ams_and_template(tmp_path):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001"), _meta("NR-0002")]  # ten sam dostawca -> 1 karta
    client = StubClient(metas, {m.ksef_number: xml for m in metas})
    ledger = Ledger(tmp_path / "ledger.db")

    report = run_export(
        Profile("F", "5252445719", "tok", "firma-a"), client, ledger,
        date(2026, 1, 1), date(2026, 1, 31), {Kind.COST}, tmp_path / "out",
        kontrahenci_file=True,
    )
    assert report.kontrahenci == 1
    karty_dir = tmp_path / "out" / "firma-a" / "kartoteka"
    files = list(karty_dir.glob("kontrahenci_firma-a_*.TXT"))
    assert len(files) == 1
    content = files[0].read_bytes().decode("cp1250")
    assert content.count("Kontrahent{") == 1
    assert "klucz =1180012345" in content
    assert "Miejscowosc =Warszawa" in content
    assert "\r\n" in content
    szablon = (karty_dir / "szablon" / "SKsefKontrahenciImport.AMS").read_bytes().decode("cp1250")
    assert szablon.startswith("#opis")
    assert '#format  "AMS"' in szablon
    ledger.close()
