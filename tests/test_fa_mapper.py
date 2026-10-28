"""Testy fa_mapper: walidacja fixtures schemą FA(3), mapowanie, pełny pipeline do AMS."""

import re
from pathlib import Path

import pytest
from lxml import etree

from app.ams_transform import AmsTransformer
from app.fa_mapper import Fa3MappingError, InvoiceContext, map_fa3_to_ksef_invoice

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
SAMPLES = PROJECT_ROOT / "samples"

ALL_FIXTURES = [
    "fa3_basic_23.xml",
    "fa3_multi_rate.xml",
    "fa3_kor.xml",
    "fa3_eur.xml",
    "fa3_no_nip.xml",
]

CTX_KOSZT = InvoiceContext(
    ksef_number="5252445719-20260115-A1B2C3D4E5F6-7A",
    ksef_date="2026-01-15",
    subject_type="Subject2",
)


@pytest.fixture(scope="module")
def fa3_schema() -> etree.XMLSchema:
    return etree.XMLSchema(
        etree.parse(str(PROJECT_ROOT / "reference" / "fa3-xsd" / "schemat_FA(3)_v1-0E.xsd"))
    )


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_fixtures_valid_against_xsd(fa3_schema, name):
    doc = etree.parse(str(FIXTURES / name))
    assert fa3_schema.validate(doc), str(fa3_schema.error_log)


def _xpath1(el, path):
    found = el.findall(path)
    assert len(found) == 1, f"{path}: {len(found)} wystąpień"
    return found[0].text


def test_basic_mapping_koszt():
    result = map_fa3_to_ksef_invoice(_load("fa3_basic_23.xml"), CTX_KOSZT)
    inv = result.element
    assert _xpath1(inv, "invoiceBound") == "EXTERNAL"
    assert _xpath1(inv, "KsefNumber") == CTX_KOSZT.ksef_number
    assert _xpath1(inv, "KSeFDate") == "2026-01-15"
    assert _xpath1(inv, "KSeFStatus") == "approved"
    assert re.fullmatch(
        r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}",
        _xpath1(inv, "eArchiveId"),
    )
    assert _xpath1(inv, "invoiceV2/Naglowek/Numer") == "FV/2026/01/0007"
    assert _xpath1(inv, "invoiceV2/Naglowek/DataDostawy") == "2026-01-14"
    assert _xpath1(inv, "invoiceV2/Sprzedawca/DaneIdentyfikacyjne/NIP") == "1180012345"
    assert _xpath1(inv, "invoiceV2/Nabywca/DaneIdentyfikacyjne/NIP") == "5252445719"
    assert _xpath1(inv, "invoiceV2/PodsumowanieVat/PodsumowanieVat/WartoscNetto") == "1000.00"
    assert _xpath1(inv, "invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVatKurs") == "230.00"
    assert _xpath1(inv, "invoiceV2/Wiersze/WierszV2/KursWaluty") == "1"
    assert _xpath1(inv, "invoiceV2/Adnotacje/PodzielonaPlatnosc") == "false"
    assert result.warnings == []


def test_earchiveid_fresh_per_generation():
    """Każdy wygenerowany plik ma ŚWIEŻY GUID — ten sam dokument zaimportowany
    ponownie nie koliduje z egzemplarzem siedzącym już w buforze FK."""
    a = map_fa3_to_ksef_invoice(_load("fa3_basic_23.xml"), CTX_KOSZT)
    b = map_fa3_to_ksef_invoice(_load("fa3_basic_23.xml"), CTX_KOSZT)
    assert _xpath1(a.element, "eArchiveId") != _xpath1(b.element, "eArchiveId")


def test_pipeline_matches_golden_koszt():
    """W2: FA(3) -> mapper -> XSLT Symfonii -> DOMYŚLNIE bajt-w-bajt jak golden
    (poza losowym GUID). Plik faktury == sprawdzony w Symfonii format."""
    result = map_fa3_to_ksef_invoice(_load("fa3_basic_23.xml"), CTX_KOSZT)
    txt = AmsTransformer().to_txt_bytes(result.element).decode("cp1250")
    golden = (SAMPLES / "ksefInvoice-koszt.TXT").read_bytes().decode("cp1250")
    normalize = lambda s: re.sub(r"eArchiveId = [0-9A-Fa-f-]{36}", "eArchiveId = <GUID>", s)
    assert normalize(txt) == normalize(golden)
    assert "Miejscowosc" not in txt  # domyślnie BEZ pól strukturalnych (klucz kontrahenta!)


