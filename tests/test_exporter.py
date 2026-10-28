"""Test exportera na stubie klienta KSeF (bez sieci)."""

import threading
from dataclasses import replace
from datetime import date
from pathlib import Path

from app.config import Profile
from app.exporter import ExportReport, Kind, run_export
from app.fa_mapper import InvoiceContext  # noqa: F401  (dokumentacja kontraktu)
from app.kontrahenci_nr import RejestrNumerow
from app.ksef_client import InvoiceMetadata
from app.ledger import Ledger

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _meta(
    nr: str, day: str = "2026-01-15", invoice_number: str = "FV/2026/01/0007"
) -> InvoiceMetadata:
    return InvoiceMetadata(
        ksef_number=nr,
        invoice_number=invoice_number,
        issue_date=f"{day}T00:00:00+00:00",
        invoicing_date=f"{day}T12:00:00+00:00",
        acquisition_date=None,
        seller_nip="1180012345",
        seller_name="Dostawca Testowy Sp. z o.o.",
        buyer_id="5252445719",
        buyer_name="Nasza Firma Sp. z o.o.",
        net=None, gross=None, vat=None,
        currency="PLN",
        form_system_code="FA (3)",
        invoice_type="Vat",
    )


class StubClient:
    def __init__(self, metas, xml_by_nr):
        self._metas = metas
        self._xml = xml_by_nr
        self.auth_called = False
        self.downloaded = []  # kolejność wywołań download_invoice

    def authenticate(self):
        self.auth_called = True

    def query_metadata(self, subject_type, date_from, date_to, **_):
        yield from self._metas

    def download_invoice(self, ksef_number):
        self.downloaded.append(ksef_number)
        return self._xml[ksef_number]

    def download_budget_remaining(self):
        return 64


class KindStubClient:
    """Stub zwracający RÓŻNE metadane per rodzaj (subject_type) — do testu
    jednoczesnego szukania numeru w sprzedaży i koszcie."""

    def __init__(self, metas_by_kind, xml_by_nr):
        self._metas_by_kind = metas_by_kind
        self._xml = xml_by_nr
        self.auth_called = False
        self.downloaded = []

    def authenticate(self):
        self.auth_called = True

    def query_metadata(self, subject_type, date_from, date_to, **_):
        yield from self._metas_by_kind.get(subject_type, [])

    def download_invoice(self, ksef_number):
        self.downloaded.append(ksef_number)
        return self._xml[ksef_number]

    def download_budget_remaining(self):
        return 64


class BatchStubClient(StubClient):
    """Stub z eksportem wsadowym — do testu progu BATCH_DOWNLOAD_THRESHOLD."""

    def __init__(self, metas, xml_by_nr):
        super().__init__(metas, xml_by_nr)
        self.single_downloads = 0
        self.batch_used = False

    def download_invoice(self, ksef_number):
        self.single_downloads += 1
        return super().download_invoice(ksef_number)

    def request_export(self, subject_type, date_from, date_to, *, date_type="Issue"):
        self.batch_used = True
        return "EXP-1"

    def wait_for_export(self, reference):
        return {"parts": [], "isTruncated": False, "invoiceCount": len(self._xml)}

    def iter_export_invoices(self, reference, package):
        yield from self._xml.items()


def test_batch_download_used_above_threshold(tmp_path):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta(f"NR-{i:04d}") for i in range(1, 13)]  # 12 > próg 10
    client = BatchStubClient(metas, {m.ksef_number: xml for m in metas})
    ledger = Ledger(tmp_path / "ledger.db")

    report = run_export(
        Profile("F", "5252445719", "tok", "f"), client, ledger,
        date(2026, 1, 1), date(2026, 1, 31), {Kind.COST}, tmp_path / "out",
    )
    assert client.batch_used
    assert client.single_downloads == 0
    assert (report.found, report.exported, report.failed) == (12, 12, [])
    assert len(list((tmp_path / "out" / "f").glob("*.TXT"))) == 12
    ledger.close()


def test_single_download_below_threshold(tmp_path):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta(f"NR-{i:04d}") for i in range(1, 4)]  # 3 <= próg
    client = BatchStubClient(metas, {m.ksef_number: xml for m in metas})
    ledger = Ledger(tmp_path / "ledger.db")

    report = run_export(
        Profile("F", "5252445719", "tok", "f"), client, ledger,
        date(2026, 1, 1), date(2026, 1, 31), {Kind.COST}, tmp_path / "out",
    )
    assert not client.batch_used
    assert client.single_downloads == 3
    assert report.exported == 3
    ledger.close()


