"""Okno „Kontrahenci…” (RejestrDialog) — tabela numerów kart FK jako ŹRÓDŁO
PRAWDY o numerach kontrahentów. Model pracy: każda edycja jest widoczna w
tabeli od razu, ale do bazy trafia dopiero po kliknięciu „Zapisz” — cały
zestaw zmian walidowany naraz i zapisywany w JEDNEJ transakcji; przy błędach
nic nie jest zapisane i nic nie jest cofane w tabeli.

Testy GUI działają na PRAWDZIWYCH widgetach (platforma offscreen), z oknem
aktywowanym (activateWindow + qWaitForWindowActive) — bez tego offscreen nie
dostarcza zdarzeń fokusu, więc edytor komórki nie zatwierdzałby danych przy
utracie fokusu tak jak w prawdziwej aplikacji. Modalne okna komunikatów
obsługuje _Okna (timer klikający wskazany przycisk w aktywnym oknie modalnym),
dzięki czemu testowane są prawdziwe QMessageBox z prawdziwymi przyciskami.

Druga część pliku — rdzeń zapisu zbiorczego RejestrNumerow.zapisz_zmiany."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import re
import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

import app.gui as gui_mod
from app.config import ConfigStore, Profile
from app.exporter import Kind, run_export
from app.gui import RejestrDialog
from app.kontrahenci_nr import RejestrNumerow, ZmianaWpisu, ZmianyOdrzucone, _klucz_seed
from app.ksef_client import InvoiceMetadata
from app.ledger import Ledger

PROFIL = "5252445719"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --------------------------------------------------------------------------- #
# infrastruktura testów GUI


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Okna:
    """Obsługa modalnych okien w trakcie testu: co 5 ms sprawdza aktywne okno
    modalne; QMessageBox -> zapisuje treść i klika przycisk z kolejki
    `odpowiedzi` (po tekście, bez '&') albo — gdy kolejka pusta — przycisk
    domyślny/Escape; inne modalne QDialog (wybór pliku, Scal z…, QInputDialog)
    -> zapisuje nazwę klasy i odrzuca (reject)."""

    def __init__(self):
        self.pokazane: list[dict] = []
        self.odpowiedzi: list[str] = []
        self.bledy: list[str] = []
        self._timer = QTimer()
        self._timer.setInterval(5)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    @staticmethod
    def _tekst(przycisk) -> str:
        return przycisk.text().replace("&", "")

    def _tick(self):
        w = QApplication.activeModalWidget()
        if w is None or not w.isVisible():
            return
        if isinstance(w, QMessageBox):
            przyciski = w.buttons()
            cel = None
            if self.odpowiedzi:
                oczekiwany = self.odpowiedzi.pop(0)
                cel = next((b for b in przyciski if self._tekst(b) == oczekiwany), None)
                if cel is None:
                    self.bledy.append(
                        f"brak przycisku {oczekiwany!r} w oknie {w.text()!r}: "
                        f"{[self._tekst(b) for b in przyciski]}"
                    )
            if cel is None:
                cel = w.escapeButton() or w.defaultButton() or przyciski[0]
            self.pokazane.append(
                {
                    "tekst": w.text(),
                    "szczegoly": w.detailedText(),
                    "przyciski": [self._tekst(b) for b in przyciski],
                    "klikniety": self._tekst(cel),
                }
            )
            cel.click()
        elif isinstance(w, QDialog):
            self.pokazane.append({"tekst": f"<{type(w).__name__}>", "szczegoly": "",
                                  "przyciski": [], "klikniety": "reject"})
            w.reject()

    def teksty(self) -> list[str]:
        return [p["tekst"] for p in self.pokazane]

    def stop(self):
        self._timer.stop()


@pytest.fixture
def okna(qapp):
    o = _Okna()
    yield o
    o.stop()
    assert o.bledy == []


@pytest.fixture
def env(tmp_path, monkeypatch, qapp):
    """Profil + config na dysku; RejestrNumerow() w dialogu otwiera bazę w
    katalogu danych aplikacji — %APPDATA% i app_data_dir (conftest) wskazują
    na katalog tymczasowy testu."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-tabela")
    profil = Profile("Firma", PROFIL, "tok", "f")
    cfg.profiles.append(profil)
    store.save(cfg)
    return SimpleNamespace(store=store, cfg=cfg, profil=profil, cfg_path=tmp_path / "config.enc")


STARTOWE = [
    # (pozycja, nip, nazwa, miejscowość, status)
    (1, "1111111111", "Alfa", "Poznań", "seed"),
    (2, "2222222222", "Beta", "Kraków", "seed"),
    (3, "3333333333", "Gamma", "Gdańsk", "auto"),
    (4, "4444444444", "Delta", "Łódź", "seed"),
]


def _zasiej(wpisy=STARTOWE):
    r = RejestrNumerow()
    for poz, nip, nazwa, miejsc, status in wpisy:
        r.utworz_wpis(PROFIL, pozycja=poz, nip=nip, nazwa=nazwa, miejscowosc=miejsc, status=status)
    r.close()


def _stan() -> dict[str, tuple]:
    """Stan BAZY (nie tabeli): klucz -> (pozycja, nip, nazwa, miejscowość, status)."""
    r = RejestrNumerow()
    try:
        return {w.klucz: (w.pozycja, w.nip, w.nazwa, w.miejscowosc, w.status) for w in r.lista(PROFIL)}
    finally:
        r.close()


def _aktywuj(dlg):
    dlg.activateWindow()
    assert QTest.qWaitForWindowActive(dlg)


def _otworz(env) -> RejestrDialog:
    dlg = RejestrDialog(env.profil, env.cfg, env.store)
    dlg.show()
    _aktywuj(dlg)
    return dlg


def _sprzatnij(dlg, okna):
    """Sprzątanie po teście: zamknięcie okna z odpowiedzią „Nie zapisuj”, gdy
    padnie pytanie o niezapisane zmiany (sam test sprawdził już, co miał)."""
    if dlg.isVisible():
        okna.odpowiedzi = ["Nie zapisuj"]
        dlg.done(0)
        QApplication.processEvents()
    okna.odpowiedzi = []


def _wiersz(dlg, nazwa: str) -> int:
    for row in range(dlg.table.rowCount()):
        if dlg.table.item(row, dlg.COL_NAZWA).text() == nazwa:
            return row
    raise AssertionError(f"brak wiersza {nazwa!r}")


def _edytuj(dlg, row: int, col: int, tekst: str, *, zatwierdz: bool = True) -> None:
    """Edycja komórki jak użytkownik: otwarcie edytora (jak dwuklik), zaznacz
    wszystko, wpisz tekst z klawiatury, Enter (zatwierdz=False — edytor zostaje
    otwarty, np. żeby sprawdzić klik „Zapisz” w trakcie edycji)."""
    _aktywuj(dlg)
    t = dlg.table
    t.setFocus()
    t.setCurrentCell(row, col)
    t.editItem(t.item(row, col))
    edytor = QApplication.focusWidget()
    assert edytor is not None and edytor is not t
    edytor.selectAll()
    QTest.keyClicks(edytor, tekst)
    if zatwierdz:
        QTest.keyClick(edytor, Qt.Key.Key_Return)
    QApplication.processEvents()


def _wybierz_status(dlg, row: int, status: str) -> None:
    """Status przez listę rozwijaną delegata: pierwsza litera wybiera pozycję
    listy (wyszukiwanie klawiaturą) i od razu zatwierdza (activated)."""
    _aktywuj(dlg)
    t = dlg.table
    t.setFocus()
    t.setCurrentCell(row, dlg.COL_STATUS)
    t.editItem(t.item(row, dlg.COL_STATUS))
    combo = QApplication.focusWidget()
    QTest.keyClicks(combo, status[0])
    QApplication.processEvents()


def _klik(widget) -> None:
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
    QApplication.processEvents()


