"""Start aplikacji ze skrótu na pulpicie — najpierw aktualizacja, potem okno.

Skrót na pulpicie księgowej:  .venv\\Scripts\\pythonw.exe uruchom.pyw
(katalog roboczy = katalog repozytorium). Szczegóły i jednorazowa
konfiguracja: AKTUALIZACJE.md.

Aktualizacja pobiera wyłącznie wersje zaakceptowane przez właściciela
(wypchnięte na main w GitHubie). Każdy jej błąd kończy się zwykłym startem
aplikacji — nigdy nie blokuje pracy; stan widać w tytule okna. Błąd samego
startu kończy się komunikatem (pythonw nie ma konsoli — bez tego aplikacja
znikałaby bez śladu) i wpisem w aktualizacje.log.

Przed aktualizacją NIE wolno importować bibliotek aplikacji (PySide6,
cryptography…): na Windows wczytana biblioteka jest zablokowana i pip nie
mógłby jej podmienić.
"""

import logging
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# stan ostatniej aktualizacji dla okna aplikacji (tytuł) — także w procesie potomnym
ZMIENNA_STANU = "KSEF_STAN_AKTUALIZACJI"
_BEZ_KONSOLI = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _zaladuj_aktualizator():
    from app import aktualizacja
    return aktualizacja


def _start_aplikacji() -> int:
    from app.main import main as start
    return start()


def _komunikat(tekst: str) -> None:
    """Okno z komunikatem bez Qt (patrz docstring modułu)."""
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, tekst, "KSeF → Symfonia", 0x40)
    else:
        print(tekst, file=sys.stderr)


def _log_bledu(tekst: str) -> None:
    """Dopisuje do aktualizacje.log bez importu modułów aplikacji."""
    try:
        katalog = Path(os.environ.get("APPDATA") or str(Path.home())) / "KsefSymfoniaExport"
        katalog.mkdir(parents=True, exist_ok=True)
        with open(katalog / "aktualizacje.log", "a", encoding="utf-8") as plik:
            plik.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} ERROR {tekst}\n")
    except OSError:
        pass


def _git_awaryjny(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, encoding="utf-8", errors="replace",
        timeout=60, env=env, creationflags=_BEZ_KONSOLI,
    )


def _awaryjna_aktualizacja() -> str:
    """Gdy aktualizator sam się nie wczytuje (zły kod na main), jedyną drogą
    naprawy jest pobranie main zwykłym gitem — inaczej aktualizacje stanęłyby
    na zawsze. Zwraca "zmieniono" | "bez_zmian" | "blad".

    Tylko gdy katalog aplikacji SAM jest repozytorium (nigdy repo nadrzędne).
    Lokalne zmiany plików -> schowek gita, lokalne commity -> gałąź kopia/<data>;
    nieudane odłożenie zmian przerywa całość (nic nie ginie). Biblioteki
    doinstaluje zwykły aktualizator przy następnym starcie (znacznik
    requirements.txt w .git)."""
    try:
        if not (REPO / ".git").exists():
            _log_bledu(f"awaryjna aktualizacja: {REPO} nie jest repozytorium")
            return "blad"
        korzen = _git_awaryjny("rev-parse", "--show-toplevel")
        if korzen.returncode != 0 or Path(korzen.stdout.strip()).resolve() != REPO.resolve():
            _log_bledu(f"awaryjna aktualizacja: {REPO} nie jest korzeniem repozytorium")
            return "blad"
        tozsamosc = ["-c", "user.name=KSeF aktualizator", "-c", "user.email=aktualizator@localhost"]
        przed = _git_awaryjny("rev-parse", "HEAD").stdout.strip()
        for krok in (("fetch", "--quiet", "origin", "main"),):
            wynik = _git_awaryjny(*krok)
            if wynik.returncode != 0:
                _log_bledu(f"awaryjna aktualizacja: git {' '.join(krok)}: {wynik.stderr.strip()}")
                return "blad"
        lokalne = _git_awaryjny("rev-list", "origin/main..HEAD").stdout.strip()
        if lokalne:
            nazwa = "kopia/" + datetime.now().strftime("%Y%m%d-%H%M%S")
            if _git_awaryjny("branch", nazwa, "HEAD").returncode != 0:
                _log_bledu("awaryjna aktualizacja: nie udało się zachować lokalnych commitów")
                return "blad"
        for krok in (
            (*tozsamosc, "stash", "push", "-m", "kopia przed awaryjną aktualizacją"),
            ("checkout", "-q", "-B", "main", "origin/main"),
            ("reset", "-q", "--hard", "origin/main"),
        ):
            wynik = _git_awaryjny(*krok)
            if wynik.returncode != 0:
                _log_bledu(f"awaryjna aktualizacja: git {' '.join(krok)}: {wynik.stderr.strip()}")
                return "blad"
        po = _git_awaryjny("rev-parse", "HEAD").stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log_bledu(f"awaryjna aktualizacja: {exc}")
        return "blad"
    return "zmieniono" if po != przed else "bez_zmian"


def main(argv: list[str]) -> int:
    try:
        aktualizacja = _zaladuj_aktualizator()
    except Exception:  # noqa: BLE001 — zły kod aktualizatora na main
        _log_bledu("Nie udało się wczytać aktualizatora:\n" + traceback.format_exc())
        stan = _awaryjna_aktualizacja()
        _komunikat(
            "Nie udało się uruchomić aplikacji (błąd aktualizatora). "
            + ("Pobrano poprawioną wersję — uruchom aplikację ponownie."
               if stan == "zmieniono" else
               "Poprawki nie ma jeszcze na serwerze (albo nie udało się jej pobrać) — "
               "zgłoś błąd właścicielowi aplikacji (szczegóły: aktualizacje.log).")
        )
        return 1

    blokada = aktualizacja.BlokadaJednejInstancji()
    try:
        if not blokada.zajmij():
            _komunikat("Aplikacja jest już uruchomiona (albo właśnie się aktualizuje). "
                       "Sprawdź pasek zadań.")
            return 0
        if "--bez-aktualizacji" not in argv:
            try:
                aktualizacja.skonfiguruj_log()
                wynik = aktualizacja.sprawdz_i_aktualizuj(REPO)
                os.environ[ZMIENNA_STANU] = wynik.stan
            except Exception:  # noqa: BLE001 — aktualizacja nie może zablokować startu
                logging.getLogger("app.aktualizacja").exception("Błąd aktualizatora")
                os.environ[ZMIENNA_STANU] = "blad"
            else:
                if wynik.stan in ("zaktualizowano", "przywrocono"):
                    # nowy kod na dysku: start w świeżym procesie, żeby nie mieszać
                    # modułów wczytanych przed aktualizacją z nowymi; blokadę
                    # zwalniamy wcześniej, bo przejmie ją nowy proces
                    blokada.zwolnij()
                    subprocess.Popen(
                        [sys.executable, str(Path(__file__).resolve()), "--bez-aktualizacji"],
                        cwd=REPO,
                    )
                    return 0
        return _start_aplikacji()
    except Exception as exc:  # noqa: BLE001 — pythonw: bez tego cisza
        _log_bledu("Nie udało się uruchomić aplikacji:\n" + traceback.format_exc())
        _komunikat(
            f"Nie udało się uruchomić aplikacji: {exc}\n\n"
            "Szczegóły: %APPDATA%\\KsefSymfoniaExport\\aktualizacje.log"
        )
        return 1
    finally:
        blokada.zwolnij()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
