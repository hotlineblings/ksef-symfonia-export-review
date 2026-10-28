"""Odczyt danych z bazy SQL Server Symfonii — WYŁĄCZNIE zapytania SELECT.

AUTORYZACJA UŻYTKOWNIKA (udzielona wprost 2026-08-24, zawężona 2026-08-24):
odczyt kartoteki kontrahentów (tabela FK.fk_kontrahenci: numer karty, kod,
nazwa, NIP, miejscowość) w instancji ".\\SAGEPL", bazy firm BETALABO /
ALFATRON / ALFATRON_SERWIS / GAMMATEST. Żadnych zapisów, zmian ani
odczytu dokumentów księgowych. Ten moduł woła sqlcmd WYŁĄCZNIE z zapytaniami
SELECT na sys.databases (lista_baz) i FK.fk_kontrahenci (czytaj_kartoteke) —
patrz MEMORY.md po pełną treść autoryzacji. Żelazne zasady: wyłącznie SELECT
(nigdy INSERT/UPDATE/DELETE/DDL/EXEC), wyłącznie te dwie tabele, timeout na
każde zapytanie.

UWAGA — pułapka id vs pozycja: FK.fk_kontrahenci ma kolumnę `id` (klucz
wewnętrzny) ORAZ osobną kolumnę `pozycja` (numer karty widoczny w Symfonii,
używany w analitykach kont). Te dwie wartości się rozjeżdżają (np. kolejność
skasowań/przenumerowań kart) — `id` NIE jest numerem karty. czytaj_kartoteke
czyta i sortuje wyłącznie po `pozycja`; patrz docstring tej funkcji po dowód
empiryczny.

Kodowanie: sqlcmd wywoływany z flagą -u (unicode output) i wynikiem
przekierowanym do pliku tymczasowego (-o) zamiast na stdout — samo
przechwycenie stdout procesu w Pythonie na Windows przekłamuje polskie
znaki (kodowanie konsoli), plik binarny czytany bezpośrednio nie. Plik
dekodowany jako UTF-16 (tak zapisuje sqlcmd z -u), z fallbackiem na
utf-8-sig/cp1250 na wypadek innej konfiguracji środowiska.

Wykrywanie błędów: sqlcmd domyślnie kończy się kodem 0 nawet gdy zapytanie
się nie powiodło (błąd T-SQL trafia do tego samego pliku wyjściowego) — flaga
-b (batch abort) wymusza kod wyjścia 1 przy błędzie, dzięki czemu returncode
jest wiarygodny.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

INSTANCJA = r".\SAGEPL"

# kandydaci na lokalizację sqlcmd — najpierw PATH, potem typowa instalacja
# Microsoft Command Line Utilities for SQL Server (patrz MEMORY.md)
_SQLCMD_KANDYDACI = (
    "sqlcmd",
    r"C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\170\Tools\Binn\sqlcmd.exe",
    r"C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\180\Tools\Binn\sqlcmd.exe",
)

_TIMEOUT_S = 30
_SEPARATOR = "\x1f"  # unit separator — praktycznie niespotykany w danych kartoteki

# bazy systemowe pomijane w lista_baz()
_BAZY_SYSTEMOWE = {"master", "model", "msdb", "tempdb"}


class SymfoniaSqlError(Exception):
    """Błąd komunikacji z bazą Symfonii (brak sqlcmd, timeout, błąd zapytania)."""


def _znajdz_sqlcmd() -> str:
    for kandydat in _SQLCMD_KANDYDACI:
        if Path(kandydat).is_absolute():
            if Path(kandydat).exists():
                return kandydat
            continue
        znaleziony = shutil.which(kandydat)
        if znaleziony:
            return znaleziony
    raise SymfoniaSqlError(
        "Nie znaleziono narzędzia sqlcmd — zainstaluj Microsoft Command Line "
        "Utilities for SQL Server albo dodaj sqlcmd do PATH."
    )


def _odczytaj_wynik(sciezka: Path) -> str:
    surowe = sciezka.read_bytes()
    try:
        return surowe.decode("utf-16")
    except UnicodeDecodeError:
        try:
            return surowe.decode("utf-8-sig")
        except UnicodeDecodeError:
            return surowe.decode("cp1250", errors="replace")


def _wykonaj(query: str, baza: str | None = None) -> list[list[str]]:
    """Uruchamia zapytanie SELECT przez sqlcmd na instancji INSTANCJA i zwraca
    wiersze wyniku jako listy kolumn (string, bez obcinania — wywołujący sam
    robi .strip()). WYŁĄCZNIE do użytku wewnętrznego tego modułu — każde
    query przekazane tutaj musi być SELECT-em na dozwolonych tabelach."""
    sqlcmd = _znajdz_sqlcmd()
    fd, tmp_name = tempfile.mkstemp(suffix=".txt", prefix="symfonia_sql_")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        args = [
            sqlcmd, "-S", INSTANCJA, "-E", "-u", "-h", "-1", "-W",
            "-s", _SEPARATOR, "-b", "-t", str(_TIMEOUT_S), "-Q", query, "-o", str(tmp_path),
        ]
        if baza:
            args += ["-d", baza]
        try:
            wynik = subprocess.run(args, capture_output=True, timeout=_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise SymfoniaSqlError(
                f"Zapytanie do bazy Symfonii przekroczyło limit czasu ({_TIMEOUT_S} s)."
            ) from exc
        except FileNotFoundError as exc:
            raise SymfoniaSqlError("Nie znaleziono narzędzia sqlcmd.") from exc

        tresc = _odczytaj_wynik(tmp_path)

        if wynik.returncode != 0:
            komunikat = tresc.strip() or wynik.stderr.decode("utf-8", errors="replace").strip()
            raise SymfoniaSqlError(f"Błąd zapytania do bazy Symfonii: {komunikat}")

        return [linia.split(_SEPARATOR) for linia in tresc.splitlines() if linia.strip()]
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def lista_baz() -> list[str]:
    """Nazwy baz danych na instancji Symfonii, bez baz systemowych
    (master/model/msdb/tempdb)."""
    wiersze = _wykonaj("SET NOCOUNT ON; SELECT name FROM sys.databases ORDER BY name")
    nazwy = (w[0].strip() for w in wiersze if w and w[0].strip())
    return [n for n in nazwy if n not in _BAZY_SYSTEMOWE]


def czytaj_kartoteke(baza: str) -> list[dict]:
    """Kartoteka kontrahentów (FK.fk_kontrahenci) danej bazy firmowej —
    słowniki {pozycja: int, kod, nazwa, nip, miejscowosc: str}, posortowane
    wg pozycji. Puste/NULL pola tekstowe wracają jako ''.

    PUŁAPKA (błąd produkcyjny odkryty empirycznie): FK.fk_kontrahenci ma DWIE
    różne kolumny numeryczne — `id` (klucz wewnętrzny, techniczny) i `pozycja`
    (numer karty widoczny w Symfonii jako pole "Pozycja", używany w
    analitykach kont). Te dwie kolumny się rozjeżdżają — `id` NIE jest
    numerem karty! Dowód: kontrahent BEDNAREK MONIKA ma id=498, ale
    pozycja=497 — a to właśnie 497 widnieje na jego karcie w Symfonii.
    W różnych bazach rozjazd obejmuje różną liczbę wierszy (np. w ALFATRON
    kilka, w BETALABO prawie wszystkie). Dlatego zapytanie MUSI czytać i
    sortować po `pozycja`, nigdy po `id`."""
    wiersze = _wykonaj(
        "SET NOCOUNT ON; SELECT pozycja, skrot, nazwa, nip, Miejscowosc "
        "FROM FK.fk_kontrahenci ORDER BY pozycja",
        baza=baza,
    )
    wynik: list[dict] = []
    for w in wiersze:
        kolumny = (list(w) + ["", "", "", "", ""])[:5]
        pozycja_raw, kod, nazwa, nip, miejscowosc = kolumny
        try:
            pozycja = int(pozycja_raw.strip())
        except ValueError:
            continue  # wiersz bez poprawnej pozycji — nie powinien wystąpić, ale nie wywala odczytu
        wynik.append(
            {
                "pozycja": pozycja,
                "kod": kod.strip(),
                "nazwa": nazwa.strip(),
                "nip": nip.strip(),
                "miejscowosc": miejscowosc.strip(),
            }
        )
    return wynik