def _klik_naglowek(dlg, col: int) -> None:
    h = dlg.table.horizontalHeader()
    x = h.sectionViewportPosition(col) + h.sectionSize(col) // 2
    QTest.mouseClick(h.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(x, h.height() // 2))
    QApplication.processEvents()


def _tlo(item):
    """Kolor tła komórki albo None, gdy brak wyróżnienia."""
    pedzel = item.background()
    if pedzel.style() == Qt.BrushStyle.NoBrush:
        return None
    return pedzel.color().name()


# --------------------------------------------------------------------------- #
# 1. nic nie trafia do bazy przed „Zapisz”; niezapisane zmiany są widoczne


def test_zgloszony_blad_edycja_po_sortowaniu_naglowkiem_trafia_do_wlasciwego(env, okna):
    """ZGŁOSZONY BŁĄD: po posortowaniu kliknięciem w nagłówek edycja nazwy
    „Alfa” -> „Alfa SA” szła pod innego kontrahenta (mapa wiersz->klucz nie
    była odświeżana po sortowaniu), kończyła się komunikatem „Kontrahent o tym
    NIP/kluczu już istnieje” i cofnięciem wpisanej wartości."""
    _zasiej()
    dlg = _otworz(env)
    try:
        _klik_naglowek(dlg, dlg.COL_NAZWA)
        _klik_naglowek(dlg, dlg.COL_NAZWA)  # drugi klik: malejąco — Alfa na dole
        assert dlg.table.item(0, dlg.COL_NAZWA).text() != "Alfa"
        _edytuj(dlg, _wiersz(dlg, "Alfa"), dlg.COL_NAZWA, "Alfa SA")
        _klik(dlg.btn_zapisz)

        stan = _stan()
        assert stan["1111111111"][:4] == (1, "1111111111", "Alfa SA", "Poznań")
        assert stan["2222222222"][2] == "Beta"
        assert stan["3333333333"][2] == "Gamma"
        assert stan["4444444444"][2] == "Delta"
        assert okna.pokazane == []  # żadnego komunikatu o błędzie
        assert dlg.table.item(_wiersz(dlg, "Alfa SA"), dlg.COL_NAZWA).text() == "Alfa SA"
    finally:
        _sprzatnij(dlg, okna)


def test_edycja_komorek_nie_zapisuje_do_bazy_przed_zapisz(env, okna):
    _zasiej()
    przed = _stan()
    dlg = _otworz(env)
    try:
        _edytuj(dlg, _wiersz(dlg, "Beta"), dlg.COL_NAZWA, "Beta Nowa")
        _edytuj(dlg, _wiersz(dlg, "Gamma"), dlg.COL_MIEJSCOWOSC, "Sopot")
        _wybierz_status(dlg, _wiersz(dlg, "Delta"), "duplikat")

        assert _stan() == przed  # baza nietknięta
        # wpisane wartości są w tabeli (nic nie cofnięte)
        assert dlg.table.item(_wiersz(dlg, "Beta Nowa"), dlg.COL_NAZWA).text() == "Beta Nowa"
        assert dlg.table.item(_wiersz(dlg, "Gamma"), dlg.COL_MIEJSCOWOSC).text() == "Sopot"
        assert dlg.table.item(_wiersz(dlg, "Delta"), dlg.COL_STATUS).text() == "duplikat"
        # zmienione komórki wyróżnione, niezmienione nie
        row = _wiersz(dlg, "Beta Nowa")
        assert _tlo(dlg.table.item(row, dlg.COL_NAZWA)) is not None
        assert _tlo(dlg.table.item(row, dlg.COL_NIP)) is None
        assert _tlo(dlg.table.item(_wiersz(dlg, "Alfa"), dlg.COL_NAZWA)) is None
        assert "Niezapisane zmiany: 3" in dlg.licznik_label.text()

        _klik(dlg.btn_zapisz)
        stan = _stan()
        assert stan["2222222222"][2] == "Beta Nowa"
        assert stan["3333333333"][3] == "Sopot"
        assert stan["4444444444"][4] == "duplikat"
        assert "Niezapisane" not in dlg.licznik_label.text()
        assert _tlo(dlg.table.item(_wiersz(dlg, "Beta Nowa"), dlg.COL_NAZWA)) is None
    finally:
        _sprzatnij(dlg, okna)


def test_edycja_w_toku_nie_ginie_przy_kliknieciu_zapisz(env, okna):
    """Księgowa wpisuje wartość i od razu klika „Zapisz” — bez Enter i bez
    wychodzenia z komórki. Otwarty edytor musi zostać zatwierdzony."""
    _zasiej()
    dlg = _otworz(env)
    try:
        _edytuj(dlg, _wiersz(dlg, "Alfa"), dlg.COL_NAZWA, "Alfa w toku", zatwierdz=False)
        _klik(dlg.btn_zapisz)
        assert _stan()["1111111111"][2] == "Alfa w toku"
    finally:
        _sprzatnij(dlg, okna)


def test_dodaj_wiersz_zapisuje_dopiero_po_zapisz(env, okna):
    _zasiej()
    przed = _stan()
    dlg = _otworz(env)
    try:
        _klik(dlg.btn_add)
        QApplication.processEvents()
        # edytor NIP-u nowego wiersza otwiera się od razu — wpisz NIP
        edytor = QApplication.focusWidget()
        assert edytor is not None and edytor is not dlg.table
        edytor.selectAll()
        QTest.keyClicks(edytor, "5555555555")
        QTest.keyClick(edytor, Qt.Key.Key_Return)
        QApplication.processEvents()
        row = dlg.table.currentRow()
        _edytuj(dlg, row, dlg.COL_NAZWA, "Epsilon")

        assert _stan() == przed
        row = _wiersz(dlg, "Epsilon")
        assert dlg.table.item(row, dlg.COL_POZYCJA).text() == "5"  # max+1
        kolor_nowego = _tlo(dlg.table.item(row, dlg.COL_MIEJSCOWOSC))
        assert kolor_nowego is not None
        assert "Niezapisane zmiany: 1" in dlg.licznik_label.text()

        _klik(dlg.btn_zapisz)
        assert _stan()["5555555555"] == (5, "5555555555", "Epsilon", "", "manual")
    finally:
        _sprzatnij(dlg, okna)


def test_usun_zaznaczone_oznacza_a_usuwa_dopiero_po_zapisz(env, okna):
    _zasiej()
    przed = _stan()
    dlg = _otworz(env)
    try:
        dlg.table.selectRow(_wiersz(dlg, "Gamma"))
        okna.odpowiedzi = ["Yes"]
        _klik(dlg.btn_delete)
        assert len(okna.pokazane) == 1  # potwierdzenie jak dotąd

        assert _stan() == przed  # jeszcze w bazie
        row = _wiersz(dlg, "Gamma")
        assert dlg.table.item(row, dlg.COL_NAZWA).font().strikeOut()
        assert "Niezapisane zmiany: 1" in dlg.licznik_label.text()

        _aktywuj(dlg)
        _klik(dlg.btn_zapisz)
        stan = _stan()
        assert "3333333333" not in stan
        assert set(stan) == {"1111111111", "2222222222", "4444444444"}
    finally:
        _sprzatnij(dlg, okna)


def test_usun_zaznaczone_drugi_raz_przywraca_wiersz(env, okna):
    _zasiej()
    dlg = _otworz(env)
    try:
        dlg.table.selectRow(_wiersz(dlg, "Gamma"))
        okna.odpowiedzi = ["Yes"]
        _klik(dlg.btn_delete)
        _aktywuj(dlg)
        dlg.table.selectRow(_wiersz(dlg, "Gamma"))
        assert dlg.btn_delete.text() == "Przywróć zaznaczone"
        _klik(dlg.btn_delete)
        assert not dlg.table.item(_wiersz(dlg, "Gamma"), dlg.COL_NAZWA).font().strikeOut()
        assert "Niezapisane" not in dlg.licznik_label.text()
    finally:
        _sprzatnij(dlg, okna)


def test_wklejanie_ctrl_v_zapisuje_dopiero_po_zapisz(env, okna):
    _zasiej()
    przed = _stan()
    dlg = _otworz(env)
    try:
        start = _wiersz(dlg, "Gamma")  # sortowanie po pozycji: Gamma (3), Delta (4)
        QApplication.clipboard().setText("Gamma Wklejona\tGdynia\nDelta Wklejona\tOpole\nZeta\tRadom\n")
        dlg.table.setFocus()
        dlg.table.setCurrentCell(start, dlg.COL_NAZWA)
        okna.odpowiedzi = []
        QTest.keySequence(dlg.table, _ctrl_v())
        QApplication.processEvents()

        assert _stan() == przed
        assert dlg.table.item(_wiersz(dlg, "Gamma Wklejona"), dlg.COL_MIEJSCOWOSC).text() == "Gdynia"
        assert dlg.table.item(_wiersz(dlg, "Delta Wklejona"), dlg.COL_MIEJSCOWOSC).text() == "Opole"
        nowy = _wiersz(dlg, "Zeta")
        assert dlg.table.item(nowy, dlg.COL_POZYCJA).text() == "5"
        assert "Niezapisane zmiany: 3" in dlg.licznik_label.text()

        _aktywuj(dlg)
        _klik(dlg.btn_zapisz)
        stan = _stan()
        assert stan["3333333333"][2:4] == ("Gamma Wklejona", "Gdynia")
        assert stan["4444444444"][2:4] == ("Delta Wklejona", "Opole")
        klucz_zety = next(k for k, v in stan.items() if v[2] == "Zeta")
        assert stan[klucz_zety][0] == 5 and stan[klucz_zety][3] == "Radom"
    finally:
        _sprzatnij(dlg, okna)


def _ctrl_v():
    from PySide6.QtGui import QKeySequence

    return QKeySequence(QKeySequence.StandardKey.Paste)


def test_wklejanie_przy_filtrze_trafia_tylko_w_widoczne_wiersze(env, okna):
    """Filtr ukrywa wiersze pomiędzy — wklejenie dwóch linii nie może trafić
    w ukryty (niewidoczny dla księgowej) wiersz."""
    _zasiej([
        (1, "1111111111", "Alfa", "Poznań", "seed"),
        (2, "2222222222", "Beta", "Kraków", "seed"),
        (3, "3333333333", "Alfa Bis", "Gdańsk", "seed"),
    ])
    dlg = _otworz(env)
    try:
        dlg.search.setText("Alfa")
        QApplication.clipboard().setText("Poz A\nPoz B")
        dlg.table.setFocus()
        dlg.table.setCurrentCell(_wiersz(dlg, "Alfa"), dlg.COL_MIEJSCOWOSC)
        QTest.keySequence(dlg.table, _ctrl_v())
        QApplication.processEvents()
        _aktywuj(dlg)
        _klik(dlg.btn_zapisz)
        stan = _stan()
        assert stan["1111111111"][3] == "Poz A"
        assert stan["3333333333"][3] == "Poz B"
        assert stan["2222222222"][3] == "Kraków"  # ukryty wiersz nietknięty
    finally:
        _sprzatnij(dlg, okna)


def test_pola_kont_ksiegowych_czekaja_na_zapisz(env, okna, monkeypatch):
    _zasiej()
    zapisy = []
    oryginalny_save = env.store.save
    monkeypatch.setattr(env.store, "save", lambda c: (zapisy.append(1), oryginalny_save(c)))
    dlg = _otworz(env)
    try:
        pole = dlg.konto_sprzedaz_netto
        pole.setFocus()
        pole.selectAll()
        QTest.keyClicks(pole, "701")
        QTest.keyClick(pole, Qt.Key.Key_Return)  # editingFinished
        dlg.table.setFocus()  # utrata fokusu — też editingFinished
        QApplication.processEvents()
        assert zapisy == []
        assert env.profil.konta_override == {}
        assert "Niezapisane zmiany: 1" in dlg.licznik_label.text()
        assert okna.pokazane == []  # Enter w polu nie uruchomił żadnej operacji

        _klik(dlg.btn_zapisz)
        assert zapisy == [1]
        assert env.profil.konta_override == {"konto_sprzedaz_netto": "701"}
        assert ConfigStore(env.cfg_path).unlock("haslo-tabela").profiles[0].konta_override == {
            "konto_sprzedaz_netto": "701"
        }
    finally:
        _sprzatnij(dlg, okna)


def test_edytor_statusu_nie_podmienia_wartosci_spoza_listy(env, okna):
    """Status spoza listy (np. wklejony z arkusza): samo otwarcie i zamknięcie
    listy rozwijanej (wyjście z komórki bez wyboru) podmieniało go po cichu
    na pierwszą pozycję listy („seed”). Wartość ma zostać — błędny status
    zgłasza dopiero „Zapisz”."""
    _zasiej()
    dlg = _otworz(env)
    try:
        QApplication.clipboard().setText("xyz")
        dlg.table.setFocus()
        dlg.table.setCurrentCell(_wiersz(dlg, "Beta"), dlg.COL_STATUS)
        QTest.keySequence(dlg.table, _ctrl_v())
        QApplication.processEvents()
        _aktywuj(dlg)
        row = _wiersz(dlg, "Beta")
        dlg.table.setFocus()
        dlg.table.setCurrentCell(row, dlg.COL_STATUS)
        dlg.table.editItem(dlg.table.item(row, dlg.COL_STATUS))
        QApplication.processEvents()
        dlg.table.setCurrentCell(_wiersz(dlg, "Alfa"), dlg.COL_NAZWA)  # wyjście z komórki
        QApplication.processEvents()
        assert dlg.table.item(_wiersz(dlg, "Beta"), dlg.COL_STATUS).text() == "xyz"

        _klik(dlg.btn_zapisz)
        tresc = okna.pokazane[-1]["tekst"]
        assert "Nie zapisano" in tresc and "Status" in tresc and "xyz" in tresc
        assert _stan()["2222222222"][4] == "seed"  # w bazie bez zmian
    finally:
        _sprzatnij(dlg, okna)


def test_enter_poza_przyciskiem_nie_uruchamia_operacji(env, okna, bez_plikow):
    """Enter w szukajce, w polu konta i w tabeli (poza edycją) „klikał”
    przycisk domyślny okna — pierwszy przycisk z autoDefault, czyli
    „Importuj kartotekę…”. W arkuszu Enter to naturalny klawisz — nie może
    uruchamiać operacji hurtowej."""
    _zasiej()
    dlg = _otworz(env)
    try:
        dlg.search.setFocus()
        QTest.keyClick(dlg.search, Qt.Key.Key_Return)
        dlg.konto_prefix_koszt.setFocus()
        QTest.keyClick(dlg.konto_prefix_koszt, Qt.Key.Key_Return)
        dlg.table.setFocus()
        dlg.table.setCurrentCell(0, dlg.COL_NAZWA)
        QTest.keyClick(dlg.table, Qt.Key.Key_Return)
        QApplication.processEvents()
        assert okna.pokazane == []
        assert bez_plikow == []
        assert dlg.isVisible()
    finally:
        _sprzatnij(dlg, okna)


# --------------------------------------------------------------------------- #
# 2. „Zapisz” waliduje cały zestaw naraz, atomowo, bez cofania wartości


def test_zapisz_przy_bledach_nic_nie_zapisuje_i_nic_nie_cofa(env, okna):
    _zasiej()
    przed = _stan()
    dlg = _otworz(env)
    try:
        _edytuj(dlg, _wiersz(dlg, "Alfa"), dlg.COL_NAZWA, "Alfa Poprawna")  # poprawna zmiana
        _edytuj(dlg, _wiersz(dlg, "Beta"), dlg.COL_POZYCJA, "0")  # pozycja < 1
        _edytuj(dlg, _wiersz(dlg, "Gamma"), dlg.COL_POZYCJA, "4")  # koliduje z Deltą
        _edytuj(dlg, _wiersz(dlg, "Delta"), dlg.COL_NIP, "1111111111")  # NIP Alfy

        _klik(dlg.btn_zapisz)
        assert _stan() == przed  # NIC nie zapisane — także poprawna zmiana Alfy
        # wartości wpisane zostają w tabeli
        assert dlg.table.item(_wiersz(dlg, "Alfa Poprawna"), dlg.COL_NAZWA).text() == "Alfa Poprawna"
        assert dlg.table.item(_wiersz(dlg, "Beta"), dlg.COL_POZYCJA).text() == "0"
        assert dlg.table.item(_wiersz(dlg, "Gamma"), dlg.COL_POZYCJA).text() == "4"
        assert dlg.table.item(_wiersz(dlg, "Delta"), dlg.COL_NIP).text() == "1111111111"
        # komunikat z listą: kto i co poprawić
        assert len(okna.pokazane) == 1
        tresc = okna.pokazane[0]["tekst"] + "\n" + okna.pokazane[0]["szczegoly"]
        assert "Beta" in tresc and "Gamma" in tresc and "Delta" in tresc
        assert "Nie zapisano" in tresc
        # komórki z błędem wyróżnione inaczej niż zwykła zmiana
        kolor_bledu = _tlo(dlg.table.item(_wiersz(dlg, "Beta"), dlg.COL_POZYCJA))
        kolor_zmiany = _tlo(dlg.table.item(_wiersz(dlg, "Alfa Poprawna"), dlg.COL_NAZWA))
        assert kolor_bledu is not None and kolor_bledu != kolor_zmiany
        assert _tlo(dlg.table.item(_wiersz(dlg, "Delta"), dlg.COL_NIP)) == kolor_bledu
        assert _tlo(dlg.table.item(_wiersz(dlg, "Gamma"), dlg.COL_POZYCJA)) == kolor_bledu

        # poprawka i ponowny „Zapisz” -> wszystko naraz
        _edytuj(dlg, _wiersz(dlg, "Beta"), dlg.COL_POZYCJA, "12")
        _edytuj(dlg, _wiersz(dlg, "Gamma"), dlg.COL_POZYCJA, "13")
        _edytuj(dlg, _wiersz(dlg, "Delta"), dlg.COL_NIP, "4444444444")
        _klik(dlg.btn_zapisz)
        stan = _stan()
        assert stan["1111111111"][2] == "Alfa Poprawna"
        assert stan["2222222222"][0] == 12
        assert stan["3333333333"][0] == 13
        assert stan["4444444444"][0] == 4
        assert len(okna.pokazane) == 1  # drugi zapis bez komunikatu błędu
    finally:
        _sprzatnij(dlg, okna)


def test_blad_w_wierszu_ukrytym_filtrem_zdejmuje_filtr(env, okna):
    """Wiersz z błędem ukryty przez szukajkę — po „Zapisz” filtr znika, żeby
    księgowa widziała czerwoną komórkę, którą ma poprawić."""
    _zasiej()
    dlg = _otworz(env)
    try:
        _edytuj(dlg, _wiersz(dlg, "Beta"), dlg.COL_POZYCJA, "3")  # koliduje z Gammą
        dlg.search.setText("Alfa")
        assert dlg.table.isRowHidden(_wiersz(dlg, "Beta"))
        _klik(dlg.btn_zapisz)
        assert dlg.search.text() == ""
        row = _wiersz(dlg, "Beta")
        assert not dlg.table.isRowHidden(row)
        assert _tlo(dlg.table.item(row, dlg.COL_POZYCJA)) == gui_mod.RejestrDialog.KOLOR_BLAD.name()
        assert dlg.table.currentRow() == row  # kursor na pierwszym błędzie
    finally:
        _sprzatnij(dlg, okna)


def test_nowy_wiersz_oznaczony_do_usuniecia_nie_trafia_do_bazy(env, okna):
    _zasiej()
    przed = _stan()
    dlg = _otworz(env)
    try:
        _klik(dlg.btn_add)
        _aktywuj(dlg)
        dlg.table.selectRow(_wiersz(dlg, "Nowy kontrahent"))
        okna.odpowiedzi = ["Yes"]
        _klik(dlg.btn_delete)
        assert "Niezapisane" not in dlg.licznik_label.text()
        _aktywuj(dlg)
        _klik(dlg.btn_zapisz)
        assert _stan() == przed
    finally:
        _sprzatnij(dlg, okna)
    assert okna.teksty()[-1:] != ["Zapisać zmiany?"]  # nic do zapisania przy zamknięciu


def test_zamiana_numerow_dwoch_kontrahentow_jednym_zapisz(env, okna):
    _zasiej([
        (5, "1111111111", "Alfa", "Poznań", "seed"),
        (7, "2222222222", "Beta", "Kraków", "seed"),
    ])
    dlg = _otworz(env)
    try:
        _edytuj(dlg, _wiersz(dlg, "Alfa"), dlg.COL_POZYCJA, "7")
        _edytuj(dlg, _wiersz(dlg, "Beta"), dlg.COL_POZYCJA, "5")
        _klik(dlg.btn_zapisz)
        stan = _stan()
        assert stan["1111111111"][0] == 7
        assert stan["2222222222"][0] == 5
        assert okna.pokazane == []
    finally:
        _sprzatnij(dlg, okna)


def test_status_po_zapisie_manual_chyba_ze_ustawiony_recznie(env, okna):
    _zasiej()
    dlg = _otworz(env)
    try:
        _edytuj(dlg, _wiersz(dlg, "Alfa"), dlg.COL_NAZWA, "Alfa 2")  # seed -> manual
        _edytuj(dlg, _wiersz(dlg, "Gamma"), dlg.COL_MIEJSCOWOSC, "Sopot")  # auto -> manual
        _edytuj(dlg, _wiersz(dlg, "Beta"), dlg.COL_NAZWA, "Beta 2")  # seed -> manual
        _wybierz_status(dlg, _wiersz(dlg, "Delta"), "auto")  # księgowa ustawiła auto
        _klik(dlg.btn_zapisz)
        stan = _stan()
        assert stan["1111111111"][4] == "manual"
        assert stan["3333333333"][4] == "manual"
        assert stan["2222222222"][4] == "manual"
        assert stan["4444444444"][4] == "auto"
        # zapisane "manual" wyróżnione pogrubieniem jak dotąd
        assert dlg.table.item(_wiersz(dlg, "Alfa 2"), dlg.COL_NAZWA).font().bold()
    finally:
        _sprzatnij(dlg, okna)


# --------------------------------------------------------------------------- #
# 4. klucz wiersza z UserRole — filtr, zmiana pozycji przesuwająca wiersz, NIP


def test_edycja_przy_filtrze_i_zmiana_nip_migruje_klucz_i_aliasy(env, okna):
    _zasiej()
    r = RejestrNumerow()
    r.ustaw(PROFIL, "9999999999", 9, nip="9999999999", nazwa="Gamma Duplikat", status="duplikat")
    r.dodaj_alias(PROFIL, "9999999999", "3333333333", zwolnij_numer=False)
    r.close()
    dlg = _otworz(env)
    try:
        dlg.search.setText("Gam")
        _edytuj(dlg, _wiersz(dlg, "Gamma"), dlg.COL_NIP, "3333333339")
        _klik(dlg.btn_zapisz)
        stan = _stan()
        assert "3333333333" not in stan
        assert stan["3333333339"][:3] == (3, "3333333339", "Gamma")
        assert stan["1111111111"][1] == "1111111111"  # inni nietknięci
        r = RejestrNumerow()
        # alias duplikatu idzie za Gammą; stary NIP Gammy (klucz jej faktur
        # z KSeF) wskazuje odtąd na jej nowy klucz — ta sama karta
        assert r.aliasy(PROFIL) == {"9999999999": "3333333339", "3333333333": "3333333339"}
        r.close()
    finally:
        _sprzatnij(dlg, okna)


def test_zmiana_pozycji_przesuwa_wiersz_a_kolejna_edycja_trafia_do_niego(env, okna):
    _zasiej()
    dlg = _otworz(env)
    try:
        _edytuj(dlg, _wiersz(dlg, "Alfa"), dlg.COL_POZYCJA, "10")
        # sortowanie po Pozycji: Alfa przeskoczyła na koniec tabeli
        assert _wiersz(dlg, "Alfa") == dlg.table.rowCount() - 1
        _edytuj(dlg, _wiersz(dlg, "Alfa"), dlg.COL_NAZWA, "Alfa Przesunieta")
        _edytuj(dlg, _wiersz(dlg, "Beta"), dlg.COL_NAZWA, "Beta B")
        _klik(dlg.btn_zapisz)
        stan = _stan()
        assert stan["1111111111"][:3] == (10, "1111111111", "Alfa Przesunieta")
        assert stan["2222222222"][:3] == (2, "2222222222", "Beta B")
        assert okna.pokazane == []
    finally:
        _sprzatnij(dlg, okna)


def test_zapisz_zachowuje_sortowanie_uzytkownika(env, okna):
    """Po „Zapisz” tabela nie wraca do sortowania po Pozycji — wiersze nie
    przeskakują pod kursorem księgowej."""
    _zasiej()
    dlg = _otworz(env)
    try:
        _klik_naglowek(dlg, dlg.COL_NAZWA)  # rosnąco po nazwie
        _edytuj(dlg, _wiersz(dlg, "Delta"), dlg.COL_MIEJSCOWOSC, "Sopot")
        _klik(dlg.btn_zapisz)
        nazwy = [dlg.table.item(r, dlg.COL_NAZWA).text() for r in range(dlg.table.rowCount())]
        assert nazwy == ["Alfa", "Beta", "Delta", "Gamma"]
    finally:
        _sprzatnij(dlg, okna)


# --------------------------------------------------------------------------- #
# 5. zamknięcie okna z niezapisanymi zmianami


@pytest.mark.parametrize("sposob", ["escape", "x", "zamknij"])
def test_zamkniecie_z_niezapisanymi_pyta_anuluj_zostawia_okno(env, okna, sposob):
    _zasiej()
    przed = _stan()
    dlg = _otworz(env)
    try:
        _edytuj(dlg, _wiersz(dlg, "Alfa"), dlg.COL_NAZWA, "Alfa X")
        okna.odpowiedzi = ["Anuluj"]
        _zamknij_sposobem(dlg, sposob)
        assert okna.teksty() == ["Zapisać zmiany?"]
        assert sorted(okna.pokazane[0]["przyciski"]) == sorted(["Zapisz", "Nie zapisuj", "Anuluj"])
        assert dlg.isVisible()
        assert _stan() == przed
        assert dlg.table.item(_wiersz(dlg, "Alfa X"), dlg.COL_NAZWA).text() == "Alfa X"
    finally:
        _sprzatnij(dlg, okna)


def _zamknij_sposobem(dlg, sposob: str) -> None:
    _aktywuj(dlg)
    if sposob == "escape":
        dlg.table.setFocus()
        QTest.keyClick(dlg.table, Qt.Key.Key_Escape)
    elif sposob == "x":
        dlg.close()
    else:
        from PySide6.QtWidgets import QDialogButtonBox

        box = dlg.findChild(QDialogButtonBox)
        _klik(box.button(QDialogButtonBox.StandardButton.Close))
    QApplication.processEvents()


def test_zamkniecie_zapisz_zapisuje_i_zamyka(env, okna):
    _zasiej()
    dlg = _otworz(env)
    _edytuj(dlg, _wiersz(dlg, "Alfa"), dlg.COL_NAZWA, "Alfa Z")
    okna.odpowiedzi = ["Zapisz"]
    dlg.close()
    QApplication.processEvents()
    assert not dlg.isVisible()
    assert _stan()["1111111111"][2] == "Alfa Z"


def test_zamkniecie_nie_zapisuj_zamyka_bez_zapisu(env, okna):
    _zasiej()
    przed = _stan()
    dlg = _otworz(env)
    _edytuj(dlg, _wiersz(dlg, "Alfa"), dlg.COL_NAZWA, "Alfa N")
    okna.odpowiedzi = ["Nie zapisuj"]
    dlg.close()
    QApplication.processEvents()
    assert not dlg.isVisible()
    assert _stan() == przed


def test_zamkniecie_zapisz_z_bledem_walidacji_zostawia_okno(env, okna):
    _zasiej()
    przed = _stan()
    dlg = _otworz(env)
    try:
        _edytuj(dlg, _wiersz(dlg, "Alfa"), dlg.COL_POZYCJA, "2")  # kolizja z Betą
        okna.odpowiedzi = ["Zapisz"]
        dlg.close()
        QApplication.processEvents()
        assert dlg.isVisible()
        assert _stan() == przed
        assert len(okna.pokazane) == 2  # pytanie + lista błędów
    finally:
        _sprzatnij(dlg, okna)


def test_zamkniecie_bez_zmian_nie_pyta(env, okna):
    _zasiej()
    dlg = _otworz(env)
    dlg.close()
    QApplication.processEvents()
    assert not dlg.isVisible()
    assert okna.pokazane == []


def test_zamkniecie_z_edytowanym_polem_konta_pyta(env, okna):
    """Dawniej done() zapisywało konto po cichu — teraz tak samo jak tabela:
    pytanie o zapis."""
    _zasiej()
    dlg = _otworz(env)
    dlg.konto_prefix_koszt.setFocus()
    dlg.konto_prefix_koszt.selectAll()
    QTest.keyClicks(dlg.konto_prefix_koszt, "999-9")
    okna.odpowiedzi = ["Nie zapisuj"]
    QTest.keyClick(dlg.konto_prefix_koszt, Qt.Key.Key_Escape)
    QApplication.processEvents()
    assert okna.teksty() == ["Zapisać zmiany?"]
    assert not dlg.isVisible()
    assert env.profil.konta_override == {}


# --------------------------------------------------------------------------- #
# 6. operacje hurtowe przy niezapisanych zmianach


OPERACJE_HURTOWE = ["btn_import", "btn_sync_sql", "btn_sync", "btn_merge", "btn_wyczysc", "btn_export"]


@pytest.fixture
def bez_plikow(monkeypatch):
    """Rejestr wywołań okien wyboru pliku / potwierdzeń — zamiast prawdziwych."""
    wolania = []

    def otworz(*a, **k):
        wolania.append("getOpenFileName")
        return "", ""

    def zapisz(*a, **k):
        wolania.append("getSaveFileName")
        return "", ""

    def tekst(*a, **k):
        wolania.append("getText")
        return "", False

    monkeypatch.setattr(gui_mod.QFileDialog, "getOpenFileName", staticmethod(otworz))
    monkeypatch.setattr(gui_mod.QFileDialog, "getSaveFileName", staticmethod(zapisz))
    monkeypatch.setattr(gui_mod.QInputDialog, "getText", staticmethod(tekst))
    return wolania


@pytest.mark.parametrize("przycisk", OPERACJE_HURTOWE)
def test_operacja_hurtowa_przy_niezapisanych_pyta_i_anuluj_nic_nie_robi(
    env, okna, bez_plikow, monkeypatch, przycisk
):
    _zasiej()
    env.profil.db_symfonia = "BAZA"
    przed = _stan()
    sync_sql = []
    monkeypatch.setattr(
        RejestrNumerow, "synchronizuj_z_sql", lambda self, *a, **k: sync_sql.append(a)
    )
    dlg = _otworz(env)
    try:
        _edytuj(dlg, _wiersz(dlg, "Beta"), dlg.COL_NAZWA, "Beta Niezapisana")
        dlg.table.selectRow(_wiersz(dlg, "Beta Niezapisana"))
        okna.odpowiedzi = ["Anuluj"]
        _klik(getattr(dlg, przycisk))
        assert len(okna.pokazane) == 1
        assert "niezapisane zmiany" in okna.pokazane[0]["tekst"]
        assert sorted(okna.pokazane[0]["przyciski"]) == ["Anuluj", "Zapisz i kontynuuj"]
        assert bez_plikow == [] and sync_sql == []  # operacja nie ruszyła
        assert _stan() == przed
        assert dlg.table.item(_wiersz(dlg, "Beta Niezapisana"), dlg.COL_NAZWA).text() == "Beta Niezapisana"
    finally:
        _sprzatnij(dlg, okna)


def test_operacja_hurtowa_zapisz_i_kontynuuj(env, okna, bez_plikow):
    _zasiej()
    dlg = _otworz(env)
    try:
        _edytuj(dlg, _wiersz(dlg, "Beta"), dlg.COL_NAZWA, "Beta Zapisana")
        okna.odpowiedzi = ["Zapisz i kontynuuj"]
        _klik(dlg.btn_export)
        assert _stan()["2222222222"][2] == "Beta Zapisana"
        assert bez_plikow == ["getSaveFileName"]  # po zapisie operacja ruszyła
    finally:
        _sprzatnij(dlg, okna)


def test_operacja_hurtowa_bez_zmian_nie_pyta(env, okna, bez_plikow):
    _zasiej()
    dlg = _otworz(env)
    try:
        _klik(dlg.btn_export)
        assert okna.pokazane == []
        assert bez_plikow == ["getSaveFileName"]
    finally:
        _sprzatnij(dlg, okna)


def test_synchronizacja_z_pliku_pokazuje_pominiete_reczne(env, okna, monkeypatch, tmp_path):
    """Wpis ustawiony ręcznie przez księgową (manual) nie jest nadpisywany przez
    synchronizację — raport w oknie wymienia go osobno."""
    from openpyxl import Workbook

    _zasiej([
        (5, "1111111111", "Alfa", "Poznań", "manual"),
        (2, "2222222222", "Beta", "Kraków", "seed"),
    ])
    sciezka = tmp_path / "kartoteka.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Pozycja", "Kod", "Nazwa", "NIP", "Adres", "Kod pocztowy", "Miejscowość", "Rejon"])
    ws.append([8, "A", "Alfa", "1111111111", "", "", "Poznań", ""])
    ws.append([2, "B", "Beta", "2222222222", "", "", "Kraków", ""])
    wb.save(sciezka)
    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(sciezka), ""))
    )
    dlg = _otworz(env)
    try:
        _klik(dlg.btn_sync)
        assert _stan()["1111111111"][0] == 5  # nie nadpisane
        raport = okna.pokazane[-1]
        assert "ustawione ręcznie" in raport["szczegoly"]
        assert "Alfa" in raport["szczegoly"] and "5" in raport["szczegoly"] and "8" in raport["szczegoly"]
    finally:
        _sprzatnij(dlg, okna)


