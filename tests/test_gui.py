"""Testy logiki GUI. Większość woła metody na obiektach-dublerach (SimpleNamespace)
zamiast tworzyć prawdziwe QWidgety — szybkie i bez zależności od QApplication.
Sekcja na końcu pliku (## Testy integracyjne) tworzy PRAWDZIWE MainWindow/
ProfileDialog na platformie Qt "offscreen" — patrz komentarz przy QT_QPA_PLATFORM
niżej po uzasadnienie."""

import os

# MUSI być ustawione przed jakąkolwiek konstrukcją QApplication (import samego
# PySide6 nie ma znaczenia — dopiero QApplication() odczytuje tę zmienną) —
# testy integracyjne tworzą prawdziwe okna/widgety, ale bez platformy graficznej
# (CI, brak wyświetlacza) i bez pokazywania czegokolwiek na ekranie dewelopera.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import date
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QMessageBox as RealQMessageBox

import app.gui as gui_mod
from app.config import ConfigStore, Profile
from app.gui import MainWindow, ProfileDialog, RejestrDialog
from app.kontrahenci_nr import RaportSync, SynchronizacjaOdrzucona
from app.symfonia_sql import SymfoniaSqlError


def _line(text: str):
    """Dubler QLineEdit — tylko .text() jest używane przez _zapisz_konta."""
    return SimpleNamespace(text=lambda: text)


def _spin(value: int):
    """Dubler QSpinBox — tylko .value() jest używane przez _zapisz_konta."""
    return SimpleNamespace(value=lambda: value)


def _dialog(profil: Profile, cfg, store, *, netto_sprzedaz=None, termin=None):
    """Buduje obiekt z dokładnie tymi atrybutami, których dotyka _zapisz_konta —
    bez tworzenia realnych QLineEdit/QSpinBox i bez QApplication."""
    return SimpleNamespace(
        profile=profil,
        cfg=cfg,
        store=store,
        konto_prefix_sprzedaz=_line(cfg.konto_prefix_sprzedaz),
        konto_prefix_koszt=_line(cfg.konto_prefix_koszt),
        konto_sprzedaz_netto=_line(
            netto_sprzedaz if netto_sprzedaz is not None else cfg.konto_sprzedaz_netto
        ),
        konto_sprzedaz_vat=_line(cfg.konto_sprzedaz_vat),
        konto_koszt_vat=_line(cfg.konto_koszt_vat),
        konto_koszt_netto=_line(cfg.konto_koszt_netto),
        termin_fallback_dni=_spin(termin if termin is not None else cfg.termin_fallback_dni),
    )


def test_zapisz_konta_dodaje_nadpisanie_i_zapisuje_na_dysk(tmp_path):
    """BETALABO-style: konto przychodów 701 zamiast wspólnego 703 -> trafia do
    profile.konta_override i przeżywa zapis/odczyt configu."""
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-gui")
    profil = Profile("BETALABO", "1112223344", "t", "aqt")
    cfg.profiles.append(profil)
    store.save(cfg)

    dlg = _dialog(profil, cfg, store, netto_sprzedaz="701")
    RejestrDialog._zapisz_konta(dlg)

    assert profil.konta_override == {"konto_sprzedaz_netto": "701"}
    loaded = ConfigStore(tmp_path / "config.enc").unlock("haslo-gui")
    assert loaded.profiles[0].konta_override == {"konto_sprzedaz_netto": "701"}


def test_zapisz_konta_usuwa_nadpisanie_gdy_rowne_globalnej(tmp_path):
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-gui2")
    profil = Profile(
        "BETALABO", "1112223344", "t", "aqt",
        konta_override={"konto_sprzedaz_netto": "701"},
    )
    cfg.profiles.append(profil)
    store.save(cfg)

    # pole ustawione z powrotem na wartość wspólną -> nadpisanie znika
    dlg = _dialog(profil, cfg, store, netto_sprzedaz=cfg.konto_sprzedaz_netto)
    RejestrDialog._zapisz_konta(dlg)

    assert profil.konta_override == {}
    loaded = ConfigStore(tmp_path / "config.enc").unlock("haslo-gui2")
    assert loaded.profiles[0].konta_override == {}