def test_full_loop_with_dedup(tmp_path):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001"), _meta("NR-0002")]
    client = StubClient(metas, {"NR-0001": xml, "NR-0002": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out",
    )
    assert client.auth_called
    assert (report.found, report.exported, report.skipped_dedup) == (2, 2, 0)
    out = tmp_path / "out" / "firma-a"
    files = sorted(p.name for p in out.glob("*.TXT"))
    assert files == ["NR-0001.TXT", "NR-0002.TXT"]
    content = (out / "NR-0001.TXT").read_bytes().decode("cp1250")
    assert "rodzajDok = zakupu" in content
    assert "KsefNumber = NR-0001" in content
    assert "kwota =1230" in content  # MA brutto (dekret kosztowy)

    # drugi przebieg (domyślny = pełny): KSeF mówi 2 -> zapisujemy znowu 2 pliki
    import re as _re
    strip_guid = lambda b: _re.sub(rb"eArchiveId = [0-9A-F-]{36}", b"", b)
    before = (out / "NR-0001.TXT").read_bytes()
    report2 = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out",
    )
    assert (report2.found, report2.exported, report2.re_exported) == (2, 2, 2)
    after = (out / "NR-0001.TXT").read_bytes()
    assert strip_guid(after) == strip_guid(before)  # treść ta sama...
    assert after != before  # ...ale świeży eArchiveId (ponowny import nie koliduje)

    # tryb przyrostowy: pomija już pobrane
    report3 = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", only_new=True,
    )
    assert (report3.found, report3.exported, report3.skipped_dedup) == (2, 0, 2)
    ledger.close()


def test_failed_invoice_does_not_stop_export(tmp_path):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001"), _meta("NR-0002")]
    client = StubClient(metas, {"NR-0001": b"<zepsuty", "NR-0002": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out",
    )
    assert report.exported == 1
    assert len(report.failed) == 1
    assert report.failed[0][0] == "NR-0001"
    assert not ledger.is_done("5252445719", "NR-0001")  # błędna NIE oznaczona jako zrobiona
    assert ledger.is_done("5252445719", "NR-0002")
    ledger.close()


def test_cancel_between_invoices(tmp_path):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta(f"NR-{i:04d}") for i in range(1, 6)]
    client = StubClient(metas, {m.ksef_number: xml for m in metas})
    ledger = Ledger(tmp_path / "ledger.db")
    cancel = threading.Event()

    def progress(done, total, nr):
        if done == 2:
            cancel.set()

    report = run_export(
        Profile("F", "5252445719", "tok", "f"), client, ledger,
        date(2026, 1, 1), date(2026, 1, 31), {Kind.COST}, tmp_path / "out",
        on_progress=progress, cancel=cancel,
    )
    assert report.cancelled
    assert report.exported < 5
    ledger.close()

def test_hmffk_jeden_plik_z_kontrahentami(tmp_path):
    """Tryb hmffk: JEDEN plik na eksport — Kontrahent{} + dokument{} razem."""
    xml_a = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    xml_b = (FIXTURES / "fa3_multi_rate.xml").read_bytes()
    metas = [_meta("NR-0001"), _meta("NR-0002")]
    client = StubClient(metas, {"NR-0001": xml_a, "NR-0002": xml_b})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk",
    )
    assert (report.found, report.exported, report.failed) == (2, 2, [])
    assert report.kontrahenci == 1  # obie faktury od tego samego dostawcy
    out = tmp_path / "out" / "firma-a"
    files = list(out.glob("hmffk_*.TXT"))
    assert len(files) == 1
    content = files[0].read_bytes().decode("cp1250")
    assert content.startswith("INFO{\r\nWersja szablonu =4\r\n")
    assert content.count("Kontrahent{") == 1
    assert content.count("dokument{") == 2
    assert "rodzaj_dok =zakupu" in content
    assert "Dane sprzedawcy{" in content
    assert "khid =1180012345" in content
    assert "KsefNumber =NR-0001" in content
    assert ledger.is_done("5252445719", "NR-0001")
    assert ledger.is_done("5252445719", "NR-0002")

    # tryb przyrostowy po pełnym eksporcie: nic nowego -> bez pliku
    report2 = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", only_new=True,
    )
    assert (report2.exported, report2.skipped_dedup) == (0, 2)
    assert len(list(out.glob("hmffk_*.TXT"))) == 1
    ledger.close()


def test_hmffk_stawka_zw_end_to_end(tmp_path):
    """Sprzedaż ze stawką 'zw' (zwolniona, P_13_7) -> w pliku AMS 'stawka =-1'
    (konwencja Symfonii, app/fa_mapper.py:76-77 / app/hmffk.py::_stawka_ams),
    NIGDY dosłowne 'stawka =zw' (szablon hmffk.AMS sparsowałby to liczbowo jako 0
    -- błąd zgłoszony z produkcji)."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    xml = xml.replace(b"<P_13_1>1000.00</P_13_1>", b"<P_13_7>1000.00</P_13_7>")
    xml = xml.replace(b"<P_14_1>230.00</P_14_1>", b"")
    xml = xml.replace(b"<P_15>1230.00</P_15>", b"<P_15>1000.00</P_15>")
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.SALES}, tmp_path / "out", fmt="hmffk",
    )
    assert (report.found, report.exported, report.failed) == (1, 1, [])
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    assert "stawka =-1" in content
    assert "stawka =zw" not in content
    ledger.close()


def test_hmffk_bledna_faktura_do_raportu(tmp_path):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001"), _meta("NR-0002")]
    client = StubClient(metas, {"NR-0001": b"<zepsuty", "NR-0002": xml})
    ledger = Ledger(tmp_path / "ledger.db")

    report = run_export(
        Profile("F", "5252445719", "tok", "f"), client, ledger,
        date(2026, 1, 1), date(2026, 1, 31), {Kind.COST}, tmp_path / "out",
        fmt="hmffk",
    )
    assert report.exported == 1
    assert len(report.failed) == 1
    assert not ledger.is_done("5252445719", "NR-0001")
    ledger.close()


# --------------------------------------------------------------------------- #
# konto rozrachunkowe / rejestr numerów kontrahentów (numeracja kart FK)

def test_hmffk_konto_rozrachunkowe_rejestr_pusty(tmp_path):
    """Pusty rejestr: pierwszy kontrahent dostaje pozycję 1, zapisaną w rejestrze."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    assert "Konto =202-2-1" in content  # koszt -> prefiks konto_prefix_koszt (domyślnie 202-2)
    assert rejestr.pozycja("5252445719", "1180012345") == 1
    # zapis rozrachunkowy (MA brutto) ma NumerDok/IdDlaRozliczen; fixture bez
    # Platnosc/TerminPlatnosci -> termin_fallback_dni (domyślnie 14) i tak daje
    # sekcję transakcja{} (P_1 2026-01-15 + 14 dni = 2026-01-29)
    assert "NumerDok =FV/2026/01/0007" in content
    assert "IdDlaRozliczen =1" in content
    assert "termin =2026-01-29" in content
    ledger.close()
    rejestr.close()


