"""Testy odczytu danych z bazy SQL Symfonii (sqlcmd, wyłącznie SELECT).

Testy jednostkowe (poniżej, bez markera) monkeypatchują subprocess.run —
symulują pliki wyjściowe sqlcmd (UTF-16, kolumny rozdzielone _SEPARATOR),
więc działają bez żywej bazy. Testy oznaczone markerem "sql" na końcu pliku
łączą się z prawdziwą instancją .\\SAGEPL (autoryzacja — patrz MEMORY.md i
nagłówek app/symfonia_sql.py) i są domyślnie pomijane (patrz pyproject.toml).
"""

import subprocess
from pathlib import Path

import pytest

from app import symfonia_sql
from app.symfonia_sql import (
    SymfoniaSqlError,
    czytaj_kartoteke,
    lista_baz,
)


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stderr: bytes = b""):
        self.returncode = returncode
        self.stderr = stderr


def _zapisz_wynik(sciezka: str, wiersze: list[list[str]]) -> None:
    """Symuluje plik wyjściowy sqlcmd -u -o: UTF-16, wiersze rozdzielone \\r\\n,
    kolumny separatorem symfonia_sql._SEPARATOR — jak prawdziwy sqlcmd -s."""
    tresc = "".join(symfonia_sql._SEPARATOR.join(w) + "\r\n" for w in wiersze)
    Path(sciezka).write_bytes(tresc.encode("utf-16"))


def _zapisz_blad(sciezka: str, komunikat: str) -> None:
    Path(sciezka).write_bytes(komunikat.encode("utf-16"))


def _sciezka_wyjscia(args: list[str]) -> str:
    return args[args.index("-o") + 1]


@pytest.fixture
def fake_sqlcmd(monkeypatch):
    """sqlcmd 'znaleziony' pod fałszywą ścieżką — test steruje wynikiem przez
    osobny monkeypatch subprocess.run."""
    monkeypatch.setattr(symfonia_sql, "_znajdz_sqlcmd", lambda: "sqlcmd.exe")


# ---------------------------------------------------------------------- #
# jednostkowe — subprocess.run monkeypatchowany


def test_lista_baz_odfiltrowuje_systemowe(fake_sqlcmd, monkeypatch):
    def fake_run(args, **kwargs):
        _zapisz_wynik(
            _sciezka_wyjscia(args),
            [
                ["master"], ["tempdb"], ["model"], ["msdb"],
                ["BETALABO_SPÓŁKA_Z_OGRANICZONĄ_ODPOWIEDZIALNOŚCIĄ"],
                ["ALFATRON_SERWIS_MEDYCZNY"],
            ],
        )
        return FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(symfonia_sql.subprocess, "run", fake_run)

    bazy = lista_baz()

    assert bazy == [
        "BETALABO_SPÓŁKA_Z_OGRANICZONĄ_ODPOWIEDZIALNOŚCIĄ",
        "ALFATRON_SERWIS_MEDYCZNY",
    ]


def test_czytaj_kartoteke_polskie_znaki_i_puste_pola(fake_sqlcmd, monkeypatch):
    def fake_run(args, **kwargs):
        assert args[args.index("-d") + 1] == "ALFATRON_SERWIS_MEDYCZNY"
        # pierwsza kolumna to `pozycja` (numer karty), NIE `id` — patrz
        # dowód w app/symfonia_sql.py::czytaj_kartoteke
        _zapisz_wynik(
            _sciezka_wyjscia(args),
            [
                ["1", "SYM", "Symfonia sp. z o.o.", "5262831503", "Warszawa"],
                ["2", "GED", "TESTDENT Piekary Śląskie", "9100010178", "Piekary Śląskie"],
                # NULL w SQL -> pusty string w wyniku sqlcmd
                ["3", "ECO", "ECO-TESTCLEAN Karolina Kącprzak", "", ""],
            ],
        )
        return FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(symfonia_sql.subprocess, "run", fake_run)

    wiersze = czytaj_kartoteke("ALFATRON_SERWIS_MEDYCZNY")

    assert wiersze == [
        {
            "pozycja": 1, "kod": "SYM", "nazwa": "Symfonia sp. z o.o.",
            "nip": "5262831503", "miejscowosc": "Warszawa",
        },
        {
            "pozycja": 2, "kod": "GED", "nazwa": "TESTDENT Piekary Śląskie",
            "nip": "9100010178", "miejscowosc": "Piekary Śląskie",
        },
        {
            "pozycja": 3, "kod": "ECO", "nazwa": "ECO-TESTCLEAN Karolina Kącprzak",
            "nip": "", "miejscowosc": "",
        },
    ]
    # dowód, że polskie znaki (w tym ogonki) przechodzą bez przekłamań
    assert "Ś" in wiersze[1]["nazwa"] or "ą" in wiersze[1]["nazwa"]
    assert "ą" in wiersze[2]["nazwa"]


