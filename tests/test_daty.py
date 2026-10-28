"""Testy poprawek dat (zgłoszenie 25.09: „data o dzień wcześniej”, „mimo zakresu
pobiera różne daty”):

1. data przyjęcia w KSeF (Datawpl / KSeFDate / okres VAT kosztu) = dzień nadania
   numeru KSeF (acquisitionDate) w czasie POLSKIM, nie pierwsze 10 znaków UTC;
2. zakres dat: zapytanie do KSeF z zapasem 1 dnia z każdej strony + lokalne
   odcięcie po dacie wystawienia — w pliku są dokładnie faktury z wybranego zakresu;
3. pole „Do” w oknie nie gubi ostatniego dnia miesiąca przy zmianie miesiąca;
4. eksport niepełny kończy się ostrzeżeniem, nie komunikatem „Eksport zakończony”.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Profile
from app.exporter import ExportReport, Kind, run_export
from app.kontrahenci_nr import RejestrNumerow
from app.ksef_client import InvoiceMetadata, KsefError, KsefQuotaExceeded, warsaw_date
from app.ledger import Ledger

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _meta(nr, *, issue="2026-07-15", invoicing=None, acquisition=None):
    return InvoiceMetadata(
        ksef_number=nr,
        invoice_number=f"FV/{nr}",
        issue_date=issue,
        invoicing_date=invoicing,
        acquisition_date=acquisition,
        seller_nip="1180012345",
        seller_name="Dostawca Testowy Sp. z o.o.",
        buyer_id="5252445719",
        buyer_name="Nasza Firma Sp. z o.o.",
        net=None, gross=None, vat=None,
        currency="PLN",
        form_system_code="FA (3)",
        invoice_type="Vat",
    )


# --------------------------------------------------------------------------- #
# 1. data przyjęcia w KSeF w czasie polskim

@pytest.mark.parametrize(
    "raw, oczekiwana",
    [
        # lato (CEST, +02:00): 22:30 UTC = 00:30 następnego dnia w Polsce
        ("2026-07-31T22:30:05.1234567+00:00", "2026-08-01"),
        ("2026-07-31T21:59:59+00:00", "2026-07-31"),
        ("2026-07-31T22:00:00Z", "2026-08-01"),
        # zima (CET, +01:00)
        ("2026-11-30T23:30:00Z", "2026-12-01"),
        ("2026-11-30T22:59:59+00:00", "2026-11-30"),
        # dzień zmiany czasu na zimowy (25.10.2026, 01:00 UTC)
        ("2026-10-24T23:30:00+00:00", "2026-10-25"),
        ("2026-10-25T23:30:00+00:00", "2026-10-26"),
        # dzień zmiany czasu na letni (29.03.2026, 01:00 UTC)
        ("2026-03-28T23:30:00+00:00", "2026-03-29"),
        ("2026-03-29T22:30:00+00:00", "2026-03-30"),
        # wartość z jawnym offsetem polskim — bez zmian
        ("2026-08-01T00:30:00+02:00", "2026-08-01"),
        # wartość bez offsetu = czas polski (tak KSeF interpretuje wartości bez strefy)
        ("2026-08-01T00:30:00", "2026-08-01"),
        # sama data — bez zmian
        ("2026-08-01", "2026-08-01"),
    ],
)
def test_warsaw_date(raw, oczekiwana):
    assert warsaw_date(raw) == oczekiwana


def test_warsaw_date_zgodna_z_baza_stref_dla_kazdej_godziny_2025_2027():
    """Reguła strefy liczona w kodzie (bez pakietu tzdata, który na Windows
    trzeba by doinstalować) — porównana z bazą stref systemu co godzinę."""
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        waw = zoneinfo.ZoneInfo("Europe/Warsaw")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("brak bazy stref w systemie")
    t = datetime(2025, 1, 1, tzinfo=timezone.utc)
    koniec = datetime(2028, 1, 1, tzinfo=timezone.utc)
    while t < koniec:
        raw = t.isoformat()
        assert warsaw_date(raw) == t.astimezone(waw).date().isoformat(), raw
        t += timedelta(minutes=30)


def test_ksef_date_to_dzien_nadania_numeru_w_czasie_polskim():
    """Prawna data otrzymania = data nadania numeru KSeF (acquisitionDate);
    sesja wsadowa otwarta przed północą ma invoicingDate z poprzedniego dnia."""
    m = _meta(
        "A",
        invoicing="2026-07-31T21:59:00+00:00",   # 31.07 23:59 w Polsce
        acquisition="2026-07-31T22:00:04+00:00",  # 01.08 00:00:04 w Polsce
    )
    assert m.ksef_date == "2026-08-01"


def test_ksef_date_bez_acquisition_bierze_invoicing_w_czasie_polskim():
    m = _meta("A", invoicing="2026-07-31T22:30:00+00:00")
    assert m.ksef_date == "2026-08-01"


def test_ksef_date_bez_dat_ksef_bierze_date_wystawienia():
    m = _meta("A", issue="2026-07-31")
    assert m.ksef_date == "2026-07-31"


def test_plik_hmffk_koszt_przyjety_po_polnocy_ma_date_wplywu_i_okres_z_dnia_polskiego(tmp_path):
    """Faktura kosztowa wystawiona 01.08, numer KSeF nadany 01.08 o 00:30 czasu
    polskiego — w pliku data wpływu, data KSeF i okres rejestru VAT = 01.08
    (wcześniej 31.07, czyli VAT w lipcu i wpływ przed wystawieniem)."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    # data wystawienia w metadanych musi mieścić się w zakresie eksportu
    meta = _meta(
        "KOSZT-NOC", issue="2026-01-15",
        acquisition="2026-07-31T22:30:00+00:00",
    )
    tekst = _eksport_hmffk(tmp_path, [meta], {"KOSZT-NOC": xml}, date(2026, 1, 1), date(2026, 1, 31))
    assert "Datawpl =2026-08-01" in tekst
    assert "KSeFDate =2026-08-01" in tekst
    assert "okres =2026-08-01" in tekst
    assert "2026-07-31" not in tekst


