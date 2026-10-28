"""Uruchamia testy PowerShell skryptu narzedzia/konfiguruj_komputer.ps1.

Potrzebny pwsh (PowerShell 7) albo Windows PowerShell; ścieżkę można podać w
KSEF_PWSH. Bez nich test jest pomijany.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

TEST_PS = Path(__file__).parent / "ps" / "test_konfiguruj_komputer.ps1"


def _powershell() -> str | None:
    return os.environ.get("KSEF_PWSH") or shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(_powershell() is None, reason="brak PowerShell (pwsh / KSEF_PWSH)")
def test_konfiguruj_komputer_ps1():
    wynik = subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(TEST_PS)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert wynik.returncode == 0, wynik.stdout + wynik.stderr