def test_hmffk_konto_rozrachunkowe_seed_i_max_plus_1_po_usunieciu(tmp_path):
    """Rejestr z 27 zajętymi pozycjami (inne klucze) -> nowy kontrahent dostaje 28;
    po usunięciu wpisu z pozycji 5 kolejny nowy i tak dostaje max+1 (29), NIE
    zwolnioną pozycję 5 — FK nie zasypuje dziur po usuniętych kartach."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    for i in range(1, 28):
        rejestr.przydziel("5252445719", f"INNY-{i}")  # zajmuje pozycje 1..27

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    assert rejestr.pozycja("5252445719", "1180012345") == 28
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    assert "Konto =202-2-28" in content

    rejestr.usun("5252445719", "INNY-5")
    assert rejestr.przydziel("5252445719", "NOWY-INNY") == 29  # max+1, dziura po 5 zostaje pusta
    ledger.close()
    rejestr.close()


def test_hmffk_konto_rozrachunkowe_ponowny_eksport_ta_sama_pozycja(tmp_path):
    """Ponowny eksport tej samej faktury nie przydziela nowej pozycji temu samemu kluczowi."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    assert rejestr.pozycja("5252445719", "1180012345") == 1

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    assert rejestr.pozycja("5252445719", "1180012345") == 1  # brak podwójnego przydziału

    out = tmp_path / "out" / "firma-a"
    files = list(out.glob("hmffk_*.TXT"))
    for plik in files:
        assert "Konto =202-2-1" in plik.read_bytes().decode("cp1250")
    ledger.close()
    rejestr.close()


def test_hmffk_alias_kanoniczny_scala_karte_i_konto(tmp_path):
    """Klucz kontrahenta z aliasem -> emisja pod kluczem kanonicznym z jego pozycją."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    poz_kanon = rejestr.przydziel(
        "5252445719", "9999999999", nazwa="Kanoniczna Firma Sp. z o.o.", nip="9999999999"
    )
    rejestr.dodaj_alias("5252445719", "1180012345", "9999999999", zwolnij_numer=True)

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    assert "id =9999999999" in content
    assert "id =1180012345" not in content
    assert f"Konto ={'202-2'}-{poz_kanon}" in content
    ledger.close()
    rejestr.close()


def test_hmffk_konto_prefix_niestandardowy(tmp_path):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
        konto_prefix_koszt="999-9",
    )
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    assert "Konto =999-9-1" in content
    ledger.close()
    rejestr.close()


def test_hmffk_lancuch_aliasow_rozwiazuje_do_konca(tmp_path):
    """Łańcuch aliasów A->pośredni->B: dokument klucza A dostaje kartę i konto B,
    w pliku nie ma karty ani A, ani pośredniego ogniwa (kanoniczny() spłaszcza cały
    łańcuch do punktu stałego, a nie tylko jeden poziom)."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    poz_b = rejestr.przydziel(
        "5252445719", "9999999999", nazwa="Kanoniczna Firma Sp. z o.o.", nip="9999999999"
    )
    rejestr.dodaj_alias("5252445719", "5555555555", "9999999999", zwolnij_numer=True)
    rejestr.dodaj_alias("5252445719", "1180012345", "5555555555", zwolnij_numer=True)

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    assert "id =9999999999" in content
    assert "id =1180012345" not in content
    assert "id =5555555555" not in content
    assert f"Konto ={'202-2'}-{poz_b}" in content
    ledger.close()
    rejestr.close()


def test_hmffk_dokument_brutto_zero_ostrzezenie(tmp_path):
    """Dokument, którego suma brutto (netto+VAT z rejestrów) wynosi 0 — konto
    rozrachunkowe zostaje mimo to ustawione, ale w raporcie jest ostrzeżenie,
    że linia zapisu z tym kontem się nie pojawi (kwota zerowa -> zadekretuj ręcznie)."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    xml = (
        xml.replace(b"<P_13_1>1000.00</P_13_1>", b"<P_13_1>0.00</P_13_1>")
        .replace(b"<P_14_1>230.00</P_14_1>", b"<P_14_1>0.00</P_14_1>")
        .replace(b"<P_15>1230.00</P_15>", b"<P_15>0.00</P_15>")
    )
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    assert any(
        "FV/2026/01/0007" in w and "suma brutto = 0" in w for w in report.warnings
    )
    assert rejestr.pozycja("5252445719", "1180012345") == 1  # konto mimo to przydzielone
    ledger.close()
    rejestr.close()


def test_hmffk_ostrzezenie_o_podobienstwie_nazwy(tmp_path):
    """Nowy kontrahent o nazwie różniącej się literówką od istniejącego wpisu ->
    ostrzeżenie w raporcie (do ręcznego scalenia w GUI), ale przydział mimo to następuje."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.ustaw("5252445719", "INNY-KLUCZ", 3, nazwa="Dostawca Testow Sp. z o.o.")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    assert any(
        "podobny" in w and "Dostawca Testowy" in w and "nr 3" in w for w in report.warnings
    )
    assert rejestr.pozycja("5252445719", "1180012345") is not None  # mimo ostrzeżenia: przydzielono
    ledger.close()
    rejestr.close()