def test_pipeline_structured_address_opt_in():
    """Opcjonalnie (--adresy-strukturalne): pola strukturalne w Kontrahent{}."""
    result = map_fa3_to_ksef_invoice(
        _load("fa3_basic_23.xml"), CTX_KOSZT, structured_address=True
    )
    txt = AmsTransformer().to_txt_bytes(result.element).decode("cp1250")
    assert "Ulica = Przykladowa" in txt
    assert "NrDomu = 12" in txt
    assert "NrLokalu = 3" in txt
    assert "KodPocztowy = 00-001" in txt
    assert "Miejscowosc = Warszawa" in txt


def test_multi_rate_buckets():
    result = map_fa3_to_ksef_invoice(_load("fa3_multi_rate.xml"), CTX_KOSZT)
    buckets = result.element.findall("invoiceV2/PodsumowanieVat/PodsumowanieVat")
    got = {b.findtext("StawkaPodatku"): b.findtext("WartoscNetto") for b in buckets}
    assert got == {"23": "1000.00", "8": "200.00", "zw": "50.00"}
    vat = {b.findtext("StawkaPodatku"): b.findtext("KwotaVat") for b in buckets}
    assert vat == {"23": "230.00", "8": "16.00", "zw": "0.00"}


def test_kor_negative_and_reference():
    result = map_fa3_to_ksef_invoice(_load("fa3_kor.xml"), CTX_KOSZT)
    inv = result.element
    assert _xpath1(inv, "invoiceV2/Naglowek/RodzajFaktury") == "KOR"
    assert _xpath1(inv, "invoiceV2/PodsumowanieVat/PodsumowanieVat/WartoscNetto") == "-100.00"
    kor = inv.find("invoiceV2/Korekta/FaKorygowana/DaneFaKorygowanejV2")
    assert kor is not None
    assert kor.findtext("NrFaKorygowanej") == "FV/2026/01/0007"
    assert kor.findtext("DataWystFaKorygowanej") == "2026-01-15"
    assert kor.findtext("NrKSeFFaKorygowanej") == "1180012345-20260115-A1B2C3D4E5F6-7A"


def test_eur_kurs_and_warning():
    result = map_fa3_to_ksef_invoice(_load("fa3_eur.xml"), CTX_KOSZT)
    inv = result.element
    assert _xpath1(inv, "invoiceV2/Naglowek/KodWaluty") == "EUR"
    assert _xpath1(inv, "invoiceV2/Wiersze/WierszV2/KursWaluty") == "4.3"
    assert _xpath1(inv, "invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVat") == "230.00"
    assert _xpath1(inv, "invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVatKurs") == "989.00"
    assert any("walutowa" in w for w in result.warnings)


def test_no_nip_buyer_wdt():
    ctx = InvoiceContext(
        ksef_number="5252445719-20260511-B2C3D4E5F6A1-8B",
        ksef_date="2026-05-11",
        subject_type="Subject1",
    )
    result = map_fa3_to_ksef_invoice(_load("fa3_no_nip.xml"), ctx)
    inv = result.element
    assert _xpath1(inv, "invoiceBound") == "INTERNAL"
    assert _xpath1(inv, "invoiceV2/Nabywca/DaneIdentyfikacyjne/NIP") == ""
    assert _xpath1(inv, "invoiceV2/Nabywca/DaneIdentyfikacyjne/BrakId") == "true"
    assert _xpath1(inv, "invoiceV2/Nabywca/DaneIdentyfikacyjne/PelnaNazwa") == "Beispiel GmbH"
    bucket = inv.find("invoiceV2/PodsumowanieVat/PodsumowanieVat")
    assert bucket.findtext("StawkaPodatku") == "0"
    assert bucket.findtext("WartoscNetto") == "5000.00"
    assert any("zagraniczny" in w for w in result.warnings)


def _mutate(name: str, old: str, new: str) -> bytes:
    return _load(name).replace(old.encode(), new.encode())


def test_unsupported_marza_rejected():
    data = _mutate("fa3_basic_23.xml", "<P_15>1230.00</P_15>", "<P_13_11>500.00</P_13_11><P_15>1230.00</P_15>")
    with pytest.raises(Fa3MappingError, match="marży"):
        map_fa3_to_ksef_invoice(data, CTX_KOSZT)


def test_missing_number_rejected():
    data = _mutate("fa3_basic_23.xml", "<P_2>FV/2026/01/0007</P_2>", "")
    with pytest.raises(Fa3MappingError, match="P_2"):
        map_fa3_to_ksef_invoice(data, CTX_KOSZT)


def test_all_fixtures_flow_through_xslt():
    """Property: każda poprawna fixture przechodzi przez XSLT bez wyjątku i daje Dokument{...}."""
    transformer = AmsTransformer()
    for name in ALL_FIXTURES:
        result = map_fa3_to_ksef_invoice(_load(name), CTX_KOSZT)
        text = transformer.to_ams_text(result.element)
        assert "Dokument{" in text, name
        assert "Wersja szablonu =3.1" in text, name