def test_zapisz_konta_termin_fallback_dni_override_i_powrot(tmp_path):
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-gui3")
    profil = Profile("Firma", "1231231231", "t", "f")
    cfg.profiles.append(profil)
    store.save(cfg)

    dlg = _dialog(profil, cfg, store, termin=cfg.termin_fallback_dni + 7)
    RejestrDialog._zapisz_konta(dlg)
    assert profil.konta_override == {"termin_fallback_dni": str(cfg.termin_fallback_dni + 7)}

    # ustawienie tej samej wartości co wspólna -> nadpisanie znika
    dlg2 = _dialog(profil, cfg, store, termin=cfg.termin_fallback_dni)
    RejestrDialog._zapisz_konta(dlg2)
    assert profil.konta_override == {}


def test_zapisz_konta_bez_zmian_nie_woła_save(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-gui4")
    profil = Profile("Firma", "1231231231", "t", "f")
    cfg.profiles.append(profil)
    store.save(cfg)

    wolania = []
    monkeypatch.setattr(store, "save", lambda c: wolania.append(c))

    dlg = _dialog(profil, cfg, store)  # wszystkie pola = wartości wspólne, bez zmian
    RejestrDialog._zapisz_konta(dlg)

    assert wolania == []


# --------------------------------------------------------------------------- #
# ProfileDialog — dopasowanie/wybór bazy Symfonii (SQL), bez tworzenia
# prawdziwych QWidgetów: _startowa_baza jest czystą funkcją (staticmethod),
# _current_baza wołana na dublerze (jak _zapisz_konta wyżej)

def test_dopasuj_baze_po_nazwie_trafia_pierwsze_znaczace_slowo():
    from app.gui import _dopasuj_baze_po_nazwie

    bazy = ["BETALABO_SPÓŁKA_Z_OGRANICZONĄ_ODPOWIEDZIALNOŚCIĄ", "ALFATRON_SERWIS_MEDYCZNY"]
    assert (
        _dopasuj_baze_po_nazwie("BETALABO sp. z o.o.", bazy)
        == "BETALABO_SPÓŁKA_Z_OGRANICZONĄ_ODPOWIEDZIALNOŚCIĄ"
    )


def test_dopasuj_baze_po_nazwie_brak_dopasowania_zwraca_none():
    from app.gui import _dopasuj_baze_po_nazwie

    assert _dopasuj_baze_po_nazwie("Zupełnie inna firma", ["BETALABO_SP_ZOO"]) is None
    assert _dopasuj_baze_po_nazwie("", ["BETALABO_SP_ZOO"]) is None


def test_startowa_baza_preferuje_istniejace_ustawienie_profilu():
    wynik = ProfileDialog._startowa_baza("MOJA_BAZA", "1112223344", "Firma", ["MOJA_BAZA", "INNA"])
    assert wynik == "MOJA_BAZA"


def test_startowa_baza_dopasowuje_po_nazwie_gdy_profil_bez_ustawienia():
    wynik = ProfileDialog._startowa_baza(
        "", "1112223344", "BETALABO sp. z o.o.", ["BETALABO_SP_ZOO", "INNA"]
    )
    assert wynik == "BETALABO_SP_ZOO"


def test_startowa_baza_brak_dopasowania_zwraca_pusty_string():
    wynik = ProfileDialog._startowa_baza("", "1112223344", "Zupełnie inna nazwa", ["COŚ_INNEGO"])
    assert wynik == ""


def _combo(current_text: str):
    """Dubler QComboBox — tylko .currentText() jest używane przez _current_baza."""
    return SimpleNamespace(currentText=lambda: current_text)


def test_current_baza_z_combo_wybrana_pozycja():
    dlg = SimpleNamespace(
        BRAK_BAZY=ProfileDialog.BRAK_BAZY,
        db_symfonia_combo=_combo("ALFATRON_SERWIS_MEDYCZNY"),
        db_symfonia_edit=None,
    )
    assert ProfileDialog._current_baza(dlg) == "ALFATRON_SERWIS_MEDYCZNY"


def test_current_baza_z_combo_pozycja_brak_zwraca_pusty_string():
    dlg = SimpleNamespace(
        BRAK_BAZY=ProfileDialog.BRAK_BAZY,
        db_symfonia_combo=_combo(ProfileDialog.BRAK_BAZY),
        db_symfonia_edit=None,
    )
    assert ProfileDialog._current_baza(dlg) == ""


def test_current_baza_z_pola_tekstowego_gdy_lista_baz_niedostepna():
    dlg = SimpleNamespace(db_symfonia_combo=None, db_symfonia_edit=_line("RECZNA_BAZA "))
    assert ProfileDialog._current_baza(dlg) == "RECZNA_BAZA"


# --------------------------------------------------------------------------- #
# _synchronizuj_sql_przed_eksportem — auto-synchronizacja rejestru numerów w
# ExportWorker przed eksportem (patrz Profile.db_symfonia/sql_autosync)

class _FakeRejestrNumerow:
    """Dubler RejestrNumerow.synchronizuj_z_sql — zapamiętuje wywołanie,
    zwraca/podnosi to, co przekazano w konstruktorze."""

    wolania: list[tuple[str, str]] = []

    def __init__(self, wynik=None, blad=None):
        self._wynik = wynik
        self._blad = blad
        self.closed = False

    def synchronizuj_z_sql(self, profil_nip, baza):
        _FakeRejestrNumerow.wolania.append((profil_nip, baza))
        if self._blad is not None:
            raise self._blad
        return self._wynik

    def close(self):
        self.closed = True


def test_sync_sql_przed_eksportem_bez_konfiguracji_nic_nie_robi(monkeypatch):
    profil = Profile("Firma", "1112223344", "t", "f")  # db_symfonia="" domyślnie
    monkeypatch.setattr(gui_mod, "RejestrNumerow", lambda: (_ for _ in ()).throw(AssertionError))
    logi = []
    assert gui_mod._synchronizuj_sql_przed_eksportem(profil, logi.append) == []
    assert logi == []


def test_sync_sql_przed_eksportem_autosync_wylaczony_nic_nie_robi(monkeypatch):
    profil = Profile("Firma", "1112223344", "t", "f", db_symfonia="BAZA", sql_autosync=False)
    monkeypatch.setattr(gui_mod, "RejestrNumerow", lambda: (_ for _ in ()).throw(AssertionError))
    assert gui_mod._synchronizuj_sql_przed_eksportem(profil, lambda s: None) == []


def test_sync_sql_przed_eksportem_sukces_zwraca_ostrzezenia_i_loguje(monkeypatch):
    profil = Profile("Firma", "1112223344", "t", "f", db_symfonia="BAZA", sql_autosync=True)
    raport = RaportSync(
        dodane=3, zaktualizowane=1, rozjazdy=[("k", 1, 2)], brak_w_fk=[], ostrzezenia=[]
    )
    fake = _FakeRejestrNumerow(wynik=raport)
    _FakeRejestrNumerow.wolania = []
    monkeypatch.setattr(gui_mod, "RejestrNumerow", lambda: fake)

    logi = []
    ostrzezenia = gui_mod._synchronizuj_sql_przed_eksportem(profil, logi.append)

    assert _FakeRejestrNumerow.wolania == [("1112223344", "BAZA")]
    assert fake.closed is True
    assert ostrzezenia == raport.jako_ostrzezenia()
    assert any("dodano 3" in l and "zaktualizowano 1" in l for l in logi)


def test_sync_sql_przed_eksportem_blad_sql_propaguje_i_zamyka_rejestr(monkeypatch):
    profil = Profile("Firma", "1112223344", "t", "f", db_symfonia="BAZA", sql_autosync=True)
    fake = _FakeRejestrNumerow(blad=SymfoniaSqlError("timeout"))
    monkeypatch.setattr(gui_mod, "RejestrNumerow", lambda: fake)

    try:
        gui_mod._synchronizuj_sql_przed_eksportem(profil, lambda s: None)
        assert False, "oczekiwano SymfoniaSqlError"
    except SymfoniaSqlError:
        pass
    assert fake.closed is True  # finally zamyka rejestr nawet przy błędzie



# --------------------------------------------------------------------------- #
# MainWindow — zakres dat Od/Do zapamiętywany per profil (patrz
# app/config.py::Profile.ostatnie_od/_do). _zakres_dla_profilu jest czystą
# funkcją modułową (nie potrzebuje QApplication ani realnych QDateEdit);
# _zapisz_zakres_profilu testowana na dublerze jak _zapisz_konta wyżej.

def test_zakres_dla_profilu_zapamietany_zakres_ma_pierwszenstwo():
    from app.gui import _zakres_dla_profilu

    profil = Profile(
        "Firma", "1112223344", "t", "f",
        ostatnie_od="2025-03-01", ostatnie_do="2025-03-31",
    )
    od, do = _zakres_dla_profilu(profil)
    assert (od.year(), od.month(), od.day()) == (2025, 3, 1)
    assert (do.year(), do.month(), do.day()) == (2025, 3, 31)


def test_zakres_dla_profilu_brak_zapamietanego_zwraca_poprzedni_miesiac():
    from app.gui import _domyslny_zakres_poprzedni_miesiac, _zakres_dla_profilu

    profil = Profile("Firma", "1112223344", "t", "f")  # ostatnie_od/_do="" domyślnie
    assert _zakres_dla_profilu(profil) == _domyslny_zakres_poprzedni_miesiac()


def test_zakres_dla_profilu_smieciowa_wartosc_zwraca_poprzedni_miesiac():
    from app.gui import _domyslny_zakres_poprzedni_miesiac, _zakres_dla_profilu

    profil = Profile(
        "Firma", "1112223344", "t", "f",
        ostatnie_od="nie-jest-data", ostatnie_do="2025-03-31",
    )
    assert _zakres_dla_profilu(profil) == _domyslny_zakres_poprzedni_miesiac()


def test_zakres_dla_profilu_od_pozniejsze_niz_do_zwraca_poprzedni_miesiac():
    from app.gui import _domyslny_zakres_poprzedni_miesiac, _zakres_dla_profilu

    profil = Profile(
        "Firma", "1112223344", "t", "f",
        ostatnie_od="2025-05-01", ostatnie_do="2025-04-01",
    )
    assert _zakres_dla_profilu(profil) == _domyslny_zakres_poprzedni_miesiac()


def test_zapisz_zakres_profilu_zapisuje_gdy_zmienione(tmp_path):
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-daty")
    profil = Profile("Firma", "1112223344", "t", "f")
    cfg.profiles.append(profil)
    store.save(cfg)

    dlg = SimpleNamespace(cfg=cfg, store=store)
    MainWindow._zapisz_zakres_profilu(dlg, profil, date(2026, 1, 1), date(2026, 1, 31))

    assert profil.ostatnie_od == "2026-01-01"
    assert profil.ostatnie_do == "2026-01-31"
    loaded = ConfigStore(tmp_path / "config.enc").unlock("haslo-daty")
    assert loaded.profiles[0].ostatnie_od == "2026-01-01"
    assert loaded.profiles[0].ostatnie_do == "2026-01-31"


def test_zapisz_zakres_profilu_bez_zmian_nie_wola_save(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.enc")
    cfg = store.initialize("haslo-daty2")
    profil = Profile(
        "Firma", "1112223344", "t", "f",
        ostatnie_od="2026-01-01", ostatnie_do="2026-01-31",
    )
    cfg.profiles.append(profil)
    store.save(cfg)

    wolania = []
    monkeypatch.setattr(store, "save", lambda c: wolania.append(c))

    dlg = SimpleNamespace(cfg=cfg, store=store)
    # ten sam zakres co już zapisany -> bez zapisu configu
    MainWindow._zapisz_zakres_profilu(dlg, profil, date(2026, 1, 1), date(2026, 1, 31))

    assert wolania == []


def test_on_profile_selected_ustawia_tylko_pola_dat():
    """_on_profile_selected nie może dotykać żadnych innych pól (numery
    faktur, kontrahenci, checkboxy) — dubler ma tylko cfg/date_from/date_to,
    więc dotknięcie czegokolwiek innego wywaliłoby AttributeError."""
    from app.gui import _zakres_dla_profilu

    profil = Profile(
        "Firma", "1112223344", "t", "f",
        ostatnie_od="2025-03-01", ostatnie_do="2025-03-31",
    )
    cfg = SimpleNamespace(profiles=[profil])
    ustawione = {}
    dlg = SimpleNamespace(
        cfg=cfg,
        date_from=SimpleNamespace(setDate=lambda d: ustawione.__setitem__("od", d)),
        date_to=SimpleNamespace(setDate=lambda d: ustawione.__setitem__("do", d)),
    )

    MainWindow._on_profile_selected(dlg, 0)

    oczekiwany_od, oczekiwany_do = _zakres_dla_profilu(profil)
    assert ustawione["od"] == oczekiwany_od
    assert ustawione["do"] == oczekiwany_do


def test_on_profile_selected_wiersz_poza_zakresem_nic_nie_robi():
    cfg = SimpleNamespace(profiles=[])
    dlg = SimpleNamespace(
        cfg=cfg,
        date_from=SimpleNamespace(setDate=lambda d: (_ for _ in ()).throw(AssertionError)),
        date_to=SimpleNamespace(setDate=lambda d: (_ for _ in ()).throw(AssertionError)),
    )
    MainWindow._on_profile_selected(dlg, -1)  # brak zaznaczenia — nie wolno nic ustawiać


def test_sync_sql_przed_eksportem_bezpiecznik_propaguje_i_zamyka_rejestr(monkeypatch):
    """(f) SynchronizacjaOdrzucona (bezpiecznik niezgodnej kartoteki/bazy —
    patrz app/kontrahenci_nr.py) propaguje się tak samo jak SymfoniaSqlError:
    eksport MUSI zostać przerwany (patrz ExportWorker.run)."""
    profil = Profile("Firma", "1112223344", "t", "f", db_symfonia="BAZA", sql_autosync=True)
    fake = _FakeRejestrNumerow(blad=SynchronizacjaOdrzucona("Kartoteka nie pasuje do firmy"))
    monkeypatch.setattr(gui_mod, "RejestrNumerow", lambda: fake)

    try:
        gui_mod._synchronizuj_sql_przed_eksportem(profil, lambda s: None)
        assert False, "oczekiwano SynchronizacjaOdrzucona"
    except SynchronizacjaOdrzucona:
        pass
    assert fake.closed is True  # finally zamyka rejestr nawet przy błędzie


# --------------------------------------------------------------------------- #
## Testy integracyjne — PRAWDZIWE widgety (offscreen), nie dublery
#
# Błąd zgłoszony z produkcji: po wyłączeniu i włączeniu aplikacji zapamiętany
# zakres dat per profil znikał. Testy powyżej (test_zakres_dla_profilu_*,
# test_zapisz_zakres_profilu_*, test_on_profile_selected_*) sprawdzają każdy
# fragment logiki OSOBNO na dublerach — i każdy z osobna faktycznie działa
# poprawnie, więc maskowały błąd, który powstawał na STYKU dwóch fragmentów
# (patrz test_profile_dialog_zachowuje_ostatni_zakres_po_edycji niżej).
# Testy w tej sekcji budują PRAWDZIWY MainWindow/ProfileDialog na PRAWDZIWYM
# ConfigStore (plik tymczasowy), żeby taki błąd był wykrywalny automatycznie.

@pytest.fixture(scope="module")
def qapp():
    """Jedna QApplication na cały moduł (Qt nie pozwala na więcej niż jedną w
    procesie). Platforma offscreen ustawiona na górze pliku — nic się nie
    wyświetla, ale prawdziwe sygnały/sloty Qt (currentRowChanged itp.) działają
    tak samo jak w uruchomionej aplikacji."""
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def brak_prawdziwego_sql(monkeypatch):
    """ProfileDialog.__init__ woła lista_baz() — prawdziwe zapytanie do bazy
    Symfonii (SQL) przez sqlcmd. Testy nie mogą zależeć od żywej bazy ani od
    obecności sqlcmd na maszynie uruchamiającej testy — dublujemy na pustą
    listę (dokładnie jak realna ścieżka "SQL niedostępne" w tym dialogu)."""
    monkeypatch.setattr(gui_mod, "lista_baz", lambda: [])


class _NoOpMessageBox:
    """Dubler QMessageBox — prawdziwy .exec() modalnego okna zablokowałby test
    (nikt nie klika), więc podmieniamy statyczne wywołania na no-opy. Wywołania
    zapamiętane w .calls, żeby test mógł sprawdzić, że NIE pojawił się np.
    komunikat błędu, gdy się go nie spodziewa."""

    calls: list[tuple[str, tuple]] = []
    StandardButton = RealQMessageBox.StandardButton

    @classmethod
    def _zapisz(cls, rodzaj, args):
        cls.calls.append((rodzaj, args[1:3]))

    @classmethod
    def warning(cls, *a, **k):
        cls._zapisz("warning", a)
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, *a, **k):
        cls._zapisz("information", a)
        return cls.StandardButton.Ok

    @classmethod
    def critical(cls, *a, **k):
        cls._zapisz("critical", a)
        return cls.StandardButton.Ok

    @classmethod
    def question(cls, *a, **k):
        cls._zapisz("question", a)
        return cls.StandardButton.Yes


@pytest.fixture
def no_dialogs(monkeypatch):
    _NoOpMessageBox.calls = []
    monkeypatch.setattr(gui_mod, "QMessageBox", _NoOpMessageBox)
    return _NoOpMessageBox


class _FakeSignal:
    """Dubler sygnału Qt — wystarczający dla .connect()/.emit() używanych przez
    _start_export; bez prawdziwego wątku ani zdarzeń Qt."""

    def __init__(self):
        self._slots: list = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *a):
        for slot in list(self._slots):
            slot(*a)