# --------------------------------------------------------------------------- #
# 2. zakres dat: zapas w zapytaniu + lokalne odcięcie

class _RecordingStub:
    """Stub KSeF: oddaje wszystkie metadane niezależnie od zakresu (jak KSeF
    liczący granice inaczej niż my) i zapisuje, o jaki zakres pytano."""

    def __init__(self, metas, xml_by_nr):
        self._metas = metas
        self._xml = xml_by_nr
        self.zapytania = []
        self.paczki = []

    def authenticate(self):
        pass

    def query_metadata(self, subject_type, date_from, date_to, *, margin=timedelta(0)):
        self.zapytania.append((subject_type, date_from - margin, date_to + margin))
        yield from self._metas

    def download_invoice(self, ksef_number):
        return self._xml[ksef_number]

    def download_budget_remaining(self):
        return 64


class _RecordingBatchStub(_RecordingStub):
    def request_export(self, subject_type, date_from, date_to, *, date_type="Issue"):
        self.paczki.append((subject_type, date_from, date_to))
        return "EXP-1"

    def wait_for_export(self, reference):
        return {"parts": [], "isTruncated": False, "invoiceCount": len(self._xml)}

    def iter_export_invoices(self, reference, package):
        yield from self._xml.items()


def _profil():
    return Profile(name="Test", nip="5252445719", token="x", subfolder="test")


def _eksport_hmffk(tmp_path, metas, xml_by_nr, od, do, client=None):
    client = client or _RecordingStub(metas, xml_by_nr)
    rej = RejestrNumerow(tmp_path / "rej.db")
    led = Ledger(tmp_path / "led.db")
    try:
        run_export(_profil(), client, led, od, do, {Kind.COST}, tmp_path,
                   fmt="hmffk", rejestr_nr=rej)
    finally:
        rej.close()
        led.close()
    plik = next((tmp_path / "test").glob("hmffk_*.TXT"))
    return plik.read_bytes().decode("cp1250")


def test_zapytanie_do_ksef_z_zapasem_jednego_dnia(tmp_path):
    client = _RecordingStub([], {})
    run_export(_profil(), client, Ledger(tmp_path / "l.db"), date(2026, 7, 1), date(2026, 7, 31),
               {Kind.COST}, tmp_path, fmt="hmffk", rejestr_nr=RejestrNumerow(tmp_path / "r.db"))
    assert client.zapytania == [("Subject2", date(2026, 6, 30), date(2026, 8, 1))]