# --------------------------------------------------------------------------- #
# rdzeń: RejestrNumerow.zapisz_zmiany — walidacja całości + jedna transakcja


def _rej(tmp_path, wpisy=STARTOWE) -> RejestrNumerow:
    r = RejestrNumerow(tmp_path / "nr.db")
    for poz, nip, nazwa, miejsc, status in wpisy:
        r.utworz_wpis(PROFIL, pozycja=poz, nip=nip, nazwa=nazwa, miejscowosc=miejsc, status=status)
    return r


def _stan_rej(r) -> dict[str, tuple]:
    return {w.klucz: (w.pozycja, w.nip, w.nazwa, w.miejscowosc, w.status) for w in r.lista(PROFIL)}


def test_zapisz_zmiany_zamiana_pozycji_i_nowy_wpis(tmp_path):
    r = _rej(tmp_path)
    mapa = r.zapisz_zmiany(
        PROFIL,
        [
            ZmianaWpisu("1111111111", "1111111111", "2", "1111111111", "Alfa", "Poznań", "manual"),
            ZmianaWpisu("2222222222", "2222222222", 1, "2222222222", "Beta", "Kraków", "manual"),
            ZmianaWpisu("nowy:1", None, "5", "5555555555", "Epsilon", "", "manual"),
        ],
    )
    stan = _stan_rej(r)
    assert stan["1111111111"][0] == 2 and stan["2222222222"][0] == 1
    assert stan["5555555555"] == (5, "5555555555", "Epsilon", "", "manual")
    assert mapa == {"1111111111": "1111111111", "2222222222": "2222222222", "nowy:1": "5555555555"}
    r.close()