class _FakeThread:
    """Dubler QThread — .start() wywołuje slot podłączony pod started
    SYNCHRONICZNIE (bez wątku), więc ExportWorker.run() (też dublowany, patrz
    _FakeExportWorker) wykonuje się od razu w wątku testu."""

    def __init__(self, parent=None):
        self.started = _FakeSignal()
        self.finished = _FakeSignal()

    def start(self):
        self.started.emit()

    def quit(self):
        self.finished.emit()

    def isRunning(self):
        return False

    def wait(self, ms=0):
        return True

    def deleteLater(self):
        pass


class _FakeExportWorker:
    """Dubler ExportWorker — run() nic nie robi (żaden prawdziwy eksport ani
    wywołanie KSeF); sygnały istnieją tylko, żeby _start_export mógł się do
    nich podłączyć bez błędu."""

    def __init__(self, *a, **k):
        self.progress = _FakeSignal()
        self.log = _FakeSignal()
        self.finished = _FakeSignal()
        self.failed = _FakeSignal()

    def moveToThread(self, thread):
        pass

    def run(self):
        pass


@pytest.fixture
def no_real_export(monkeypatch):
    monkeypatch.setattr(gui_mod, "QThread", _FakeThread)
    monkeypatch.setattr(gui_mod, "ExportWorker", _FakeExportWorker)