@pytest.mark.parametrize("format_daty", ["{d}", "{d}T00:00:00+00:00"])
def test_w_eksporcie_tylko_faktury_z_wybranego_zakresu(tmp_path, format_daty):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    dni = {"PRZED": "2026-06-30", "PIERWSZY": "2026-07-01", "OSTATNI": "2026-07-31", "PO": "2026-08-01"}
    metas = [_meta(nr, issue=format_daty.format(d=d)) for nr, d in dni.items()]
    client = _RecordingStub(metas, {nr: xml for nr in dni})
    led = Ledger(tmp_path / "l.db")
    rep = run_export(_profil(), client, led, date(2026, 7, 1), date(2026, 7, 31),
                     {Kind.COST}, tmp_path, fmt="hmffk", rejestr_nr=RejestrNumerow(tmp_path / "r.db"))
    assert rep.found == 2
    assert rep.exported == 2
    assert led.is_done("5252445719", "PIERWSZY")
    assert led.is_done("5252445719", "OSTATNI")
    assert not led.is_done("5252445719", "PRZED")
    assert not led.is_done("5252445719", "PO")


def test_paczka_eksportu_z_zapasem_i_tylko_faktury_z_zakresu(tmp_path):
    """Powyżej progu (10 faktur) pobieranie idzie paczką — ten sam zapas
    w zleceniu paczki i to samo odcięcie."""
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    w_zakresie = [_meta(f"W-{i:02d}", issue="2026-07-10") for i in range(11)]
    poza = [_meta("PO", issue="2026-08-01")]
    metas = w_zakresie + poza
    client = _RecordingBatchStub(metas, {m.ksef_number: xml for m in metas})
    led = Ledger(tmp_path / "l.db")
    rep = run_export(_profil(), client, led, date(2026, 7, 1), date(2026, 7, 31),
                     {Kind.COST}, tmp_path, fmt="hmffk", rejestr_nr=RejestrNumerow(tmp_path / "r.db"))
    assert client.paczki == [("Subject2", date(2026, 6, 30), date(2026, 8, 1))]
    assert rep.found == 11
    assert rep.exported == 11
    assert not rep.failed
    assert not led.is_done("5252445719", "PO")


# --------------------------------------------------------------------------- #
# 3. pole „Do” trzyma koniec miesiąca

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def okno(tmp_path, qapp):
    from app.config import ConfigStore
    from app.gui import MainWindow

    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo")
    cfg.profiles.append(Profile("FirmaA", "1112223344", "tok", "fa",
                                ostatnie_od="2026-06-01", ostatnie_do="2026-06-30"))
    store.save(cfg)
    win = MainWindow(store, cfg)
    win.show()
    # aktywne okno — inaczej platforma offscreen nie dostarcza zdarzeń fokusu
    # (kliknięcie obok = focusOut), a testy przejść między polami kłamią
    win.activateWindow()
    from PySide6.QtTest import QTest
    QTest.qWaitForWindowActive(win)
    yield win
    win.close()


def _ustaw(win, od, do):
    from PySide6.QtCore import QDate
    win.date_from.setDate(QDate(*od))
    win.date_to.setDate(QDate(*do))


def _do(win):
    return win.date_to.date().toPython()


def _krok_miesiaca(pole, kroki=1):
    from PySide6.QtWidgets import QDateTimeEdit
    pole.setFocus()
    pole.setCurrentSection(QDateTimeEdit.Section.MonthSection)
    pole.stepBy(kroki)


def _wpisz(pole, sekcja, tekst):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDateTimeEdit
    pole.setFocus()
    s = getattr(QDateTimeEdit.Section, sekcja)
    pole.setCurrentSection(s)
    pole.setSelectedSection(s)
    QTest.keyClicks(pole, tekst)


def test_start_okna_pokazuje_zapamietany_zakres_bez_zmian(okno):
    assert okno.date_from.date().toPython() == date(2026, 6, 1)
    assert _do(okno) == date(2026, 6, 30)