def test_czytaj_kartoteke_zapytanie_uzywa_kolumny_pozycja_nie_id(fake_sqlcmd, monkeypatch):
    """Regresja błędu produkcyjnego: `id` w FK.fk_kontrahenci to klucz
    wewnętrzny, a NIE numer karty — prawdziwy numer karty to kolumna
    `pozycja`. Zapytanie musi SELECTować i ORDER BY po `pozycja`, nigdy
    po samym `id` (patrz app/symfonia_sql.py::czytaj_kartoteke)."""
    zapytania: list[str] = []

    def fake_run(args, **kwargs):
        query = args[args.index("-Q") + 1]
        zapytania.append(query)
        _zapisz_wynik(_sciezka_wyjscia(args), [["1", "SYM", "Symfonia", "", ""]])
        return FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(symfonia_sql.subprocess, "run", fake_run)

    czytaj_kartoteke("BAZA")

    (query,) = zapytania
    assert "SELECT pozycja," in query
    assert "ORDER BY pozycja" in query
    assert "id" not in query.replace("pozycja", "")  # brak `id` jako osobnej kolumny/sortowania


def test_blad_zapytania_podnosi_symfoniasqlerror(fake_sqlcmd, monkeypatch):
    def fake_run(args, **kwargs):
        _zapisz_blad(
            _sciezka_wyjscia(args),
            "Msg 208, Level 16, State 1, Server X\r\nInvalid object name 'FK.brak'.\r\n",
        )
        return FakeCompletedProcess(returncode=1)

    monkeypatch.setattr(symfonia_sql.subprocess, "run", fake_run)

    with pytest.raises(SymfoniaSqlError, match="Invalid object name"):
        czytaj_kartoteke("BAZA")


def test_blad_logowania_podnosi_symfoniasqlerror(fake_sqlcmd, monkeypatch):
    def fake_run(args, **kwargs):
        _zapisz_blad(_sciezka_wyjscia(args), "Login failed for user 'x'.\r\n")
        return FakeCompletedProcess(returncode=1)

    monkeypatch.setattr(symfonia_sql.subprocess, "run", fake_run)

    with pytest.raises(SymfoniaSqlError, match="Login failed"):
        lista_baz()


def test_timeout_podnosi_symfoniasqlerror(fake_sqlcmd, monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(symfonia_sql.subprocess, "run", fake_run)

    with pytest.raises(SymfoniaSqlError, match="limit czasu"):
        lista_baz()


def test_brak_sqlcmd_podnosi_symfoniasqlerror(monkeypatch):
    def raise_brak():
        raise SymfoniaSqlError("Nie znaleziono narzędzia sqlcmd.")

    monkeypatch.setattr(symfonia_sql, "_znajdz_sqlcmd", raise_brak)

    with pytest.raises(SymfoniaSqlError, match="sqlcmd"):
        lista_baz()


# ---------------------------------------------------------------------- #
# integracyjne — żywa baza .\SAGEPL (autoryzacja: patrz MEMORY.md), tylko
# SELECT, domyślnie pomijane (marker "sql" w addopts pyproject.toml)


@pytest.mark.sql
def test_zywa_baza_lista_baz_zawiera_alfatron_serwis():
    assert "ALFATRON_SERWIS_MEDYCZNY" in lista_baz()


@pytest.mark.sql
def test_zywa_baza_czytaj_kartoteke_alfatron_serwis():
    wiersze = czytaj_kartoteke("ALFATRON_SERWIS_MEDYCZNY")

    assert len(wiersze) > 900

    pierwszy = next(w for w in wiersze if w["pozycja"] == 1)
    assert pierwszy["nazwa"].startswith("Symfonia")

    # dowód poprawnego kodowania polskich znaków z żywej bazy
    assert any("ó" in w["nazwa"] or "ł" in w["nazwa"] for w in wiersze)


@pytest.mark.sql
def test_zywa_baza_czytaj_kartoteke_alfatron_pozycja_a_nie_id():
    """Regresja błędu produkcyjnego id vs pozycja (patrz MEMORY.md i
    app/symfonia_sql.py::czytaj_kartoteke). W bazie
    ALFATRON_SPÓŁKA_Z_OGRANICZONĄ_ODPOWIEDZIALNOŚCIĄ kontrahent BEDNAREK ma
    w tabeli FK.fk_kontrahenci id=498, ale pozycja=497 — to właśnie 497
    widnieje na jego karcie w Symfonii. Jeśli ta asercja kiedyś zacznie
    widzieć pozycja==498, to znak, że czytaj_kartoteke znów czyta z
    kolumny `id` zamiast `pozycja`."""
    wiersze = czytaj_kartoteke("ALFATRON_SPÓŁKA_Z_OGRANICZONĄ_ODPOWIEDZIALNOŚCIĄ")

    bednarek = next(w for w in wiersze if w["nazwa"].startswith("BEDNAREK"))
    assert bednarek["pozycja"] == 497  # id w bazie to 498 — dowód regresyjny

    # ciągłość numeracji: najwyższa pozycja odpowiada liczbie kart
    assert max(w["pozycja"] for w in wiersze) == 502
    assert len(wiersze) == 502