def test_zapisz_zmiany_usuniecie_zwalnia_pozycje_w_tej_samej_transakcji(tmp_path):
    r = _rej(tmp_path)
    r.zapisz_zmiany(
        PROFIL,
        [ZmianaWpisu("4444444444", "4444444444", "3", "4444444444", "Delta", "Łódź", "manual")],
        usuniete=["3333333333"],
    )
    stan = _stan_rej(r)
    assert "3333333333" not in stan
    assert stan["4444444444"][0] == 3
    r.close()


def test_zapisz_zmiany_lancuch_zmian_kluczy_bez_kolizji(tmp_path):
    """A przejmuje NIP B, B dostaje nowy NIP — w stanie końcowym klucze są
    unikalne, więc zapis musi przejść (kolejność operacji nie może blokować)."""
    r = _rej(tmp_path)
    r.dodaj_alias(PROFIL, "ALIAS-B", "2222222222", zwolnij_numer=True)
    r.zapisz_zmiany(
        PROFIL,
        [
            ZmianaWpisu("1111111111", "1111111111", 1, "2222222222", "Alfa", "Poznań", "manual"),
            ZmianaWpisu("2222222222", "2222222222", 2, "6666666666", "Beta", "Kraków", "manual"),
        ],
    )
    stan = _stan_rej(r)
    assert stan["2222222222"][2] == "Alfa"
    assert stan["6666666666"][2] == "Beta"
    assert "1111111111" not in stan
    # alias wskazujący na starego B idzie za B (nie za nowym właścicielem klucza);
    # stary klucz A (1111111111) -> nowy klucz A; stary klucz B (2222222222)
    # BEZ aliasu — przejął go A, więc faktury z tym NIP-em idą do A
    assert r.aliasy(PROFIL) == {"ALIAS-B": "6666666666", "1111111111": "2222222222"}
    r.close()