def test_hmffk_raport_wymienia_nowych_kontrahentow(tmp_path):
    """Nowy kontrahent dostaje numer karty max+1 — aplikacja nie ma łączności
    z Symfonią, więc księgowa musi po imporcie sprawdzić, czy Symfonia nadała
    ten sam numer. Raport eksportu wymienia takich kontrahentów (nazwa, NIP,
    numer, konto); ponowny eksport (kontrahent już w rejestrze) — nie."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    for i in range(1, 28):
        rejestr.przydziel("5252445719", f"INNY-{i}")  # pozycje 1..27

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    assert report.nowi_kontrahenci == [
        ("Dostawca Testowy Sp. z o.o.", "1180012345", 28, "202-2-28")
    ]
    linie = [w for w in report.warnings if w.startswith("Nowi kontrahenci w pliku")]
    assert len(linie) == 1
    assert "Kontrahenci" in linie[0]
    assert "Dostawca Testowy Sp. z o.o." in linie[0]
    assert "1180012345" in linie[0] and "28" in linie[0] and "202-2-28" in linie[0]

    ponownie = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    assert ponownie.nowi_kontrahenci == []
    assert not any(w.startswith("Nowi kontrahenci") for w in ponownie.warnings)
    ledger.close()
    rejestr.close()


# --------------------------------------------------------------------------- #
# dwa prefiksy konta rozrachunkowego (sprzedaż/koszt) + pełny dekret sprzedaży

def test_hmffk_oba_prefiksy_w_jednym_eksporcie(tmp_path):
    """Jeden eksport z fakturą sprzedażową i kosztową: WN brutto (sprzedaż) dostaje
    konto_prefix_sprzedaz, MA brutto (koszt) konto_prefix_koszt — różne konta naraz."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.SALES, Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    assert report.exported == 2
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    assert "rodzaj_dok =sprzedaży" in content
    assert "rodzaj_dok =zakupu" in content
    poz_sprzedaz = rejestr.pozycja("5252445719", "5252445719")
    poz_koszt = rejestr.pozycja("5252445719", "1180012345")
    assert f"Konto =201-2-{poz_sprzedaz}" in content   # domyślny konto_prefix_sprzedaz
    assert f"Konto =202-2-{poz_koszt}" in content       # domyślny konto_prefix_koszt
    ledger.close()
    rejestr.close()


def test_hmffk_pelny_dekret_sprzedazy_trzy_konta(tmp_path):
    """Sprzedaż: WN brutto (konto rozrachunkowe), MA netto (703), MA VAT (222-1)
    w jednym dokumencie."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.SALES}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    poz = rejestr.pozycja("5252445719", "5252445719")
    assert f"Konto =201-2-{poz}" in content
    assert "Konto =703" in content
    assert "Konto =222-1" in content
    assert content.count("Konto =") == 3
    ledger.close()
    rejestr.close()


def test_hmffk_koszt_domyslnie_vat_z_kontem_netto_bez_konta(tmp_path):
    """Koszt: MA brutto (rozrachunkowe) i WN VAT dostają domyślne konta
    (202-2-<pozycja> / 221-1); WN netto zostaje BEZ konta (domyślnie puste —
    konto kosztowe przypisuje księgowa ręcznie w FK)."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    poz = rejestr.pozycja("5252445719", "1180012345")
    assert f"Konto =202-2-{poz}" in content  # MA brutto (rozrachunkowe)
    assert "Konto =221-1" in content          # WN VAT (domyślne)
    assert content.count("Konto =") == 2      # WN netto bez konta
    ledger.close()
    rejestr.close()


def test_hmffk_koszt_konto_netto_niestandardowe(tmp_path):
    """konto_koszt_netto ustawione na stałe -> WN netto też dostaje konto."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
        konto_koszt_netto="401",
    )
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    poz = rejestr.pozycja("5252445719", "1180012345")
    assert f"Konto =202-2-{poz}" in content
    assert "Konto =221-1" in content
    assert "Konto =401" in content
    assert content.count("Konto =") == 3
    ledger.close()
    rejestr.close()


def test_hmffk_koszt_konto_vat_niestandardowe(tmp_path):
    """konto_koszt_vat niestandardowe trafia na linię WN VAT dekretu kosztu."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
        konto_koszt_vat="221-9",
    )
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    assert "Konto =221-9" in content
    assert "Konto =221-1" not in content
    ledger.close()
    rejestr.close()