def test_strzalka_miesiaca_z_konca_czerwca_daje_koniec_lipca(okno):
    _krok_miesiaca(okno.date_from)
    _krok_miesiaca(okno.date_to)
    assert okno.date_from.date().toPython() == date(2026, 7, 1)
    assert _do(okno) == date(2026, 7, 31)


def test_strzalki_przez_luty_nie_gubia_dni(okno):
    _ustaw(okno, (2026, 1, 1), (2026, 1, 31))
    wyniki = []
    for _ in range(3):
        _krok_miesiaca(okno.date_to)
        wyniki.append(_do(okno))
    assert wyniki == [date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]


def test_strzalka_w_dol_tez_trzyma_koniec_miesiaca(okno):
    _ustaw(okno, (2026, 3, 1), (2026, 3, 31))
    _krok_miesiaca(okno.date_to, -1)
    assert _do(okno) == date(2026, 2, 28)


def test_wpisanie_samego_miesiaca_daje_koniec_miesiaca(okno):
    _wpisz(okno.date_to, "MonthSection", "07")
    assert _do(okno) == date(2026, 7, 31)


def test_wpisanie_miesiaca_i_dnia_zostawia_wpisany_dzien(okno):
    _wpisz(okno.date_to, "MonthSection", "07")
    _wpisz(okno.date_to, "DaySection", "15")
    assert _do(okno) == date(2026, 7, 15)


def test_zapamietany_juz_skrocony_zakres_naprawia_sie_przy_zmianie_miesiaca(okno):
    """Profile, w których błąd zdążył zapisać 01.07–30.07: przestawienie
    miesiąca daje pełny sierpień, a nie 30.08."""
    _ustaw(okno, (2026, 7, 1), (2026, 7, 30))
    _krok_miesiaca(okno.date_from)
    _krok_miesiaca(okno.date_to)
    assert _do(okno) == date(2026, 8, 31)


def test_zakres_do_polowy_miesiaca_nie_jest_rozszerzany(okno):
    _ustaw(okno, (2026, 7, 1), (2026, 7, 15))
    _krok_miesiaca(okno.date_to)
    assert _do(okno) == date(2026, 8, 15)


def test_wybor_z_kalendarza_zostawia_wybrany_dzien(okno):
    from PySide6.QtCore import QDate
    okno.date_to.calendarWidget().clicked.emit(QDate(2026, 7, 30))
    assert _do(okno) == date(2026, 7, 30)


# --------------------------------------------------------------------------- #
# 4. eksport niepełny = ostrzeżenie

class _Okna:
    def __init__(self):
        self.calls = []

    def warning(self, *a, **k):
        self.calls.append(("warning", a[1]))

    def information(self, *a, **k):
        self.calls.append(("information", a[1]))


def test_eksport_niepelny_konczy_sie_ostrzezeniem(okno, monkeypatch):
    import app.gui as gui_mod
    okna = _Okna()
    monkeypatch.setattr(gui_mod, "QMessageBox", okna)
    raport = ExportReport(found=6, exported=4)
    raport.warnings.append("Limit KSeF (pobierania faktur) wyczerpany — wznowienie ok. 14:32 Pozostało 2 faktur")
    okno._on_finished(raport)
    assert okna.calls and okna.calls[0][0] == "warning"
    assert "niepełny" in okna.calls[0][1].lower()


def test_eksport_pelny_konczy_sie_informacja(okno, monkeypatch):
    import app.gui as gui_mod
    okna = _Okna()
    monkeypatch.setattr(gui_mod, "QMessageBox", okna)
    okno._on_finished(ExportReport(found=4, exported=4))
    assert okna.calls == [("information", "Eksport zakończony")]


def test_tylko_nowe_bez_brakow_to_eksport_pelny(okno, monkeypatch):
    import app.gui as gui_mod
    okna = _Okna()
    monkeypatch.setattr(gui_mod, "QMessageBox", okna)
    okno._on_finished(ExportReport(found=6, exported=2, skipped_dedup=4))
    assert okna.calls == [("information", "Eksport zakończony")]


# --------------------------------------------------------------------------- #
# 5. uwagi z przeglądu (28.09)

