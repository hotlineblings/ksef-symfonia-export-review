"""Testy paczki zbiorczej: wiele ksefInvoice w jednym pliku AMS .TXT."""

from datetime import date
from pathlib import Path

from lxml import etree

from app.ams_transform import AmsTransformer
from app.config import Profile
from app.exporter import Kind, run_export
from app.ledger import Ledger
from tests.test_exporter import StubClient, _meta

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = PROJECT_ROOT / "samples"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def test_batch_transform_equals_concatenation():
    """Paczka z 2 faktur == INFO{} + sekcje Dokument{} obu pojedynczych plików."""
    transformer = AmsTransformer()
    koszt = etree.parse(str(SAMPLES / "ksefInvoice-koszt-EXTERNAL.xml")).getroot()
    sprzedaz = etree.parse(str(SAMPLES / "ksefInvoice-sprzedaz-INTERNAL.xml")).getroot()

    single_koszt = transformer.to_txt_bytes(koszt).decode("cp1250")
    single_sprzedaz = transformer.to_txt_bytes(sprzedaz).decode("cp1250")

    root = etree.Element("ksefInvoices")
    root.append(koszt)
    root.append(sprzedaz)
    batch = transformer.to_txt_bytes(root).decode("cp1250")

    assert batch.count("INFO{") == 1
    assert batch.count("Dokument{") == 2
    assert batch.count("Wersja szablonu =3.1") == 1
    # każda sekcja Dokument{ z plików pojedynczych występuje w paczce dosłownie
    dok_koszt = single_koszt[single_koszt.index("Dokument{"):]
    dok_sprzedaz = single_sprzedaz[single_sprzedaz.index("Dokument{"):]
    assert dok_koszt in batch
    assert dok_sprzedaz in batch
    # kolejność zachowana: koszt przed sprzedażą
    assert batch.index(dok_koszt) < batch.index(dok_sprzedaz)


def test_export_combined_single_file(tmp_path):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001"), _meta("NR-0002"), _meta("NR-0003")]
    client = StubClient(metas, {m.ksef_number: xml for m in metas})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", combined=True,
    )
    assert (report.found, report.exported, report.failed) == (3, 3, [])
    out = tmp_path / "out" / "firma-a"
    files = list(out.glob("*.TXT"))
    assert len(files) == 1
    assert files[0].name.startswith("paczka_koszt_2026-01-01_2026-01-31_")
    content = files[0].read_bytes().decode("cp1250")
    assert content.count("INFO{") == 1
    assert content.count("Dokument{") == 3
    for nr in ("NR-0001", "NR-0002", "NR-0003"):
        assert f"KsefNumber = {nr}" in content
        assert ledger.is_done("5252445719", nr)

    # tryb przyrostowy: brak nowej paczki
    report2 = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", combined=True, only_new=True,
    )
    assert (report2.exported, report2.skipped_dedup) == (0, 3)
    assert len(list(out.glob("*.TXT"))) == 1

    # tryb pełny (domyślny): nowa paczka z kompletem 3 faktur
    report3 = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", combined=True,
    )
    assert (report3.exported, report3.re_exported) == (3, 3)
    ledger.close()


def test_export_combined_failed_invoice_excluded(tmp_path):
    """Błędna faktura nie trafia do paczki, ale nie blokuje pozostałych."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001"), _meta("NR-0002")]
    client = StubClient(metas, {"NR-0001": b"<zepsuty", "NR-0002": xml})
    ledger = Ledger(tmp_path / "ledger.db")

    report = run_export(
        Profile("F", "5252445719", "tok", "f"), client, ledger,
        date(2026, 1, 1), date(2026, 1, 31), {Kind.COST}, tmp_path / "out",
        combined=True,
    )
    assert report.exported == 1
    assert len(report.failed) == 1
    files = list((tmp_path / "out" / "f").glob("*.TXT"))
    assert len(files) == 1
    content = files[0].read_bytes().decode("cp1250")
    assert "NR-0002" in content and "NR-0001" not in content
    assert not ledger.is_done("5252445719", "NR-0001")
    ledger.close()