def test_hmffk_konta_niestandardowe_wszystkie_cztery(tmp_path):
    """Wszystkie cztery flagi kont naraz — niestandardowe wartości trafiają na
    odpowiednie linie zapisu."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.SALES}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
        konto_prefix_sprzedaz="301-1", konto_sprzedaz_netto="700-1",
        konto_sprzedaz_vat="223-1",
    )
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    poz = rejestr.pozycja("5252445719", "5252445719")
    assert f"Konto =301-1-{poz}" in content
    assert "Konto =700-1" in content
    assert "Konto =223-1" in content
    ledger.close()
    rejestr.close()


# --------------------------------------------------------------------------- #
# rozrachunki: NumerDok/IdDlaRozliczen + transakcja{} end-to-end (exporter -> hmffk)
# przepis: reference/rozrachunki-hmffk-E2E.md

def _z_terminem_platnosci(xml: bytes, termin: str) -> bytes:
    return xml.replace(
        b"</Fa>",
        f"<Platnosc><TerminPlatnosci><Termin>{termin}</Termin></TerminPlatnosci></Platnosc></Fa>".encode(),
    )


def test_hmffk_rozrachunek_sprzedaz_z_terminem_end_to_end(tmp_path):
    xml = _z_terminem_platnosci((FIXTURES / "fa3_basic_23.xml").read_bytes(), "2026-02-10")
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.SALES}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    poz = rejestr.pozycja("5252445719", "5252445719")
    oczekiwany = (
        "zapis{\r\n"
        "strona =WN\r\n"
        "kwota =1230.00\r\n"
        f"Konto =201-2-{poz}\r\n"
        "NumerDok =FV/2026/01/0007\r\n"
        "IdDlaRozliczen =1\r\n"
        "}\r\n"
        "transakcja{\r\n"
        "idDlaRozliczen =1\r\n"
        "termin =2026-02-10\r\n"
        "kwota =1230.00\r\n"
        "}\r\n"
    )
    assert oczekiwany in content
    ledger.close()
    rejestr.close()


# --------------------------------------------------------------------------- #
# termin_fallback_dni: brak TerminPlatnosci w FA(3) -> termin = P_1 + N dni

def test_hmffk_termin_fallback_domyslne_14_dni(tmp_path):
    """Fixture bez Platnosc/TerminPlatnosci; P_1=2026-01-15 -> run_export z
    domyślnym termin_fallback_dni=14 przekazuje go do map_fa3_to_hmffk, dając
    termin 2026-01-29 i sekcję transakcja{} mimo braku terminu w FA(3)."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.SALES}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    assert "transakcja{\r\nidDlaRozliczen =1\r\ntermin =2026-01-29\r\nkwota =1230.00\r\n}\r\n" in content
    ledger.close()
    rejestr.close()


def test_hmffk_termin_fallback_niestandardowa_liczba_dni(tmp_path):
    """termin_fallback_dni konfigurowalny przez run_export -> inna liczba dni."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.SALES}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
        termin_fallback_dni=30,
    )
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    assert "termin =2026-02-14" in content  # 2026-01-15 + 30 dni
    ledger.close()
    rejestr.close()


def test_hmffk_rozrachunek_koszt_z_terminem_end_to_end(tmp_path):
    xml = _z_terminem_platnosci((FIXTURES / "fa3_basic_23.xml").read_bytes(), "2026-03-01")
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    poz = rejestr.pozycja("5252445719", "1180012345")
    oczekiwany = (
        "zapis{\r\n"
        "strona =MA\r\n"
        "kwota =1230.00\r\n"
        f"Konto =202-2-{poz}\r\n"
        "NumerDok =FV/2026/01/0007\r\n"
        "IdDlaRozliczen =1\r\n"
        "}\r\n"
        "transakcja{\r\n"
        "idDlaRozliczen =1\r\n"
        "termin =2026-03-01\r\n"
        "kwota =1230.00\r\n"
        "}\r\n"
    )
    assert oczekiwany in content
    ledger.close()
    rejestr.close()


# --------------------------------------------------------------------------- #
# pobieranie po numerze faktury WYSTAWCY (invoice_numbers) — KSeF nie umie
# szukać po tym numerze, więc filtrujemy metadane pobrane z zakresu dat

def test_numery_pobiera_tylko_pasujaca_fakture(tmp_path):
    """3 faktury w zakresie, szukany 1 numer -> pobrana i wyeksportowana tylko ta
    jedna (bez download pozostałych), ledger ma dokładnie 1 wpis."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [
        _meta("NR-0001", invoice_number="A/1"),
        _meta("NR-0002", invoice_number="A/2"),
        _meta("NR-0003", invoice_number="A/3"),
    ]
    client = StubClient(metas, {m.ksef_number: xml for m in metas})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
        invoice_numbers=["A/2"],
    )
    assert client.downloaded == ["NR-0002"]
    assert report.exported == 1
    assert (report.numbers_searched, report.numbers_found) == (1, 1)
    out = tmp_path / "out" / "firma-a"
    content = next(out.glob("hmffk_*.TXT")).read_bytes().decode("cp1250")
    assert content.count("dokument{") == 1
    assert ledger.is_done("5252445719", "NR-0002")
    assert not ledger.is_done("5252445719", "NR-0001")
    assert not ledger.is_done("5252445719", "NR-0003")
    ledger.close()
    rejestr.close()


