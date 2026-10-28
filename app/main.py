"""Punkt wejścia aplikacji: hasło główne -> odblokowanie konfiguracji -> główne okno."""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QLibraryInfo, QTranslator
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from app.config import ConfigStore, WrongMasterPassword, ConfigCorrupted, app_data_dir
from app.gui import MainWindow, MasterPasswordDialog

MAX_ATTEMPTS = 3


def _setup_logging() -> None:
    """Log do pliku %APPDATA%\\KsefSymfoniaExport\\app.log; na konsolę tylko gdy istnieje
    (w aplikacji spakowanej --windowed sys.stderr jest None)."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_dir = app_data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "app.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)


def zaladuj_tlumaczenia_qt(app: QApplication) -> bool:
    """Polskie napisy standardowych przycisków Qt („Pokaż szczegóły…”, „Tak”,
    „Anuluj”) zamiast angielskich. Brak pliku tłumaczeń (np. w spakowanym .exe)
    = angielskie napisy, bez błędu."""
    tlumacz = QTranslator(app)
    sciezka = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if not tlumacz.load("qtbase_pl", sciezka):
        return False
    app.installTranslator(tlumacz)
    app._tlumacz_qt = tlumacz  # referencja, żeby tłumacz nie został zwolniony
    return True


def main() -> int:
    _setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("KSeF → Symfonia FK")
    zaladuj_tlumaczenia_qt(app)
    store = ConfigStore()

    if not store.exists():
        dlg = MasterPasswordDialog(first_run=True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return 0
        cfg = store.initialize(dlg.password.text())
    else:
        cfg = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            dlg = MasterPasswordDialog(first_run=False)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return 0
            try:
                cfg = store.unlock(dlg.password.text())
                break
            except WrongMasterPassword:
                if attempt == MAX_ATTEMPTS:
                    QMessageBox.critical(
                        None, "Hasło", "Trzykrotnie błędne hasło. Zamykam aplikację."
                    )
                    return 1
                QMessageBox.warning(
                    None, "Hasło", f"Błędne hasło (próba {attempt}/{MAX_ATTEMPTS})."
                )
            except ConfigCorrupted as exc:
                QMessageBox.critical(None, "Konfiguracja", str(exc))
                return 1
        if cfg is None:
            return 1

    window = MainWindow(store, cfg)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