@pytest.mark.parametrize(
    "raw, oczekiwana",
    [
        ("2026-07-31T22:30:05.1+00:00", "2026-08-01"),
        ("2026-07-31T22:30:05.12+00:00", "2026-08-01"),
        ("2026-07-31T22:30:05.1234+00:00", "2026-08-01"),
        ("2026-07-31T22:30:05.12345+00:00", "2026-08-01"),
        ("2026-07-31T22:30:05.12Z", "2026-08-01"),
        ("2026-07-31T22:30:05+0000", "2026-08-01"),
        ("2026-07-31 22:30:05+00:00", "2026-08-01"),
    ],
)
def test_warsaw_date_niezalezna_od_parsera_pythona(raw, oczekiwana, monkeypatch):
    """Na Pythonie 3.10 fromisoformat przyjmuje tylko 3 albo 6 cyfr ułamka i nie
    zna 'Z' ani '+0000' — wynik nie może zależeć od wersji interpretera, z którą
    zbudowano exe."""
    import app.ksef_client as kc

    class _BezFromisoformat(datetime):
        @classmethod
        def fromisoformat(cls, s):
            raise ValueError("symulacja starego parsera")

    monkeypatch.setattr(kc, "datetime", _BezFromisoformat)
    assert kc.warsaw_date(raw) == oczekiwana


class _LimitNaKosztach(_RecordingStub):
    """Sprzedaż przechodzi, zapytanie o koszty trafia na limit KSeF."""

    def query_metadata(self, subject_type, date_from, date_to, *, margin=timedelta(0)):
        if subject_type == "Subject2":
            raise KsefQuotaExceeded("zapytań o metadane", 0.0)
        yield from super().query_metadata(subject_type, date_from, date_to, margin=margin)


def test_limit_na_etapie_zapytania_oznacza_eksport_niepelny(tmp_path):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta("S1", issue="2026-07-10")]
    client = _LimitNaKosztach(metas, {"S1": xml})
    rep = run_export(_profil(), client, Ledger(tmp_path / "l.db"), date(2026, 7, 1), date(2026, 7, 31),
                     {Kind.SALES, Kind.COST}, tmp_path, fmt="hmffk",
                     rejestr_nr=RejestrNumerow(tmp_path / "r.db"))
    assert rep.exported == rep.found == 1
    assert rep.niepelny
    assert "NIEPEŁNY" in rep.summary()


def test_okno_ostrzega_gdy_raport_niepelny_mimo_zgodnych_liczb(okno, monkeypatch):
    import app.gui as gui_mod
    okna = _Okna()
    monkeypatch.setattr(gui_mod, "QMessageBox", okna)
    raport = ExportReport(found=3, exported=3)
    raport.niepelny = True
    okno._on_finished(raport)
    assert okna.calls[0] == ("warning", "Eksport niepełny")


def test_bledy_i_braki_razem_mowia_o_brakach(okno, monkeypatch):
    import app.gui as gui_mod
    zapisane = []
    okna = _Okna()
    okna.warning = lambda *a, **k: zapisane.append((a[1], a[2]))
    monkeypatch.setattr(gui_mod, "QMessageBox", okna)
    raport = ExportReport(found=10, exported=4, failed=[("X", "błąd mapowania")])
    raport.niepelny = True
    okno._on_finished(raport)
    tytul, tresc = zapisane[0]
    assert tytul == "Eksport zakończony z błędami"
    assert "Nie wszystkie faktury zostały pobrane" in tresc


