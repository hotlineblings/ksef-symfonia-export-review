"""Testy mechanizmu aktualizacji przy starcie (app/aktualizacja.py).

Scenariusz jak w życiu: repo na GitHubie (tu: lokalne repo „bare”), komputer
księgowej (klon, z którego startuje aplikacja) i komputer właściciela (klon,
z którego wypycha zaakceptowane zmiany). Prawdziwy git, katalogi tymczasowe.
"""

import subprocess
from pathlib import Path

import pytest

from app.aktualizacja import opis_wersji, sprawdz_i_aktualizuj


def _git(cwd: Path, *args: str) -> str:
    wynik = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
             "HOME": str(cwd)},
    )
    return wynik.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """(origin, biuro, wlasciciel) — biuro i właściciel to klony origin na main."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    wlasciciel = tmp_path / "wlasciciel"
    _git(tmp_path, "clone", "-q", str(origin), str(wlasciciel))
    (wlasciciel / "app.py").write_text("wersja = 1\n")
    (wlasciciel / "requirements.txt").write_text("httpx==0.28.1\n")
    _git(wlasciciel, "add", ".")
    _git(wlasciciel, "commit", "-q", "-m", "v1")
    _git(wlasciciel, "push", "-q", "origin", "HEAD:main")
    biuro = tmp_path / "biuro"
    _git(tmp_path, "clone", "-q", str(origin), str(biuro))
    return origin, biuro, wlasciciel


def _wypchnij(wlasciciel: Path, plik: str, tresc: str, opis: str) -> str:
    (wlasciciel / plik).write_text(tresc)
    _git(wlasciciel, "commit", "-q", "-am", opis)
    _git(wlasciciel, "push", "-q", "origin", "HEAD:main")
    return _git(wlasciciel, "rev-parse", "HEAD")


class _Pip:
    def __init__(self, ok=True):
        self.ok = ok
        self.wywolania = 0

    def __call__(self, repo_dir: Path) -> bool:
        self.wywolania += 1
        return self.ok


def test_aktualna_wersja_niczego_nie_zmienia(repo):
    _, biuro, _ = repo
    przed = _git(biuro, "rev-parse", "HEAD")
    pip = _Pip()
    wynik = sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=pip)
    assert wynik.stan == "aktualna"
    assert _git(biuro, "rev-parse", "HEAD") == przed
    assert pip.wywolania == 0


def test_nowa_wersja_z_githuba_jest_pobierana(repo):
    _, biuro, wlasciciel = repo
    nowy = _wypchnij(wlasciciel, "app.py", "wersja = 2\n", "v2")
    wynik = sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip())
    assert wynik.stan == "zaktualizowano"
    assert _git(biuro, "rev-parse", "HEAD") == nowy
    assert (biuro / "app.py").read_text() == "wersja = 2\n"


def test_lokalne_zmiany_nie_blokuja_aktualizacji_i_sa_odlozone(repo):
    """Zmiany wprowadzone na komputerze księgowej (np. przez asystenta AI) nie
    blokują aktualizacji i nie przepadają — lądują w schowku gita."""
    _, biuro, wlasciciel = repo
    (biuro / "app.py").write_text("wersja = 'lokalna poprawka'\n")
    (biuro / "nowy_plik.py").write_text("x = 1\n")
    nowy = _wypchnij(wlasciciel, "app.py", "wersja = 2\n", "v2")
    wynik = sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip())
    assert wynik.stan == "zaktualizowano"
    assert _git(biuro, "rev-parse", "HEAD") == nowy
    assert (biuro / "app.py").read_text() == "wersja = 2\n"
    # nieśledzone pliki zostają na miejscu (to mogą być dane księgowej)
    assert (biuro / "nowy_plik.py").exists()
    assert wynik.odlozone_zmiany
    schowek = _git(biuro, "stash", "list")
    assert "przed aktualizacją" in schowek
    assert "lokalna poprawka" in _git(biuro, "stash", "show", "-p", "stash@{0}")


def test_lokalne_commity_nie_blokuja_aktualizacji(repo):
    _, biuro, wlasciciel = repo
    (biuro / "app.py").write_text("wersja = 'commit u ksiegowej'\n")
    _git(biuro, "commit", "-q", "-am", "lokalny")
    lokalny = _git(biuro, "rev-parse", "HEAD")
    nowy = _wypchnij(wlasciciel, "app.py", "wersja = 2\n", "v2")
    wynik = sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip())
    assert _git(biuro, "rev-parse", "HEAD") == nowy
    # lokalny commit zachowany na gałęzi kopii
    galezie = [g.strip(" *") for g in _git(biuro, "branch", "--list", "kopia/*").splitlines()]
    assert len(galezie) == 1
    assert _git(biuro, "rev-parse", galezie[0]) == lokalny
    assert wynik.odlozone_zmiany


def test_zmiana_bibliotek_instaluje_zaleznosci(repo):
    _, biuro, wlasciciel = repo
    _wypchnij(wlasciciel, "requirements.txt", "httpx==0.28.1\nlxml==6.1.1\n", "deps")
    pip = _Pip()
    wynik = sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=pip)
    assert wynik.stan == "zaktualizowano"
    assert pip.wywolania == 1


def test_bez_zmiany_bibliotek_nie_instaluje(repo):
    _, biuro, wlasciciel = repo
    _wypchnij(wlasciciel, "app.py", "wersja = 2\n", "v2")
    pip = _Pip()
    sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=pip)
    assert pip.wywolania == 0


def test_nieudana_instalacja_bibliotek_wraca_do_poprzedniej_wersji(repo):
    _, biuro, wlasciciel = repo
    przed = _git(biuro, "rev-parse", "HEAD")
    _wypchnij(wlasciciel, "requirements.txt", "nieistniejacy-pakiet==9.9\n", "zle deps")
    wynik = sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip(ok=False))
    assert wynik.stan == "wycofano"
    assert _git(biuro, "rev-parse", "HEAD") == przed
    assert (biuro / "requirements.txt").read_text() == "httpx==0.28.1\n"


def test_brak_internetu_uruchamia_to_co_jest(repo, tmp_path):
    origin, biuro, _ = repo
    przed = _git(biuro, "rev-parse", "HEAD")
    _git(biuro, "remote", "set-url", "origin", str(tmp_path / "nie-ma-takiego-repo.git"))
    wynik = sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip())
    assert wynik.stan == "offline"
    assert _git(biuro, "rev-parse", "HEAD") == przed


def _falszywy_ssh(tmp_path: Path, komunikat: str) -> str:
    (tmp_path / "bin").mkdir(exist_ok=True)
    skrypt = tmp_path / "bin" / "ssh"   # nazwa „ssh” = wariant ssh gita (obsługa portu 443)
    skrypt.write_text(f"#!/bin/sh\necho '{komunikat}' >&2\nexit 255\n")
    skrypt.chmod(0o755)
    return str(skrypt)


def test_odrzucony_klucz_to_blad_a_nie_brak_internetu(repo, tmp_path):
    """Po przejściu na klucz SSH odrzucony dostęp (klucz usunięty z GitHuba, zły
    known_hosts) nie może udawać braku internetu — tytuł okna ma pokazać
    „aktualizacja nieudana”, inaczej aktualizacje staną niezauważone."""
    _, biuro, _ = repo
    _git(biuro, "remote", "set-url", "origin", "ssh://git@ssh.github.com:443/hotlineblings/x.git")
    _git(biuro, "config", "core.sshCommand",
         _falszywy_ssh(tmp_path, "git@ssh.github.com: Permission denied (publickey)."))
    wynik = sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip())
    assert wynik.stan == "blad"
    assert "Permission denied" in wynik.komunikat


def test_brak_polaczenia_przez_ssh_to_offline(repo, tmp_path):
    _, biuro, _ = repo
    _git(biuro, "remote", "set-url", "origin", "ssh://git@ssh.github.com:443/hotlineblings/x.git")
    _git(biuro, "config", "core.sshCommand",
         _falszywy_ssh(tmp_path, "ssh: connect to host ssh.github.com port 443: Connection timed out"))
    wynik = sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip())
    assert wynik.stan == "offline"


def test_katalog_bez_gita_to_brak_aktualizacji(tmp_path):
    wynik = sprawdz_i_aktualizuj(tmp_path, instaluj_zaleznosci=_Pip())
    assert wynik.stan == "brak-repo"


def test_opis_wersji_to_skrot_commita_i_data(repo):
    _, biuro, _ = repo
    skrot = _git(biuro, "rev-parse", "--short=7", "HEAD")
    opis = opis_wersji(biuro)
    assert opis.startswith(skrot)
    assert " z " in opis


def test_opis_wersji_bez_gita_jest_pusty(tmp_path):
    assert opis_wersji(tmp_path) == ""


def test_lokalne_zmiany_bez_nowej_wersji_przywracaja_wersje_zaakceptowana(repo):
    """Na komputerze księgowej ma działać tylko wersja zaakceptowana przez
    właściciela (wypchnięta na main). Zmiany zrobione lokalnie — np. przez
    asystenta AI — są odkładane do schowka także wtedy, gdy nowej wersji nie ma."""
    _, biuro, _ = repo
    przed = _git(biuro, "rev-parse", "HEAD")
    (biuro / "app.py").write_text("wersja = 'poprawka AI'\n")
    wynik = sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip())
    assert wynik.stan == "przywrocono"
    assert wynik.odlozone_zmiany
    assert _git(biuro, "rev-parse", "HEAD") == przed
    assert (biuro / "app.py").read_text() == "wersja = 1\n"
    assert "poprawka AI" in _git(biuro, "stash", "show", "-p", "stash@{0}")


def test_nie_dotyka_repo_nadrzednego(tmp_path, repo):
    """Katalog aplikacji bez własnego .git w środku innego repo — nie wolno
    aktualizować repo nadrzędnego."""
    _, biuro, _ = repo
    podkatalog = biuro / "podkatalog"
    podkatalog.mkdir()
    wynik = sprawdz_i_aktualizuj(podkatalog, instaluj_zaleznosci=_Pip())
    assert wynik.stan == "brak-repo"


def test_tytul_okna_pokazuje_wersje(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import app.gui as gui_mod
    from app.config import ConfigStore
    monkeypatch.setattr(gui_mod, "opis_wersji", lambda _repo: "9419166 z 28.09.2026")
    store = ConfigStore(tmp_path / "c.enc")
    win = gui_mod.MainWindow(store, store.initialize("x"))
    assert win.windowTitle() == "KSeF → Symfonia FK — eksport faktur (wersja 9419166 z 28.09.2026)"
    win.close()


def test_tytul_okna_bez_wersji_jak_dotad(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import app.gui as gui_mod
    from app.config import ConfigStore
    monkeypatch.setattr(gui_mod, "opis_wersji", lambda _repo: "")
    store = ConfigStore(tmp_path / "c.enc")
    win = gui_mod.MainWindow(store, store.initialize("x"))
    assert win.windowTitle() == "KSeF → Symfonia FK — eksport faktur"
    win.close()


def test_program_startowy_po_aktualizacji_uruchamia_nowy_proces(monkeypatch, tmp_path):
    """uruchom.pyw: po zmianie kodu na dysku aplikacja startuje w świeżym
    procesie (bez mieszania starych i nowych modułów); bez zmian — od razu."""
    monkeypatch.setenv("APPDATA", str(tmp_path))  # blokada instancji w tmp, nie w katalogu domowym
    import importlib.machinery
    import importlib.util
    import app.aktualizacja as akt
    # .pyw nie ma domyślnego loadera — ładujemy jako zwykły plik źródłowy
    loader = importlib.machinery.SourceFileLoader(
        "uruchom", str(Path(__file__).resolve().parent.parent / "uruchom.pyw")
    )
    uruchom = importlib.util.module_from_spec(importlib.util.spec_from_loader("uruchom", loader))
    loader.exec_module(uruchom)
    procesy, starty = [], []
    monkeypatch.setattr(uruchom.subprocess, "Popen", lambda args, **k: procesy.append(args))
    monkeypatch.setattr(uruchom, "_start_aplikacji", lambda: starty.append(1) or 0)
    monkeypatch.setattr(akt, "skonfiguruj_log", lambda *a, **k: None)

    monkeypatch.setattr(akt, "sprawdz_i_aktualizuj", lambda repo: akt.WynikAktualizacji("zaktualizowano"))
    assert uruchom.main([]) == 0
    assert procesy and procesy[0][-1] == "--bez-aktualizacji" and not starty

    procesy.clear()
    monkeypatch.setattr(akt, "sprawdz_i_aktualizuj", lambda repo: akt.WynikAktualizacji("aktualna"))
    uruchom.main([])
    assert not procesy and starty == [1]

    starty.clear()
    monkeypatch.setattr(akt, "sprawdz_i_aktualizuj", lambda repo: (_ for _ in ()).throw(RuntimeError("x")))
    uruchom.main([])
    assert starty == [1]  # awaria aktualizatora nigdy nie blokuje startu aplikacji



# --------------------------------------------------------------------------- #
# runda 2 — uwagi z przeglądu integracyjnego (28.09)

def test_pliki_danych_ksiegowej_zostaja_na_dysku(repo):
    """„Eksportuj CSV…” zapisuje domyślnie w katalogu aplikacji (CWD skrótu),
    folder eksportu też może leżeć w repo — aktualizacja nie może ich chować."""
    _, biuro, wlasciciel = repo
    (biuro / "kontrahenci.csv").write_text("1;Alfa\n")
    (biuro / "eksport").mkdir()
    (biuro / "eksport" / "hmffk_x.TXT").write_text("dane")
    _wypchnij(wlasciciel, "app.py", "wersja = 2\n", "v2")
    assert sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip()).stan == "zaktualizowano"
    assert (biuro / "kontrahenci.csv").read_text() == "1;Alfa\n"
    assert (biuro / "eksport" / "hmffk_x.TXT").read_text() == "dane"
    # także bez nowej wersji (przywrócenie wersji zaakceptowanej)
    (biuro / "app.py").write_text("zmiana AI\n")
    assert sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip()).stan == "przywrocono"
    assert (biuro / "kontrahenci.csv").exists()
    assert (biuro / "eksport" / "hmffk_x.TXT").exists()


def test_nieudana_instalacja_z_lokalnym_commitem_wraca_do_wersji_zaakceptowanej(repo):
    _, biuro, wlasciciel = repo
    zaakceptowana = _git(biuro, "rev-parse", "HEAD")
    (biuro / "app.py").write_text("wersja = 'NIEZAAKCEPTOWANA'\n")
    _git(biuro, "commit", "-q", "-am", "commit asystenta")
    _wypchnij(wlasciciel, "requirements.txt", "zly==9.9\n", "zle deps")
    wynik = sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip(ok=False))
    assert wynik.stan == "wycofano"
    assert _git(biuro, "rev-parse", "HEAD") == zaakceptowana
    assert (biuro / "app.py").read_text() == "wersja = 1\n"


def test_przerwany_merge_nie_blokuje_aktualizacji_na_zawsze(repo):
    _, biuro, wlasciciel = repo
    _git(biuro, "checkout", "-q", "-b", "x")
    (biuro / "app.py").write_text("wersja = 'x'\n")
    _git(biuro, "commit", "-q", "-am", "x")
    _git(biuro, "checkout", "-q", "main")
    (biuro / "app.py").write_text("wersja = 'm'\n")
    _git(biuro, "commit", "-q", "-am", "m")
    subprocess.run(["git", "merge", "x"], cwd=biuro, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(biuro)})
    nowy = _wypchnij(wlasciciel, "app.py", "wersja = 2\n", "v2")
    wynik = sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip())
    assert wynik.stan == "zaktualizowano"
    assert _git(biuro, "rev-parse", "HEAD") == nowy


def test_stary_index_lock_po_zabitym_gicie_jest_usuwany(repo):
    import os
    import time
    _, biuro, wlasciciel = repo
    lock = biuro / ".git" / "index.lock"
    lock.write_text("")
    stary = time.time() - 15 * 60
    os.utime(lock, (stary, stary))
    nowy = _wypchnij(wlasciciel, "app.py", "wersja = 2\n", "v2")
    wynik = sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip())
    assert wynik.stan == "zaktualizowano"
    assert _git(biuro, "rev-parse", "HEAD") == nowy


def test_swiezy_index_lock_nie_jest_ruszany(repo):
    _, biuro, wlasciciel = repo
    (biuro / ".git" / "index.lock").write_text("")
    _wypchnij(wlasciciel, "app.py", "wersja = 2\n", "v2")
    wynik = sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip())
    assert wynik.stan == "blad"
    assert (biuro / ".git" / "index.lock").exists()


def test_git_nie_otwiera_okien_logowania(monkeypatch, tmp_path):
    import app.aktualizacja as akt
    widziane = []

    def fake_run(args, **kw):
        widziane.append(kw.get("env") or {})
        raise OSError("stop")

    monkeypatch.setattr(akt.subprocess, "run", fake_run)
    (tmp_path / ".git").mkdir()
    akt.sprawdz_i_aktualizuj(tmp_path)
    assert widziane
    assert widziane[0].get("GIT_TERMINAL_PROMPT") == "0"
    assert widziane[0].get("GCM_INTERACTIVE") == "never"


def test_log_aktualizacji_nie_wczytuje_bibliotek_aplikacji(tmp_path):
    """Na Windows wczytana biblioteka (.pyd) jest zablokowana — aktualizator
    nie może jej załadować przed instalacją nowych wersji bibliotek."""
    import sys
    import os
    kod = (
        "import sys; sys.path.insert(0, %r); import app.aktualizacja as a; "
        "a.skonfiguruj_log(); "
        "print(any(m.split('.')[0] in ('cryptography', 'PySide6', 'httpx', 'lxml') for m in sys.modules))"
    ) % (str(Path(__file__).resolve().parent.parent),)
    wynik = subprocess.run([sys.executable, "-c", kod], capture_output=True, text=True, check=True,
                           env={**os.environ, "APPDATA": str(tmp_path)})
    assert wynik.stdout.strip() == "False"
    assert (tmp_path / "KsefSymfoniaExport" / "aktualizacje.log").exists()


def test_blokada_jednej_instancji(tmp_path):
    from app.aktualizacja import BlokadaJednejInstancji
    pierwsza = BlokadaJednejInstancji(tmp_path / "uruchom.lock")
    druga = BlokadaJednejInstancji(tmp_path / "uruchom.lock")
    assert pierwsza.zajmij()
    assert not druga.zajmij()
    pierwsza.zwolnij()
    assert druga.zajmij()
    druga.zwolnij()


def test_skrypt_konfiguracyjny_ma_bom_dla_windows_powershell():
    """PowerShell 5.1 czyta plik bez BOM jako cp1250 — polskie znaki psują
    składnię (np. „ń” staje się cudzysłowem)."""
    skrypt = Path(__file__).resolve().parent.parent / "narzedzia" / "konfiguruj_komputer.ps1"
    assert skrypt.read_bytes().startswith(b"\xef\xbb\xbf")


def test_tytul_okna_mowi_o_nieudanej_aktualizacji(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import app.gui as gui_mod
    from app.config import ConfigStore
    monkeypatch.setattr(gui_mod, "opis_wersji", lambda _repo: "9419166 z 28.09.2026")
    monkeypatch.setenv("KSEF_STAN_AKTUALIZACJI", "wycofano")
    store = ConfigStore(tmp_path / "c.enc")
    win = gui_mod.MainWindow(store, store.initialize("x"))
    assert "aktualizacja nieudana" in win.windowTitle()
    win.close()


def _zaladuj_uruchom():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader(
        "uruchom", str(Path(__file__).resolve().parent.parent / "uruchom.pyw")
    )
    modul = importlib.util.module_from_spec(importlib.util.spec_from_loader("uruchom", loader))
    loader.exec_module(modul)
    return modul


def test_druga_instancja_nie_startuje_i_mowi_dlaczego(monkeypatch):
    import app.aktualizacja as akt
    uruchom = _zaladuj_uruchom()
    komunikaty, starty = [], []
    monkeypatch.setattr(uruchom, "_komunikat", lambda tekst: komunikaty.append(tekst))
    monkeypatch.setattr(uruchom, "_start_aplikacji", lambda: starty.append(1) or 0)
    monkeypatch.setattr(akt, "skonfiguruj_log", lambda *a, **k: None)

    class _Zajeta:
        def __init__(self, *a):
            pass

        def zajmij(self):
            return False

        def zwolnij(self):
            pass

    monkeypatch.setattr(akt, "BlokadaJednejInstancji", _Zajeta)
    assert uruchom.main([]) == 0
    assert not starty
    assert komunikaty and "już uruchomiona" in komunikaty[0]


def test_stan_aktualizacji_przekazany_do_aplikacji(monkeypatch, tmp_path):
    import os
    import app.aktualizacja as akt
    uruchom = _zaladuj_uruchom()
    monkeypatch.setattr(uruchom, "_start_aplikacji", lambda: 0)
    monkeypatch.setattr(akt, "skonfiguruj_log", lambda *a, **k: None)
    monkeypatch.setattr(akt, "BlokadaJednejInstancji", lambda *a: type(
        "B", (), {"zajmij": lambda s: True, "zwolnij": lambda s: None})())
    monkeypatch.setattr(akt, "sprawdz_i_aktualizuj", lambda repo: akt.WynikAktualizacji("wycofano"))
    monkeypatch.delenv("KSEF_STAN_AKTUALIZACJI", raising=False)
    uruchom.main([])
    assert os.environ.get("KSEF_STAN_AKTUALIZACJI") == "wycofano"


# --------------------------------------------------------------------------- #
# runda 3 — końcowy przegląd (28.09)

def test_git_czytany_jako_utf8_niezaleznie_od_jezyka_windows(monkeypatch, tmp_path):
    """Na polskim Windows text=True dekoduje wyjście gita jako cp1250, a git
    pisze ścieżki w UTF-8 — ścieżka z „Ł/Ń/Ę” wyłączała aktualizacje albo
    wysypywała start aplikacji."""
    import app.aktualizacja as akt
    widziane = []

    def fake_run(args, **kw):
        widziane.append(kw)
        raise OSError("stop")

    monkeypatch.setattr(akt.subprocess, "run", fake_run)
    (tmp_path / ".git").mkdir()
    akt.sprawdz_i_aktualizuj(tmp_path)
    assert widziane
    assert widziane[0].get("encoding") == "utf-8"
    assert widziane[0].get("errors") == "replace"
    assert not widziane[0].get("text")


def test_opis_wersji_nigdy_nie_wysypuje_startu(monkeypatch, repo):
    import app.aktualizacja as akt
    _, biuro, _ = repo

    def zly_git(*a, **k):
        raise UnicodeDecodeError("cp1250", b"\x81", 0, 1, "undefined")

    monkeypatch.setattr(akt, "_git", zly_git)
    assert akt.opis_wersji(biuro) == ""


def test_polska_sciezka_repo_dziala(tmp_path):
    """Prawdziwy git w katalogu ze znakami spoza ASCII."""
    katalog = tmp_path / "KSIĘGOWOŚĆ Łódź"
    katalog.mkdir()
    origin = katalog / "origin.git"
    _git(katalog, "init", "-q", "--bare", "-b", "main", str(origin))
    wl = katalog / "wl"
    _git(katalog, "clone", "-q", str(origin), str(wl))
    (wl / "app.py").write_text("wersja = 1\n")
    (wl / "requirements.txt").write_text("a==1\n")
    _git(wl, "add", ".")
    _git(wl, "commit", "-q", "-m", "v1")
    _git(wl, "push", "-q", "origin", "HEAD:main")
    biuro = katalog / "biuro Spółka"
    _git(katalog, "clone", "-q", str(origin), str(biuro))
    nowy = _wypchnij(wl, "app.py", "wersja = 2\n", "v2")
    assert sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=_Pip()).stan == "zaktualizowano"
    assert _git(biuro, "rev-parse", "HEAD") == nowy
    assert opis_wersji(biuro)


def test_nieczytelny_git_to_blad_a_nie_brak_repo(tmp_path):
    """.git istnieje, ale git go nie odczyta (dubious ownership, brak gita
    w PATH, uszkodzone repo) — nie może to być ciche „brak repo”."""
    (tmp_path / ".git").mkdir()
    wynik = sprawdz_i_aktualizuj(tmp_path, instaluj_zaleznosci=_Pip())
    assert wynik.stan == "blad"
    assert wynik.komunikat


def test_blad_startu_aplikacji_konczy_sie_komunikatem(monkeypatch, tmp_path):
    import app.aktualizacja as akt
    uruchom = _zaladuj_uruchom()
    monkeypatch.setenv("APPDATA", str(tmp_path))
    komunikaty = []
    monkeypatch.setattr(uruchom, "_komunikat", lambda tekst: komunikaty.append(tekst))
    monkeypatch.setattr(akt, "sprawdz_i_aktualizuj", lambda repo: akt.WynikAktualizacji("aktualna"))

    def zly_start():
        raise RuntimeError("zepsuty import")

    monkeypatch.setattr(uruchom, "_start_aplikacji", zly_start)
    assert uruchom.main([]) == 1
    assert komunikaty and "Nie udało się uruchomić" in komunikaty[0]
    assert "zepsuty import" in (tmp_path / "KsefSymfoniaExport" / "aktualizacje.log").read_text(encoding="utf-8")


def test_zepsuty_aktualizator_probuje_awaryjnie_pobrac_poprawke(monkeypatch, tmp_path):
    """Jeśli sam aktualizator się nie importuje (zły push), jedyną drogą
    naprawy jest awaryjne pobranie main — inaczej aktualizacje staną na zawsze."""
    uruchom = _zaladuj_uruchom()
    monkeypatch.setenv("APPDATA", str(tmp_path))
    komunikaty, awaryjne = [], []
    monkeypatch.setattr(uruchom, "_komunikat", lambda tekst: komunikaty.append(tekst))
    monkeypatch.setattr(uruchom, "_awaryjna_aktualizacja", lambda: awaryjne.append(1) or "zmieniono")

    def zly_import():
        raise SyntaxError("zły push")

    monkeypatch.setattr(uruchom, "_zaladuj_aktualizator", zly_import)
    assert uruchom.main([]) == 1
    assert awaryjne == [1]
    assert komunikaty and "ponownie" in komunikaty[0]


def test_awaryjna_aktualizacja_na_prawdziwym_gicie(monkeypatch, repo):
    _, biuro, wlasciciel = repo
    uruchom = _zaladuj_uruchom()
    monkeypatch.setattr(uruchom, "REPO", biuro)
    nowy = _wypchnij(wlasciciel, "app.py", "wersja = 'naprawiona'\n", "naprawa")
    (biuro / "app.py").write_text("zepsute\n")
    assert uruchom._awaryjna_aktualizacja() == "zmieniono"
    assert _git(biuro, "rev-parse", "HEAD") == nowy


def test_przycisk_szczegolow_po_polsku():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from app.main import zaladuj_tlumaczenia_qt
    from PySide6.QtCore import QCoreApplication
    assert zaladuj_tlumaczenia_qt(app)
    try:
        assert QCoreApplication.translate("QMessageBox", "Show Details...") != "Show Details..."
    finally:
        # QApplication jest wspólna dla całego przebiegu testów — inne testy
        # klikają standardowe przyciski po angielskich napisach
        app.removeTranslator(app._tlumacz_qt)


# --------------------------------------------------------------------------- #
# runda 4 — weryfikacja integracja3 (28.09)

def test_awaryjna_aktualizacja_nie_dotyka_repo_nadrzednego(monkeypatch, repo):
    """Katalog aplikacji bez własnego .git wewnątrz innego repo: awaryjna
    aktualizacja nie może przestawić gałęzi ani zrobić resetu w repo nadrzędnym."""
    _, biuro, _ = repo
    uruchom = _zaladuj_uruchom()
    podkatalog = biuro / "aplikacja"
    podkatalog.mkdir()
    monkeypatch.setattr(uruchom, "REPO", podkatalog)
    (biuro / "app.py").write_text("niezapisana zmiana w cudzym repo\n")
    przed = _git(biuro, "rev-parse", "HEAD")
    assert uruchom._awaryjna_aktualizacja() == "blad"
    assert (biuro / "app.py").read_text() == "niezapisana zmiana w cudzym repo\n"
    assert _git(biuro, "rev-parse", "HEAD") == przed
    assert not _git(biuro, "stash", "list")


def test_awaryjna_aktualizacja_bez_poprawki_na_serwerze_nie_kaze_restartowac(monkeypatch, repo, tmp_path):
    _, biuro, _ = repo
    uruchom = _zaladuj_uruchom()
    monkeypatch.setattr(uruchom, "REPO", biuro)
    assert uruchom._awaryjna_aktualizacja() == "bez_zmian"
    monkeypatch.setenv("APPDATA", str(tmp_path))
    komunikaty = []
    monkeypatch.setattr(uruchom, "_komunikat", lambda tekst: komunikaty.append(tekst))
    monkeypatch.setattr(uruchom, "_zaladuj_aktualizator", lambda: (_ for _ in ()).throw(SyntaxError("x")))
    uruchom.main([])
    assert komunikaty and "uruchom aplikację ponownie" not in komunikaty[0]
    assert "zgłoś" in komunikaty[0]


def test_awaryjna_aktualizacja_zachowuje_lokalne_commity(monkeypatch, repo):
    _, biuro, wlasciciel = repo
    uruchom = _zaladuj_uruchom()
    monkeypatch.setattr(uruchom, "REPO", biuro)
    (biuro / "app.py").write_text("commit asystenta\n")
    _git(biuro, "commit", "-q", "-am", "lokalny")
    lokalny = _git(biuro, "rev-parse", "HEAD")
    nowy = _wypchnij(wlasciciel, "app.py", "wersja = 'naprawiona'\n", "naprawa")
    assert uruchom._awaryjna_aktualizacja() == "zmieniono"
    assert _git(biuro, "rev-parse", "HEAD") == nowy
    galezie = [g.strip(" *") for g in _git(biuro, "branch", "--list", "kopia/*").splitlines()]
    assert len(galezie) == 1 and _git(biuro, "rev-parse", galezie[0]) == lokalny


def test_biblioteki_doinstalowane_przy_kolejnym_starcie_po_awaryjnej(monkeypatch, repo):
    """Awaryjna aktualizacja nie uruchamia pip — następny zwykły start musi
    wykryć, że zainstalowane biblioteki nie odpowiadają requirements.txt."""
    _, biuro, wlasciciel = repo
    pip = _Pip()
    assert sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=pip).stan == "aktualna"
    assert pip.wywolania == 0  # pierwszy start: stan wyjściowy zapamiętany, bez instalacji
    uruchom = _zaladuj_uruchom()
    monkeypatch.setattr(uruchom, "REPO", biuro)
    _wypchnij(wlasciciel, "requirements.txt", "httpx==0.28.1\nnowa==1.0\n", "deps")
    assert uruchom._awaryjna_aktualizacja() == "zmieniono"
    assert sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=pip).stan == "aktualna"
    assert pip.wywolania == 1
    assert sprawdz_i_aktualizuj(biuro, instaluj_zaleznosci=pip).stan == "aktualna"
    assert pip.wywolania == 1  # zapamiętane — bez ponownej instalacji


def test_worktree_z_plikiem_git_nie_psuje_aktualizacji(repo, tmp_path):
    """W kopii roboczej `git worktree` .git jest PLIKIEM, nie katalogiem —
    znacznik bibliotek nie może wtedy wysypać aktualizacji."""
    _, biuro, _ = repo
    wt = tmp_path / "wt"
    _git(biuro, "worktree", "add", "-q", str(wt), "-b", "wt-test")
    assert (wt / ".git").is_file()
    wynik = sprawdz_i_aktualizuj(wt, instaluj_zaleznosci=_Pip())
    assert wynik.stan != "blad", wynik.komunikat