def test_mainwindow_pelny_cykl_zapamietywania_zakresu_dat(
    tmp_path, qapp, no_dialogs, no_real_export
):
    """Odtwarza CAŁY zgłoszony scenariusz na prawdziwych widgetach:
    (a) start MainWindow z profilem domyślnie zaznaczonym (wiersz 0), który ma
        już zapamiętany zakres -> QDateEdit-y muszą go pokazać od razu;
    (b) zmiana dat + _start_export (z dublowanym wątkiem/workerem) -> nowy
        zakres trafia NA DYSK (weryfikacja NOWĄ instancją ConfigStore, tak jak
        przy prawdziwym restarcie procesu);
    (c) "wyłączenie i włączenie aplikacji" — nowe MainWindow zbudowane z
        configu świeżo odczytanego z dysku odtwarza ten zakres."""
    cfg_path = tmp_path / "config.enc"
    store = ConfigStore(cfg_path)
    cfg = store.initialize("haslo-integracyjny")
    profil_z_zakresem = Profile(
        "FirmaA", "1112223344", "tok1", "fa",
        ostatnie_od="2025-03-01", ostatnie_do="2025-03-15",
    )
    profil_bez_zakresu = Profile("FirmaB", "2223334455", "tok2", "fb")
    cfg.profiles.append(profil_z_zakresem)
    cfg.profiles.append(profil_bez_zakresu)
    cfg.target_dir = str(tmp_path / "out")
    store.save(cfg)

    # (a)
    win = MainWindow(store, cfg)
    try:
        assert win.profile_list.currentRow() == 0
        assert win.date_from.date().toPython() == date(2025, 3, 1)
        assert win.date_to.date().toPython() == date(2025, 3, 15)

        # (b)
        win.date_from.setDate(QDate(2026, 5, 1))
        win.date_to.setDate(QDate(2026, 5, 20))
        win.chk_cost.setChecked(True)
        win._start_export()
        qapp.processEvents()

        assert no_dialogs.calls == []  # żadnego ostrzeżenia/błędu po drodze
        assert profil_z_zakresem.ostatnie_od == "2026-05-01"
        assert profil_z_zakresem.ostatnie_do == "2026-05-20"

        odczytany = ConfigStore(cfg_path).unlock("haslo-integracyjny")
        assert odczytany.profiles[0].ostatnie_od == "2026-05-01"
        assert odczytany.profiles[0].ostatnie_do == "2026-05-20"
    finally:
        win.close()
        qapp.processEvents()

    # (c) — nowa instancja ConfigStore + świeżo odczytany config, dokładnie
    # jak przy starcie app/main.py po ponownym uruchomieniu procesu
    store2 = ConfigStore(cfg_path)
    cfg2 = store2.unlock("haslo-integracyjny")
    win2 = MainWindow(store2, cfg2)
    try:
        assert win2.profile_list.currentRow() == 0
        assert win2.date_from.date().toPython() == date(2026, 5, 1)
        assert win2.date_to.date().toPython() == date(2026, 5, 20)
    finally:
        win2.close()
        qapp.processEvents()


