"""GUI PySide6: profile firm, zakres dat, checkboxy sprzedaż/koszt, eksport w wątku."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, QObject, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.aktualizacja import opis_wersji
from app.config import AppConfig, ConfigStore, Profile
from app.exporter import ExportReport, Kind, run_export
from app.kontrahenci_nr import (
    NAZWA_ZASTEPCZA,
    POZYCJA_MAX,
    RaportSync,
    RejestrNumerow,
    SynchronizacjaOdrzucona,
    WpisNumeru,
    ZmianaWpisu,
    ZmianyOdrzucone,
    _digits,
)
from app.ksef_client import KsefClient
from app.ledger import Ledger
from app.symfonia_sql import SymfoniaSqlError, lista_baz

logger = logging.getLogger(__name__)


def nip_valid(nip: str) -> bool:
    digits = "".join(c for c in nip if c.isdigit())
    if len(digits) != 10:
        return False
    weights = [6, 5, 7, 2, 3, 4, 5, 6, 7]
    checksum = sum(int(d) * w for d, w in zip(digits[:9], weights)) % 11
    return checksum != 10 and checksum == int(digits[9])


def parse_invoice_numbers(text: str) -> list[str]:
    """Parsuje wieloliniowe pole numerów faktur (pole „Pobierz tylko wybrane
    faktury”) — separatorem jest nowa linia, przecinek lub średnik; puste
    wpisy są pomijane. Deduplikacja/normalizacja numeru dzieje się później,
    w run_export (_norm_numer)."""
    numery: list[str] = []
    for linia in text.splitlines():
        for kawalek in re.split(r"[,;]", linia):
            kawalek = kawalek.strip()
            if kawalek:
                numery.append(kawalek)
    return numery


def parse_contractor_ids(text: str) -> list[str]:
    """Parsuje pole identyfikatorów kontrahentów (pole „Tylko faktury
    kontrahentów”) — identyfikator to NIP albo numer karty z rejestru numerów,
    rozdzielone przecinkami, średnikami lub dowolnymi białymi znakami (spacja,
    nowa linia — pole może być jedno- lub wielolinijkowe). Tłumaczenie numeru
    karty na NIP i walidacja dzieją się później, w run_export."""
    return [kawalek for kawalek in re.split(r"[,;\s]+", text.strip()) if kawalek]


class _DataEdit(QDateEdit):
    """Pole daty, w którym wpisanie daty z kreskami działa jak należy.

    Zwykły QDateEdit po wpisaniu pełnej sekcji (np. roku „2026”) sam przechodzi
    do następnej; kreska wpisana zaraz potem przeskakiwała jeszcze jedną sekcję,
    więc „2026-07-31” dawało 2026-06-07. Kreska, kropka, ukośnik albo spacja
    tuż po automatycznym przejściu są teraz pomijane."""

    _SEPARATORY = "-./ "

    def __init__(self, data: QDate):
        super().__init__(data)
        self._auto_przejscie = False

    def keyPressEvent(self, event) -> None:
        tekst = event.text()
        if self._auto_przejscie and tekst and tekst in self._SEPARATORY:
            self._auto_przejscie = False
            event.accept()
            return
        sekcja_przed = self.currentSectionIndex()
        super().keyPressEvent(event)
        self._auto_przejscie = (
            bool(tekst) and tekst.isdigit() and self.currentSectionIndex() != sekcja_przed
        )


class _DataDoEdit(_DataEdit):
    """Pole „Do”: przy zmianie samego miesiąca lub roku (strzałki, kółko myszy,
    wpisanie z klawiatury) zostaje na końcu miesiąca, jeśli tam było.

    Zwykły QDateEdit zostawia dzień: zapamiętane 30.06 po przestawieniu miesiąca
    dawało 30.07, a ten krótszy zakres zapisywał się w profilu i przechodził na
    kolejne miesiące — faktury z ostatniego dnia miesiąca wypadały z eksportu.
    Wybór z kalendarza i ustawienie daty z kodu działają bez zmian."""

    _SEKCJE_MIESIACA = (
        QDateTimeEdit.Section.MonthSection,
        QDateTimeEdit.Section.YearSection,
    )

    def __init__(self, data: QDate, pole_od: QDateEdit):
        super().__init__(data)
        self._pole_od = pole_od

    def stepBy(self, steps: int) -> None:
        przed, sekcja = self.date(), self.currentSection()
        super().stepBy(steps)
        self._trzymaj_koniec_miesiaca(przed, sekcja)

    def keyPressEvent(self, event) -> None:
        przed, sekcja = self.date(), self.currentSection()
        super().keyPressEvent(event)
        self._trzymaj_koniec_miesiaca(przed, sekcja)

    def focusNextPrevChild(self, next_: bool) -> bool:
        # Tab/Shift+Tab między sekcjami zatwierdza niedokończony wpis (np. „1”
        # w miesiącu -> styczeń) — ta ścieżka nie przechodzi przez keyPressEvent
        przed, sekcja = self.date(), self.currentSection()
        wynik = super().focusNextPrevChild(next_)
        self._trzymaj_koniec_miesiaca(przed, sekcja)
        return wynik

    def focusOutEvent(self, event) -> None:
        # kliknięcie obok też zatwierdza niedokończony wpis
        przed, sekcja = self.date(), self.currentSection()
        super().focusOutEvent(event)
        self._trzymaj_koniec_miesiaca(przed, sekcja)

    def _trzymaj_koniec_miesiaca(self, przed: QDate, sekcja) -> None:
        if sekcja not in self._SEKCJE_MIESIACA:
            return
        po = self.date()
        if (po.year(), po.month()) == (przed.year(), przed.month()):
            return
        if po.day() != min(przed.day(), po.daysInMonth()):
            return  # dzień zmieniony świadomie
        if not self._byl_koniec_miesiaca(przed):
            return
        koniec = QDate(po.year(), po.month(), po.daysInMonth())
        if koniec != po:
            self.setDate(koniec)

    def _byl_koniec_miesiaca(self, przed: QDate) -> bool:
        if przed.day() == przed.daysInMonth():
            return True
        # zakres od 1. dnia miesiąca do 28.–30. — tak wyglądają zakresy zapisane
        # w profilach przez błąd opisany wyżej; traktujemy je jak pełny miesiąc
        return przed.day() >= 28 and self._pole_od.date().day() == 1


def _domyslny_zakres_poprzedni_miesiac() -> tuple[QDate, QDate]:
    """Domyślny zakres eksportu, gdy profil nie ma zapamiętanego własnego —
    cały poprzedni miesiąc kalendarzowy (licząc od dzisiaj)."""
    today = date.today()
    first_prev = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    last_prev = today.replace(day=1) - timedelta(days=1)
    return (
        QDate(first_prev.year, first_prev.month, first_prev.day),
        QDate(last_prev.year, last_prev.month, last_prev.day),
    )


def _zakres_dla_profilu(profile: Profile) -> tuple[QDate, QDate]:
    """Zakres dat Od/Do do ustawienia w polach po wybraniu profilu na liście —
    zapamiętany zakres TEJ firmy (profile.ostatnie_od/_do), jeśli poprawny
    (obie daty parsowalne ISO i od <= do); w każdym innym przypadku (brak,
    śmieciowa wartość, od > do) domyślny poprzedni miesiąc kalendarzowy."""
    try:
        od = date.fromisoformat(profile.ostatnie_od)
        do = date.fromisoformat(profile.ostatnie_do)
    except ValueError:
        return _domyslny_zakres_poprzedni_miesiac()
    if od > do:
        return _domyslny_zakres_poprzedni_miesiac()
    return QDate(od.year, od.month, od.day), QDate(do.year, do.month, do.day)


# nazwy baz Symfonii tej instalacji mają postać "FIRMA_FORMA_PRAWNA..."
# (np. "BETALABO_SPÓŁKA_Z_OGRANICZONĄ_ODPOWIEDZIALNOŚCIĄ") — te człony formy
# prawnej pomijamy przy szukaniu "znaczącego" słowa nazwy firmy do dopasowania
_SLOWA_POMIJANE_NAZWA_FIRMY = {
    "sp", "spolka", "spółka", "z", "o", "oo", "ograniczona", "ograniczoną",
    "odpowiedzialnoscia", "odpowiedzialnością", "sa", "firma", "przedsiebiorstwo",
    "przedsiębiorstwo",
}


def _dopasuj_baze_po_nazwie(nazwa_firmy: str, bazy: list[str]) -> str | None:
    """Heurystyka dopasowania bazy Symfonii do profilu bez wcześniejszego
    ustawienia: pierwsze "znaczące" słowo nazwy firmy (pomijając typowe
    człony formy prawnej i krótkie wyrazy) jako fragment nazwy bazy — oba
    porównania casefold. Nic pewnego -> None (użytkownik wybiera ręcznie)."""
    slowa = re.findall(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+", nazwa_firmy or "")
    znaczace = [s for s in slowa if len(s) >= 3 and s.casefold() not in _SLOWA_POMIJANE_NAZWA_FIRMY]
    if not znaczace:
        return None
    fragment = znaczace[0].casefold()
    for baza in bazy:
        if fragment in baza.casefold():
            return baza
    return None


# --------------------------------------------------------------------------- #
# Dialogi

class MasterPasswordDialog(QDialog):
    """Ustawienie (pierwszy start) lub podanie hasła głównego."""

    def __init__(self, first_run: bool, parent=None):
        super().__init__(parent)
        self.first_run = first_run
        self.setWindowTitle("KSeF → Symfonia — hasło główne")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        info = (
            "Ustaw hasło główne. Szyfruje ono tokeny KSeF zapisane na dysku.\n"
            "Zapamiętaj je — bez niego konfiguracja jest nie do odzyskania."
            if first_run
            else "Podaj hasło główne, aby odblokować konfigurację."
        )
        layout.addWidget(QLabel(info))
        form = QFormLayout()
        self.password = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        form.addRow("Hasło:", self.password)
        if first_run:
            self.password2 = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
            form.addRow("Powtórz hasło:", self.password2)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.password.setFocus()

    def _accept(self):
        if not self.password.text():
            QMessageBox.warning(self, "Hasło", "Hasło nie może być puste.")
            return
        if self.first_run and self.password.text() != self.password2.text():
            QMessageBox.warning(self, "Hasło", "Hasła nie są identyczne.")
            return
        self.accept()


class ProfileDialog(QDialog):
    BRAK_BAZY = "(brak — synchronizacja ręczna)"

    def __init__(self, profile: Profile | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Profil firmy")
        self.setMinimumWidth(460)
        self._existing_token = profile.token if profile else ""
        self._existing_konta_override = dict(profile.konta_override) if profile else {}
        # zakres dat Od/Do zapamiętany dla TEJ firmy (patrz Profile.ostatnie_od/_do
        # i app/gui.py::_zakres_dla_profilu) — dialog nie ma pól do jego edycji,
        # więc trzeba go jawnie przenieść do nowego obiektu Profile w
        # result_profile(); w przeciwnym razie KAŻDA edycja profilu (Edytuj)
        # cichnie zerowałaby zapamiętany zakres z powrotem na "brak" (poprzedni
        # miesiąc) — patrz test_profile_dialog_zachowuje_ostatni_zakres_po_edycji
        self._existing_ostatnie_od = profile.ostatnie_od if profile else ""
        self._existing_ostatnie_do = profile.ostatnie_do if profile else ""
        form = QFormLayout(self)
        self.name = QLineEdit(profile.name if profile else "")
        self.nip = QLineEdit(profile.nip if profile else "")
        self.token = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText(
            "(bez zmian)" if profile and profile.token else "wklej token KSeF"
        )
        self.subfolder = QLineEdit(profile.subfolder if profile else "")
        form.addRow("Nazwa firmy:", self.name)
        form.addRow("NIP:", self.nip)
        form.addRow("Token KSeF:", self.token)
        form.addRow("Podfolder:", self.subfolder)

        self.db_symfonia_combo: QComboBox | None = None
        self.db_symfonia_edit: QLineEdit | None = None
        istniejaca_baza = profile.db_symfonia if profile else ""
        try:
            bazy = lista_baz()
        except SymfoniaSqlError as exc:
            self.db_symfonia_edit = QLineEdit(istniejaca_baza)
            self.db_symfonia_edit.setPlaceholderText("nazwa bazy Symfonii (SQL)")
            self.db_symfonia_edit.setToolTip(
                f"Lista baz Symfonii niedostępna — wpisz nazwę bazy ręcznie.\n\n{exc}"
            )
            form.addRow("Baza Symfonii (SQL):", self.db_symfonia_edit)
            self.db_symfonia_edit.textChanged.connect(self._sync_autosync_checkbox)
        else:
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItem(self.BRAK_BAZY)
            combo.addItems(bazy)
            wybrana = self._startowa_baza(
                istniejaca_baza,
                profile.nip if profile else self.nip.text(),
                profile.name if profile else self.name.text(),
                bazy,
            )
            idx = combo.findText(wybrana) if wybrana else -1
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.currentTextChanged.connect(self._sync_autosync_checkbox)
            self.db_symfonia_combo = combo
            form.addRow("Baza Symfonii (SQL):", combo)

        self.chk_sql_autosync = QCheckBox("Synchronizuj z Symfonią przed każdym eksportem")
        self.chk_sql_autosync.setChecked(profile.sql_autosync if profile else True)
        form.addRow("", self.chk_sql_autosync)
        self._sync_autosync_checkbox()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    @staticmethod
    def _startowa_baza(istniejaca: str, nip: str, nazwa: str, bazy: list[str]) -> str:
        """Nazwa bazy do prefill combo: ustawienie już zapisane w profilu ma
        pierwszeństwo; dla profilu bez ustawienia jedyną podpowiedzią jest
        dopasowanie po fragmencie nazwy firmy. Pusty wynik = brak dopasowania
        (zostaje pozycja „brak”)."""
        if istniejaca:
            return istniejaca
        return _dopasuj_baze_po_nazwie(nazwa, bazy) or ""

    def _current_baza(self) -> str:
        if self.db_symfonia_combo is not None:
            text = self.db_symfonia_combo.currentText().strip()
            return "" if text == self.BRAK_BAZY else text
        if self.db_symfonia_edit is not None:
            return self.db_symfonia_edit.text().strip()
        return ""

    def _sync_autosync_checkbox(self, *_):
        self.chk_sql_autosync.setEnabled(bool(self._current_baza()))

    def _accept(self):
        if not self.name.text().strip():
            QMessageBox.warning(self, "Profil", "Podaj nazwę firmy.")
            return
        if not nip_valid(self.nip.text()):
            QMessageBox.warning(self, "Profil", "NIP jest niepoprawny (10 cyfr + suma kontrolna).")
            return
        if not self._existing_token and not self.token.text().strip():
            QMessageBox.warning(self, "Profil", "Podaj token KSeF (uprawnienie: przeglądanie faktur).")
            return
        self.accept()

    def result_profile(self) -> Profile:
        nip = "".join(c for c in self.nip.text() if c.isdigit())
        token = self.token.text().strip() or self._existing_token
        subfolder = self.subfolder.text().strip() or nip
        return Profile(
            self.name.text().strip(), nip, token, subfolder,
            konta_override=dict(self._existing_konta_override),
            db_symfonia=self._current_baza(),
            sql_autosync=self.chk_sql_autosync.isChecked(),
            ostatnie_od=self._existing_ostatnie_od,
            ostatnie_do=self._existing_ostatnie_do,
        )


class ScalKontrahentaDialog(QDialog):
    """Wybór kontrahenta docelowego przy scalaniu duplikatu (dodaj_alias)."""

    def __init__(
        self, rejestr: RejestrNumerow, profil_nip: str, zrodlo: WpisNumeru, parent=None
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Scal kontrahenta — {zrodlo.nazwa or zrodlo.klucz}")
        self.setMinimumSize(520, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"Wybierz kontrahenta docelowego, z którym scalić „{zrodlo.nazwa or zrodlo.klucz}” "
                f"(pozycja {zrodlo.pozycja}).\nPo scaleniu wszystkie kolejne faktury tego "
                "kontrahenta zostaną zaksięgowane na numer docelowy — pozycja źródłowa "
                "przestaje być używana."
            )
        )
        self.lista = QListWidget()
        podobni = rejestr.podobni(profil_nip, zrodlo.nazwa)
        podobne_klucze = {w.klucz for w, _ in podobni}
        for w, ratio in podobni:
            # cele scalania: bez samego siebie i bez już scalonych duplikatów
            # (scalanie z duplikatem tworzyłoby łańcuch zamiast punktu stałego)
            if w.klucz == zrodlo.klucz or w.status == "duplikat":
                continue
            item = QListWidgetItem(
                f"{w.pozycja}   {w.nazwa}   ({w.klucz})   — podobieństwo {ratio:.0%}"
            )
            item.setData(Qt.ItemDataRole.UserRole, w.klucz)
            self.lista.addItem(item)
        for w in rejestr.lista(profil_nip):
            if w.klucz == zrodlo.klucz or w.klucz in podobne_klucze or w.status == "duplikat":
                continue
            item = QListWidgetItem(f"{w.pozycja}   {w.nazwa}   ({w.klucz})")
            item.setData(Qt.ItemDataRole.UserRole, w.klucz)
            self.lista.addItem(item)
        layout.addWidget(self.lista, stretch=1)
        self.chk_usunieta = QCheckBox(
            "Karta-duplikat została/zostanie usunięta w FK (zwolnij jej numer)"
        )
        layout.addWidget(self.chk_usunieta)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self):
        if self.lista.currentItem() is None:
            QMessageBox.warning(self, "Scal", "Wybierz kontrahenta docelowego.")
            return
        self.accept()

    def result_values(self) -> tuple[str, bool]:
        klucz_docelowy = self.lista.currentItem().data(Qt.ItemDataRole.UserRole)
        return klucz_docelowy, self.chk_usunieta.isChecked()


class StatusDelegate(QStyledItemDelegate):
    """Edytor komórki Status — lista rozwijana zamiast dowolnego tekstu, żeby nie
    dało się wpisać wartości spoza obsługiwanego zbioru statusów."""

    STATUSY = ["seed", "auto", "manual", "duplikat"]

    def createEditor(self, parent, option, index):  # noqa: D102 — sygnatura Qt
        combo = QComboBox(parent)
        combo.addItems(self.STATUSY)
        # wybór z listy commituje od razu — bez dodatkowego Enter/utraty fokusu
        combo.activated.connect(lambda _: self._commit(combo))
        return combo

    def _commit(self, combo: QComboBox) -> None:
        self.commitData.emit(combo)
        self.closeEditor.emit(combo)

    def setEditorData(self, editor: QComboBox, index) -> None:
        wartosc = index.data(Qt.ItemDataRole.DisplayRole) or ""
        # wartość spoza listy (np. wklejona z arkusza) -> BEZ zaznaczenia (-1):
        # samo otwarcie i zamknięcie edytora nie może po cichu podmienić jej na
        # pierwszą pozycję listy ("seed") — błędny status wychwyci „Zapisz”
        editor.setCurrentIndex(editor.findText(wartosc))

    def setModelData(self, editor: QComboBox, model, index) -> None:
        if editor.currentIndex() < 0:
            return  # nic nie wybrano — zostaw wartość komórki bez zmian
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)


class RejestrDialog(QDialog):
    """Rejestr numerów kontrahentów dla wybranego profilu — w trybie ARKUSZA
    kalkulacyjnego: edycja bezpośrednio w komórkach, wiersze dodaje/usuwa się
    swobodnie, wklejanie z Excela. Tabela jest ŹRÓDŁEM PRAWDY o numerach kart:
    aplikacja nie ma łączności z Symfonią, księgowa sprawdza numery w Symfonii
    ręcznie i poprawia je tutaj.

    MODEL ZAPISU — przycisk „Zapisz” (Ctrl+S): każda edycja (komórki, „Dodaj
    wiersz”, „Usuń zaznaczone”, wklejanie, status, pola kont księgowych) jest
    od razu widoczna w tabeli, ale do rejestru/configu trafia DOPIERO po
    kliknięciu „Zapisz”. Niezapisane zmiany są wyróżnione (zmieniona komórka —
    żółte tło, nowy wiersz — zielone, wiersz do usunięcia — szary i
    przekreślony) i policzone w etykiecie pod tabelą. „Zapisz” waliduje CAŁY
    zestaw naraz i zapisuje go w jednej transakcji (RejestrNumerow.zapisz_zmiany)
    — przy błędach nic nie jest zapisane, wpisane wartości zostają w tabeli, a
    komórki z błędem są czerwone (lista w komunikacie). Nic w tabeli nie jest
    cofane automatycznie. Edytowany wpis dostaje status "manual" (chyba że
    księgowa sama zmieniła status; wiersz-duplikat zostaje "duplikat") —
    synchronizacja z Symfonią takich wpisów nie nadpisuje.

    Zmiana NIP-u albo nazwy/miejscowości (kontrahent bez NIP) zmienia KLUCZ
    wpisu, a eksport liczy klucz z faktury KSeF — zapis dopisuje więc alias
    stary klucz -> nowy (RejestrNumerow._aliasy_po_zmianach), żeby kolejna
    faktura tego kontrahenta trafiła na tę samą kartę, a nie na nowy numer.

    Identyfikator wiersza to ZAWSZE Qt.UserRole jego komórek (klucz wpisu w
    bazie albo "nowy:N" dla niezapisanego wiersza), nigdy indeks wiersza —
    wiersze przesuwają się przy sortowaniu nagłówkiem, filtrze i zmianie
    pozycji (dawna mapa wiersz->klucz nieodświeżana po sortowaniu kierowała
    edycję do innego kontrahenta).

    Zamknięcie okna (X, Escape, „Zamknij”) z niezapisanymi zmianami pyta
    „Zapisać zmiany?”; operacje hurtowe (import, synchronizacje, scalanie,
    czyszczenie, eksport CSV) działają na zapisanym rejestrze, więc najpierw
    wymagają zapisu.

    Numer (pozycja) = numer porządkowy karty w kartotece FK — patrz
    app/kontrahenci_nr.py. Dialog otwiera własny RejestrNumerow i zamyka go
    przy zamknięciu (patrz done()).

    Zaprojektowany pod realną skalę (500+ wpisów): szukajka filtrująca na
    żywo, sortowanie kolumn (Pozycja numerycznie), klucz techniczny w
    tooltipie zamiast osobnej kolumny, wyróżnienie wierszy wg statusu."""

    KOLUMNY = ["Pozycja", "NIP", "Nazwa", "Miejscowość", "Status"]
    COL_POZYCJA, COL_NIP, COL_NAZWA, COL_MIEJSCOWOSC, COL_STATUS = range(5)
    # pole błędu walidacji (BladZmiany.pole) -> kolumna tabeli
    _KOLUMNA_POLA = {
        "pozycja": COL_POZYCJA, "nip": COL_NIP, "nazwa": COL_NAZWA,
        "miejscowosc": COL_MIEJSCOWOSC, "status": COL_STATUS,
    }
    # wyróżnienia niezapisanych zmian — tło komórki
    KOLOR_ZMIANA = QColor(255, 236, 170)
    KOLOR_NOWY = QColor(206, 238, 206)
    KOLOR_USUNIETY = QColor(230, 230, 230)
    KOLOR_BLAD = QColor(255, 160, 160)
    KOLOR_WYSZARZONY = QColor(150, 150, 150)
    PREFIKS_NOWEGO = "nowy:"
    # pola kont księgowych — nazwy widgetów = klucze profile.konta_override =
    # atrybuty wartości wspólnych w AppConfig
    POLA_KONT = (
        "konto_prefix_sprzedaz", "konto_prefix_koszt", "konto_sprzedaz_netto",
        "konto_sprzedaz_vat", "konto_koszt_vat", "konto_koszt_netto",
    )

    def __init__(self, profile: Profile, cfg: AppConfig, store: ConfigStore, parent=None):
        super().__init__(parent)
        self.profile = profile
        self.profil_nip = profile.nip
        self.cfg = cfg
        self.store = store
        self.rejestr = RejestrNumerow()
        # stan ZAPISANY (z bazy, po ostatnim _refresh) — punkt odniesienia dla
        # wykrywania niezapisanych zmian
        self._wpisy: list[WpisNumeru] = []
        self._by_klucz: dict[str, WpisNumeru] = {}
        # stan niezapisanych zmian — kluczem jest identyfikator wiersza z
        # UserRole (patrz docstring klasy); wartości komórek są w samej tabeli
        self._nowe: set[str] = set()
        self._usuniete: set[str] = set()
        self._bledy: dict[str, dict[int, str]] = {}  # id wiersza -> {kolumna: komunikat}
        self._licznik_nowych = 0
        # blokada obsługi itemChanged na czas programowego wypełniania komórek
        self._loading = False

        self.setWindowTitle(f"Numeracja kontrahentów — {profile.name}")
        self.resize(900, 600)
        self.setMinimumSize(700, 450)
        layout = QVBoxLayout(self)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Szukaj (numer / NIP / nazwa / miejscowość)")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)

        self.table = QTableWidget(0, len(self.KOLUMNY))
        self.table.setHorizontalHeaderLabels(self.KOLUMNY)
        # tryb arkusza: dwuklik/F2/pisanie od razu edytuje komórkę
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self._status_delegate = StatusDelegate(self.table)
        self.table.setItemDelegateForColumn(self.COL_STATUS, self._status_delegate)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(self.COL_NAZWA, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(self.COL_POZYCJA, 70)
        self.table.setColumnWidth(self.COL_NIP, 110)
        self.table.setColumnWidth(self.COL_MIEJSCOWOSC, 160)
        self.table.setColumnWidth(self.COL_STATUS, 90)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table, stretch=1)

        # skróty klawiszowe tabeli — WidgetShortcut: działają tylko gdy fokus jest
        # na samej tabeli, więc nie przeszkadzają edytorowi komórki (np. Delete
        # kasujące znak w polu tekstowym, Ctrl+C/V w edytowanym polu)
        self._skrot_usun = QShortcut(QKeySequence.StandardKey.Delete, self.table)
        self._skrot_usun.setContext(Qt.ShortcutContext.WidgetShortcut)
        self._skrot_usun.activated.connect(self._usun_zaznaczone)
        self._skrot_kopiuj = QShortcut(QKeySequence.StandardKey.Copy, self.table)
        self._skrot_kopiuj.setContext(Qt.ShortcutContext.WidgetShortcut)
        self._skrot_kopiuj.activated.connect(self._kopiuj)
        self._skrot_wklej = QShortcut(QKeySequence.StandardKey.Paste, self.table)
        self._skrot_wklej.setContext(Qt.ShortcutContext.WidgetShortcut)
        self._skrot_wklej.activated.connect(self._wklej)

        self.licznik_label = QLabel()
        layout.addWidget(self.licznik_label)

        pomoc_label = QLabel(
            "Nowe karty dostają numer max+1 (jak Symfonia FK). Po ręcznych zmianach "
            "kart w FK użyj „Synchronizuj z Symfonią” (SQL, gdy profil ma przypisaną "
            "bazę) albo „Synchronizuj z pliku…” z aktualnym eksportem kartoteki."
        )
        pomoc_label.setWordWrap(True)
        pomoc_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(pomoc_label)

        # Wiersz 1: import/synchronizacja/eksport kartoteki
        btn_row1 = QHBoxLayout()
        self.btn_import = QPushButton("Importuj kartotekę…")
        self.btn_sync_sql = QPushButton("Synchronizuj z Symfonią")
        self.btn_sync_sql.setToolTip(
            "Czyta kartotekę FK.fk_kontrahenci wprost z bazy SQL Symfonii "
            "przypisanej temu profilowi (tylko odczyt) — bez pośredniego pliku "
            "eksportu. Baza Symfonii profilu ustawiana jest w edycji profilu firmy."
        )
        self.btn_sync = QPushButton("Synchronizuj z pliku…")
        self.btn_sync.setToolTip(
            "Kartoteka z eksportu FK jest źródłem prawdy: dodaje brakujące karty, "
            "uzupełnia puste pola, bierze nazwę kontrahenta z NIP-em z Symfonii, poprawia rozjazdy pozycji. Niczego nie usuwa. "
            "Wpisów ustawionych ręcznie (status manual) nie zmienia — różnice "
            "pokazuje w raporcie."
        )
        self.btn_export = QPushButton("Eksportuj CSV…")
        btn_row1.addWidget(self.btn_import)
        btn_row1.addWidget(self.btn_sync_sql)
        btn_row1.addWidget(self.btn_sync)
        btn_row1.addWidget(self.btn_export)
        btn_row1.addStretch()
        layout.addLayout(btn_row1)

        # Wiersz 2: wiersze arkusza (dodawanie/usuwanie/scalanie)
        btn_row2 = QHBoxLayout()
        self.btn_add = QPushButton("Dodaj wiersz")
        self.btn_delete = QPushButton("Usuń zaznaczone")
        self.btn_merge = QPushButton("Scal z…")
        btn_row2.addWidget(self.btn_add)
        btn_row2.addWidget(self.btn_delete)
        btn_row2.addWidget(self.btn_merge)
        btn_row2.addStretch()
        layout.addLayout(btn_row2)

        # Wiersz 3: operacja awaryjna na całym rejestrze profilu
        btn_row3 = QHBoxLayout()
        self.btn_wyczysc = QPushButton("Wyczyść rejestr profilu…")
        self.btn_wyczysc.setToolTip(
            "Ostateczność — usuwa WSZYSTKIE wpisy numeracji tego profilu. Procedurą "
            "naprawczą jest „Synchronizuj z FK…” (nie usuwa danych); użyj tego "
            "przycisku tylko, gdy synchronizacja nie rozwiązuje problemu."
        )
        btn_row3.addWidget(self.btn_wyczysc)
        btn_row3.addStretch()
        layout.addLayout(btn_row3)

        efektywne = cfg.konta_dla(profile)
        konta_box = QGroupBox(f"Konta księgowe — {profile.name}")
        konto_form = QFormLayout(konta_box)
        opis_konta = QLabel(
            "Konta obowiązują tylko dla tej firmy; pozostaw wartość wspólną, "
            "jeśli firma nie ma odstępstw."
        )
        opis_konta.setWordWrap(True)
        opis_konta.setStyleSheet("color: palette(mid);")
        konto_form.addRow(opis_konta)
        self.konto_prefix_sprzedaz = QLineEdit(efektywne["konto_prefix_sprzedaz"])
        self.konto_prefix_sprzedaz.setToolTip(
            "Prefiks konta rozrachunkowego emitowanego w linii WN brutto sekcji "
            "zapis{} pliku hmffk dla faktur SPRZEDAŻOWYCH — pełne konto to "
            "„<prefiks>-<pozycja>”, np. 201-2-31.\n\n"
            f"Puste pole = wartość wspólna (obecnie „{cfg.konto_prefix_sprzedaz}”)."
        )
        konto_form.addRow("Rozrachunki sprzedaży (WN brutto):", self.konto_prefix_sprzedaz)
        self.konto_prefix_koszt = QLineEdit(efektywne["konto_prefix_koszt"])
        self.konto_prefix_koszt.setToolTip(
            "Prefiks konta rozrachunkowego emitowanego w linii MA brutto sekcji "
            "zapis{} pliku hmffk dla faktur KOSZTOWYCH — pełne konto to "
            "„<prefiks>-<pozycja>”, np. 202-2-31.\n\n"
            f"Puste pole = wartość wspólna (obecnie „{cfg.konto_prefix_koszt}”)."
        )
        konto_form.addRow("Rozrachunki kosztów (MA brutto):", self.konto_prefix_koszt)
        self.konto_sprzedaz_netto = QLineEdit(efektywne["konto_sprzedaz_netto"])
        self.konto_sprzedaz_netto.setToolTip(
            "Konto linii MA netto pełnego dekretu sprzedaży (nie dotyczy kosztów — "
            "konta kosztowe przypisuje księgowa).\n\n"
            f"Puste pole = wartość wspólna (obecnie „{cfg.konto_sprzedaz_netto}”)."
        )
        konto_form.addRow("Przychody (MA netto, sprzedaż):", self.konto_sprzedaz_netto)
        self.konto_sprzedaz_vat = QLineEdit(efektywne["konto_sprzedaz_vat"])
        self.konto_sprzedaz_vat.setToolTip(
            "Konto linii MA VAT należnego pełnego dekretu sprzedaży (nie dotyczy "
            "kosztów — konta kosztowe przypisuje księgowa).\n\n"
            f"Puste pole = wartość wspólna (obecnie „{cfg.konto_sprzedaz_vat}”)."
        )
        konto_form.addRow("VAT należny (MA VAT, sprzedaż):", self.konto_sprzedaz_vat)
        self.konto_koszt_vat = QLineEdit(efektywne["konto_koszt_vat"])
        self.konto_koszt_vat.setToolTip(
            "Konto linii WN VAT naliczonego pełnego dekretu kosztu.\n\n"
            f"Puste pole = wartość wspólna (obecnie „{cfg.konto_koszt_vat}”)."
        )
        konto_form.addRow("VAT naliczony (WN VAT, koszty):", self.konto_koszt_vat)
        self.konto_koszt_netto = QLineEdit(efektywne["konto_koszt_netto"])
        self.konto_koszt_netto.setToolTip(
            "Konto linii WN netto pełnego dekretu kosztu. Puste — pole zostaje "
            "puste w pliku hmffk, konto przypisuje księgowa ręcznie w FK.\n\n"
            f"Puste pole = wartość wspólna (obecnie „{cfg.konto_koszt_netto}”)."
        )
        konto_form.addRow(
            "Koszty (WN netto, koszty; puste = przypisuje księgowa):", self.konto_koszt_netto
        )
        self.termin_fallback_dni = QSpinBox()
        self.termin_fallback_dni.setRange(0, 365)
        self.termin_fallback_dni.setValue(efektywne["termin_fallback_dni"])
        self.termin_fallback_dni.setToolTip(
            "Gdy faktura z KSeF (FA(3)) nie ma podanego terminu płatności, termin "
            "liczony jest jako data wystawienia + tyle dni.\n\n"
            f"Taka sama wartość jak wspólna (obecnie {cfg.termin_fallback_dni} dni) "
            "= brak nadpisania dla tej firmy."
        )
        konto_form.addRow(
            "Termin płatności: +dni gdy brak na fakturze", self.termin_fallback_dni
        )
        layout.addWidget(konta_box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        # „Zapisz” obok „Zamknij” (ApplyRole — nie zamyka okna); bez autoDefault,
        # żeby Enter w tabeli/polu nie zapisywał przypadkiem
        self.btn_zapisz = buttons.addButton("Zapisz", QDialogButtonBox.ButtonRole.ApplyRole)
        self.btn_zapisz.setAutoDefault(False)
        self.btn_zapisz.setToolTip(
            "Zapisuje WSZYSTKIE zmiany z tabeli i pól kont naraz (Ctrl+S). Do czasu "
            "kliknięcia nic nie trafia do rejestru; przy błędach nic nie jest "
            "zapisane, a komórki do poprawienia są wyróżnione na czerwono."
        )
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._skrot_zapisz = QShortcut(QKeySequence.StandardKey.Save, self)
        self._skrot_zapisz.activated.connect(self._zapisz)

        # punkt odniesienia dla niezapisanych zmian w polach kont — to, co
        # wynikałoby z pól w chwili otwarcia okna
        self._konta_bazowe = RejestrDialog._override_z_pol(self)

        self.search.textChanged.connect(self._filtruj)
        self.table.itemSelectionChanged.connect(self._aktualizuj_przyciski)
        self.table.itemChanged.connect(self._on_item_changed)
        self.btn_zapisz.clicked.connect(self._zapisz)
        self.btn_import.clicked.connect(self._importuj)
        self.btn_sync_sql.clicked.connect(self._synchronizuj_sql)
        self.btn_sync.clicked.connect(self._synchronizuj)
        self.btn_add.clicked.connect(self._dodaj_wiersz)
        self.btn_delete.clicked.connect(self._usun_zaznaczone)
        self.btn_merge.clicked.connect(self._scal)
        self.btn_export.clicked.connect(self._eksportuj)
        self.btn_wyczysc.clicked.connect(self._wyczysc_rejestr)
        # pola kont NIE zapisują się same (dawniej editingFinished/done()) —
        # tylko liczą się jako niezapisana zmiana do czasu „Zapisz”
        for nazwa in self.POLA_KONT:
            getattr(self, nazwa).textChanged.connect(lambda *_: self._aktualizuj_licznik())
        self.termin_fallback_dni.valueChanged.connect(lambda *_: self._aktualizuj_licznik())

        self._refresh()
        # domyślne sortowanie przy otwarciu: rosnąco po Pozycji; potem zostaje
        # to, co wybierze użytkownik (także po „Zapisz” — patrz _refresh)
        self.table.sortByColumn(self.COL_POZYCJA, Qt.SortOrder.AscendingOrder)

    # ------------------------------------------------------------------ #
    # wypełnianie tabeli

    def _ustaw_wartosc(self, item: QTableWidgetItem, col: int, wartosc) -> None:
        if col == self.COL_POZYCJA:
            # DisplayRole jako int (nie str) -> sortowanie numeryczne, nie
            # leksykograficzne ("9" przed "10"); tekst niebędący liczbą (np.
            # wklejony) zostaje tekstem — odrzuci go walidacja przy „Zapisz”.
            # Liczba ponad POZYCJA_MAX też zostaje TEKSTEM: Qt nie mieści
            # liczby > int64 w DisplayRole (OverflowError urywał wklejanie w
            # połowie), a walidacja i tak ją odrzuci („poza zakresem”)
            if isinstance(wartosc, str):
                tekst = wartosc.strip()
                if tekst.isascii() and tekst.isdigit() and int(tekst) <= POZYCJA_MAX:
                    wartosc = int(tekst)
                else:
                    wartosc = tekst
            item.setData(Qt.ItemDataRole.DisplayRole, wartosc)
        else:
            item.setText("" if wartosc is None else str(wartosc))

    def _ustaw_wiersz(self, row: int, wid: str, wartosci) -> None:
        """Wypełnia JEDEN wiersz — nowy QTableWidgetItem na każdą komórkę, każda
        z identyfikatorem wiersza w UserRole. Wyróżnienia nakłada
        _dekoruj_wiersz."""
        for col, wartosc in enumerate(wartosci):
            item = QTableWidgetItem()
            self._ustaw_wartosc(item, col, wartosc)
            item.setData(Qt.ItemDataRole.UserRole, wid)
            self.table.setItem(row, col, item)

    def _komorki(self, row: int) -> list[QTableWidgetItem]:
        return [self.table.item(row, col) for col in range(len(self.KOLUMNY))]

    def _id_wiersza(self, row: int) -> str | None:
        item = self.table.item(row, self.COL_POZYCJA) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _wiersz_id(self, wid: str) -> int:
        for row in range(self.table.rowCount()):
            if self._id_wiersza(row) == wid:
                return row
        return -1

    def _refresh(self, mapa_kluczy: dict[str, str] | None = None):
        """Wczytuje tabelę z BAZY i porzuca stan niezapisanych zmian — wołane
        tylko, gdy nie ma czego stracić: przy otwarciu, po udanym „Zapisz” i po
        operacjach hurtowych (te wymagają wcześniejszego zapisu — patrz
        _przed_operacja_hurtowa). Zachowuje sortowanie wybrane przez
        użytkownika, filtr i bieżący wiersz (po kluczu — mapa_kluczy
        przekłada identyfikator wiersza na klucz po zapisie, gdy klucz się
        zmienił albo wiersz był nowy)."""
        biezacy = self._id_wiersza(self.table.currentRow())
        kolumna = max(self.table.currentColumn(), 0)
        if biezacy is not None and mapa_kluczy:
            biezacy = mapa_kluczy.get(biezacy, biezacy)
        self._nowe.clear()
        self._usuniete.clear()
        self._bledy.clear()
        # sortowanie WYŁĄCZONE i sygnały zablokowane na czas wypełniania —
        # inaczej Qt przesortowuje tabelę po każdym setItem() (wiersze mieszają
        # się w trakcie budowy), a itemChanged brałby wypełnianie za edycję
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        self._loading = True
        try:
            wpisy = self.rejestr.lista(self.profil_nip)
            self._wpisy = wpisy
            self._by_klucz = {w.klucz: w for w in wpisy}
            self.table.setRowCount(len(wpisy))
            for row, w in enumerate(wpisy):
                self._ustaw_wiersz(row, w.klucz, (w.pozycja, w.nip, w.nazwa, w.miejscowosc, w.status))
                self._dekoruj_wiersz(self._komorki(row))
        finally:
            self._loading = False
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)
            # ponowne włączenie sortuje wg wskaźnika w nagłówku — czyli tak,
            # jak użytkownik ostatnio posortował (bez powrotu na siłę do Pozycji)
            self.table.setSortingEnabled(True)
        if biezacy is not None:
            row = self._wiersz_id(biezacy)
            if row >= 0:
                self.table.setCurrentCell(row, kolumna)
        self._filtruj()
        self._aktualizuj_przyciski()

    def _dekoruj_wiersz(self, items: list[QTableWidgetItem]) -> None:
        """Wyróżnienia wiersza wg stanu niezapisanych zmian — tło: błąd
        walidacji (czerwone) > do usunięcia (szare + przekreślenie) > nowy
        (zielone) > zmieniona komórka (żółte); tooltip z kluczem, wartością
        sprzed zmiany i treścią błędu. Formatowanie wg ZAPISANEGO statusu jak
        dotąd: manual pogrubiony, duplikat wyszarzony.

        Sygnały MODELU są zablokowane na czas zmiany tła/czcionki: to dane
        czysto wizualne, a dataChanged wywołałby itemChanged i ponowne
        sortowanie (przesunięcie wiersza w trakcie obsługi edycji)."""
        if not items or items[0] is None:
            return
        wid = items[0].data(Qt.ItemDataRole.UserRole)
        nowy = wid in self._nowe
        usuniety = wid in self._usuniete
        orig = None if nowy else self._by_klucz.get(wid)
        zmienione = self._roznice(items, orig) if orig is not None else set()
        bledy = self._bledy.get(wid, {})
        status_zapisany = "manual" if nowy else (orig.status if orig is not None else "")
        model = self.table.model()
        poprzednio = model.blockSignals(True)
        try:
            for col, item in enumerate(items):
                if item is None:
                    continue
                if col in bledy:
                    tlo = self.KOLOR_BLAD
                elif usuniety:
                    tlo = self.KOLOR_USUNIETY
                elif nowy:
                    tlo = self.KOLOR_NOWY
                elif col in zmienione:
                    tlo = self.KOLOR_ZMIANA
                else:
                    tlo = None
                item.setData(Qt.ItemDataRole.BackgroundRole, QBrush(tlo) if tlo is not None else None)
                if status_zapisany == "manual" or usuniety:
                    font = QFont()
                    font.setBold(status_zapisany == "manual")
                    font.setStrikeOut(usuniety)
                    item.setFont(font)
                else:
                    item.setData(Qt.ItemDataRole.FontRole, None)
                if usuniety or status_zapisany == "duplikat":
                    item.setForeground(self.KOLOR_WYSZARZONY)
                elif tlo is not None:
                    # jasne tło wyróżnienia -> ciemny tekst także przy ciemnym
                    # motywie systemu (Qt 6 na Windows 11 potrafi go przejąć)
                    item.setForeground(QColor(0, 0, 0))
                else:
                    item.setData(Qt.ItemDataRole.ForegroundRole, None)
                if nowy:
                    podpowiedz = "Nowy wiersz — trafi do rejestru po kliknięciu „Zapisz”"
                else:
                    podpowiedz = f"Klucz: {wid}"
                if usuniety:
                    podpowiedz += (
                        "\nDo usunięcia — zostanie usunięty po kliknięciu „Zapisz” "
                        "(ponowne „Usuń zaznaczone” przywraca wiersz)"
                    )
                elif col in zmienione:
                    podpowiedz += (
                        f"\nZmienione, niezapisane — było: {self._wartosc_zapisana(orig, col)}"
                    )
                if col in bledy:
                    podpowiedz += f"\nBŁĄD: {bledy[col]}"
                item.setToolTip(podpowiedz)
        finally:
            model.blockSignals(poprzednio)
        self.table.viewport().update()

    def _odswiez_wyglad(self) -> None:
        for row in range(self.table.rowCount()):
            self._dekoruj_wiersz(self._komorki(row))

    def _wartosc_zapisana(self, w: WpisNumeru, col: int) -> str:
        wartosc = [str(w.pozycja), w.nip, w.nazwa, w.miejscowosc, w.status][col]
        return f"„{wartosc}”" if wartosc else "(puste)"

    def _roznice(self, items: list[QTableWidgetItem], w: WpisNumeru) -> set[int]:
        """Kolumny, w których wartość w tabeli różni się od zapisanej — po
        normalizacji takiej jak przy zapisie (pozycja jako liczba, NIP same
        cyfry, przycięte spacje), żeby np. „525-244-57-19” zamiast
        „5252445719” nie liczyło się jako zmiana."""
        teksty = [(i.text() if i is not None else "") for i in items]
        zmienione: set[int] = set()
        # bez limitu zakresu: numer ponad POZYCJA_MAX zapisany wcześniej w
        # bazie (np. z synchronizacji) nie jest zmianą, póki go nie ruszono
        if RejestrNumerow._liczba_z_komorki(teksty[self.COL_POZYCJA]) != w.pozycja:
            zmienione.add(self.COL_POZYCJA)
        if _digits(teksty[self.COL_NIP]) != _digits(w.nip):
            zmienione.add(self.COL_NIP)
        if teksty[self.COL_NAZWA].strip() != (w.nazwa or "").strip():
            zmienione.add(self.COL_NAZWA)
        if teksty[self.COL_MIEJSCOWOSC].strip() != (w.miejscowosc or "").strip():
            zmienione.add(self.COL_MIEJSCOWOSC)
        if teksty[self.COL_STATUS].strip() != (w.status or "").strip():
            zmienione.add(self.COL_STATUS)
        return zmienione

    def _selected_wpis(self) -> WpisNumeru | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, self.COL_POZYCJA)
        if item is None:
            return None
        klucz = item.data(Qt.ItemDataRole.UserRole)
        return self._by_klucz.get(klucz)

    def _zaznaczone_id(self) -> list[str]:
        wynik: list[str] = []
        for row in sorted({idx.row() for idx in self.table.selectedIndexes()}):
            wid = self._id_wiersza(row)
            if wid and wid not in wynik:
                wynik.append(wid)
        return wynik

    def _aktualizuj_przyciski(self):
        self.btn_merge.setEnabled(self._selected_wpis() is not None)
        zaznaczone = self._zaznaczone_id()
        self.btn_delete.setEnabled(bool(zaznaczone))
        # zaznaczone same wiersze oznaczone do usunięcia -> ten sam przycisk je przywraca
        przywroc = bool(zaznaczone) and all(wid in self._usuniete for wid in zaznaczone)
        self.btn_delete.setText("Przywróć zaznaczone" if przywroc else "Usuń zaznaczone")

    def _filtruj(self):
        """Filtrowanie na żywo prostą iteracją (bez modelu proxy) — wystarczające
        przy skali 500+ wierszy, a dużo prostsze w utrzymaniu."""
        query = self.search.text().strip().casefold()
        kolumny_szukane = (self.COL_POZYCJA, self.COL_NIP, self.COL_NAZWA, self.COL_MIEJSCOWOSC)
        for row in range(self.table.rowCount()):
            if not query:
                widoczny = True
            else:
                widoczny = any(
                    query in (self.table.item(row, col).text() or "").casefold()
                    for col in kolumny_szukane
                )
            self.table.setRowHidden(row, not widoczny)
        self._aktualizuj_licznik()

    def _aktualizuj_licznik(self):
        total = len(self._wpisy)
        n_seed = sum(1 for w in self._wpisy if w.status == "seed")
        n_auto = sum(1 for w in self._wpisy if w.status == "auto")
        n_manual = sum(1 for w in self._wpisy if w.status == "manual")
        n_dup = sum(1 for w in self._wpisy if w.status == "duplikat")
        tekst = (
            f"Wpisów: {total} (seed {n_seed}, auto {n_auto}, "
            f"manual {n_manual}, duplikaty {n_dup})"
        )
        widoczne = sum(
            1 for row in range(self.table.rowCount()) if not self.table.isRowHidden(row)
        )
        if widoczne != self.table.rowCount():
            tekst += f" — pokazano {widoczne}"
        niezapisane = self._liczba_niezapisanych()
        if niezapisane:
            tekst += f" — Niezapisane zmiany: {niezapisane} (kliknij „Zapisz”)"
        self.licznik_label.setText(tekst)

    # ------------------------------------------------------------------ #
    # niezapisane zmiany — zbieranie, zapis, pytania

    def _zbierz_zmiany(self) -> tuple[list[ZmianaWpisu], list[str]]:
        """Zestaw niezapisanych zmian wprost z tabeli: zmienione i nowe wiersze
        + klucze wpisów do usunięcia. Wiersz niezmieniony (po normalizacji —
        patrz _roznice) nie trafia do zestawu, więc zachowuje swój status."""
        zmiany: list[ZmianaWpisu] = []
        usuniete: list[str] = []
        for row in range(self.table.rowCount()):
            items = self._komorki(row)
            if items[0] is None:
                continue
            wid = items[0].data(Qt.ItemDataRole.UserRole)
            if wid in self._usuniete:
                if wid not in self._nowe:
                    usuniete.append(wid)
                continue
            teksty = [(i.text() if i is not None else "") for i in items]
            if wid in self._nowe:
                stary_klucz = None
                status = teksty[self.COL_STATUS].strip() or "manual"
            else:
                w = self._by_klucz.get(wid)
                if w is None:
                    continue
                roznice = self._roznice(items, w)
                if not roznice:
                    continue
                stary_klucz = wid
                # edytowany wpis = ustawiony ręcznie; chyba że księgowa sama
                # wybrała status w kolumnie Status. Wiersz-duplikat (karta
                # scalona aliasem, numer nadal zajęty w FK) zostaje duplikatem —
                # „manual” pokazywałby go jako aktywną kartę, choć faktury idą na
                # kartę celu scalenia (patrz RejestrNumerow._aliasy_po_zmianach)
                if self.COL_STATUS in roznice:
                    status = teksty[self.COL_STATUS].strip()
                elif w.status == "duplikat":
                    status = "duplikat"
                else:
                    status = "manual"
            zmiany.append(ZmianaWpisu(
                id=wid,
                stary_klucz=stary_klucz,
                pozycja=teksty[self.COL_POZYCJA],
                nip=teksty[self.COL_NIP],
                nazwa=teksty[self.COL_NAZWA],
                miejscowosc=teksty[self.COL_MIEJSCOWOSC],
                status=status,
            ))
        return zmiany, usuniete

    def _liczba_zmian_kont(self) -> int:
        teraz = RejestrDialog._override_z_pol(self)
        klucze = set(teraz) | set(self._konta_bazowe)
        return sum(1 for k in klucze if teraz.get(k) != self._konta_bazowe.get(k))

    def _liczba_niezapisanych(self) -> int:
        zmiany, usuniete = self._zbierz_zmiany()
        return len(zmiany) + len(usuniete) + self._liczba_zmian_kont()

    def _zatwierdz_edytor(self) -> None:
        """Otwarty edytor komórki (księgowa wpisała wartość i od razu kliknęła
        „Zapisz”/„Zamknij” albo operację hurtową) — zatwierdź wpisaną wartość,
        zanim zbierzemy zmiany; inaczej zależałoby to od tego, czy kliknięty
        przycisk zabiera fokus (na różnych platformach różnie)."""
        if self.table.state() != QAbstractItemView.State.EditingState:
            return
        edytor = self.table.indexWidget(self.table.currentIndex())
        if edytor is not None:
            self.table.commitData(edytor)
            self.table.closeEditor(edytor, QAbstractItemDelegate.EndEditHint.NoHint)

    def _zapisz(self) -> bool:
        """Przycisk „Zapisz” (Ctrl+S): cały zestaw zmian tabeli w jednej
        transakcji + pola kont. Zwraca True, gdy nie zostało nic niezapisanego
        (zapisano albo nie było czego); False przy błędach — wtedy NIC z
        tabeli nie jest zapisane, a wpisane wartości zostają."""
        self._zatwierdz_edytor()
        zmiany, usuniete = self._zbierz_zmiany()
        konta_do_zapisu = RejestrDialog._override_z_pol(self) != self.profile.konta_override
        if not zmiany and not usuniete and not konta_do_zapisu:
            if self._bledy:
                self._bledy.clear()
                self._odswiez_wyglad()
            self._aktualizuj_licznik()
            return True
        mapa: dict[str, str] = {}
        if zmiany or usuniete:
            try:
                mapa = self.rejestr.zapisz_zmiany(self.profil_nip, zmiany, usuniete)
            except ZmianyOdrzucone as exc:
                self._pokaz_bledy(exc.bledy)
                return False
            except sqlite3.Error as exc:
                QMessageBox.critical(
                    self, "Nie zapisano zmian",
                    f"Nie zapisano żadnej zmiany — błąd bazy rejestru ({exc}). Wpisane "
                    "wartości zostały w tabeli; spróbuj ponownie.",
                )
                return False
        if konta_do_zapisu:
            try:
                self._zapisz_konta()
            except Exception as exc:  # noqa: BLE001 — komunikat dla użytkownika
                if zmiany or usuniete:
                    self._refresh(mapa)  # tabela JEST już zapisana
                QMessageBox.critical(
                    self, "Nie zapisano kont",
                    "Zmiany w tabeli kontrahentów zostały zapisane, ale zapis kont "
                    f"księgowych nie powiódł się: {exc}",
                )
                return False
            self._konta_bazowe = RejestrDialog._override_z_pol(self)
        if zmiany or usuniete:
            self._refresh(mapa)
        else:
            self._aktualizuj_licznik()
        return True

    def _pokaz_bledy(self, bledy) -> None:
        """Błędy walidacji zestawu: komórki na czerwono (błąd znika z komórki
        po jej edycji), kursor na pierwszym błędzie, komunikat z listą —
        który wiersz/kontrahent, która kolumna i co poprawić."""
        self._bledy = {}
        for b in bledy:
            col = self._KOLUMNA_POLA.get(b.pole, self.COL_NAZWA)
            komorka = self._bledy.setdefault(b.id, {})
            komorka[col] = f"{komorka[col]}\n{b.komunikat}" if col in komorka else b.komunikat
        # wiersz z błędem ukryty filtrem -> pokaż wszystkie, żeby dało się go poprawić
        if any(
            self.table.isRowHidden(row) and self._id_wiersza(row) in self._bledy
            for row in range(self.table.rowCount())
        ):
            self.search.clear()
        self._odswiez_wyglad()
        linie: list[str] = []
        pierwszy: tuple[int, int] | None = None
        for row in range(self.table.rowCount()):
            wid = self._id_wiersza(row)
            if wid not in self._bledy:
                continue
            nazwa = self._tekst_komorki(row, self.COL_NAZWA).strip()
            nip = self._tekst_komorki(row, self.COL_NIP).strip()
            kto = f"„{nazwa}”" if nazwa else (f"NIP {nip}" if nip else "(pusty wiersz)")
            for col, komunikat in sorted(self._bledy[wid].items()):
                if pierwszy is None:
                    pierwszy = (row, col)
                linie.append(
                    f"Wiersz {row + 1} — {kto}, kolumna {self.KOLUMNY[col]}: "
                    + komunikat.replace("\n", "; ")
                )
        if pierwszy is not None:
            self.table.setCurrentCell(*pierwszy)
            self.table.scrollToItem(self.table.item(*pierwszy))
        self._aktualizuj_licznik()
        widoczne = linie[:15]
        tekst = (
            f"Nie zapisano żadnej zmiany — do poprawienia: {len(linie)} (komórki "
            "wyróżnione na czerwono). Wpisane wartości zostały w tabeli; popraw je "
            "i kliknij „Zapisz” ponownie.\n\n" + "\n".join(widoczne)
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Nie zapisano zmian")
        if len(linie) > len(widoczne):
            tekst += f"\n… i {len(linie) - len(widoczne)} więcej (pełna lista w szczegółach)"
            box.setDetailedText("\n".join(linie))
        box.setText(tekst)
        box.exec()

    def _zapytaj(self, tytul: str, tresc: str, przyciski: list[str], informacja: str = "") -> str | None:
        """Pytanie z przyciskami o polskich etykietach; zwraca tekst klikniętego
        przycisku (None — okno zamknięte inaczej). "Anuluj" = Escape,
        "Nie zapisuj" = rola destrukcyjna, pierwszy przycisk = domyślny."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(tytul)
        box.setText(tresc)
        if informacja:
            box.setInformativeText(informacja)
        dodane: list[tuple[QPushButton, str]] = []
        for tekst in przyciski:
            if tekst == "Anuluj":
                rola = QMessageBox.ButtonRole.RejectRole
            elif tekst == "Nie zapisuj":
                rola = QMessageBox.ButtonRole.DestructiveRole
            else:
                rola = QMessageBox.ButtonRole.AcceptRole
            przycisk = box.addButton(tekst, rola)
            dodane.append((przycisk, tekst))
            if tekst == "Anuluj":
                box.setEscapeButton(przycisk)
        box.setDefaultButton(dodane[0][0])
        box.exec()
        klikniety = box.clickedButton()
        return next((tekst for przycisk, tekst in dodane if przycisk is klikniety), None)

    def _przed_operacja_hurtowa(self, nazwa: str) -> bool:
        """Operacje hurtowe działają na ZAPISANYM rejestrze i po nich tabela
        jest wczytywana od nowa — przy niezapisanych zmianach najpierw zapis
        (albo rezygnacja z operacji), żeby niczego nie zgubić ani nie mieszać."""
        self._zatwierdz_edytor()
        niezapisane = self._liczba_niezapisanych()
        if not niezapisane:
            return True
        odpowiedz = self._zapytaj(
            nazwa,
            f"Masz niezapisane zmiany ({niezapisane}). Operacja „{nazwa}” działa na "
            "zapisanym rejestrze — najpierw trzeba zapisać zmiany.\n\nZapisać teraz?",
            ["Zapisz i kontynuuj", "Anuluj"],
        )
        if odpowiedz == "Zapisz i kontynuuj":
            return self._zapisz()
        return False

    def _mozna_zamknac(self) -> bool:
        self._zatwierdz_edytor()
        niezapisane = self._liczba_niezapisanych()
        if not niezapisane:
            return True
        odpowiedz = self._zapytaj(
            "Niezapisane zmiany",
            "Zapisać zmiany?",
            ["Zapisz", "Nie zapisuj", "Anuluj"],
            informacja=(
                f"Niezapisanych zmian: {niezapisane}. „Nie zapisuj” zamyka okno i "
                "porzuca je; „Anuluj” wraca do tabeli."
            ),
        )
        if odpowiedz == "Zapisz":
            return self._zapisz()
        return odpowiedz == "Nie zapisuj"

    # ------------------------------------------------------------------ #
    # operacje hurtowe (na zapisanym rejestrze)

    def _importuj(self):
        if not self._przed_operacja_hurtowa("Importuj kartotekę"):
            return
        if self._wpisy:
            # profil niepusty — zwykły import (dawna procedura naprawcza) zwykle
            # jest niepotrzebny i ryzykowny (duplikaty kluczy/pozycji); zaproponuj
            # synchronizację jako domyślną ścieżkę
            box = QMessageBox(self)
            box.setWindowTitle("Importuj kartotekę")
            box.setText(
                f"Rejestr tego profilu ma już {len(self._wpisy)} wpis(ów).\n\n"
                "Zalecana jest synchronizacja — uzupełnia braki i poprawia rozjazdy "
                "pozycji bez utraty danych. Zwykły import pomija wiersze, których "
                "klucz lub pozycja już istnieje w rejestrze."
            )
            btn_sync = box.addButton("Synchronizuj (zalecane)", QMessageBox.ButtonRole.AcceptRole)
            btn_import = box.addButton("Importuj mimo to", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("Anuluj", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(btn_sync)
            box.exec()
            klikniety = box.clickedButton()
            if klikniety is btn_sync:
                self._synchronizuj()
                return
            if klikniety is not btn_import:
                return  # Anuluj
        sciezka, _ = QFileDialog.getOpenFileName(
            self, "Importuj kartotekę FK", "", "Kartoteka (*.xlsx *.csv)"
        )
        if not sciezka:
            return
        dodane, ostrzezenia = self.rejestr.importuj_kartoteke(self.profil_nip, Path(sciezka))
        komunikat = f"Dodano {dodane} kontrahentów."
        if ostrzezenia:
            widoczne = ostrzezenia[:50]
            komunikat += "\n\nOstrzeżenia:\n" + "\n".join(widoczne)
            if len(ostrzezenia) > len(widoczne):
                komunikat += f"\n… i {len(ostrzezenia) - len(widoczne)} więcej"
        QMessageBox.information(self, "Import kartoteki", komunikat)
        self._refresh()

    def _synchronizuj_sql(self):
        if not self._przed_operacja_hurtowa("Synchronizuj z Symfonią"):
            return
        if not self.profile.db_symfonia:
            QMessageBox.information(
                self, "Synchronizuj z Symfonią",
                "Ten profil nie ma przypisanej bazy Symfonii (SQL). Ustaw ją w "
                "edycji profilu firmy (pole „Baza Symfonii (SQL)”) albo użyj "
                "„Synchronizuj z pliku…”.",
            )
            return
        try:
            raport = self.rejestr.synchronizuj_z_sql(self.profil_nip, self.profile.db_symfonia)
        except SymfoniaSqlError as exc:
            QMessageBox.warning(self, "Synchronizuj z Symfonią", str(exc))
            return
        except SynchronizacjaOdrzucona as exc:
            QMessageBox.warning(self, "Synchronizuj z Symfonią", str(exc))
            return
        self._refresh()
        nazwy = {w.klucz: (w.nazwa or w.klucz) for w in self._wpisy}
        self._pokaz_raport_sync(raport, nazwy)

    def _synchronizuj(self):
        if not self._przed_operacja_hurtowa("Synchronizuj z pliku"):
            return
        sciezka, _ = QFileDialog.getOpenFileName(
            self, "Synchronizuj z kartoteką FK", "", "Kartoteka (*.xlsx *.csv)"
        )
        if not sciezka:
            return
        try:
            raport = self.rejestr.synchronizuj_z_kartoteka(self.profil_nip, Path(sciezka))
        except SynchronizacjaOdrzucona as exc:
            QMessageBox.warning(self, "Synchronizacja z FK", str(exc))
            return
        except ValueError as exc:
            QMessageBox.warning(self, "Synchronizacja z FK", str(exc))
            return
        self._refresh()
        nazwy = {w.klucz: (w.nazwa or w.klucz) for w in self._wpisy}
        self._pokaz_raport_sync(raport, nazwy)

    @staticmethod
    def _ile_wpisow(n: int) -> str:
        """„1 wpis”, „2 wpisy”, „5 wpisów”, „22 wpisy”, „12 wpisów”."""
        if n == 1:
            return "1 wpis"
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return f"{n} wpisy"
        return f"{n} wpisów"

    def _pokaz_raport_sync(self, raport: RaportSync, nazwy: dict[str, str]) -> None:
        komunikat = f"Dodano {raport.dodane}, zaktualizowano {raport.zaktualizowane}."
        pominiete = len(raport.pominiete_reczne) + len(raport.zablokowane)
        if pominiete:
            komunikat += f" Pominięto {pominiete} (ustawione ręcznie) — szczegóły poniżej."
        if raport.reczne:
            komunikat += (
                f" Status manual (ustawione ręcznie) ma {self._ile_wpisow(raport.reczne)} — "
                "synchronizacja ich nie zmienia (jak to zmienić — w szczegółach)."
            )
        szczegoly: list[str] = []
        if raport.rozjazdy:
            linie = [
                f"{nazwy.get(klucz, klucz)}: było {nasza} -> jest {fk}"
                for klucz, nasza, fk in raport.rozjazdy
            ]
            szczegoly.append("Rozjazdy pozycji (poprawione wg FK):\n" + "\n".join(linie))
        if raport.pominiete_reczne:
            linie = [
                f"{nazwy.get(klucz, klucz)}: było {nasza}, w Symfonii {fk}"
                for klucz, nasza, fk in raport.pominiete_reczne
            ]
            szczegoly.append(
                "Pominięto — ustawione ręcznie (rejestr bez zmian):\n"
                + "\n".join(linie)
                + "\n\nTe numery ustawiono ręcznie w tym oknie, więc synchronizacja "
                "ich nie zmienia. Jeśli w Symfonii jest inaczej — sprawdź kartę i "
                "popraw numer w tabeli (albo ustaw status seed/auto, żeby kolejna "
                "synchronizacja mogła go poprawić)."
            )
        if raport.zablokowane:
            linie = [
                f"{nazwy.get(klucz, klucz)}: w Symfonii pozycja {fk} — w rejestrze zajęta "
                f"przez {nazwy.get(zajmujacy, zajmujacy)}"
                for klucz, fk, zajmujacy in raport.zablokowane
            ]
            szczegoly.append(
                "Pominięto — pozycja z Symfonii zajęta przez wpis ustawiony ręcznie "
                "(nie dodano / nie przesunięto):\n" + "\n".join(linie)
            )
        if raport.brak_w_fk:
            linie = [f"{nazwy.get(klucz, klucz)} (pozycja {poz})" for klucz, poz in raport.brak_w_fk]
            szczegoly.append(
                "Wpisy nieobecne w eksporcie kartoteki FK (NIE zostały usunięte):\n"
                + "\n".join(linie)
                + "\n\nJeśli karta została usunięta w FK — usuń ten wpis (zaznacz "
                "wiersz i wciśnij Del, potem „Zapisz”). Jeśli to inny NIP/nazwa niż "
                "w FK — popraw wartość bezpośrednio w komórce i kliknij „Zapisz”."
            )
        if raport.reczne:
            # wpisy manual mogą pochodzić ze starszej wersji tego okna (każdy
            # dodany/wklejony wiersz dostawał wtedy "manual"), a od modelu z
            # „Zapisz” synchronizacja ich nie dotyka — tylko wskazówka, bez
            # automatycznej zmiany danych (patrz README)
            szczegoly.append(
                f"Status manual ma {self._ile_wpisow(raport.reczne)} w rejestrze — synchronizacja "
                "nie zmienia ich w żaden sposób (ani numeru, ani pustych pól). Status "
                "manual dostaje wiersz poprawiony w tym oknie, ale w starszej wersji "
                "okna dostawał go też KAŻDY dodany lub wklejony wiersz — nie musi więc "
                "znaczyć, że numer sprawdzono w Symfonii.\n"
                "Jeśli synchronizacja ma poprawiać taki wpis według Symfonii: w kolumnie "
                "Status wybierz „seed” i kliknij „Zapisz” (kolejna synchronizacja "
                "poprawi numer, nazwę i puste pola). Zostaw „manual” tam, gdzie numer "
                "sprawdzono ręcznie i ma zostać taki, jak w tabeli."
            )
        if raport.ostrzezenia:
            szczegoly.append("Ostrzeżenia:\n" + "\n".join(raport.ostrzezenia))
        box = QMessageBox(self)
        box.setWindowTitle("Synchronizacja z FK")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(komunikat)
        if szczegoly:
            box.setDetailedText("\n\n".join(szczegoly))
        box.exec()

    # ------------------------------------------------------------------ #
    # edycja komórek — tylko w tabeli, zapis dopiero przyciskiem „Zapisz”

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Edycja komórki przez użytkownika: NIC nie jest zapisywane ani
        cofane — tylko wyróżnienie wiersza i licznik niezapisanych zmian.
        Wiersz identyfikowany po UserRole edytowanej komórki. self._loading
        gasi wywołania z programowego wypełniania (patrz _refresh/_wklej)."""
        if self._loading:
            return
        wid = item.data(Qt.ItemDataRole.UserRole)
        if wid is None:
            return
        bledy = self._bledy.get(wid)
        if bledy:
            bledy.pop(item.column(), None)  # poprawiana komórka — błąd do ponownej oceny
            if not bledy:
                del self._bledy[wid]
        self._dekoruj_wiersz(self._komorki(item.row()))
        self._aktualizuj_licznik()

    def _tekst_komorki(self, row: int, col: int) -> str:
        item = self.table.item(row, col)
        return item.text() if item else ""

    # ------------------------------------------------------------------ #
    # wiersze — dodawanie / usuwanie (niezapisane do czasu „Zapisz”)

    def _nastepna_pozycja(self) -> int:
        """max+1 (jak Symfonia FK) liczone po tabeli i po bazie — nowy wiersz
        nie dostanie numeru, który jest jeszcze gdziekolwiek w użyciu."""
        maks = self.rejestr.nastepny_numer(self.profil_nip) - 1
        for row in range(self.table.rowCount()):
            pozycja = RejestrNumerow._parsuj_pozycje(self._tekst_komorki(row, self.COL_POZYCJA))
            if pozycja is not None:
                maks = max(maks, pozycja)
        return maks + 1

    def _nowy_id(self) -> str:
        self._licznik_nowych += 1
        wid = f"{self.PREFIKS_NOWEGO}{self._licznik_nowych}"
        self._nowe.add(wid)
        return wid

    def _dodaj_wiersz(self):
        self._zatwierdz_edytor()
        self.search.clear()  # nowy wiersz musi być widoczny, żeby móc go edytować
        # nazwa zastępcza z licznikiem — kilka nowych wierszy bez NIP-u nie
        # może mieć tego samego klucza (ta sama nazwa -> ten sam klucz K_...).
        # Bez NIP-u „Zapisz” jej nie przyjmie (kontrahenci_nr.czy_nazwa_zastepcza):
        # przypadkowo dodany i niewypełniony wiersz nie zajmie numeru max+1
        nazwy = {self._tekst_komorki(r, self.COL_NAZWA).strip() for r in range(self.table.rowCount())}
        nazwa = NAZWA_ZASTEPCZA
        licznik = 2
        while nazwa in nazwy:
            nazwa = f"{NAZWA_ZASTEPCZA} {licznik}"
            licznik += 1
        wid = self._nowy_id()
        wartosci = (self._nastepna_pozycja(), "", nazwa, "", "manual")
        self._loading = True
        self.table.setSortingEnabled(False)
        try:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._ustaw_wiersz(row, wid, wartosci)
        finally:
            self.table.setSortingEnabled(True)
            self._loading = False
        row = self._wiersz_id(wid)
        self._dekoruj_wiersz(self._komorki(row))
        self._aktualizuj_licznik()
        self.table.setCurrentCell(row, self.COL_NIP)
        nip_item = self.table.item(row, self.COL_NIP)
        if nip_item is not None:
            self.table.scrollToItem(nip_item)
            self.table.editItem(nip_item)

    def _usun_zaznaczone(self):
        """Oznacza zaznaczone wiersze do usunięcia (usunięcie przy „Zapisz”);
        gdy wszystkie zaznaczone są już oznaczone — przywraca je."""
        self._zatwierdz_edytor()
        zaznaczone = self._zaznaczone_id()
        if not zaznaczone:
            QMessageBox.information(self, "Usuń zaznaczone", "Zaznacz wiersze do usunięcia.")
            return
        if all(wid in self._usuniete for wid in zaznaczone):
            self._usuniete.difference_update(zaznaczone)
        else:
            istniejace = {wid for wid in zaznaczone if wid not in self._nowe}
            aliasy = self.rejestr.aliasy(self.profil_nip)
            powiazane = {
                alias for alias, kanon in aliasy.items()
                if alias in istniejace or kanon in istniejace
            }
            tresc = (
                f"Oznaczyć {len(zaznaczone)} zaznaczonych wpisów do usunięcia?\n\n"
                "Zostaną usunięte z rejestru po kliknięciu „Zapisz” (do tego czasu "
                "są przekreślone; ponowne „Usuń zaznaczone” na nich je przywraca)."
            )
            if powiazane:
                tresc += (
                    f"\n\nPrzy zapisie zostanie też usuniętych {len(powiazane)} "
                    "powiązanych aliasów scaleń."
                )
            tresc += (
                "\n\nNumery wrócą do puli wolnych numerów — usuń TYLKO te wpisy, których "
                "karty zostały (albo zostaną) usunięte także w FK. W przeciwnym razie "
                "kolejny nowy kontrahent dostanie ten sam numer co wciąż istniejąca karta."
            )
            answer = QMessageBox.question(self, "Usuń zaznaczone", tresc)
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._usuniete.update(zaznaczone)
        for wid in zaznaczone:
            row = self._wiersz_id(wid)
            if row >= 0:
                self._dekoruj_wiersz(self._komorki(row))
        self._aktualizuj_licznik()
        self._aktualizuj_przyciski()

    # ------------------------------------------------------------------ #
    # schowek — kopiuj/wklej jak w arkuszu (TSV)

    def _kopiuj(self):
        zakresy = self.table.selectedRanges()
        if not zakresy:
            return
        # rzeczywiste zaznaczone wiersze (nie prostokąt otaczający) — przy
        # niespójnym zaznaczeniu (Ctrl+klik osobnych wierszy) selectedRanges()
        # zwraca kilka rozłącznych zakresów; bounding box dociągnąłby też
        # niezaznaczone wiersze leżące pomiędzy nimi
        left = min(r.leftColumn() for r in zakresy)
        right = max(r.rightColumn() for r in zakresy)
        rows = sorted({row for r in zakresy for row in range(r.topRow(), r.bottomRow() + 1)})
        linie = []
        for row in rows:
            komorki = [self._tekst_komorki(row, col) for col in range(left, right + 1)]
            linie.append("\t".join(komorki))
        QApplication.clipboard().setText("\n".join(linie))

    def _wklej(self):
        """Wklejenie bloku TSV (z Excela) od bieżącej komórki w dół — do
        kolejnych WIDOCZNYCH wierszy (przy filtrze nie trafia w ukryte);
        linie ponad liczbę wierszy -> nowe wiersze. Tylko w tabeli: zapis
        (i walidacja) dopiero przyciskiem „Zapisz”."""
        tekst = QApplication.clipboard().text()
        if not tekst:
            return
        linie = tekst.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        while linie and linie[-1] == "":
            linie.pop()  # końcowy pusty wiersz — zwykły artefakt kopiowania z Excela
        if not linie:
            return
        self._zatwierdz_edytor()
        start_row = self.table.currentRow()
        start_col = self.table.currentColumn()
        if start_row < 0 or start_col < 0:
            start_row, start_col = 0, 0
        n_kolumn = len(self.KOLUMNY)
        # komórki docelowe zebrane PRZED zmianami — to obiekty itemów, więc
        # nie zależą od tego, czy wiersze przesuną się przy sortowaniu
        cele = [
            self._komorki(row)
            for row in range(start_row, self.table.rowCount())
            if not self.table.isRowHidden(row)
        ]
        nastepna = self._nastepna_pozycja()
        zmienione = dodane = 0
        self._loading = True
        self.table.setSortingEnabled(False)
        try:
            for i, linia in enumerate(linie):
                wartosci = linia.split("\t")[: n_kolumn - start_col]
                if i < len(cele):
                    items = cele[i]
                    for offset, wartosc in enumerate(wartosci):
                        self._ustaw_wartosc(items[start_col + offset], start_col + offset, wartosc)
                    zmienione += 1
                    continue
                dane: list = ["", "", "", "", "manual"]
                for offset, wartosc in enumerate(wartosci):
                    dane[start_col + offset] = wartosc
                pozycja = RejestrNumerow._parsuj_pozycje(dane[self.COL_POZYCJA])
                if not str(dane[self.COL_POZYCJA]).strip():
                    dane[self.COL_POZYCJA] = nastepna
                    pozycja = nastepna
                if pozycja is not None:
                    nastepna = max(nastepna, pozycja + 1)
                wid = self._nowy_id()
                row = self.table.rowCount()
                self.table.insertRow(row)
                self._ustaw_wiersz(row, wid, dane)
                dodane += 1
        finally:
            self.table.setSortingEnabled(True)
            self._loading = False
        self._odswiez_wyglad()
        self._aktualizuj_licznik()
        self._aktualizuj_przyciski()
        QMessageBox.information(
            self, "Wklejanie",
            f"Wklejono: {zmienione} zmienionych wierszy, {dodane} nowych.\n\n"
            "Zmiany są widoczne w tabeli (wyróżnione) — do rejestru trafią po "
            "kliknięciu „Zapisz”.",
        )

    def _scal(self):
        if not self._przed_operacja_hurtowa("Scal z…"):
            return
        wpis = self._selected_wpis()
        if wpis is None:
            QMessageBox.information(
                self, "Scal", "Zaznacz wiersz (kontrahenta-duplikat) do scalenia."
            )
            return
        dlg = ScalKontrahentaDialog(self.rejestr, self.profil_nip, wpis, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        klucz_docelowy, zwolnij = dlg.result_values()
        self.rejestr.dodaj_alias(self.profil_nip, wpis.klucz, klucz_docelowy, zwolnij)
        self._refresh()

    def _eksportuj(self):
        if not self._przed_operacja_hurtowa("Eksportuj CSV"):
            return
        sciezka, _ = QFileDialog.getSaveFileName(
            self, "Eksportuj CSV", "kontrahenci.csv", "CSV (*.csv)"
        )
        if not sciezka:
            return
        self.rejestr.eksportuj_csv(self.profil_nip, Path(sciezka))
        QMessageBox.information(self, "Eksport CSV", f"Zapisano: {sciezka}")

    def _wyczysc_rejestr(self):
        if not self._przed_operacja_hurtowa("Wyczyść rejestr profilu"):
            return
        n = len(self._wpisy)
        if n == 0:
            QMessageBox.information(self, "Wyczyść rejestr", "Rejestr tego profilu jest już pusty.")
            return
        tresc = (
            f"Usuwa wszystkie wpisy numeracji tego profilu ({n}).\n\n"
            "W większości przypadków rozjazd z kartoteką FK naprawia „Synchronizuj "
            "z FK…” — bez utraty danych. Użyj tej opcji (Wyczyść) tylko jako "
            "ostateczność, gdy synchronizacja nie rozwiązuje problemu. Po "
            "wyczyszczeniu zaimportuj świeżą kartotekę z FK.\n\nKontynuować?"
        )
        odpowiedz = QMessageBox.warning(
            self, "Wyczyść rejestr profilu", tresc,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if odpowiedz != QMessageBox.StandardButton.Yes:
            return
        # drugi stopień: operacja nieodwracalna, wymaga wpisania słowa wprost —
        # zwykłe Tak/Nie zbyt łatwo kliknąć przez przypadek przy 500+ wpisach
        slowo, ok = QInputDialog.getText(
            self, "Potwierdzenie",
            "Operacja nieodwracalna. Wpisz słowo USUŃ, aby potwierdzić:",
        )
        if not ok or slowo.strip() != "USUŃ":
            QMessageBox.information(self, "Wyczyść rejestr", "Nie potwierdzono — anulowano.")
            return
        usuniete = self.rejestr.wyczysc_profil(self.profil_nip)
        QMessageBox.information(self, "Wyczyść rejestr", f"Usunięto {usuniete} wpisów.")
        self._refresh()

    # ------------------------------------------------------------------ #
    # konta księgowe profilu

    @staticmethod
    def _override_z_pol(dlg) -> dict[str, str]:
        """profile.konta_override, jakie wynikałoby z bieżących wartości pól:
        wartość różna od wspólnej -> nadpisanie; równa wspólnej albo puste pole
        tekstowe -> brak nadpisania (wartość wspólna). termin_fallback_dni:
        QSpinBox nie ma stanu "puste" — powrót do wspólnej to ta sama liczba
        dni. Klucze spoza pól kont zostają bez zmian. Staticmethod na `dlg`,
        żeby działało też na dublerze w testach (patrz tests/test_gui.py)."""
        override = dict(dlg.profile.konta_override)
        for klucz in RejestrDialog.POLA_KONT:
            wartosc = getattr(dlg, klucz).text().strip()
            if not wartosc or wartosc == getattr(dlg.cfg, klucz):
                override.pop(klucz, None)
            else:
                override[klucz] = wartosc
        termin = dlg.termin_fallback_dni.value()
        if termin == dlg.cfg.termin_fallback_dni:
            override.pop("termin_fallback_dni", None)
        else:
            override["termin_fallback_dni"] = str(termin)
        return override

    def _zapisz_konta(self):
        """Zapis pól kont do profile.konta_override (i configu na dysk, tylko
        gdy coś się zmieniło) — wołane WYŁĄCZNIE z „Zapisz” (patrz _zapisz);
        dawny zapis na editingFinished i w done() usunięty: pola kont czekają
        na „Zapisz” jak tabela."""
        nowe = RejestrDialog._override_z_pol(self)
        if nowe == self.profile.konta_override:
            return
        self.profile.konta_override.clear()
        self.profile.konta_override.update(nowe)
        self.store.save(self.cfg)

    def keyPressEvent(self, event):
        # Enter/Return, który dotarł aż do okna (szukajka, pola kont, tabela
        # poza edycją), QDialog zamieniłby na klik przycisku domyślnego — tu
        # „Importuj kartotekę…” (pierwszy przycisk z autoDefault w kolejności
        # fokusu). W arkuszu Enter to zwykły klawisz, więc go połykamy. Przycisk
        # z fokusem obsługuje Enter sam, zanim zdarzenie dotrze do okna.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

    def done(self, result):
        # wspólna ścieżka zamknięcia: X (closeEvent -> reject), Escape (reject)
        # i przycisk Zamknij (rejected -> reject) kończą się tutaj. Przy
        # niezapisanych zmianach pytanie „Zapisać zmiany?” — „Anuluj” albo
        # nieudany zapis (błędy walidacji) zostawia okno otwarte: wracamy BEZ
        # super().done(), więc exec() trwa, a closeEvent ignoruje zamknięcie.
        if not self._mozna_zamknac():
            return
        # dialog otwiera własny RejestrNumerow (sqlite) -> zamknij go przy
        # każdej ścieżce zamknięcia okna
        self.rejestr.close()
        super().done(result)


# --------------------------------------------------------------------------- #
# Worker

def _synchronizuj_sql_przed_eksportem(profile: Profile, log: Callable[[str], None]) -> list[str]:
    """Jeśli profil ma skonfigurowaną bazę Symfonii (SQL) i włączoną
    auto-synchronizację (Profile.db_symfonia/sql_autosync) — synchronizuje
    rejestr numerów kontrahentów z tą bazą PRZED eksportem i zwraca ostrzeżenia
    (rozjazdy/brak_w_fk) do dopisania do raportu eksportu. Bez konfiguracji —
    nic nie robi (pusta lista). SymfoniaSqlError i SynchronizacjaOdrzucona
    (bezpiecznik — patrz app/kontrahenci_nr.py) propagują się do wywołującego:
    eksport MUSI zostać przerwany, żeby nie zaburzyć numeracji kont
    kontrahentów w plikach księgowych (patrz ExportWorker.run)."""
    if not (profile.db_symfonia and profile.sql_autosync):
        return []
    rejestr = RejestrNumerow()
    try:
        raport = rejestr.synchronizuj_z_sql(profile.nip, profile.db_symfonia)
    finally:
        rejestr.close()
    log(f"Synchronizacja z Symfonią: dodano {raport.dodane}, zaktualizowano {raport.zaktualizowane}.")
    return raport.jako_ostrzezenia()


class ExportWorker(QObject):
    progress = Signal(int, int, str)
    log = Signal(str)
    finished = Signal(object)   # ExportReport
    failed = Signal(str)

    def __init__(
        self, profile: Profile, base_url: str, date_from: date, date_to: date,
        kinds: set[Kind], target_dir: Path, fmt: str, combined: bool, only_new: bool,
        kontrahenci_file: bool, structured_address: bool,
        konto_prefix_sprzedaz: str, konto_prefix_koszt: str,
        konto_sprzedaz_netto: str, konto_sprzedaz_vat: str,
        konto_koszt_vat: str, konto_koszt_netto: str, termin_fallback_dni: int,
        cancel: threading.Event,
        invoice_numbers: list[str] | None = None,
        contractor_filter: list[str] | None = None,
    ):
        super().__init__()
        self._args = (
            profile, base_url, date_from, date_to, kinds, target_dir,
            fmt, combined, only_new, kontrahenci_file, structured_address,
            konto_prefix_sprzedaz, konto_prefix_koszt,
            konto_sprzedaz_netto, konto_sprzedaz_vat,
            konto_koszt_vat, konto_koszt_netto, termin_fallback_dni,
            invoice_numbers, contractor_filter,
        )
        self._cancel = cancel

    def run(self):
        (profile, base_url, date_from, date_to, kinds, target_dir,
         fmt, combined, only_new, kontrahenci_file, structured_address,
         konto_prefix_sprzedaz, konto_prefix_koszt,
         konto_sprzedaz_netto, konto_sprzedaz_vat,
         konto_koszt_vat, konto_koszt_netto, termin_fallback_dni,
         invoice_numbers, contractor_filter) = self._args
        client = None
        ledger = None
        try:
            sync_warnings = _synchronizuj_sql_przed_eksportem(profile, self.log.emit)
            client = KsefClient(base_url, profile.nip, profile.token, cancel=self._cancel)
            ledger = Ledger()
            report = run_export(
                profile, client, ledger, date_from, date_to, kinds, target_dir,
                fmt=fmt, combined=combined, only_new=only_new,
                invoice_numbers=invoice_numbers,
                contractor_filter=contractor_filter,
                kontrahenci_file=kontrahenci_file,
                structured_address=structured_address,
                konto_prefix_sprzedaz=konto_prefix_sprzedaz,
                konto_prefix_koszt=konto_prefix_koszt,
                konto_sprzedaz_netto=konto_sprzedaz_netto,
                konto_sprzedaz_vat=konto_sprzedaz_vat,
                konto_koszt_vat=konto_koszt_vat,
                konto_koszt_netto=konto_koszt_netto,
                termin_fallback_dni=termin_fallback_dni,
                on_progress=lambda i, n, nr: self.progress.emit(i, n, nr),
                cancel=self._cancel,
            )
            report.warnings = sync_warnings + report.warnings
            self.finished.emit(report)
        except SymfoniaSqlError as exc:
            self.failed.emit(
                f"Synchronizacja numeracji kontrahentów z Symfonią nie powiodła się: {exc}\n\n"
                "Eksport przerwany, żeby nie zaburzyć numeracji kont kontrahentów w plikach "
                "księgowych. Jeśli chcesz eksportować mimo to, wyłącz auto-synchronizację "
                "w edycji profilu firmy (pole „Baza Symfonii (SQL)”) i zsynchronizuj "
                "numerację ręcznie później."
            )
        except SynchronizacjaOdrzucona as exc:
            self.failed.emit(
                f"Synchronizacja numeracji kontrahentów z Symfonią przerwana: {exc}\n\n"
                "Eksport przerwany, żeby nie zaburzyć numeracji kont kontrahentów w plikach "
                "księgowych. Sprawdź bazę Symfonii (SQL) przypisaną do tego profilu — jeśli "
                "podmiana kartoteki jest zamierzona, użyj najpierw „Wyczyść rejestr "
                "profilu…” w oknie rejestru numerów, a potem zsynchronizuj ponownie."
            )
        except Exception as exc:  # noqa: BLE001 — wątek roboczy nie może umrzeć cicho
            logger.exception("Nieoczekiwany błąd eksportu")
            self.failed.emit(str(exc))
        finally:
            if client is not None:
                client.close()
            if ledger is not None:
                ledger.close()


class _GuiLogBridge(QObject, logging.Handler):
    """Most logging -> sygnał Qt (bezpieczny między wątkami)."""

    message = Signal(str)

    def __init__(self):
        QObject.__init__(self)
        logging.Handler.__init__(self, level=logging.INFO)
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.message.emit(self.format(record))


# --------------------------------------------------------------------------- #
# Główne okno

class MainWindow(QMainWindow):
    def __init__(self, store: ConfigStore, cfg: AppConfig):
        super().__init__()
        self.store = store
        self.cfg = cfg
        self._thread: QThread | None = None
        self._worker: ExportWorker | None = None
        self._cancel = threading.Event()

        # wersja w tytule: od razu widać, który commit działa u księgowej
        # (pusta dla spakowanego .exe — tytuł jak dotąd)
        wersja = opis_wersji(Path(__file__).resolve().parent.parent)
        tytul = "KSeF → Symfonia FK — eksport faktur" + (f" (wersja {wersja})" if wersja else "")
        # stan aktualizacji z uruchom.pyw — nieudana aktualizacja nie może być cicha
        if os.environ.get("KSEF_STAN_AKTUALIZACJI") in ("blad", "wycofano"):
            tytul += " — aktualizacja nieudana (szczegóły: aktualizacje.log)"
        self.setWindowTitle(tytul)
        self.setMinimumSize(920, 560)
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        # lewy panel: profile
        left = QVBoxLayout()
        left.addWidget(QLabel("Profile firm:"))
        self.profile_list = QListWidget()
        left.addWidget(self.profile_list, stretch=1)
        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("Dodaj")
        self.btn_edit = QPushButton("Edytuj")
        self.btn_del = QPushButton("Usuń")
        self.btn_kontrahenci = QPushButton("Kontrahenci…")
        for b in (self.btn_add, self.btn_edit, self.btn_del, self.btn_kontrahenci):
            btn_row.addWidget(b)
        left.addLayout(btn_row)
        root.addLayout(left, stretch=1)

        # prawy panel
        right = QVBoxLayout()
        params = QGroupBox("Parametry eksportu")
        form = QFormLayout(params)
        domyslny_od, domyslny_do = _domyslny_zakres_poprzedni_miesiac()
        self.date_from = _DataEdit(domyslny_od)
        self.date_to = _DataDoEdit(domyslny_do, self.date_from)
        for de in (self.date_from, self.date_to):
            de.setCalendarPopup(True)
            de.setDisplayFormat("yyyy-MM-dd")
        form.addRow("Od:", self.date_from)
        form.addRow("Do:", self.date_to)
        kinds_row = QHBoxLayout()
        self.chk_sales = QCheckBox("Sprzedaż")
        self.chk_cost = QCheckBox("Koszt")
        self.chk_cost.setChecked(True)
        kinds_row.addWidget(self.chk_sales)
        kinds_row.addWidget(self.chk_cost)
        kinds_row.addStretch()
        form.addRow("Rodzaj faktur:", kinds_row)
        self.chk_hmffk = QCheckBox(
            "Jeden plik pod szablon hmffk — kontrahenci + faktury razem (zalecane)"
        )
        self.chk_hmffk.setChecked(True)
        self.chk_hmffk.setToolTip(
            "Wynik: JEDEN plik .TXT importowany w FK standardowym szablonem\n"
            "„Dane z programu Handel, format FK 3.0” (hmffk.AMS) — bez kopiowania\n"
            "czegokolwiek do folderu szablonów Symfonii. Karty kontrahentów są\n"
            "wbudowane w plik. Po odznaczeniu: dotychczasowy tryb SKsefInvoiceImport."
        )
        form.addRow("Format:", self.chk_hmffk)
        self.chk_combined = QCheckBox("Jedna paczka .TXT na rodzaj (zamiast pliku per faktura)")
        form.addRow("", self.chk_combined)
        self.chk_only_new = QCheckBox("Pomiń już pobrane (tylko nowe faktury)")
        self.chk_only_new.setToolTip(
            "Domyślnie pobierany jest komplet faktur z zakresu — ponowny eksport "
            "odtwarza identyczne pliki. Zaznacz do codziennej pracy przyrostowej."
        )
        form.addRow("", self.chk_only_new)
        self.chk_kontrahenci = QCheckBox("Zapisz plik kart kontrahentów (kartoteka)")
        self.chk_kontrahenci.setToolTip(
            "Plik kartoteka\\kontrahenci_*.TXT + szablon kartoteka\\szablon\\"
            "SKsefKontrahenciImport.AMS.\nW Symfonii: osobny profil Katalogów "
            "wymiany danych z szablonem „Import kontrahentow z KSeF”."
        )
        form.addRow("", self.chk_kontrahenci)
        self.chk_addr = QCheckBox("Adresy strukturalne w fakturach (eksperymentalne)")
        self.chk_addr.setToolTip(
            "Dodaje Ulica/NrDomu/KodPocztowy/Miejscowość do sekcji Kontrahent{} "
            "plików faktur.\nUWAGA: Miejscowość zmienia klucz kontrahenta w "
            "szablonie Symfonii — włącz dopiero po potwierdzeniu, że import "
            "faktur działa, i przetestuj na 1 fakturze."
        )
        form.addRow("", self.chk_addr)
        target_row = QHBoxLayout()
        self.target_dir = QLineEdit(self.cfg.target_dir)
        self.btn_browse = QPushButton("Przeglądaj…")
        target_row.addWidget(self.target_dir, stretch=1)
        target_row.addWidget(self.btn_browse)
        form.addRow("Folder docelowy:", target_row)
        right.addWidget(params)

        numbers_box = QGroupBox("Pobierz tylko wybrane faktury (po numerze faktury)")
        numbers_layout = QVBoxLayout(numbers_box)
        self.numbers_edit = QPlainTextEdit()
        self.numbers_edit.setPlaceholderText(
            "Pełne numery, same końcówki (247) albo zakresy (247-251) — "
            "rozdzielone przecinkami/średnikami/nowymi liniami; "
            "puste = eksport wszystkich z zakresu"
        )
        self.numbers_edit.setMaximumHeight(90)
        numbers_layout.addWidget(self.numbers_edit)
        numbers_layout.addWidget(QLabel(
            "Numery są wyszukiwane w wybranym zakresie dat i rodzajach "
            "(Sprzedaż/Koszt) — ustaw je jak przy zwykłym eksporcie."
        ))
        right.addWidget(numbers_box)

        contractors_box = QGroupBox(
            "Tylko faktury kontrahentów (numery kart lub NIP-y, rozdzielone przecinkami/spacjami)"
        )
        contractors_layout = QVBoxLayout(contractors_box)
        self.contractors_edit = QLineEdit()
        self.contractors_edit.setPlaceholderText(
            "np. 42, 9100010014 — numery kart z rejestru numeracji albo NIP-y"
        )
        self.contractors_edit.setToolTip(
            "Sprzedaż: nabywca, Koszt: sprzedawca. Numery kart tłumaczone przez "
            "rejestr numeracji."
        )
        contractors_layout.addWidget(self.contractors_edit)
        right.addWidget(contractors_box)

        action_row = QHBoxLayout()
        self.btn_export = QPushButton("Eksportuj")
        self.btn_export.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        self.btn_cancel = QPushButton("Przerwij")
        self.btn_cancel.setEnabled(False)
        action_row.addWidget(self.btn_export)
        action_row.addWidget(self.btn_cancel)
        action_row.addStretch()
        right.addLayout(action_row)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setValue(0)
        right.addWidget(self.progress)

        right.addWidget(QLabel("Log:"))
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(5000)
        right.addWidget(self.log_view, stretch=1)
        root.addLayout(right, stretch=2)

        # sygnały
        self.chk_hmffk.toggled.connect(self._sync_format_options)
        self._sync_format_options()
        self.numbers_edit.textChanged.connect(self._sync_export_button_label)
        self.contractors_edit.textChanged.connect(self._sync_export_button_label)
        self._sync_export_button_label()
        self.btn_add.clicked.connect(self._add_profile)
        self.btn_edit.clicked.connect(self._edit_profile)
        self.btn_del.clicked.connect(self._delete_profile)
        self.btn_kontrahenci.clicked.connect(self._open_kontrahenci)
        self.profile_list.itemDoubleClicked.connect(lambda _: self._edit_profile())
        # zakres dat Od/Do jest per-profil (patrz _zakres_dla_profilu) — currentRowChanged
        # (nie itemSelectionChanged) odpala się też po _refresh_profiles()
        # (setCurrentRow() tam wywołane), więc obejmuje zarówno ręczny wybór na
        # liście, jak i dodanie/edycję/usunięcie profilu oraz pierwsze otwarcie okna
        self.profile_list.currentRowChanged.connect(self._on_profile_selected)
        self.btn_browse.clicked.connect(self._browse_target)
        self.target_dir.editingFinished.connect(self._save_target)
        self.btn_export.clicked.connect(self._start_export)
        self.btn_cancel.clicked.connect(self._cancel_export)

        # logi -> widok
        self._log_bridge = _GuiLogBridge()
        self._log_bridge.message.connect(self.log_view.appendPlainText)
        logging.getLogger().addHandler(self._log_bridge)
        logging.getLogger().setLevel(logging.INFO)

        self._refresh_profiles()

    def _sync_format_options(self, *_):
        """Opcje trybu SKsef nie mają zastosowania przy pliku hmffk."""
        sksef = not self.chk_hmffk.isChecked()
        for w in (self.chk_combined, self.chk_kontrahenci, self.chk_addr):
            w.setEnabled(sksef)

    def _sync_export_button_label(self, *_):
        """Etykieta przycisku odzwierciedla, czy pola numerów/kontrahentów są
        wypełnione — jeden przycisk „Eksportuj” obsługuje wszystkie tryby (patrz
        _start_export)."""
        n = len(parse_invoice_numbers(self.numbers_edit.toPlainText()))
        m = len(parse_contractor_ids(self.contractors_edit.text()))
        if n and m:
            opis = f"{n} numerów, {m} kontrahentów"
        elif m:
            opis = f"{m} kontrahentów"
        elif n:
            opis = str(n)
        else:
            opis = ""
        self.btn_export.setText(f"Eksportuj wybrane ({opis})" if opis else "Eksportuj")

    # -- profile ----------------------------------------------------------

    def _refresh_profiles(self):
        self.profile_list.clear()
        for p in self.cfg.profiles:
            QListWidgetItem(f"{p.name}   (NIP {p.nip})", self.profile_list)
        if self.profile_list.count():
            self.profile_list.setCurrentRow(0)

    def _selected_profile(self) -> Profile | None:
        row = self.profile_list.currentRow()
        return self.cfg.profiles[row] if 0 <= row < len(self.cfg.profiles) else None

    def _on_profile_selected(self, row: int):
        """Reakcja na zmianę zaznaczenia profilu (patrz podłączenie sygnału w
        __init__): ustawia TYLKO pola dat Od/Do wg zapamiętanego zakresu tego
        profilu (albo domyślnego poprzedniego miesiąca) — inne pola (numery
        faktur, kontrahenci, checkboxy) zostają nietknięte, eksport się nie
        odpala."""
        profile = self.cfg.profiles[row] if 0 <= row < len(self.cfg.profiles) else None
        if profile is None:
            return
        d_from, d_to = _zakres_dla_profilu(profile)
        self.date_from.setDate(d_from)
        self.date_to.setDate(d_to)

    def _add_profile(self):
        dlg = ProfileDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.cfg.profiles.append(dlg.result_profile())
            self.store.save(self.cfg)
            self._refresh_profiles()
            self.profile_list.setCurrentRow(self.profile_list.count() - 1)

    def _edit_profile(self):
        row = self.profile_list.currentRow()
        profile = self._selected_profile()
        if profile is None:
            return
        dlg = ProfileDialog(profile, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.cfg.profiles[row] = dlg.result_profile()
            self.store.save(self.cfg)
            self._refresh_profiles()
            self.profile_list.setCurrentRow(row)

    def _delete_profile(self):
        row = self.profile_list.currentRow()
        profile = self._selected_profile()
        if profile is None:
            return
        answer = QMessageBox.question(
            self, "Usuń profil",
            f"Usunąć profil „{profile.name}” (razem z tokenem)?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            del self.cfg.profiles[row]
            self.store.save(self.cfg)
            self._refresh_profiles()

    def _open_kontrahenci(self):
        profile = self._selected_profile()
        if profile is None:
            QMessageBox.information(self, "Kontrahenci", "Zaznacz profil firmy.")
            return
        dlg = RejestrDialog(profile, self.cfg, self.store, parent=self)
        dlg.exec()

    # -- folder docelowy ----------------------------------------------------

    def _browse_target(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "Folder docelowy eksportu", self.target_dir.text() or str(Path.home())
        )
        if chosen:
            self.target_dir.setText(chosen)
            self._save_target()

    def _save_target(self):
        if self.cfg.target_dir != self.target_dir.text():
            self.cfg.target_dir = self.target_dir.text()
            self.store.save(self.cfg)

    def _zapisz_zakres_profilu(self, profile: Profile, d_from: date, d_to: date) -> None:
        """Zapamiętuje wybrany zakres eksportu w profilu (per firma) — TYLKO
        gdy różni się od już zapisanego, żeby nie zapisywać configu bez
        potrzeby przy każdym eksporcie. Wołane z _start_export PO przejściu
        walidacji dat."""
        od_iso, do_iso = d_from.isoformat(), d_to.isoformat()
        if profile.ostatnie_od == od_iso and profile.ostatnie_do == do_iso:
            return
        profile.ostatnie_od = od_iso
        profile.ostatnie_do = do_iso
        self.store.save(self.cfg)

    # -- eksport --------------------------------------------------------------

    def _start_export(self):
        profile = self._selected_profile()
        if profile is None:
            QMessageBox.warning(self, "Eksport", "Dodaj i wybierz profil firmy.")
            return
        kinds: set[Kind] = set()
        if self.chk_sales.isChecked():
            kinds.add(Kind.SALES)
        if self.chk_cost.isChecked():
            kinds.add(Kind.COST)
        if not kinds:
            QMessageBox.warning(self, "Eksport", "Zaznacz sprzedaż i/lub koszt.")
            return
        target = self.target_dir.text().strip()
        if not target:
            QMessageBox.warning(self, "Eksport", "Wskaż folder docelowy.")
            return
        d_from = self.date_from.date().toPython()
        d_to = self.date_to.date().toPython()
        if d_from > d_to:
            QMessageBox.warning(self, "Eksport", "Data „od” jest późniejsza niż „do”.")
            return
        self._zapisz_zakres_profilu(profile, d_from, d_to)
        invoice_numbers = parse_invoice_numbers(self.numbers_edit.toPlainText())
        contractor_filter = parse_contractor_ids(self.contractors_edit.text())
        self._save_target()

        self._cancel.clear()
        self._set_running(True)
        self.progress.setRange(0, 0)  # nieoznaczony do czasu pierwszego postępu
        self.log_view.appendPlainText("—" * 60)

        konta = self.cfg.konta_dla(profile)
        self._thread = QThread(self)
        self._worker = ExportWorker(
            profile, self.cfg.ksef_base_url, d_from, d_to, kinds, Path(target),
            "hmffk" if self.chk_hmffk.isChecked() else "sksef",
            self.chk_combined.isChecked(), self.chk_only_new.isChecked(),
            self.chk_kontrahenci.isChecked(), self.chk_addr.isChecked(),
            konta["konto_prefix_sprzedaz"], konta["konto_prefix_koszt"],
            konta["konto_sprzedaz_netto"], konta["konto_sprzedaz_vat"],
            konta["konto_koszt_vat"], konta["konto_koszt_netto"],
            konta["termin_fallback_dni"],
            self._cancel,
            invoice_numbers or None,
            contractor_filter or None,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _cancel_export(self):
        self._cancel.set()
        self.log_view.appendPlainText("Przerywanie po bieżącej fakturze…")

    def _set_running(self, running: bool):
        self.btn_export.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        for w in (self.btn_add, self.btn_edit, self.btn_del, self.btn_kontrahenci,
                  self.profile_list, self.date_from, self.date_to, self.chk_sales,
                  self.chk_cost, self.chk_hmffk, self.chk_combined, self.chk_only_new,
                  self.chk_kontrahenci, self.chk_addr, self.target_dir, self.btn_browse,
                  self.numbers_edit, self.contractors_edit):
            w.setEnabled(not running)
        if not running:
            self._sync_format_options()

    def _on_progress(self, done: int, total: int, ksef_number: str):
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress.setFormat(f"{done}/{total}  {ksef_number}")

    def _on_finished(self, report: ExportReport):
        self._set_running(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.progress.setFormat("gotowe")
        lines = [f"Zakończono: {report.summary()}"]
        lines += [f"BŁĄD {nr}: {msg}" for nr, msg in report.failed]
        lines += [f"NIE POBRANO {nr}: {msg}" for nr, msg in report.niepobrane]
        lines += [f"UWAGA: {w}" for w in report.warnings]
        self.log_view.appendPlainText("\n".join(lines))
        niepelny = report.niepelny or report.cancelled or (
            report.exported + report.skipped_dedup + len(report.failed) < report.found
        )

        def ile(n: int, jedna: str, kilka: str, wiele: str) -> str:
            if n == 1:
                return f"1 {jedna}"
            if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
                return f"{n} {kilka}"
            return f"{n} {wiele}"

        czesci = [report.summary()]
        if report.failed:
            # tylko prawdziwe błędy (mapowanie, nietrafione wyszukiwanie) — W6a
            czesci.append(
                f"Faktury z błędami ({len(report.failed)}) należy zaksięgować ręcznie — "
                "szczegóły w logu."
            )
        if report.niepobrane:
            n = len(report.niepobrane)
            czesci.append(
                f"Nie pobrano {n} {'faktury' if n == 1 else 'faktur'} (błąd paczki eksportu "
                "albo KSeF — szczegóły w logu). Uruchom eksport ponownie z zaznaczonym "
                "„Pomiń już pobrane”, żeby je dociągnąć — nie księguj ich ręcznie."
            )
        elif niepelny:
            czesci.append(
                "Nie wszystkie faktury zostały pobrane (przerwanie albo limit KSeF — "
                "szczegóły w logu). Uruchom eksport ponownie z zaznaczonym „Pomiń już "
                "pobrane”, żeby dociągnąć resztę."
            )
        szczegoly = []
        zwykle = [s for s in report.spoznione if not s.bezpiecznik]
        bezp = [s for s in report.spoznione if s.bezpiecznik]
        if zwykle:
            czesci.append(
                "Dołączono "
                + ile(len(zwykle), "spóźnioną fakturę", "spóźnione faktury", "spóźnionych faktur")
                + " z wcześniejszych okresów (wpłynęły do KSeF po domknięciu swojego miesiąca) — "
                "lista w szczegółach."
            )
            szczegoly.append(
                "Spóźnione faktury dołączone do pliku "
                "(numer faktury | data wystawienia | rodzaj | nr KSeF):"
            )
            szczegoly += [
                f"  {s.invoice_number} | {s.issue_date} | {s.rodzaj} | {s.ksef_number}"
                for s in zwykle
            ]
        if bezp:
            from app.exporter import CZEKANIE_NA_OKRES_MAX

            dni = CZEKANIE_NA_OKRES_MAX.days
            czesci.append(
                "Dołączono "
                + ile(len(bezp), "fakturę, która czekała", "faktury, które czekały",
                      "faktur, które czekały")
                + f" ponad {dni} dni od wpływu do KSeF na domknięcie swojego miesiąca "
                "(eksport całego miesiąca) — sprawdź, "
                + ("czy nie jest już zaksięgowana." if len(bezp) == 1
                   else "czy nie są już zaksięgowane.")
            )
            szczegoly.append(
                f"Dołączone po {dni} dniach oczekiwania — sprawdź, czy nie są już "
                "zaksięgowane (numer faktury | data wystawienia | rodzaj | nr KSeF):"
            )
            szczegoly += [
                f"  {s.invoice_number} | {s.issue_date} | {s.rodzaj} | {s.ksef_number}"
                for s in bezp
            ]
        if report.nowi_kontrahenci:
            czesci.append(
                ile(len(report.nowi_kontrahenci), "nowy kontrahent", "nowi kontrahenci",
                    "nowych kontrahentów")
                + " — sprawdź numery kart w Symfonii po imporcie i popraw w oknie "
                "Kontrahenci… (lista w szczegółach)."
            )
            szczegoly.append("Nowi kontrahenci (nazwa | NIP | nr karty | konto):")
            szczegoly += [
                f"  {nazwa} | {nip or 'brak'} | {nr} | {konta}"
                for nazwa, nip, nr, konta in report.nowi_kontrahenci
            ]
        # D-N2: eksport całego miesiąca przycięty (dane KSeF niekompletne) —
        # miesiąc nie jest domknięty, trzeba powtórzyć domknięcie
        czesci += list(report.niedomkniete)

        if report.failed:
            tytul = "Eksport zakończony z błędami"
        elif niepelny:
            tytul = "Eksport niepełny"
        else:
            tytul = "Eksport zakończony"
        ostrzezenie = bool(report.failed or niepelny or szczegoly or report.niedomkniete)
        self._pokaz_okno_konca(ostrzezenie, tytul, "\n\n".join(czesci), "\n".join(szczegoly))

    def _pokaz_okno_konca(self, ostrzezenie: bool, tytul: str, tresc: str, szczegoly: str):
        """Okno końca eksportu. Z listą szczegółów (spóźnione, nowi kontrahenci)
        — instancja QMessageBox z setDetailedText („Pokaż szczegóły…”); bez —
        statyczne QMessageBox.warning/information."""
        if not szczegoly:
            if ostrzezenie:
                QMessageBox.warning(self, tytul, tresc)
            else:
                QMessageBox.information(self, tytul, tresc)
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning if ostrzezenie else QMessageBox.Icon.Information)
        box.setWindowTitle(tytul)
        box.setText(tresc)
        box.setDetailedText(szczegoly)
        box.exec()

    def _on_failed(self, message: str):
        self._set_running(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.log_view.appendPlainText(f"BŁĄD KRYTYCZNY: {message}")
        QMessageBox.critical(self, "Błąd eksportu", message)

    def closeEvent(self, event):
        if self._thread is not None and self._thread.isRunning():
            answer = QMessageBox.question(
                self, "Eksport w toku", "Trwa eksport. Przerwać i zamknąć?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._cancel.set()
            self._thread.quit()
            self._thread.wait(5000)
        logging.getLogger().removeHandler(self._log_bridge)
        event.accept()