def test_zapisz_zmiany_waliduje_wszystko_i_nic_nie_zapisuje(tmp_path):
    r = _rej(tmp_path)
    przed = _stan_rej(r)
    with pytest.raises(ZmianyOdrzucone) as exc:
        r.zapisz_zmiany(
            PROFIL,
            [
                ZmianaWpisu("1111111111", "1111111111", 1, "1111111111", "Alfa OK", "Poznań", "manual"),
                ZmianaWpisu("2222222222", "2222222222", "abc", "2222222222", "Beta", "", "manual"),
                ZmianaWpisu("3333333333", "3333333333", 4, "3333333333", "Gamma", "", "manual"),
                ZmianaWpisu("4444444444", "4444444444", 4, "", "", "", "manual"),
                ZmianaWpisu("nowy:1", None, 9, "1111111111", "Kopia Alfy", "", "manual"),
                ZmianaWpisu("nowy:2", None, 10, "7777777777", "Zły status", "", "cokolwiek"),
            ],
        )
    bledy = {(b.id, b.pole) for b in exc.value.bledy}
    assert ("2222222222", "pozycja") in bledy
    assert ("3333333333", "pozycja") in bledy  # 4 zajęte też przez Deltę (zmienianą)
    assert ("4444444444", "nazwa") in bledy  # brak NIP i nazwy
    # Delta nie zmienia pozycji (4) — to Gamma weszła na jej numer, więc błąd
    # pozycji dostaje tylko Gamma; tak samo Alfa (klucz bez zmian) nie dostaje
    # błędu za to, że nowy wiersz podał jej NIP
    assert ("4444444444", "pozycja") not in bledy
    assert ("nowy:1", "nip") in bledy  # klucz = NIP Alfy
    assert ("nowy:2", "status") in bledy
    assert all(b.id != "1111111111" for b in exc.value.bledy)
    assert _stan_rej(r) == przed
    r.close()


def test_zapisz_zmiany_kolizja_z_niezmienionym_wpisem(tmp_path):
    r = _rej(tmp_path)
    with pytest.raises(ZmianyOdrzucone) as exc:
        r.zapisz_zmiany(
            PROFIL, [ZmianaWpisu("1111111111", "1111111111", 2, "1111111111", "Alfa", "", "manual")]
        )
    (blad,) = exc.value.bledy
    assert blad.pole == "pozycja" and "Beta" in blad.komunikat
    r.close()


def test_zapisz_zmiany_blad_bazy_w_trakcie_wycofuje_calosc(tmp_path):
    r = _rej(tmp_path)
    przed = _stan_rej(r)
    r._conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON numery WHEN NEW.nazwa='BOOM' "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    r._conn.commit()
    with pytest.raises(sqlite3.DatabaseError):
        r.zapisz_zmiany(
            PROFIL,
            [
                ZmianaWpisu("1111111111", "1111111111", 7, "1111111111", "Alfa 7", "", "manual"),
                ZmianaWpisu("nowy:1", None, 8, "8888888888", "BOOM", "", "manual"),
            ],
            usuniete=["2222222222"],
        )
    assert _stan_rej(r) == przed
    # połączenie nadal używalne po wycofaniu
    r.zapisz_zmiany(
        PROFIL, [ZmianaWpisu("1111111111", "1111111111", 7, "1111111111", "Alfa 7", "", "manual")]
    )
    assert _stan_rej(r)["1111111111"][0] == 7
    r.close()