class _Pokazane:
    """Prawdziwe QMessageBox z setDetailedText — exec() podmienione, żeby test
    nie czekał na kliknięcie; zapamiętuje pokazane okna."""

    def __init__(self, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        self.okna = []
        self.statyczne = []
        self.tytuly = []  # macOS ignoruje tytuł QMessageBox (windowTitle() == "")
        monkeypatch.setattr(QMessageBox, "exec", lambda box: self.okna.append(box) or 0)
        monkeypatch.setattr(QMessageBox, "setWindowTitle", lambda box, t: self.tytuly.append(t))
        # statyczne okna (bez listy szczegółów) też bez blokowania testu
        for nazwa in ("warning", "information"):
            monkeypatch.setattr(
                QMessageBox, nazwa,
                staticmethod(lambda *a, _n=nazwa, **k: self.statyczne.append((_n, a[1])) or 0),
            )


def _raport_ze_spoznionymi_i_nowymi():
    from app.exporter import SpoznionaFaktura

    raport = ExportReport(found=5, exported=5)
    raport.spoznione = [
        SpoznionaFaktura("5252445719-20260903-AAAA-01", "FV/12/2026", "2026-08-28", "koszt"),
        SpoznionaFaktura("5252445719-20260904-BBBB-02", "FV/13/2026", "2026-08-29", "sprzedaż"),
    ]
    raport.nowi_kontrahenci = [("Dostawca Testowy Sp. z o.o.", "1180012345", 28, "202-2-28")]
    raport.warnings = ["Dołączono 2 spóźnione faktury …", "Nowi kontrahenci w pliku …"]
    return raport


def test_okno_konca_spoznione_i_nowi_kontrahenci_to_ostrzezenie_z_lista(okno, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    pokazane = _Pokazane(monkeypatch)
    okno._on_finished(_raport_ze_spoznionymi_i_nowymi())

    [box] = pokazane.okna
    assert box.icon() == QMessageBox.Icon.Warning  # ostrzeżenie, nie informacja
    assert pokazane.tytuly == ["Eksport zakończony"]
    tresc = box.text()
    assert "Dołączono 2 spóźnione faktury" in tresc
    assert "1 nowy kontrahent" in tresc and "Kontrahenci…" in tresc
    szczegoly = box.detailedText()
    for fragment in ("FV/12/2026", "2026-08-28", "koszt", "5252445719-20260903-AAAA-01",
                     "FV/13/2026", "sprzedaż",
                     "Dostawca Testowy Sp. z o.o.", "1180012345", "28", "202-2-28"):
        assert fragment in szczegoly


def test_okno_konca_same_nowi_kontrahenci_tez_ostrzega(okno, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    pokazane = _Pokazane(monkeypatch)
    raport = ExportReport(found=1, exported=1)
    raport.nowi_kontrahenci = [("Klient B", None, 7, "201-2-7")]
    okno._on_finished(raport)
    [box] = pokazane.okna
    assert box.icon() == QMessageBox.Icon.Warning
    assert "Klient B" in box.detailedText() and "brak" in box.detailedText()


def test_okno_konca_bezpiecznik_opisany_osobno(okno, monkeypatch):
    from app.exporter import SpoznionaFaktura

    pokazane = _Pokazane(monkeypatch)
    raport = ExportReport(found=1, exported=1)
    raport.spoznione = [SpoznionaFaktura("K-1", "FV/1", "2026-06-20", "koszt", bezpiecznik=True)]
    okno._on_finished(raport)
    [box] = pokazane.okna
    assert "60 dni" in box.text()
    assert "nie eksportowała" not in box.text()  # D4
    assert "domknięcie swojego miesiąca" in box.text()
    assert "Dołączono 1 fakturę, która czekała" in box.text()
    assert "czy nie jest już zaksięgowana" in box.text()
    assert "FV/1" in box.detailedText()


def test_okno_konca_bezpiecznik_liczba_mnoga(okno, monkeypatch):
    from app.exporter import SpoznionaFaktura

    pokazane = _Pokazane(monkeypatch)
    raport = ExportReport(found=2, exported=2)
    raport.spoznione = [SpoznionaFaktura("K-1", "FV/1", "2026-06-20", "koszt", bezpiecznik=True),
                        SpoznionaFaktura("K-2", "FV/2", "2026-06-21", "koszt", bezpiecznik=True)]
    okno._on_finished(raport)
    [box] = pokazane.okna
    assert "Dołączono 2 faktury, które czekały" in box.text()
    assert "czy nie są już zaksięgowane" in box.text()


def test_okno_konca_miesiac_niedomkniety_to_ostrzezenie(okno, monkeypatch):
    """D-N2: eksport objął cały miesiąc, ale dane KSeF nie są jeszcze kompletne
    — okno końcowe ostrzega, że miesiąc nie jest domknięty."""
    import app.gui as gui_mod

    zapisane = []
    okna = _Okna()
    okna.warning = lambda *a, **k: zapisane.append((a[1], a[2]))
    monkeypatch.setattr(gui_mod, "QMessageBox", okna)
    raport = ExportReport(found=3, exported=3)
    komunikat = ("Miesiąc wrzesień 2026 nie został jeszcze domknięty (dane KSeF kompletne do "
                 "29.09.2026) — powtórz domknięcie jutro.")
    raport.niedomkniete = [komunikat]
    raport.warnings = [komunikat]
    okno._on_finished(raport)
    [(tytul, tresc)] = zapisane
    assert tytul == "Eksport zakończony"
    assert komunikat in tresc


def test_okno_konca_niepobrane_to_uruchom_ponownie_a_nie_ksieguj_recznie(okno, monkeypatch):
    import app.gui as gui_mod

    zapisane = []
    okna = _Okna()
    okna.warning = lambda *a, **k: zapisane.append((a[1], a[2]))
    monkeypatch.setattr(gui_mod, "QMessageBox", okna)
    raport = ExportReport(found=15, exported=3)
    raport.niepobrane = [(f"S-{i:02d}", "nie pobrano — błąd paczki eksportu") for i in range(12)]
    raport.niepelny = True
    okno._on_finished(raport)
    tytul, tresc = zapisane[0]
    assert tytul == "Eksport niepełny"
    assert "Nie pobrano 12 faktur" in tresc
    assert "Pomiń już pobrane" in tresc
    assert "ręcznie" not in tresc.replace("nie księguj ich ręcznie", "")


def test_okno_konca_bledy_mapowania_i_niepobrane_osobno(okno, monkeypatch):
    import app.gui as gui_mod

    zapisane = []
    okna = _Okna()
    okna.warning = lambda *a, **k: zapisane.append((a[1], a[2]))
    monkeypatch.setattr(gui_mod, "QMessageBox", okna)
    raport = ExportReport(found=10, exported=5, failed=[("X", "błąd mapowania")])
    raport.niepobrane = [("A", "x"), ("B", "x"), ("C", "x"), ("D", "x")]
    raport.niepelny = True
    okno._on_finished(raport)
    tytul, tresc = zapisane[0]
    assert tytul == "Eksport zakończony z błędami"
    assert "Faktury z błędami (1) należy zaksięgować ręcznie" in tresc
    assert "Nie pobrano 4 faktur" in tresc


def test_okno_konca_bledy_i_spoznione_ostrzezenie_z_lista(okno, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    pokazane = _Pokazane(monkeypatch)
    raport = _raport_ze_spoznionymi_i_nowymi()
    raport.failed = [("X", "błąd mapowania")]
    okno._on_finished(raport)
    [box] = pokazane.okna
    assert pokazane.tytuly == ["Eksport zakończony z błędami"]
    assert box.icon() == QMessageBox.Icon.Warning
    assert "FV/12/2026" in box.detailedText()


def test_zapas_nie_zwieksza_liczby_zapytan_dla_kwartalu(tmp_path):
    client = _RecordingStub([], {})
    run_export(_profil(), client, Ledger(tmp_path / "l.db"), date(2026, 1, 1), date(2026, 3, 31),
               {Kind.COST}, tmp_path, fmt="hmffk", rejestr_nr=RejestrNumerow(tmp_path / "r.db"))
    # exporter przekazuje zapas do klienta, który dokleja go do KAŻDEGO okna
    # (patrz test w test_ksef_client.py) — zakres użytkownika bez zmian
    assert client.zapytania == [("Subject2", date(2025, 12, 31), date(2026, 4, 1))]


class _PaczkiPerOkno(_RecordingBatchStub):
    def __init__(self, metas, xml_by_nr, nieudane=()):
        super().__init__(metas, xml_by_nr)
        self._nieudane = set(nieudane)
        self._okno_ref = {}

    def request_export(self, subject_type, date_from, date_to, *, date_type="Issue"):
        self.paczki.append((subject_type, date_from, date_to))
        ref = f"EXP-{len(self.paczki)}"
        self._okno_ref[ref] = (date_from, date_to)
        if len(self.paczki) in self._nieudane:
            raise KsefError("eksport wsadowy: HTTP 400: 21405")
        return ref

    def iter_export_invoices(self, reference, package):
        od, do = self._okno_ref[reference]
        for m in self._metas:
            if od <= date.fromisoformat(m.issue_date[:10]) <= do:
                yield m.ksef_number, self._xml[m.ksef_number]


def test_paczka_dzielona_na_okna_nie_przekracza_100_dni(tmp_path):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    metas = [_meta(f"A-{i:02d}", issue="2026-02-10") for i in range(8)]
    metas += [_meta(f"B-{i:02d}", issue="2026-04-08") for i in range(4)]
    client = _PaczkiPerOkno(metas, {m.ksef_number: xml for m in metas})
    rep = run_export(_profil(), client, Ledger(tmp_path / "l.db"), date(2026, 1, 1), date(2026, 4, 9),
                     {Kind.COST}, tmp_path, fmt="hmffk", rejestr_nr=RejestrNumerow(tmp_path / "r.db"))
    assert len(client.paczki) == 2
    for _, od, do in client.paczki:
        assert (do - od).days + 1 <= 100
    assert rep.exported == 12
    assert not rep.failed
    assert not rep.niepelny


def test_blad_paczki_nie_kasuje_przetworzonej_sprzedazy(tmp_path):
    xml = (FIXTURES / "fa3_basic_23.xml").read_bytes()
    sprzedaz = [_meta(f"S-{i:02d}", issue="2026-07-10") for i in range(3)]
    koszty = [_meta(f"K-{i:02d}", issue="2026-07-10") for i in range(12)]

    class _Stub(_PaczkiPerOkno):
        def query_metadata(self, subject_type, date_from, date_to, *, margin=timedelta(0)):
            yield from (sprzedaz if subject_type == "Subject1" else koszty)

    client = _Stub(sprzedaz + koszty, {m.ksef_number: xml for m in sprzedaz + koszty}, nieudane={1})
    rep = run_export(_profil(), client, Ledger(tmp_path / "l.db"), date(2026, 7, 1), date(2026, 7, 31),
                     {Kind.SALES, Kind.COST}, tmp_path, fmt="hmffk",
                     rejestr_nr=RejestrNumerow(tmp_path / "r.db"))
    assert rep.exported == 3
    # W6a: niepobrane z nieudanej paczki to nie „błędy do ręcznego księgowania”
    assert rep.failed == []
    assert len(rep.niepobrane) == 12
    assert rep.niepelny
    assert list((tmp_path / "test").glob("hmffk_*.TXT"))


def test_wpisanie_stycznia_jedna_cyfra_i_tab_daje_koniec_stycznia(okno):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    _wpisz(okno.date_to, "MonthSection", "1")
    QTest.keyClick(okno.date_to, Qt.Key.Key_Tab)
    assert _do(okno) == date(2026, 1, 31)


def test_wpisanie_stycznia_jedna_cyfra_i_klik_obok_daje_koniec_stycznia(okno):
    _wpisz(okno.date_to, "MonthSection", "1")
    okno.date_from.setFocus()
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()
    assert _do(okno) == date(2026, 1, 31)


@pytest.mark.parametrize("pole, tekst, oczekiwana", [
    ("date_to", "2026-07-31", date(2026, 7, 31)),
    ("date_to", "2026-7-31", date(2026, 7, 31)),
    ("date_to", "2026.07.31", date(2026, 7, 31)),
    ("date_to", "20260731", date(2026, 7, 31)),
    ("date_from", "2026-07-01", date(2026, 7, 1)),
    ("date_from", "2026-1-5", date(2026, 1, 5)),
])
def test_wpisanie_daty_z_kreskami_od_roku(okno, pole, tekst, oczekiwana):
    """Kreska wpisana zaraz po automatycznym przejściu do następnej sekcji nie
    może przeskakiwać sekcji (było: '2026-07-31' -> 2026-06-07)."""
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDateTimeEdit
    edit = getattr(okno, pole)
    edit.setFocus()
    edit.setCurrentSection(QDateTimeEdit.Section.YearSection)
    edit.setSelectedSection(QDateTimeEdit.Section.YearSection)
    QTest.keyClicks(edit, tekst)
    assert edit.date().toPython() == oczekiwana
