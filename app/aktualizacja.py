"""Aktualizacja aplikacji przy starcie (skrót na pulpicie -> uruchom.pyw).

Model: na komputerze księgowej działa WYŁĄCZNIE wersja zaakceptowana przez
właściciela, czyli to, co właściciel wypchnął na gałąź main w GitHubie.
Komputer księgowej ma do repo dostęp tylko do odczytu (klucz wdrożeniowy,
patrz AKTUALIZACJE.md), więc sam niczego tam nie zapisze.

Przy każdym starcie:
1. `git fetch` z GitHuba (brak sieci -> start tego, co jest na dysku);
2. lokalne zmiany śledzonych plików i lokalne commity (np. od asystenta AI)
   są ODKŁADANE, nie kasowane: zmiany do schowka gita (`git stash`), commity
   na gałąź `kopia/<data>`; pliki nieśledzone (np. zapisane przez księgową
   eksporty CSV) zostają na dysku bez zmian;
3. katalog ustawiany dokładnie na origin/main;
4. gdy zmieniły się biblioteki (requirements.txt) — instalacja; jeśli się nie
   uda, powrót do poprzedniej wersji, żeby aplikacja w ogóle wystartowała.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

GALAZ = "main"
_TOZSAMOSC = ["-c", "user.name=KSeF aktualizator", "-c", "user.email=aktualizator@localhost"]
# bez migającego okna konsoli, gdy aplikacja startuje przez pythonw.exe
_BEZ_KONSOLI = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class WynikAktualizacji:
    stan: str  # aktualna | zaktualizowano | przywrocono | wycofano | offline | brak-repo | blad
    z: str = ""
    na: str = ""
    odlozone_zmiany: bool = False
    komunikat: str = ""


class _BladGita(Exception):
    pass


# git uruchamiany bez użytkownika przy klawiaturze: żadnych pytań o hasło ani
# okien logowania menedżera poświadczeń na pulpicie księgowej
_ENV_BEZ_PYTAN = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}

# GitHub odpowiedział, ale odmówił (klucz usunięty, zły known_hosts, brak repo) —
# to nie brak internetu: aktualizacje stoją, a tytuł okna ma to pokazać
_ODMOWA_DOSTEPU = re.compile(
    r"permission denied|host key verification failed|repository not found"
    r"|no such identity|bad permissions|not accessible",
    re.IGNORECASE,
)

# index.lock starszy niż tyle sekund to pozostałość po zabitym gicie (np. timeout);
# świeższy może należeć do działającego procesu — wtedy go nie ruszamy
_STARY_LOCK_S = 5 * 60


def _git(repo: Path, *args: str, timeout: float = 60) -> str:
    try:
        # git pisze ścieżki w UTF-8; text=True dekodowałoby je stroną kodową
        # Windows (cp1250) — ścieżka z „Ł/Ń/Ę” wysypywała aktualizację i start
        wynik = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=_BEZ_KONSOLI,
            env={**os.environ, **_ENV_BEZ_PYTAN},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _BladGita(f"git {' '.join(args)}: {exc}") from exc
    if wynik.returncode != 0:
        raise _BladGita(f"git {' '.join(args)}: {wynik.stderr.strip()}")
    return wynik.stdout.strip()


def _stan_repo(repo: Path) -> tuple[str, str]:
    """("ok", "") gdy katalog SAM jest korzeniem repo (nigdy repo nadrzędne);
    ("brak", ...) gdy nie ma w nim .git; ("blad", opis) gdy .git jest, ale git
    go nie odczyta (np. „dubious ownership”, brak gita w PATH) — to nie może
    być ciche „brak repo”, bo aktualizacje przestałyby działać bez śladu."""
    if not (Path(repo) / ".git").exists():
        return "brak", f"{repo} nie jest repozytorium git"
    try:
        korzen = _git(repo, "rev-parse", "--show-toplevel", timeout=15)
    except _BladGita as exc:
        return "blad", f"git nie może odczytać repozytorium {repo}: {exc}"
    if Path(korzen).resolve() != Path(repo).resolve():
        return "brak", f"{repo} nie jest korzeniem repozytorium ({korzen})"
    return "ok", ""


def _jest_korzeniem_repo(repo: Path) -> bool:
    return _stan_repo(repo)[0] == "ok"


def _usun_stary_index_lock(repo: Path) -> None:
    lock = Path(repo) / ".git" / "index.lock"
    try:
        wiek = time.time() - lock.stat().st_mtime
    except OSError:
        return
    if wiek > _STARY_LOCK_S:
        lock.unlink(missing_ok=True)
        logger.warning("Usunięto pozostałość po przerwanym gicie: %s (%.0f min)", lock, wiek / 60)


def _przerwij_niedokonczone_operacje(repo: Path) -> None:
    """Niedokończony merge/rebase/cherry-pick (np. po eksperymentach asystenta
    AI) blokowałby odkładanie zmian przy każdym starcie."""
    git_dir = Path(repo) / ".git"
    for znacznik, polecenie in (
        ("MERGE_HEAD", ("merge", "--abort")),
        ("CHERRY_PICK_HEAD", ("cherry-pick", "--abort")),
        ("REVERT_HEAD", ("revert", "--abort")),
        ("rebase-merge", ("rebase", "--abort")),
        ("rebase-apply", ("rebase", "--abort")),
    ):
        if (git_dir / znacznik).exists():
            logger.warning("Przerywam niedokończoną operację gita: git %s", " ".join(polecenie))
            _git(repo, *polecenie)


def _instaluj_pipem(repo: Path) -> bool:
    try:
        wynik = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
            cwd=repo, capture_output=True, encoding="utf-8", errors="replace", timeout=900,
            creationflags=_BEZ_KONSOLI,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("Instalacja bibliotek nie powiodła się: %s", exc)
        return False
    if wynik.returncode != 0:
        logger.error("Instalacja bibliotek nie powiodła się: %s", wynik.stderr[-2000:])
        return False
    return True


_ZNACZNIK_BIBLIOTEK = "ksef_requirements.sha256"


def _skrot_requirements(repo: Path) -> str | None:
    import hashlib
    try:
        return hashlib.sha256((Path(repo) / "requirements.txt").read_bytes()).hexdigest()
    except OSError:
        return None


def _zapamietaj_biblioteki(repo: Path) -> None:
    skrot = _skrot_requirements(repo)
    if not skrot:
        return
    try:
        (Path(repo) / ".git" / _ZNACZNIK_BIBLIOTEK).write_text(skrot, encoding="ascii")
    except OSError as exc:  # np. .git jest plikiem (git worktree)
        logger.warning("Nie zapisano znacznika bibliotek: %s", exc)


def _doinstaluj_jesli_trzeba(repo: Path, instaluj: Callable[[Path], bool]) -> bool | None:
    """Znacznik w .git zapamiętuje requirements.txt, dla którego zainstalowano
    biblioteki. Różnica (np. po awaryjnej aktualizacji, która nie uruchamia pip)
    -> instalacja. Brak znacznika (pierwszy start) -> zapamiętanie stanu bez
    instalacji. Zwraca None (nic do zrobienia) albo wynik instalacji."""
    if not (Path(repo) / ".git").is_dir():
        return None  # np. kopia `git worktree` (.git jest plikiem) — bez znacznika
    znacznik = Path(repo) / ".git" / _ZNACZNIK_BIBLIOTEK
    skrot = _skrot_requirements(repo)
    if skrot is None:
        return None
    try:
        zapisany = znacznik.read_text(encoding="ascii").strip()
    except OSError:
        _zapamietaj_biblioteki(repo)
        return None
    if zapisany == skrot:
        return None
    logger.info("Biblioteki nie odpowiadają requirements.txt — instaluję")
    ok = instaluj(repo)
    if ok:
        _zapamietaj_biblioteki(repo)
    return ok


def sprawdz_i_aktualizuj(
    repo_dir: Path,
    *,
    instaluj_zaleznosci: Callable[[Path], bool] | None = None,
    galaz: str = GALAZ,
) -> WynikAktualizacji:
    repo = Path(repo_dir)
    instaluj = instaluj_zaleznosci or _instaluj_pipem
    stan, opis = _stan_repo(repo)
    if stan == "brak":
        logger.info("%s — bez aktualizacji", opis)
        return WynikAktualizacji("brak-repo", komunikat=opis)
    if stan == "blad":
        logger.warning("Aktualizacja niemożliwa: %s", opis)
        return WynikAktualizacji("blad", komunikat=opis)
    try:
        _git(repo, "fetch", "--quiet", "origin", galaz, timeout=30)
    except _BladGita as exc:
        if _ODMOWA_DOSTEPU.search(str(exc)):
            logger.warning("GitHub odmówił dostępu — start bez aktualizacji (%s)", exc)
            return WynikAktualizacji("blad", komunikat=f"GitHub odmówił dostępu: {exc}")
        logger.warning("Brak połączenia z GitHubem — start bez aktualizacji (%s)", exc)
        return WynikAktualizacji("offline", komunikat=str(exc))

    try:
        _usun_stary_index_lock(repo)
        _przerwij_niedokonczone_operacje(repo)
        zdalna = f"origin/{galaz}"
        lokalny = _git(repo, "rev-parse", "HEAD")
        docelowy = _git(repo, "rev-parse", zdalna)
        zmiany_plikow = bool(_git(repo, "status", "--porcelain", "--untracked-files=no"))
        lokalne_commity = bool(_git(repo, "rev-list", f"{zdalna}..HEAD"))
        if lokalny == docelowy and not zmiany_plikow:
            if _doinstaluj_jesli_trzeba(repo, instaluj) is False:
                return WynikAktualizacji(
                    "blad", z=lokalny, na=docelowy,
                    komunikat="instalacja bibliotek nie powiodła się",
                )
            return WynikAktualizacji("aktualna", z=lokalny, na=docelowy)

        znacznik = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        odlozone = False
        if zmiany_plikow:
            # bez --include-untracked: pliki nieśledzone (dane księgowej) zostają
            _git(repo, *_TOZSAMOSC, "stash", "push",
                 "-m", f"kopia lokalnych zmian przed aktualizacją {znacznik}")
            logger.warning("Lokalne zmiany plików odłożone do schowka gita (git stash list)")
            odlozone = True
        if lokalne_commity:
            nazwa = "kopia/" + datetime.now().strftime("%Y%m%d-%H%M%S")
            _git(repo, "branch", nazwa, lokalny)
            logger.warning("Lokalne commity zachowane na gałęzi %s", nazwa)
            odlozone = True

        biblioteki = _git(repo, "diff", "--name-only", lokalny, docelowy, "--", "requirements.txt")
        _git(repo, "checkout", "-q", "-B", galaz, zdalna)
        _git(repo, "reset", "-q", "--hard", zdalna)

        if lokalny == docelowy:
            logger.info("Przywrócono wersję zaakceptowaną %s", docelowy[:7])
            _doinstaluj_jesli_trzeba(repo, instaluj)
            return WynikAktualizacji("przywrocono", z=lokalny, na=docelowy, odlozone_zmiany=odlozone)

        if biblioteki and not instaluj(repo):
            # powrót do ostatniej wersji ZAAKCEPTOWANEJ (wspólny przodek z main),
            # nigdy do lokalnego commitu asystenta (ten jest na gałęzi kopia/*)
            poprzednia = _git(repo, "merge-base", lokalny, docelowy)
            _git(repo, "reset", "-q", "--hard", poprzednia)
            logger.error(
                "Nowa wersja %s wymaga bibliotek, których nie udało się zainstalować — "
                "powrót do %s", docelowy[:7], poprzednia[:7],
            )
            return WynikAktualizacji(
                "wycofano", z=lokalny, na=docelowy, odlozone_zmiany=odlozone,
                komunikat="instalacja bibliotek nie powiodła się",
            )
        if biblioteki:
            _zapamietaj_biblioteki(repo)
        else:
            _doinstaluj_jesli_trzeba(repo, instaluj)
        logger.info("Zaktualizowano %s -> %s", lokalny[:7], docelowy[:7])
        return WynikAktualizacji("zaktualizowano", z=lokalny, na=docelowy, odlozone_zmiany=odlozone)
    except _BladGita as exc:
        logger.error("Aktualizacja przerwana: %s — start bez zmian", exc)
        return WynikAktualizacji("blad", komunikat=str(exc))


def opis_wersji(repo_dir: Path) -> str:
    """„9419166 z 28.09.2026” dla okna aplikacji; pusty napis, gdy katalog nie
    jest repozytorium (np. spakowane .exe)."""
    # wołane z okna głównego: żaden błąd nie może zatrzymać startu aplikacji
    try:
        repo = Path(repo_dir)
        if not _jest_korzeniem_repo(repo):
            return ""
        skrot = _git(repo, "rev-parse", "--short=7", "HEAD", timeout=15)
        data = _git(repo, "log", "-1", "--format=%cd", "--date=format:%d.%m.%Y", timeout=15)
    except Exception:  # noqa: BLE001
        return ""
    return f"{skrot} z {data}"


def katalog_danych() -> Path:
    """%APPDATA%\\KsefSymfoniaExport — jak app.config.app_data_dir, ale bez
    importu app.config (ten wczytuje cryptography; na Windows wczytana .pyd
    jest zablokowana i pip nie mógłby jej podmienić)."""
    return Path(os.environ.get("APPDATA") or str(Path.home())) / "KsefSymfoniaExport"


class BlokadaJednejInstancji:
    """Blokada pliku: drugie kliknięcie skrótu w trakcie pracy (albo w trakcie
    aktualizacji) nie uruchamia drugiej kopii aplikacji na tych samych plikach."""

    def __init__(self, sciezka: Path | None = None):
        self.sciezka = Path(sciezka) if sciezka else katalog_danych() / "uruchom.lock"
        self._plik = None

    def zajmij(self) -> bool:
        self.sciezka.parent.mkdir(parents=True, exist_ok=True)
        plik = open(self.sciezka, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                plik.seek(0)
                msvcrt.locking(plik.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(plik.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            plik.close()
            return False
        self._plik = plik
        return True

    def zwolnij(self) -> None:
        if self._plik is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._plik.seek(0)
                msvcrt.locking(self._plik.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._plik.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._plik.close()
        self._plik = None


def skonfiguruj_log(katalog: Path | None = None) -> None:
    """Log aktualizacji: %APPDATA%\\KsefSymfoniaExport\\aktualizacje.log."""
    if katalog is None:
        katalog = katalog_danych()
    katalog.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(katalog / "aktualizacje.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