def test_zapisz_zmiany_pozycja_z_excela_jako_liczba_zmiennoprzecinkowa(tmp_path):
    r = _rej(tmp_path)
    r.zapisz_zmiany(
        PROFIL, [ZmianaWpisu("1111111111", "1111111111", " 11.0 ", "1111111111", "Alfa", "", "manual")]
    )
    assert _stan_rej(r)["1111111111"][0] == 11
    r.close()


# --------------------------------------------------------------------------- #
# runda 2 po przeglądzie — W1: zmiana klucza w tabeli nie może odciąć faktur
# od karty. Klucz kontrahenta = NIP albo K_<hash(nazwa|miejscowość)>; eksport
# liczy go Z FAKTURY KSeF, więc po zmianie NIP-u/nazwy w tabeli kolejna faktura
# tego kontrahenta niesie STARY klucz — bez aliasu stary -> nowy dostawała
# nowy numer karty (max+1) zamiast poprawionej karty.


def test_zapisz_zmiany_zmiana_nip_dopisuje_alias_stary_na_nowy(tmp_path):
    r = _rej(tmp_path)
    r.zapisz_zmiany(
        PROFIL, [ZmianaWpisu("3333333333", "3333333333", 3, "3333333334", "Gamma", "Gdańsk", "manual")]
    )
    assert r.aliasy(PROFIL) == {"3333333333": "3333333334"}
    assert r.pozycja(PROFIL, r.kanoniczny(PROFIL, "3333333333")) == 3
    r.close()


def test_zapisz_zmiany_zmiana_nazwy_bez_nip_dopisuje_alias(tmp_path):
    stary = _klucz_seed("Beispiel GmbH", "", "Berlin")
    nowy = _klucz_seed("Beispiel GmbH Berlin", "", "Berlin")
    r = _rej(tmp_path, STARTOWE + [(6, "", "Beispiel GmbH", "Berlin", "auto")])
    mapa = r.zapisz_zmiany(
        PROFIL, [ZmianaWpisu(stary, stary, 6, "", "Beispiel GmbH Berlin", "Berlin", "manual")]
    )
    assert mapa == {stary: nowy}
    assert r.aliasy(PROFIL) == {stary: nowy}
    assert r.pozycja(PROFIL, r.kanoniczny(PROFIL, stary)) == 6
    r.close()


def test_zapisz_zmiany_zmiana_klucza_celu_aliasow_przepina_je_bez_lancucha(tmp_path):
    """Wiersz był celem scalenia (Delta -> Gamma) — po zmianie NIP-u Gammy oba
    aliasy (Delta i stary NIP Gammy) wskazują WPROST na nowy klucz."""
    r = _rej(tmp_path)
    r.dodaj_alias(PROFIL, "4444444444", "3333333333", zwolnij_numer=True)
    r.zapisz_zmiany(
        PROFIL, [ZmianaWpisu("3333333333", "3333333333", 3, "3333333334", "Gamma", "Gdańsk", "manual")]
    )
    aliasy = r.aliasy(PROFIL)
    assert aliasy == {"4444444444": "3333333334", "3333333333": "3333333334"}
    zywe = {w.klucz for w in r.lista(PROFIL)}
    assert set(aliasy.values()) <= zywe  # płasko: cele to żywe wpisy
    assert not set(aliasy) & zywe  # żaden żywy wpis nie jest aliasem (martwy wiersz)
    r.close()


@pytest.mark.parametrize(
    "status, nowe_aliasy",
    [
        # dalej duplikat -> także nowy klucz wskazuje kartę Gammy
        ("duplikat", {"9999999998": "3333333333"}),
        # przestaje być duplikatem -> nowy klucz to zwykły wpis
        ("manual", {}),
    ],
)
def test_zapisz_zmiany_zmiana_klucza_wiersza_duplikatu_zachowuje_stary_alias(
    tmp_path, status, nowe_aliasy
):
    """Wiersz-duplikat (klucz = alias na Gammę, numer nadal zajęty w FK):
    zmiana jego NIP-u NIE może przepiąć ani skasować starego aliasu — faktury
    ze starym NIP-em dalej trafiają na kartę Gammy."""
    r = _rej(tmp_path)
    r.ustaw(PROFIL, "9999999999", 9, nip="9999999999", nazwa="Gamma Duplikat", status="duplikat")
    r.dodaj_alias(PROFIL, "9999999999", "3333333333", zwolnij_numer=False)
    r.zapisz_zmiany(
        PROFIL,
        [ZmianaWpisu("9999999999", "9999999999", 9, "9999999998", "Gamma Duplikat", "", status)],
    )
    assert r.aliasy(PROFIL) == {"9999999999": "3333333333", **nowe_aliasy}
    assert r.kanoniczny(PROFIL, "9999999999") == "3333333333"
    assert _stan_rej(r)["9999999998"][0] == 9
    r.close()


def test_zapisz_zmiany_powrot_do_poprzedniego_klucza_bez_cyklu(tmp_path):
    r = _rej(tmp_path)
    r.zapisz_zmiany(
        PROFIL, [ZmianaWpisu("3333333333", "3333333333", 3, "3333333334", "Gamma", "Gdańsk", "manual")]
    )
    r.zapisz_zmiany(
        PROFIL, [ZmianaWpisu("3333333334", "3333333334", 3, "3333333333", "Gamma", "Gdańsk", "manual")]
    )
    assert r.aliasy(PROFIL) == {"3333333334": "3333333333"}
    assert r.kanoniczny(PROFIL, "3333333333") == "3333333333"
    assert r.kanoniczny(PROFIL, "3333333334") == "3333333333"
    r.close()


def test_zapisz_zmiany_zamiana_nipow_dwoch_wierszy_bez_aliasow(tmp_path):
    """Oba stare klucze mają w stanie końcowym nowych właścicieli — faktura z
    NIP-em X trafia do wiersza, który TERAZ ma NIP X; alias byłby błędem."""
    r = _rej(tmp_path)
    r.zapisz_zmiany(
        PROFIL,
        [
            ZmianaWpisu("1111111111", "1111111111", 1, "2222222222", "Alfa", "Poznań", "manual"),
            ZmianaWpisu("2222222222", "2222222222", 2, "1111111111", "Beta", "Kraków", "manual"),
        ],
    )
    assert r.aliasy(PROFIL) == {}
    r.close()


def test_zapisz_zmiany_nip_innego_kontrahenta_nadal_odrzucony(tmp_path):
    r = _rej(tmp_path)
    przed = _stan_rej(r)
    with pytest.raises(ZmianyOdrzucone) as exc:
        r.zapisz_zmiany(
            PROFIL, [ZmianaWpisu("3333333333", "3333333333", 3, "1111111111", "Gamma", "Gdańsk", "manual")]
        )
    assert [(b.id, b.pole) for b in exc.value.bledy] == [("3333333333", "nip")]
    assert _stan_rej(r) == przed
    assert r.aliasy(PROFIL) == {}
    r.close()


def test_zapisz_zmiany_nie_przestawia_niezwiazanych_aliasow_z_dawnych_danych(tmp_path):
    """Łańcuch/cykl aliasów z uszkodzonych dawnych danych: zapis bez zmian
    kluczy nie dotyka tabeli aliasów, a zmiana klucza innego wpisu nie może
    po cichu przestawić kierowania faktur przez cykl (spłaszczenie łańcucha
    nie zmienia wyniku kanoniczny())."""
    r = _rej(tmp_path)
    r._conn.executemany(
        "INSERT INTO aliasy(profil_nip, klucz_alias, klucz_kanoniczny) VALUES (?, ?, ?)",
        [(PROFIL, "A1", "A2"), (PROFIL, "A2", "1111111111"), (PROFIL, "C1", "C2"), (PROFIL, "C2", "C1")],
    )
    r._conn.commit()
    przed = r.aliasy(PROFIL)
    kierowanie = {k: r.kanoniczny(PROFIL, k) for k in przed}
    r.zapisz_zmiany(PROFIL, [ZmianaWpisu("2222222222", "2222222222", 12, "2222222222", "Beta", "Kraków", "manual")])
    assert r.aliasy(PROFIL) == przed
    r.zapisz_zmiany(
        PROFIL, [ZmianaWpisu("3333333333", "3333333333", 3, "3333333334", "Gamma", "Gdańsk", "manual")]
    )
    assert {k: r.kanoniczny(PROFIL, k) for k in przed} == kierowanie
    assert r.aliasy(PROFIL)["3333333333"] == "3333333334"
    r.close()


# (6) klucz nowego/zmienionego wiersza równy kluczowi ALIASU — kanoniczny()
# zawsze wybierze alias, więc taki wiersz byłby martwy (faktury szłyby dalej na
# kartę celu scalenia). Wybrane: walidacja odrzuca z czytelnym komunikatem.


def test_zapisz_zmiany_klucz_rowny_aliasowi_odrzucony(tmp_path):
    r = _rej(tmp_path)
    r.dodaj_alias(PROFIL, "4444444444", "2222222222", zwolnij_numer=True)  # Delta -> Beta
    przed = _stan_rej(r)
    for zmiana in (
        ZmianaWpisu("nowy:1", None, 9, "4444444444", "Delta Nowa", "", "manual"),
        ZmianaWpisu("3333333333", "3333333333", 3, "4444444444", "Gamma", "Gdańsk", "manual"),
    ):
        with pytest.raises(ZmianyOdrzucone) as exc:
            r.zapisz_zmiany(PROFIL, [zmiana])
        (blad,) = exc.value.bledy
        assert (blad.id, blad.pole) == (zmiana.id, "nip")
        assert "4444444444" in blad.komunikat and "Beta" in blad.komunikat
        assert "scal" in blad.komunikat
    assert _stan_rej(r) == przed
    assert r.aliasy(PROFIL) == {"4444444444": "2222222222"}
    r.close()