def test_profile_dialog_zachowuje_ostatni_zakres_po_edycji(qapp):
    """PRZYCZYNA ŹRÓDŁOWA zgłoszonego błędu: ProfileDialog.result_profile()
    budowało zupełnie NOWY obiekt Profile i nie przepisywało
    ostatnie_od/ostatnie_do z edytowanego profilu. Efekt: każde „Edytuj” na
    profilu (choćby bez zmiany jednego pola — np. tylko podejrzenie tokenu)
    cichcem zerowało zapamiętany zakres z powrotem na pusty, więc kolejny
    start aplikacji pokazywał domyślny poprzedni miesiąc zamiast zapamiętanego
    zakresu — dokładnie objaw zgłoszony z produkcji."""
    profil = Profile(
        "FirmaA", "1112223344", "tok1", "fa",
        ostatnie_od="2026-05-01", ostatnie_do="2026-05-20",
    )
    dlg = ProfileDialog(profil)
    try:
        nowy = dlg.result_profile()
    finally:
        dlg.close()
    assert nowy.ostatnie_od == "2026-05-01"
    assert nowy.ostatnie_do == "2026-05-20"


def test_profile_dialog_nowy_profil_ma_pusty_zakres(qapp):
    """Nowy profil (bez wcześniejszej historii) nie ma czego przywracać —
    ostatnie_od/ostatnie_do zostają puste (domyślne), tak jak dotąd."""
    dlg = ProfileDialog()
    try:
        dlg.name.setText("Nowa Firma")
        dlg.nip.setText("1112223344")
        dlg.token.setText("token")
        nowy = dlg.result_profile()
    finally:
        dlg.close()
    assert nowy.ostatnie_od == ""
    assert nowy.ostatnie_do == ""