def test_numery_nieznaleziony_nie_pobiera(tmp_path):
    """Szukany numer, którego nie ma w zakresie -> report.failed z komunikatem
    'nie znaleziono', download_invoice w ogóle nie wywołane."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001", invoice_number="A/1")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
        invoice_numbers=["ZZZ/999"],
    )
    assert client.downloaded == []
    assert report.exported == 0
    assert (report.numbers_searched, report.numbers_found) == (1, 0)
    assert len(report.failed) == 1
    assert report.failed[0][0] == "ZZZ/999"
    assert "nie znaleziono" in report.failed[0][1]
    ledger.close()
    rejestr.close()


def test_numery_normalizacja_wielkosci_liter_i_spacji(tmp_path):
    """'fv 12/2026' (szukany) i 'FV12/2026' (na fakturze) — to trafienie mimo
    różnic w wielkości liter i białych znakach."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001", invoice_number="FV12/2026")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
        invoice_numbers=["fv 12/2026"],
    )
    assert report.exported == 1
    assert (report.numbers_searched, report.numbers_found) == (1, 1)
    assert not report.failed
    ledger.close()
    rejestr.close()


def test_numery_sprzedaz_i_koszt_jednoczesnie(tmp_path):
    """Numer z kosztu i numer ze sprzedaży szukane naraz -> jeden plik hmffk
    z oboma dokumentami i poprawnymi (różnymi) kontami rozrachunkowymi."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    meta_sale = _meta("NR-S1", invoice_number="FV/SALE/0001")
    meta_cost = _meta("NR-C1", invoice_number="FV/COST/0001")
    client = KindStubClient(
        {Kind.SALES.value: [meta_sale], Kind.COST.value: [meta_cost]},
        {"NR-S1": xml, "NR-C1": xml},
    )
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.SALES, Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
        invoice_numbers=["FV/SALE/0001", "FV/COST/0001"],
    )
    assert report.exported == 2
    assert (report.numbers_searched, report.numbers_found) == (2, 2)
    out = tmp_path / "out" / "firma-a"
    files = list(out.glob("hmffk_*_numery_*.TXT"))
    assert len(files) == 1
    content = files[0].read_bytes().decode("cp1250")
    assert content.count("dokument{") == 2
    assert "rodzaj_dok =sprzedaży" in content
    assert "rodzaj_dok =zakupu" in content
    poz_sprzedaz = rejestr.pozycja("5252445719", "5252445719")
    poz_koszt = rejestr.pozycja("5252445719", "1180012345")
    assert f"Konto =201-2-{poz_sprzedaz}" in content
    assert f"Konto =202-2-{poz_koszt}" in content
    ledger.close()
    rejestr.close()


def test_numery_none_zachowanie_bez_zmian(tmp_path):
    """invoice_numbers=None (jawnie) -> zachowanie identyczne jak bez tego
    parametru: pełny eksport zakresu, bez filtrowania."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001", invoice_number="A/1"), _meta("NR-0002", invoice_number="A/2")]
    client = StubClient(metas, {"NR-0001": xml, "NR-0002": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", invoice_numbers=None,
    )
    assert (report.found, report.exported, report.numbers_searched) == (2, 2, 0)
    assert sorted(client.downloaded) == ["NR-0001", "NR-0002"]
    out = tmp_path / "out" / "firma-a"
    files = sorted(p.name for p in out.glob("*.TXT"))
    assert files == ["NR-0001.TXT", "NR-0002.TXT"]
    ledger.close()


def test_numery_tylko_nowe_juz_w_rejestrze_skipped(tmp_path):
    """Szukany numer trafiony, ale faktura już jest w rejestrze (poprzedni pełny
    eksport) i tryb 'tylko nowe' -> pominięta (skipped_dedup), nic nie pobrane."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001", invoice_number="A/1")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
    )
    assert ledger.is_done("5252445719", "NR-0001")

    report2 = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
        only_new=True, invoice_numbers=["A/1"],
    )
    assert (report2.exported, report2.skipped_dedup) == (0, 1)
    assert (report2.numbers_searched, report2.numbers_found) == (1, 1)
    assert not report2.failed
    ledger.close()
    rejestr.close()


# --------------------------------------------------------------------------- #
# skrócone formy wpisu w invoice_numbers: sama końcówka ("247") i zakres
# ("247-251") — księgowa dociąga pominięte faktury z serii FV/07/2026/00xxx
# bez przepisywania pełnego numeru

def test_numery_koncowka_dopasowuje_po_ostatnim_ciagu_cyfr(tmp_path):
    """"247" trafia fakturę kończącą się na 247 (00247), ale NIE fakturę
    kończącą się na 1247 (01247) — porównanie liczbowe po ostatnim ciągu cyfr."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    trafienie = _meta("NR-1", invoice_number="FV/07/2026/00247")
    decoy = _meta("NR-2", invoice_number="FV/07/2026/01247")
    client = StubClient([trafienie, decoy], {"NR-1": xml, "NR-2": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", invoice_numbers=["247"],
    )
    assert client.downloaded == ["NR-1"]
    assert report.exported == 1
    assert (report.numbers_searched, report.numbers_found) == (1, 1)
    assert not report.failed
    assert not report.warnings
    ledger.close()


def test_numery_zakres_rozwija_sie_do_wielu_koncowek(tmp_path):
    """Zakres "247-251" -> 5 trafień (5 faktur o końcówkach 247..251), a
    faktury spoza zakresu (246, 252) nie są pobierane; zakres liczy się jako
    JEDEN wpis w numbers_searched, mimo że obejmuje 5 wartości."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    w_zakresie = [
        _meta(f"NR-{n}", invoice_number=f"FV/07/2026/{n:05d}") for n in range(247, 252)
    ]
    poza_zakresem = [
        _meta("NR-246", invoice_number="FV/07/2026/00246"),
        _meta("NR-252", invoice_number="FV/07/2026/00252"),
    ]
    metas = w_zakresie + poza_zakresem
    client = StubClient(metas, {m.ksef_number: xml for m in metas})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", invoice_numbers=["247-251"],
    )
    assert sorted(client.downloaded) == [f"NR-{n}" for n in range(247, 252)]
    assert report.exported == 5
    assert (report.numbers_searched, report.numbers_found) == (1, 1)
    assert not report.failed
    assert not report.warnings
    ledger.close()


def test_numery_zakres_nieprawidlowy_failed_bez_pobierania(tmp_path):
    """Zakres odwrócony ("251-247") i zbyt duży ("1-99999", rozpiętość >2000)
    -> oba trafiają do report.failed z czytelnym komunikatem, BEZ pobierania
    czegokolwiek (walidacja lokalna, bez zapytań do KSeF)."""
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    client = StubClient([], {})

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", invoice_numbers=["251-247", "1-99999"],
    )
    assert client.downloaded == []
    assert report.exported == 0
    assert report.numbers_searched == 2
    assert report.numbers_found == 0
    assert len(report.failed) == 2
    assert report.failed[0][0] == "zakres 251-247"
    assert report.failed[1][0] == "zakres 1-99999"
    assert "nieprawidłowy zakres" in report.failed[0][1]
    assert "nieprawidłowy zakres" in report.failed[1][1]
    ledger.close()


def test_numery_koncowka_wspolna_dla_dwoch_serii_ostrzezenie(tmp_path):
    """Końcówka "247" wspólna dla dwóch różnych serii/wystawców -> pobrane są
    OBIE faktury (jak przy pełnych numerach) i pojawia się ostrzeżenie."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [
        _meta("NR-A", invoice_number="FV/A/00247"),
        _meta("NR-B", invoice_number="FV/B/00247"),
    ]
    client = StubClient(metas, {m.ksef_number: xml for m in metas})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", invoice_numbers=["247"],
    )
    assert sorted(client.downloaded) == ["NR-A", "NR-B"]
    assert report.exported == 2
    assert (report.numbers_searched, report.numbers_found) == (1, 1)
    assert not report.failed
    assert len(report.warnings) == 1
    assert "247" in report.warnings[0] and "2 faktur" in report.warnings[0]
    ledger.close()