def test_zapisz_zmiany_nowy_wiersz_ze_starym_kluczem_po_zmianie_nazwy_odrzucony(tmp_path):
    """Po poprawieniu nazwy stara nazwa (klucz faktur) wskazuje na kartę — nowy
    wiersz ze starą nazwą i miejscowością byłby martwy."""
    stary = _klucz_seed("Beispiel GmbH", "", "Berlin")
    r = _rej(tmp_path, STARTOWE + [(6, "", "Beispiel GmbH", "Berlin", "auto")])
    r.zapisz_zmiany(
        PROFIL, [ZmianaWpisu(stary, stary, 6, "", "Beispiel GmbH Berlin", "Berlin", "manual")]
    )
    with pytest.raises(ZmianyOdrzucone) as exc:
        r.zapisz_zmiany(PROFIL, [ZmianaWpisu("nowy:1", None, 9, "", "Beispiel GmbH", "Berlin", "manual")])
    (blad,) = exc.value.bledy
    assert blad.pole == "nazwa" and "Beispiel GmbH Berlin" in blad.komunikat
    r.close()


def test_zapisz_zmiany_klucz_aliasu_dozwolony_dla_duplikatu_i_po_usunieciu_celu(tmp_path):
    r = _rej(tmp_path)
    r.dodaj_alias(PROFIL, "4444444444", "2222222222", zwolnij_numer=True)
    # wiersz ze statusem duplikat pod kluczem aliasu = stan zamierzony (karta
    # nadal w FK, numer zajęty, faktury na kartę celu)
    r.zapisz_zmiany(PROFIL, [ZmianaWpisu("nowy:1", None, 9, "4444444444", "Delta", "", "duplikat")])
    assert r.kanoniczny(PROFIL, "4444444444") == "2222222222"

    r2 = _rej(tmp_path / "b")
    r2.dodaj_alias(PROFIL, "4444444444", "2222222222", zwolnij_numer=True)
    # cel scalenia usuwany w tym samym zapisie -> alias znika kaskadowo, więc
    # nowy wiersz z tym NIP-em jest żywy
    r2.zapisz_zmiany(
        PROFIL,
        [ZmianaWpisu("nowy:1", None, 9, "4444444444", "Delta", "", "manual")],
        usuniete=["2222222222"],
    )
    assert r2.aliasy(PROFIL) == {}
    assert r2.pozycja(PROFIL, r2.kanoniczny(PROFIL, "4444444444")) == 9
    r.close()
    r2.close()


# (1) nazwa zastępcza z „Dodaj wiersz” bez NIP-u — przypadkowy pusty wiersz nie
# może trafić do rejestru (zajmowałby numer max+1 i przesuwał numerację)


@pytest.mark.parametrize("nazwa", ["Nowy kontrahent", "Nowy kontrahent 3", "  nowy KONTRAHENT "])
def test_zapisz_zmiany_nazwa_zastepcza_bez_nip_odrzucona(tmp_path, nazwa):
    r = _rej(tmp_path)
    przed = _stan_rej(r)
    with pytest.raises(ZmianyOdrzucone) as exc:
        r.zapisz_zmiany(PROFIL, [ZmianaWpisu("nowy:1", None, 5, "", nazwa, "", "manual")])
    (blad,) = exc.value.bledy
    assert (blad.id, blad.pole) == ("nowy:1", "nazwa")
    assert "nazwę" in blad.komunikat and "usuń" in blad.komunikat
    assert _stan_rej(r) == przed
    assert r.nastepny_numer(PROFIL) == 5
    r.close()


def test_zapisz_zmiany_nazwa_zastepcza_z_nipem_przechodzi(tmp_path):
    r = _rej(tmp_path)
    r.zapisz_zmiany(PROFIL, [ZmianaWpisu("nowy:1", None, 5, "5555555555", "Nowy kontrahent", "", "manual")])
    assert _stan_rej(r)["5555555555"][0] == 5
    r.close()


# (2) pozycja poza rozsądnym zakresem (np. wklejona ogromna liczba)


@pytest.mark.parametrize("pozycja", ["1000000", 1000000, "99999999999999999999", 10**20])
def test_zapisz_zmiany_pozycja_poza_zakresem_odrzucona(tmp_path, pozycja):
    r = _rej(tmp_path)
    przed = _stan_rej(r)
    with pytest.raises(ZmianyOdrzucone) as exc:
        r.zapisz_zmiany(
            PROFIL, [ZmianaWpisu("1111111111", "1111111111", pozycja, "1111111111", "Alfa", "Poznań", "manual")]
        )
    (blad,) = exc.value.bledy
    assert blad.pole == "pozycja" and "poza zakresem" in blad.komunikat and "999 999" in blad.komunikat
    assert _stan_rej(r) == przed
    r.close()


def test_zapisz_zmiany_pozycja_na_granicy_zakresu(tmp_path):
    r = _rej(tmp_path)
    r.zapisz_zmiany(
        PROFIL, [ZmianaWpisu("1111111111", "1111111111", "999999", "1111111111", "Alfa", "Poznań", "manual")]
    )
    assert _stan_rej(r)["1111111111"][0] == 999999
    r.close()


# --------------------------------------------------------------------------- #
# runda 2 — okno: tabela -> „Zapisz” -> eksport (run_export hmffk na stubie)


def _meta_ksef(nr: str) -> InvoiceMetadata:
    return InvoiceMetadata(
        ksef_number=nr, invoice_number=f"FV/{nr}", issue_date="2026-01-15T00:00:00+00:00",
        invoicing_date="2026-01-15T12:00:00+00:00", acquisition_date=None,
        seller_nip="1180012345", seller_name="Dostawca Testowy Sp. z o.o.",
        buyer_id="", buyer_name="", net=None, gross=None, vat=None, currency="PLN",
        form_system_code="FA (3)", invoice_type="Vat",
    )


class _KlientKsef:
    """Stub KSeF: jedna faktura (ten sam XML) pod wskazanym numerem KSeF."""

    def __init__(self, nr: str, xml: bytes):
        self.nr, self.xml = nr, xml

    def authenticate(self):
        pass

    def query_metadata(self, subject_type, date_from, date_to, **_):
        yield _meta_ksef(self.nr)

    def download_invoice(self, ksef_number):
        return self.xml

    def download_budget_remaining(self):
        return 64


def _eksport(env, tmp_path, nr: str, fixture: str, kind: Kind):
    """Eksport hmffk jednej faktury; rejestr numerów = ten sam plik, który
    otwiera okno Kontrahenci… (app_data_dir z conftest). Zwraca (raport,
    konta rozrachunkowe z pliku)."""
    rejestr = RejestrNumerow()
    ledger = Ledger(tmp_path / "ledger.db")
    out = tmp_path / f"out-{nr}"
    try:
        raport = run_export(
            env.profil, _KlientKsef(nr, (FIXTURES / fixture).read_bytes()), ledger,
            date(2026, 1, 1), date(2026, 1, 31), {kind}, out, fmt="hmffk", rejestr_nr=rejestr,
        )
    finally:
        ledger.close()
        rejestr.close()
    (plik,) = (out / env.profil.subfolder).glob("hmffk_*.TXT")
    tekst = plik.read_bytes().decode("cp1250")
    return raport, sorted(set(re.findall(r"Konto =(20[12]-2-\d+)", tekst)))


def _zapisz_w_oknie(env, okna, nazwa_wiersza: str, col: int, wartosc: str) -> None:
    dlg = _otworz(env)
    try:
        _edytuj(dlg, _wiersz(dlg, nazwa_wiersza), col, wartosc)
        _klik(dlg.btn_zapisz)
        assert okna.pokazane == []  # zapis bez błędów
    finally:
        _sprzatnij(dlg, okna)


def test_w1_zmiana_nazwy_w_tabeli_kolejna_faktura_na_te_sama_karte(env, okna, tmp_path):
    """Odtworzenie W1 (t_klucz.py): kontrahent bez NIP (Beispiel GmbH, Berlin)
    dostaje przy eksporcie nr 5; księgowa poprawia nazwę w tabeli jak w FK
    („Beispiel GmbH Berlin”) i zapisuje; kolejna faktura tego kontrahenta
    (klucz z KSeF = stara nazwa) ma trafić na TĘ SAMĄ kartę, nie na nr 6."""
    _zasiej()
    raport1, konta1 = _eksport(env, tmp_path, "NR-1", "fa3_no_nip.xml", Kind.SALES)
    assert [(n, nr) for n, _, nr, _ in raport1.nowi_kontrahenci] == [("Beispiel GmbH", 5)]
    assert konta1 == ["201-2-5"]

    _zapisz_w_oknie(env, okna, "Beispiel GmbH", RejestrDialog.COL_NAZWA, "Beispiel GmbH Berlin")

    raport2, konta2 = _eksport(env, tmp_path, "NR-2", "fa3_no_nip.xml", Kind.SALES)
    assert raport2.nowi_kontrahenci == []
    assert konta2 == ["201-2-5"]
    wpisy = [v for v in _stan().values() if v[2].startswith("Beispiel")]
    assert wpisy == [(5, "", "Beispiel GmbH Berlin", wpisy[0][3], "manual")]


def test_w1_zmiana_nip_w_tabeli_kolejna_faktura_na_te_sama_karte(env, okna, tmp_path):
    """To samo dla kontrahenta z NIP-em: karta nr 5 z NIP-em z faktury
    kosztowej (1180012345); księgowa poprawia NIP na ten z karty w FK —
    następna faktura (nadal z NIP-em 1180012345) idzie na kartę 5."""
    _zasiej()
    raport1, konta1 = _eksport(env, tmp_path, "NR-1", "fa3_basic_23.xml", Kind.COST)
    assert [nr for _, _, nr, _ in raport1.nowi_kontrahenci] == [5]
    assert konta1 == ["202-2-5"]

    _zapisz_w_oknie(env, okna, "Dostawca Testowy Sp. z o.o.", RejestrDialog.COL_NIP, "1180012346")

    raport2, konta2 = _eksport(env, tmp_path, "NR-2", "fa3_basic_23.xml", Kind.COST)
    assert raport2.nowi_kontrahenci == []
    assert konta2 == ["202-2-5"]
    assert _stan()["1180012346"][0] == 5 and "1180012345" not in _stan()


def test_zmiana_nip_na_nip_innego_kontrahenta_w_oknie_odrzucona(env, okna):
    _zasiej()
    przed = _stan()
    dlg = _otworz(env)
    try:
        _edytuj(dlg, _wiersz(dlg, "Gamma"), dlg.COL_NIP, "1111111111")
        _klik(dlg.btn_zapisz)
        assert _stan() == przed
        assert "Nie zapisano" in okna.pokazane[-1]["tekst"] and "1111111111" in okna.pokazane[-1]["tekst"]
    finally:
        _sprzatnij(dlg, okna)


def test_edycja_wiersza_duplikatu_zostawia_status_duplikat_i_alias(env, okna):
    """Wiersz-duplikat (scalony bez zwolnienia numeru) po edycji zostaje
    duplikatem — nie zamienia się po cichu w „manual” (aktywną kartę) — a
    faktury z jego starym i nowym kluczem dalej idą na kartę celu."""
    _zasiej()
    r = RejestrNumerow()
    r.ustaw(PROFIL, "9999999999", 9, nip="9999999999", nazwa="Gamma Duplikat", status="duplikat")
    r.dodaj_alias(PROFIL, "9999999999", "3333333333", zwolnij_numer=False)
    r.close()
    dlg = _otworz(env)
    try:
        _edytuj(dlg, _wiersz(dlg, "Gamma Duplikat"), dlg.COL_NIP, "9999999998")
        _klik(dlg.btn_zapisz)
        assert okna.pokazane == []
        assert _stan()["9999999998"][4] == "duplikat"
        r = RejestrNumerow()
        assert r.kanoniczny(PROFIL, "9999999999") == "3333333333"
        assert r.kanoniczny(PROFIL, "9999999998") == "3333333333"
        r.close()
    finally:
        _sprzatnij(dlg, okna)


# --------------------------------------------------------------------------- #
# runda 2 — okno: fantomowy wiersz (1), ogromna pozycja (2), klucz aliasu (6),
# raport synchronizacji o wpisach manual (12)


def test_przypadkowy_dodaj_wiersz_esc_i_zapisz_przy_zamknieciu_nie_zapisuje_fantomu(env, okna):
    """(1) „Dodaj wiersz” -> Esc (nic nie wpisane) -> zamknięcie okna ->
    „Zapisz” (domyślny przycisk pytania) zapisywało „Nowy kontrahent” z
    numerem max+1, przesuwając numerację kolejnego eksportu. Teraz zapis jest
    odrzucony z komunikatem, okno zostaje otwarte, a „Nie zapisuj” je zamyka."""
    _zasiej()
    przed = _stan()
    dlg = _otworz(env)
    try:
        _klik(dlg.btn_add)
        edytor = QApplication.focusWidget()
        assert edytor is not None and edytor is not dlg.table
        QTest.keyClick(edytor, Qt.Key.Key_Escape)
        QApplication.processEvents()
        okna.odpowiedzi = ["Zapisz"]
        dlg.close()
        QApplication.processEvents()
        assert dlg.isVisible()
        assert _stan() == przed
        assert okna.teksty()[0] == "Zapisać zmiany?"
        blad = okna.pokazane[1]["tekst"]
        assert "Nie zapisano" in blad and "Nowy kontrahent" in blad and "usuń" in blad
        r = RejestrNumerow()
        assert r.nastepny_numer(PROFIL) == 5
        r.close()

        okna.odpowiedzi = ["Nie zapisuj"]
        dlg.close()
        QApplication.processEvents()
        assert not dlg.isVisible()
        assert _stan() == przed
    finally:
        _sprzatnij(dlg, okna)


def test_wklejona_ogromna_pozycja_zostaje_tekstem_a_zapis_ja_odrzuca(env, okna):
    """(2) Wklejenie ogromnej liczby do kolumny Pozycja rzucało OverflowError
    (Qt nie mieści jej w int64) i urywało wklejanie w połowie. Wartość ma
    zostać w komórce jako tekst, reszta bloku wkleić się normalnie, a błąd
    pokazać „Zapisz”."""
    _zasiej()
    przed = _stan()
    dlg = _otworz(env)
    try:
        dlg.table.setCurrentCell(_wiersz(dlg, "Alfa"), dlg.COL_POZYCJA)
        QApplication.clipboard().setText("99999999999999999999\n12")
        dlg._wklej()  # wprost — wyjątek w slocie skrótu Qt tylko by wypisał
        QApplication.processEvents()
        assert dlg.table.item(_wiersz(dlg, "Alfa"), dlg.COL_POZYCJA).text() == "99999999999999999999"
        assert dlg.table.item(_wiersz(dlg, "Beta"), dlg.COL_POZYCJA).text() == "12"  # druga linia
        assert "Wklejono: 2" in okna.pokazane[-1]["tekst"]

        assert dlg._zapisz() is False
        tresc = okna.pokazane[-1]["tekst"]
        assert "Nie zapisano" in tresc and "poza zakresem" in tresc
        assert _stan() == przed
    finally:
        _sprzatnij(dlg, okna)


def test_nowy_wiersz_z_nipem_scalonym_odrzucony_w_oknie(env, okna):
    """(6) NIP scalonej (i usuniętej w FK) karty wpisany w nowym wierszu —
    komunikat mówi, na którą kartę trafiają faktury z tym NIP-em."""
    _zasiej()
    r = RejestrNumerow()
    r.dodaj_alias(PROFIL, "4444444444", "2222222222", zwolnij_numer=True)
    r.close()
    przed = _stan()
    dlg = _otworz(env)
    try:
        _klik(dlg.btn_add)
        edytor = QApplication.focusWidget()
        edytor.selectAll()
        QTest.keyClicks(edytor, "4444444444")
        QTest.keyClick(edytor, Qt.Key.Key_Return)
        QApplication.processEvents()
        _edytuj(dlg, dlg.table.currentRow(), dlg.COL_NAZWA, "Delta Wraca")
        _klik(dlg.btn_zapisz)
        assert _stan() == przed
        tresc = okna.pokazane[-1]["tekst"]
        assert "Nie zapisano" in tresc and "Beta" in tresc and "scal" in tresc
    finally:
        _sprzatnij(dlg, okna)


def test_synchronizacja_raport_liczy_wpisy_manual_i_podpowiada_jak_odblokowac(
    env, okna, monkeypatch, tmp_path
):
    """(12) Wpisy manual mogą pochodzić ze starszego okna (każdy dodany/
    wklejony wiersz dostawał „manual”) — synchronizacja ich nie dotyka. Raport
    ma podać ich liczbę i jak je „odblokować” (status seed), bez zmiany danych."""
    from openpyxl import Workbook

    _zasiej([
        (5, "1111111111", "Alfa", "Poznań", "manual"),
        (2, "2222222222", "Beta", "Kraków", "manual"),
        (3, "3333333333", "Gamma", "Gdańsk", "seed"),
    ])
    sciezka = tmp_path / "kartoteka.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Pozycja", "Kod", "Nazwa", "NIP", "Adres", "Kod pocztowy", "Miejscowość", "Rejon"])
    ws.append([8, "A", "Alfa", "1111111111", "", "", "Poznań", ""])
    ws.append([2, "B", "Beta", "2222222222", "", "", "Kraków", ""])
    ws.append([3, "C", "Gamma", "3333333333", "", "", "Gdańsk", ""])
    wb.save(sciezka)
    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(sciezka), ""))
    )
    przed = _stan()
    dlg = _otworz(env)
    try:
        _klik(dlg.btn_sync)
        assert _stan() == przed  # manual nietknięte, Gamma bez zmian
        raport = okna.pokazane[-1]
        tekst = raport["tekst"] + "\n" + raport["szczegoly"]
        assert "2 wpis" in raport["tekst"] and "manual" in raport["tekst"]
        assert "seed" in raport["szczegoly"] and "Zapisz" in raport["szczegoly"]
        assert "Alfa" in tekst
    finally:
        _sprzatnij(dlg, okna)


def test_zapisana_wczesniej_pozycja_ponad_zakresem_nie_blokuje_edycji(env, okna):
    """Limit zakresu dotyczy WPISYWANEJ pozycji. Numer ponad POZYCJA_MAX już
    zapisany w bazie (np. z synchronizacji albo wpisany kiedyś przez pole
    liczbowe) nie jest „niezapisaną zmianą” i nie blokuje edycji innych
    kolumn tego wiersza."""
    _zasiej()
    r = RejestrNumerow()
    r.ustaw(PROFIL, "7777777777", 1_500_000, nip="7777777777", nazwa="Omega", status="seed")
    r.close()
    dlg = _otworz(env)
    try:
        assert "Niezapisane" not in dlg.licznik_label.text()
        _edytuj(dlg, _wiersz(dlg, "Omega"), dlg.COL_NAZWA, "Omega SA")
        _klik(dlg.btn_zapisz)
        assert okna.pokazane == []
        assert _stan()["7777777777"][:3] == (1_500_000, "7777777777", "Omega SA")
    finally:
        _sprzatnij(dlg, okna)