def test_numery_mieszanka_pelny_koncowka_zakres(tmp_path):
    """Pełny numer, sama końcówka i zakres podane naraz -> wszystkie trzy
    formy działają razem, dokładnie 3 wpisy w numbers_searched (zakres to
    jeden wpis mimo 3 wartości), faktury spoza filtrów pominięte."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    trafiony_pelny = _meta("NR-1", invoice_number="A/1")
    decoy_pelny = _meta("NR-2", invoice_number="B/2")
    trafiona_koncowka = _meta("NR-3", invoice_number="FV/2026/00500")
    trafiony_zakres = [
        _meta(f"NR-{n}", invoice_number=f"FV/2026/{n:05d}") for n in range(700, 703)
    ]
    decoy_zakres = _meta("NR-703", invoice_number="FV/2026/00703")
    metas = [trafiony_pelny, decoy_pelny, trafiona_koncowka, *trafiony_zakres, decoy_zakres]
    client = StubClient(metas, {m.ksef_number: xml for m in metas})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", invoice_numbers=["A/1", "500", "700-702"],
    )
    assert sorted(client.downloaded) == ["NR-1", "NR-3", "NR-700", "NR-701", "NR-702"]
    assert report.exported == 5
    assert (report.numbers_searched, report.numbers_found) == (3, 3)
    assert not report.failed
    assert not report.warnings
    ledger.close()


# --------------------------------------------------------------------------- #
# pobieranie po KONTRAHENCIE (contractor_filter) — identyfikator to NIP albo
# numer karty z rejestru (tłumaczony na NIP); dla SALES kontrahent = buyer_id
# meta, dla COST = seller_nip meta

def test_kontrahent_numer_karty_pobiera_tylko_faktury_do_tego_nabywcy(tmp_path):
    """3 faktury sprzedaży do 3 różnych nabywców, filtr = numer karty jednego
    z nich -> pobrana i wyeksportowana tylko ta jedna (plik z 1 dokumentem)."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    meta_a = replace(_meta("NR-A", invoice_number="FV/A"), buyer_id="1111111111")
    meta_b = replace(_meta("NR-B", invoice_number="FV/B"), buyer_id="2222222222")
    meta_c = replace(_meta("NR-C", invoice_number="FV/C"), buyer_id="3333333333")
    client = StubClient([meta_a, meta_b, meta_c], {"NR-A": xml, "NR-B": xml, "NR-C": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    rejestr.przydziel(profile.nip, "1111111111", nip="1111111111")  # karta 1
    rejestr.przydziel(profile.nip, "2222222222", nip="2222222222")  # karta 2
    rejestr.przydziel(profile.nip, "3333333333", nip="3333333333")  # karta 3

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.SALES}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
        contractor_filter=["2"],
    )

    assert client.downloaded == ["NR-B"]
    assert report.exported == 1
    assert (report.contractors_searched, report.contractors_found) == (1, 1)
    out = tmp_path / "out" / "firma-a"
    files = list(out.glob("hmffk_*_kontrahenci_*.TXT"))
    assert len(files) == 1
    assert files[0].read_bytes().decode("cp1250").count("dokument{") == 1
    ledger.close()
    rejestr.close()


def test_kontrahent_filtr_nip_bezposrednio(tmp_path):
    """Filtr może być NIP-em wprost (z prefiksem PL i separatorami — _digits)."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    meta_a = replace(_meta("NR-A"), buyer_id="1111111111")
    meta_b = replace(_meta("NR-B"), buyer_id="2222222222")
    client = StubClient([meta_a, meta_b], {"NR-A": xml, "NR-B": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.SALES}, tmp_path / "out",
        contractor_filter=["PL 222-222-22-22"],
    )

    assert client.downloaded == ["NR-B"]
    assert report.exported == 1
    assert (report.contractors_searched, report.contractors_found) == (1, 1)
    out = tmp_path / "out" / "firma-a"
    assert [p.name for p in out.glob("*.TXT")] == ["NR-B.TXT"]
    ledger.close()


def test_kontrahent_koszt_filtruje_po_sprzedawcy(tmp_path):
    """Dla kosztów kontrahentem jest SPRZEDAWCA (seller_nip), nie nabywca."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    meta_a = replace(_meta("NR-A"), seller_nip="1111111111")
    meta_b = replace(_meta("NR-B"), seller_nip="2222222222")
    client = StubClient([meta_a, meta_b], {"NR-A": xml, "NR-B": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out",
        contractor_filter=["2222222222"],
    )

    assert client.downloaded == ["NR-B"]
    assert report.exported == 1
    ledger.close()


def test_kontrahent_karta_bez_nip_ostrzezenie_nic_nie_pobrane_failed(tmp_path):
    """Numer karty bez przypisanego NIP w rejestrze -> ostrzeżenie, nic nie
    pobrane (filtr traktowany jako aktywny mimo braku rozwiązania), failed."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001")]
    client = StubClient(metas, {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    _, pozycja = rejestr.utworz_wpis(profile.nip, nazwa="Firma Bez NIP", miejscowosc="Warszawa")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
        contractor_filter=[str(pozycja)],
    )

    assert client.downloaded == []
    assert report.exported == 0
    assert any("nie ma NIP" in w for w in report.warnings)
    assert report.failed == [(
        f"kontrahent {pozycja}",
        "brak faktur w KSeF w zakresie 2026-01-01–2026-01-31 dla wybranych rodzajów",
    )]
    ledger.close()
    rejestr.close()


def test_kontrahent_brak_trafien_failed(tmp_path):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    meta = replace(_meta("NR-0001"), buyer_id="1111111111")
    client = StubClient([meta], {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.SALES}, tmp_path / "out",
        contractor_filter=["9999999999"],
    )

    assert client.downloaded == []
    assert report.exported == 0
    assert (report.contractors_searched, report.contractors_found) == (1, 0)
    assert report.failed == [(
        "kontrahent 9999999999",
        "brak faktur w KSeF w zakresie 2026-01-01–2026-01-31 dla wybranych rodzajów",
    )]
    ledger.close()


def test_kontrahent_filtr_none_zachowanie_bez_zmian(tmp_path):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("NR-0001"), _meta("NR-0002")]
    client = StubClient(metas, {"NR-0001": xml, "NR-0002": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.COST}, tmp_path / "out", contractor_filter=None,
    )

    assert (report.found, report.exported, report.contractors_searched) == (2, 2, 0)
    assert sorted(client.downloaded) == ["NR-0001", "NR-0002"]
    ledger.close()


def test_kontrahent_i_numer_razem_koniunkcja_z_infiksem_pliku(tmp_path):
    """invoice_numbers + contractor_filter naraz -> KONIUNKCJA (oba muszą
    pasować), a nazwa pliku dostaje oba infiksy: _numery_kontrahenci."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    trafienie = replace(_meta("NR-1", invoice_number="A/1"), buyer_id="1111111111")
    zly_nabywca = replace(_meta("NR-2", invoice_number="A/1"), buyer_id="2222222222")
    zly_numer = replace(_meta("NR-3", invoice_number="A/2"), buyer_id="1111111111")
    client = StubClient(
        [trafienie, zly_nabywca, zly_numer],
        {"NR-1": xml, "NR-2": xml, "NR-3": xml},
    )
    ledger = Ledger(tmp_path / "ledger.db")
    profile = Profile("Firma A", "5252445719", "tok", "firma-a")
    rejestr = RejestrNumerow(tmp_path / "nr.db")

    report = run_export(
        profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
        {Kind.SALES}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
        invoice_numbers=["A/1"], contractor_filter=["1111111111"],
    )

    assert client.downloaded == ["NR-1"]
    assert report.exported == 1
    out = tmp_path / "out" / "firma-a"
    assert len(list(out.glob("hmffk_*_numery_kontrahenci_*.TXT"))) == 1
    ledger.close()
    rejestr.close()
