"""Rejestr numerów kontrahentów — SQLite z WAL, per profil (per NIP firmy).

USTALENIE EMPIRYCZNE Z PRODUKCJI (2026-08-18): Symfonia FK NIE zasypuje
dziur po kartach usuniętych — nowa karta dostaje ZAWSZE max(pozycja)+1,
niezależnie od dziur po usuniętych kartach (dziury 182/183/453 pozostały
puste, nowe karty poszły od 883). Dotychczasowa zasada "pierwszy wolny
numer" była BŁĘDNA. Ten rejestr odwzorowuje zasadę max+1
(nastepny_numer/przydziel), żeby konto rozrachunkowe "201-1-<pozycja>"
emitowane w pliku hmffk zgadzało się z numerem karty faktycznie nadanym
w FK.

Kartoteka bywa też zasilana RĘCZNIE bezpośrednio w FK (poza tym eksportem),
co z czasem rozjeżdża rejestr z rzeczywistym stanem FK — po takich zmianach
użyj synchronizuj_z_kartoteka (patrz niżej): eksport kartoteki z FK jest
wtedy źródłem prawdy.

Status wpisu w tabeli numery:
- "seed"     — wczytany z eksportu kartoteki FK (importuj_kartoteke),
- "auto"     — przydzielony automatycznie przy eksporcie (przydziel),
- "manual"   — ustawiony ręcznie (ustaw, okno Kontrahenci… w GUI — patrz
  zapisz_zmiany); synchronizacja z FK takiego wpisu NIE zmienia (różnice
  raportuje — RaportSync.pominiete_reczne / zablokowane),
- "duplikat" — karta-duplikat scalona aliasem, ale NIEusunięta w FK: numer
  nadal zajęty, wpis nie jest emitowany (patrz dodaj_alias).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from app.config import app_data_dir
from app.symfonia_sql import czytaj_kartoteke as _czytaj_kartoteke_sql

# kolumny tolerowane w eksporcie kartoteki FK, których nie wykorzystujemy
# wprost (Kod, Adres, Kod pocztowy, Rejon) — patrz importuj_kartoteke


@dataclass
class WpisNumeru:
    klucz: str
    pozycja: int
    nazwa: str
    nip: str
    miejscowosc: str
    status: str
    skrot: str = ""  # kod karty w FK (FK.fk_kontrahenci.skrot); '' = nieznany


@dataclass
class RaportSync:
    """Wynik synchronizuj_z_kartoteka — patrz tam."""

    dodane: int
    zaktualizowane: int
    rozjazdy: list[tuple[str, int, int]]  # (klucz, nasza_pozycja, pozycja_fk)
    brak_w_fk: list[tuple[str, int]]  # (klucz, nasza_pozycja)
    ostrzezenia: list[str]
    # wpisy "manual" (numer ustawiony ręcznie przez księgową), których pozycja
    # różni się od kartoteki FK — NIE zmienione: (klucz, pozycja_w_rejestrze, pozycja_fk)
    pominiete_reczne: list[tuple[str, int, int]] = field(default_factory=list)
    # klucze z kartoteki, których pozycja FK jest w rejestrze zajęta przez wpis
    # pozostawiony bez zmian (manual albo sam zablokowany) — NIE dodane / NIE
    # przesunięte: (klucz_fk, pozycja_fk, klucz_zajmujacy)
    zablokowane: list[tuple[str, int, str]] = field(default_factory=list)
    # liczba WSZYSTKICH wpisów "manual" w rejestrze w chwili synchronizacji —
    # synchronizacja pominęła je w całości. Część może pochodzić ze starszej
    # wersji okna Kontrahenci… (każdy dodany/wklejony wiersz dostawał wtedy
    # "manual"), a nie z ręcznej weryfikacji w Symfonii — patrz raport w GUI
    # (RejestrDialog._pokaz_raport_sync) i README.
    reczne: int = 0

    def jako_ostrzezenia(self) -> list[str]:
        """Rozjazdy pozycji, wpisy nieobecne w kartotece FK, wpisy pominięte
        (ustawione ręcznie) i ostrzeżenia parsowania jako lista tekstowa — do
        dopisania w logu/raporcie eksportu przy automatycznej synchronizacji
        przed eksportem (patrz app/gui.py ExportWorker i app/cli.py --sync-sql).

        Podpowiedź, jak „odblokować” wpisy manual (status seed), pojawia się
        tylko, gdy synchronizacja faktycznie coś przez nie pominęła
        (pominiete_reczne/zablokowane) — sama obecność wpisów manual nie jest
        problemem i nie może szumieć w raporcie każdego eksportu."""
        wynik: list[str] = []
        for klucz, nasza, fk in self.rozjazdy:
            wynik.append(f"Synchronizacja numeracji: {klucz} — pozycja poprawiona {nasza} -> {fk}")
        for klucz, poz in self.brak_w_fk:
            wynik.append(
                f"Synchronizacja numeracji: {klucz} (pozycja {poz}) nieobecny w kartotece FK"
            )
        for klucz, nasza, fk in self.pominiete_reczne:
            wynik.append(
                f"Synchronizacja numeracji: {klucz} — pominięto (ustawione ręcznie): "
                f"było {nasza}, w Symfonii {fk} — sprawdź i w razie potrzeby popraw "
                "w oknie Kontrahenci…"
            )
        for klucz, fk, zajmujacy in self.zablokowane:
            wynik.append(
                f"Synchronizacja numeracji: {klucz} — pominięto: pozycja {fk} z Symfonii "
                f"jest w rejestrze zajęta przez {zajmujacy} (wpis pozostawiony bez zmian)"
            )
        if self.pominiete_reczne or self.zablokowane:
            ile = f" (w rejestrze: {self.reczne})" if self.reczne else ""
            wynik.append(
                f"Synchronizacja numeracji: wpisów ze statusem manual{ile} synchronizacja "
                "nie zmienia — część może pochodzić ze starszej wersji okna Kontrahenci… "
                "(dodane/wklejone wiersze dostawały wtedy status manual). Jeśli numer w "
                "Symfonii jest właściwy, ustaw wpisowi status seed w oknie Kontrahenci… i "
                "kliknij „Zapisz” — kolejna synchronizacja go poprawi."
            )
        wynik.extend(f"Synchronizacja numeracji: {w}" for w in self.ostrzezenia)
        return wynik


STATUSY = ("seed", "auto", "manual", "duplikat")

# górna granica numeru karty (pozycji) przyjmowanego z tabeli okna
# Kontrahenci… — realne numery kart FK to tysiące; większa liczba to pomyłka
# (np. wklejony NIP albo kwota), a powyżej int64 nie mieści się nawet w Qt/SQLite
POZYCJA_MAX = 999_999
_POZYCJA_MAX_TEKST = f"{POZYCJA_MAX:,}".replace(",", " ")  # „999 999”

# nazwa zastępcza wiersza z „Dodaj wiersz” (okno Kontrahenci…, z licznikiem:
# „Nowy kontrahent 2”…) — bez NIP-u taki wiersz nie może trafić do rejestru
# (patrz _waliduj_zmiany)
NAZWA_ZASTEPCZA = "Nowy kontrahent"
_NAZWA_ZASTEPCZA_RE = re.compile(r"nowy kontrahent(?: \d+)?")


def czy_nazwa_zastepcza(nazwa: str) -> bool:
    """Czy nazwa to nazwa zastępcza z „Dodaj wiersz” (bez względu na wielkość
    liter i spacje) — patrz NAZWA_ZASTEPCZA."""
    return _NAZWA_ZASTEPCZA_RE.fullmatch(" ".join((nazwa or "").casefold().split())) is not None


@dataclass
class ZmianaWpisu:
    """Jeden wiersz do zapisu zbiorczego (RejestrNumerow.zapisz_zmiany) — stan
    wiersza tabeli z okna „Kontrahenci…” w chwili kliknięcia „Zapisz”.

    `id` identyfikuje wiersz po stronie wywołującego (w GUI: klucz wpisu z
    bazy albo tymczasowy identyfikator nowego wiersza) — służy tylko do
    przypisania błędów walidacji i wyniku do wierszy. `stary_klucz` = klucz
    wpisu w bazie (None = nowy wpis). `pozycja` może być tekstem prosto z
    komórki — walidacja jest po stronie zapisu."""

    id: str
    stary_klucz: str | None
    pozycja: int | str
    nip: str = ""
    nazwa: str = ""
    miejscowosc: str = ""
    status: str = "manual"


@dataclass
class BladZmiany:
    """Błąd walidacji jednej zmiany: id wiersza (ZmianaWpisu.id), pole do
    poprawienia ("pozycja" / "nip" / "nazwa" / "status") i komunikat."""

    id: str
    pole: str
    komunikat: str


class ZmianyOdrzucone(ValueError):
    """Zestaw zmian nie przeszedł walidacji — NIC nie zostało zapisane. Pełna
    lista błędów w `bledy` (wszystkie naraz, nie tylko pierwszy)."""

    def __init__(self, bledy: list[BladZmiany]):
        self.bledy = list(bledy)
        super().__init__("\n".join(b.komunikat for b in self.bledy))


class SynchronizacjaOdrzucona(Exception):
    """BEZPIECZNIK: kartoteka/baza wskazana do synchronizacji nie wygląda na
    kartotekę TEJ firmy — patrz _sprawdz_zgodnosc_z_fk.

    Wprowadzone po incydencie produkcyjnym (2026-08-25): użytkowniczka przy
    profilu ALFATRON (NIP 9100010020) wybrała przez pomyłkę sąsiednią bazę
    ALFATRON_SERWIS_MEDYCZNY — synchronizacja dopisała ~908 obcych kart i
    nadpisała 285 pozycji, zanim ktokolwiek zauważył pomyłkę. Rzucenie tego
    wyjątku PRZED jakąkolwiek zmianą w rejestrze ma to uniemożliwić."""


# próg zgodności i minimalna liczba wpisów z NIP-em do włączenia kontroli —
# patrz _sprawdz_zgodnosc_z_fk
_PROG_ZGODNOSCI_SYNC = 0.60
_MIN_WPISOW_NIP_DO_KONTROLI = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SUFIKS_FLOAT = re.compile(r"^(.*?)[.,]0+$")


def _digits(value: str | None) -> str:
    """Same cyfry — usuwa też prefiks 'PL' i inne znaki (jak app/hmffk.py).

    Przed zebraniem cyfr odcina sufiks floatowy ".0"/",0" (np. NIP wklejony
    z Excela jako liczba trafia tu jako "9100010014.0" — bez tego cięcia
    _digits dokleiłoby fałszywe zero z ".0" do klucza). Druga linia obrony
    obok stringifikacji komórek w _wczytaj_xlsx — dla ścieżek wejścia, gdzie
    ten sam artefakt mógłby się pojawić inaczej (np. CSV wklejony z arkusza)."""
    s = (value or "").strip()
    m = _SUFIKS_FLOAT.match(s)
    if m:
        s = m.group(1)
    return "".join(c for c in s if c.isdigit())


def _wartosc_komorki(val: object) -> str:
    """Stringifikacja komórki xlsx (openpyxl, data_only=True).

    Excel przechowuje liczby jako float niezależnie od formatu wyświetlania —
    komórka z NIP wpisanym jako liczba (bez formatu tekstowego) wraca z
    openpyxl jako np. 9100010014.0, a naiwne str(val) dałoby "9100010014.0":
    _digits() zebrałby wtedy cyfry WRAZ z zerem z ".0", psując klucz (11 cyfr
    zamiast 10). Dotyczy to każdej kolumny liczbowej, nie tylko NIP-u.
    Bool sprawdzamy przed float/int, bo w Pythonie bool jest podtypem int."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return str(val).strip()
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    if isinstance(val, int):
        return str(val)
    return str(val).strip()


# wykrywanie wiersza nagłówka w importowanych plikach (xlsx/csv) — patrz
# _naglowki_z_wiersza / _czy_wiersz_naglowka / _mapuj_wiersze_danych, użyte przez
# RejestrNumerow._wczytaj_xlsx i ._wczytaj_csv
_MAX_WIERSZY_SZUKAJ_NAGLOWKA = 10
_SEPARATORY_CSV = (";", ",", "\t")


def _naglowki_z_wiersza(komorki) -> list[str]:
    """Normalizuje komórki potencjalnego wiersza nagłówka: stringify (jak dla
    komórek xlsx — działa też na zwykłych str z CSV) + strip + casefold, żeby
    porównanie z "pozycja"/"nazwa" nie zależało od wielkości liter ani spacji."""
    return [_wartosc_komorki(c).strip().casefold() for c in komorki]


def _czy_wiersz_naglowka(naglowki: list[str]) -> bool:
    """Nagłówek rozpoznajemy po jednoczesnej obecności kolumn 'Pozycja' i
    'Nazwa' — kolejność, wielkość liter i pozostałe kolumny (Kod, Adres,
    nadmiarowe bez nazwy itd.) są dowolne. Wiersze przed takim wierszem (np.
    śmieciowy pierwszy wiersz eksportu z Arkuszy Google) są ignorowane."""
    return "pozycja" in naglowki and "nazwa" in naglowki


def _mapuj_wiersze_danych(wiersze_danych, naglowki: list[str]) -> list[dict[str, str]]:
    """Mapuje wiersze danych na słowniki kolumna->wartość wg nazw z wiersza
    nagłówka. Kolumny bez nazwy w nagłówku (np. nadmiarowe kolumny bez opisu,
    jak w eksporcie z Arkuszy Google) są ignorowane. Wiersze całkowicie puste
    są pomijane."""
    wynik: list[dict[str, str]] = []
    for row in wiersze_danych:
        if row is None:
            continue
        if all(_wartosc_komorki(v) == "" for v in row):
            continue
        mapa: dict[str, str] = {}
        for idx, nazwa_kol in enumerate(naglowki):
            if not nazwa_kol:
                continue
            val = row[idx] if idx < len(row) else None
            mapa[nazwa_kol] = _wartosc_komorki(val)
        wynik.append(mapa)
    return wynik


def _parsuj_wiersz_kartoteki(
    wiersz: dict[str, str], i: int
) -> tuple[tuple[int, str, str, str] | None, str | None]:
    """Parsuje jeden wiersz kartoteki FK (kolumny pozycja/nazwa/nip/miejscowość) na
    (pozycja, nazwa, nip, miejscowosc) — albo (None, ostrzeżenie) gdy wiersz jest
    niepoprawny (brak pozycji, zła pozycja, brak NIP i nazwy). Wspólne dla
    importuj_kartoteke i synchronizuj_z_kartoteka."""
    pozycja_raw = (wiersz.get("pozycja") or "").strip()
    if not pozycja_raw:
        return None, f"Wiersz {i}: brak pozycji — pominięto"
    try:
        pozycja = int(float(pozycja_raw))
    except ValueError:
        return None, f"Wiersz {i}: nieprawidłowa pozycja {pozycja_raw!r} — pominięto"
    nazwa = (wiersz.get("nazwa") or "").strip()
    nip = _digits(wiersz.get("nip"))
    miejscowosc = (wiersz.get("miejscowość") or "").strip()
    if not nazwa and not nip:
        return None, f"Wiersz {i}: brak NIP i nazwy — pominięto"
    return (pozycja, nazwa, nip, miejscowosc), None


def _blad_brak_naglowka(rodzaj: str, sprawdzono: int) -> ValueError:
    return ValueError(
        f"Nie znaleziono w {rodzaj} wiersza nagłówka z kolumnami 'Pozycja' i "
        f"'Nazwa' (sprawdzono pierwsze {sprawdzono} wierszy pliku)."
    )


def _klucz_seed(nazwa: str, nip: str, miejscowosc: str) -> str:
    """Klucz kontrahenta — IDENTYCZNIE jak app/hmffk.py::_kontrahent_z_podmiotu."""
    if nip:
        return nip
    digest = hashlib.sha1(f"{nazwa}|{miejscowosc}".encode("utf-8"))
    return "K_" + digest.hexdigest()[:12].upper()


def _norm(s: str) -> str:
    """casefold + NFKD bez znaków łączących (ogonki) + pojedyncze spacje."""
    s = unicodedata.normalize("NFKD", (s or "").casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split())


# tokeny formy prawnej / generyczne — pomijane przy budowie rdzenia nazwy,
# żeby "ABC Sp. z o.o." vs "ABD Sp. z o.o." nie dawało wysokiego ratio z powodu
# wspólnego, długiego dopisku formy prawnej
_RDZEN_STOPWORDS = {
    "sp", "z", "o", "oo", "spolka", "ograniczona", "odpowiedzialnoscia", "akcyjna",
    "sa", "sc", "sj", "sk", "komandytowa", "jawna", "cywilna", "pphu", "phu", "fhu",
    "ppuh", "przedsiebiorstwo", "handlowo", "uslugowe", "uslugowo", "handlowe",
    "firma", "zaklad",
}


def _rdzen(nazwa: str) -> str:
    """Rdzeń nazwy do porównań fuzzy: _norm + bez interpunkcji + bez tokenów formy
    prawnej/generycznych. Bez tego np. "PPHU Max" vs "PPHU Mix" dostawałoby wysokie
    ratio z samego wspólnego "pphu", mimo że rdzenna nazwa jest zupełnie inna."""
    s = _norm(nazwa)
    s = "".join(c if c.isalnum() or c.isspace() else " " for c in s)
    tokeny = [t for t in s.split() if t not in _RDZEN_STOPWORDS]
    return " ".join(tokeny)


def _nazwa_po_sync(wpis: WpisNumeru, nazwa_fk: str, nip_fk: str) -> str:
    """Nazwa wpisu po synchronizacji z FK: kontrahent z NIP-em — zawsze nazwa
    z FK (eksport wysyła ją do Symfonii, musi być równa nazwie karty; klucz
    z NIP-u od nazwy nie zależy); bez NIP-u — tylko uzupełnienie pustej."""
    if _digits(wpis.nip or nip_fk) and nazwa_fk.strip():
        return nazwa_fk.strip()
    return wpis.nazwa or nazwa_fk


class RejestrNumerow:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path else app_data_dir() / "kontrahenci_nr.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS numery(
                   profil_nip TEXT,
                   klucz TEXT,
                   pozycja INTEGER,
                   nazwa TEXT DEFAULT '',
                   nip TEXT DEFAULT '',
                   miejscowosc TEXT DEFAULT '',
                   status TEXT DEFAULT 'auto',
                   added_at TEXT,
                   PRIMARY KEY (profil_nip, klucz)
               )"""
        )
        # kolumna dodana 29.09 (kod karty z FK) — rejestry sprzed tej wersji
        # dostają ją tu, puste wartości = kod nieznany
        kolumny = {r[1] for r in self._conn.execute("PRAGMA table_info(numery)")}
        if "skrot" not in kolumny:
            self._conn.execute("ALTER TABLE numery ADD COLUMN skrot TEXT DEFAULT ''")
        # pozycja karty, z której pochodzi skrot — kod ważny tylko, gdy wpis
        # nadal ma tę pozycję (zmiana numeru w tabeli/synchronizacji go unieważnia)
        if "skrot_poz" not in kolumny:
            self._conn.execute("ALTER TABLE numery ADD COLUMN skrot_poz INTEGER")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_numery_pozycja "
            "ON numery(profil_nip, pozycja)"
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS aliasy(
                   profil_nip TEXT,
                   klucz_alias TEXT,
                   klucz_kanoniczny TEXT,
                   PRIMARY KEY (profil_nip, klucz_alias)
               )"""
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # numeracja

    def pozycja(self, profil_nip: str, klucz: str) -> int | None:
        row = self._conn.execute(
            "SELECT pozycja FROM numery WHERE profil_nip=? AND klucz=?",
            (profil_nip, klucz),
        ).fetchone()
        return row[0] if row else None

    def wpis(self, profil_nip: str, klucz: str) -> WpisNumeru | None:
        row = self._conn.execute(
            "SELECT klucz, pozycja, nazwa, nip, miejscowosc, status, skrot, skrot_poz "
            "FROM numery WHERE profil_nip=? AND klucz=?",
            (profil_nip, klucz),
        ).fetchone()
        if row is None:
            return None
        *pola, skrot, skrot_poz = row
        wpis = WpisNumeru(*pola)
        if skrot and skrot_poz == wpis.pozycja:
            wpis.skrot = skrot
        return wpis

    def kanoniczny(self, profil_nip: str, klucz: str) -> str:
        """Rozwiązuje łańcuch aliasów do punktu stałego; bez aliasu zwraca klucz bez
        zmian. Chroni się przed cyklem zbiorem odwiedzonych — na cykl przerywa i
        zwraca ostatni bieżący klucz (w normalnej pracy dodaj_alias nie dopuszcza
        do powstania cyklu, ale ta metoda ma być bezpieczna niezależnie)."""
        odwiedzone = {klucz}
        biezacy = klucz
        while True:
            row = self._conn.execute(
                "SELECT klucz_kanoniczny FROM aliasy WHERE profil_nip=? AND klucz_alias=?",
                (profil_nip, biezacy),
            ).fetchone()
            if row is None:
                return biezacy
            nastepny = row[0]
            if nastepny in odwiedzone:
                return biezacy
            odwiedzone.add(nastepny)
            biezacy = nastepny

    def pierwszy_wolny(self, profil_nip: str) -> int:
        """Najmniejsza dodatnia liczba nieobecna wśród zajętych pozycji.

        NIE odzwierciedla numeracji FK — FK nadaje max+1; używać tylko do
        celów pomocniczych."""
        rows = self._conn.execute(
            "SELECT pozycja FROM numery WHERE profil_nip=? ORDER BY pozycja",
            (profil_nip,),
        ).fetchall()
        oczekiwana = 1
        for (poz,) in rows:
            if poz == oczekiwana:
                oczekiwana += 1
            elif poz > oczekiwana:
                break
        return oczekiwana

    def nastepny_numer(self, profil_nip: str) -> int:
        """Numer, jaki dostanie NASTĘPNA nowa karta: max(pozycja)+1 (1, gdy
        profil pusty). Zgodnie z ustaleniem empirycznym z produkcji — FK NIE
        zasypuje dziur po usuniętych kartach, więc zwolniony numer nigdy nie
        jest ponownie użyty."""
        row = self._conn.execute(
            "SELECT MAX(pozycja) FROM numery WHERE profil_nip=?", (profil_nip,)
        ).fetchone()
        maks = row[0]
        return (maks or 0) + 1

    def przydziel(
        self, profil_nip: str, klucz: str, nazwa: str = "", nip: str = "", miejscowosc: str = ""
    ) -> int:
        istniejaca = self.pozycja(profil_nip, klucz)
        if istniejaca is not None:
            return istniejaca
        poz = self.nastepny_numer(profil_nip)
        self._conn.execute(
            "INSERT INTO numery(profil_nip, klucz, pozycja, nazwa, nip, miejscowosc, "
            "status, added_at) VALUES (?, ?, ?, ?, ?, ?, 'auto', ?)",
            (profil_nip, klucz, poz, nazwa, nip, miejscowosc, _now()),
        )
        self._conn.commit()
        return poz

    def ustaw(
        self,
        profil_nip: str,
        klucz: str,
        pozycja: int,
        nazwa: str = "",
        nip: str = "",
        miejscowosc: str = "",
        status: str = "manual",
    ) -> None:
        konflikt = self._conn.execute(
            "SELECT klucz FROM numery WHERE profil_nip=? AND pozycja=? AND klucz<>?",
            (profil_nip, pozycja, klucz),
        ).fetchone()
        if konflikt is not None:
            raise ValueError(
                f"Pozycja {pozycja} jest już zajęta przez kontrahenta o kluczu "
                f"'{konflikt[0]}'."
            )
        self._conn.execute(
            "INSERT INTO numery(profil_nip, klucz, pozycja, nazwa, nip, miejscowosc, "
            "status, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(profil_nip, klucz) DO UPDATE SET "
            "pozycja=excluded.pozycja, nazwa=excluded.nazwa, nip=excluded.nip, "
            "miejscowosc=excluded.miejscowosc, status=excluded.status",
            (profil_nip, klucz, pozycja, nazwa, nip, miejscowosc, status, _now()),
        )
        self._conn.commit()

    def zmien_dane(
        self,
        profil_nip: str,
        stary_klucz: str,
        *,
        pozycja: int,
        nip: str,
        nazwa: str,
        miejscowosc: str,
        status: str,
    ) -> str:
        """Edycja JEDNEGO wpisu "jak w arkuszu" — wolno zmienić każde pole, łącznie
        z NIP-em lub nazwą, od których zależy KLUCZ (patrz _klucz_seed). To
        zapis_zmiany z jedną zmianą: ta sama walidacja (kolizja klucza/pozycji,
        klucz równy aliasowi — ZmianyOdrzucone, podklasa ValueError) i ta sama
        obsługa zmiany klucza, w tym aliasów (stary klucz -> nowy, żeby kolejne
        faktury z KSeF ze starym kluczem trafiały na tę samą kartę — patrz
        _aliasy_po_zmianach). Zwraca finalny klucz wpisu (stary_klucz albo
        nowo wyliczony)."""
        if isinstance(pozycja, bool) or not isinstance(pozycja, int) or pozycja < 1:
            raise ValueError("Pozycja musi być liczbą całkowitą >= 1")
        if not _digits(nip) and not (nazwa or "").strip():
            raise ValueError("Podaj NIP lub nazwę")
        mapa = self.zapisz_zmiany(
            profil_nip,
            [ZmianaWpisu(
                id=stary_klucz, stary_klucz=stary_klucz, pozycja=pozycja, nip=nip,
                nazwa=nazwa, miejscowosc=miejscowosc, status=status,
            )],
        )
        return mapa[stary_klucz]

    def zapisz_zmiany(
        self,
        profil_nip: str,
        zmiany: list[ZmianaWpisu],
        usuniete: list[str] | tuple[str, ...] = (),
    ) -> dict[str, str]:
        """Zapis ZBIORCZY z okna „Kontrahenci…” (przycisk „Zapisz”): zmienione
        i nowe wiersze (`zmiany`) oraz klucze wpisów do usunięcia (`usuniete`).

        Najpierw walidacja CAŁEGO zestawu naraz na stanie końcowym (baza po
        wszystkich zmianach): pozycja całkowita 1..POZYCJA_MAX, NIP lub nazwa
        (nie sama nazwa zastępcza „Nowy kontrahent” bez NIP-u), status z
        dozwolonego zbioru, unikalna pozycja i unikalny klucz (NIP) w profilu,
        klucz nowego/zmienionego wiersza nie może być kluczem ALIASU (wiersz
        byłby martwy — kanoniczny() zawsze wybierze alias). Błąd dostaje tylko
        wiersz, który do konfliktu DOPROWADZIŁ (zmienił pozycję/klucz albo jest
        nowy) — wpis pozostawiony bez zmian nie jest „winny”. Jakikolwiek błąd
        -> ZmianyOdrzucone z pełną listą i NIC nie jest zapisane.

        Potem zapis w JEDNEJ transakcji (BEGIN IMMEDIATE ... COMMIT; błąd w
        trakcie -> ROLLBACK): usunięcia (z aliasami jak usun()), przeniesienie
        zmienianych wpisów na tymczasowe ujemne pozycje (zamiana numerów A<->B
        w jednym zapisie — jak w _synchronizuj_z_fk), zmiany kluczy (NIP/nazwa
        -> nowy klucz: stary wpis znika), nowe wpisy, aliasy wg
        _aliasy_po_zmianach (m.in. stary klucz -> nowy, bo eksport liczy klucz
        z faktury KSeF — bez tego kolejna faktura kontrahenta po poprawce
        NIP-u/nazwy w tabeli dostawała NOWY numer karty). Zwraca
        {ZmianaWpisu.id: klucz końcowy}. Status zapisywany dokładnie taki, jak
        w ZmianaWpisu — o "manual" dla edytowanych wierszy decyduje
        wywołujący (GUI)."""
        zmiany = list(zmiany)
        usuniete_set = set(usuniete)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            obecne = {w.klucz: w for w in self.lista(profil_nip)}
            usuniete_set &= set(obecne)  # już nieistniejące — nic do usunięcia
            aliasy = self.aliasy(profil_nip)
            gotowe, aliasy_koncowe = self._waliduj_zmiany(obecne, zmiany, usuniete_set, aliasy)
            mapa = self._zastosuj_zmiany(profil_nip, gotowe, usuniete_set)
            if aliasy_koncowe != aliasy:
                self._conn.execute("DELETE FROM aliasy WHERE profil_nip=?", (profil_nip,))
                self._conn.executemany(
                    "INSERT INTO aliasy(profil_nip, klucz_alias, klucz_kanoniczny) VALUES (?, ?, ?)",
                    [(profil_nip, a, k) for a, k in aliasy_koncowe.items()],
                )
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()
        return mapa

    @staticmethod
    def _liczba_z_komorki(wartosc: int | str | None) -> int | None:
        """Liczba całkowita z komórki tabeli: int albo tekst z samymi cyframi
        (także "12.0"/"12,0" — artefakt liczby wklejonej z Excela), BEZ
        sprawdzania zakresu. None = to nie jest liczba całkowita."""
        if isinstance(wartosc, bool):
            return None
        if isinstance(wartosc, int):
            return wartosc
        m = re.fullmatch(r"\s*(\d+)(?:[.,]0+)?\s*", str(wartosc or ""))
        return int(m.group(1)) if m is not None else None

    @staticmethod
    def _parsuj_pozycje(wartosc: int | str | None) -> int | None:
        """Pozycja z komórki tabeli (patrz _liczba_z_komorki). None =
        niepoprawna albo poza zakresem 1..POZYCJA_MAX."""
        liczba = RejestrNumerow._liczba_z_komorki(wartosc)
        if liczba is None or not 1 <= liczba <= POZYCJA_MAX:
            return None
        return liczba

    @staticmethod
    def _aliasy_po_zmianach(
        aliasy: dict[str, str],
        rename: dict[str, str],
        usuniete: set[str],
        statusy_koncowe: dict[str, str],
    ) -> dict[str, str]:
        """Tabela aliasów (alias -> kanoniczny) po zapisie zestawu zmian.

        `rename` — stary klucz -> nowy klucz wpisów, którym zapis zmienia klucz
        (NIP albo nazwa/miejscowość kontrahenta bez NIP); `statusy_koncowe` —
        klucz -> status WSZYSTKICH wpisów w stanie końcowym.

        Eksport liczy klucz kontrahenta Z FAKTURY KSeF (app/hmffk.py
        _kontrahent_z_podmiotu), więc po zmianie klucza w tabeli kolejne
        faktury tego kontrahenta niosą STARY klucz. Reguły:
        1. Istniejące aliasy: kaskada usunięć (alias albo cel usunięty ->
           alias znika, jak usun()); cel przemianowanego wpisu -> jego nowy
           klucz. Klucza aliasu NIE przemianowujemy — faktury z tym kluczem
           dalej trafiają tam, gdzie trafiały (wiersz-duplikat zachowuje stary
           alias; dawniej alias był przenoszony na nowy klucz i stary klucz
           tracił kartę). Alias na samego siebie (powrót wpisu do klucza,
           który był jego aliasem) znika.
        2. Wpis ze zmienionym kluczem, którego stary klucz nie jest aliasem:
           alias stary -> nowy (W1) — chyba że stary klucz przejął w stanie
           końcowym inny wpis (zamiana NIP-ów, łańcuch A->B, B->C): faktury z
           tym kluczem idą wtedy do jego nowego właściciela.
        3. Wpis-duplikat (stary klucz jest aliasem na C): stary alias zostaje
           (p. 1); gdy wpis po zmianie nadal ma status duplikat, także nowy
           klucz wskazuje C (wiersz zostaje duplikatem pod nowym kluczem).
        4. Spłaszczenie do punktu stałego (bez łańcuchów) — tabela aliasów
           nigdy nie zawiera łańcuchów (patrz kanoniczny/dodaj_alias). Cykl
           (możliwy tylko w uszkodzonych danych — reguły wyżej go nie tworzą)
           zostaje bez zmian, żeby zapis nie zmieniał po cichu kierowania
           faktur niezwiązanych z nim aliasów.
        Zapis bez zmian kluczy i bez usunięć nie dotyka aliasów wcale."""
        if not rename and not usuniete:
            return dict(aliasy)
        po_kaskadzie: dict[str, str] = {}
        for alias, kanon in aliasy.items():
            if alias in usuniete or kanon in usuniete:
                continue
            kanon = rename.get(kanon, kanon)
            if alias != kanon:
                po_kaskadzie[alias] = kanon
        wynik = dict(po_kaskadzie)
        for stary, nowy in rename.items():
            if stary in po_kaskadzie:
                cel = po_kaskadzie[stary]
                if statusy_koncowe.get(nowy) == "duplikat" and nowy != cel:
                    wynik.setdefault(nowy, cel)
                continue
            if stary in statusy_koncowe:
                continue
            wynik[stary] = nowy

        def punkt_staly(alias: str) -> str | None:
            """Cel aliasu po łańcuchu; None = cykl."""
            odwiedzone = {alias}
            klucz = wynik[alias]
            while klucz in wynik:
                if klucz in odwiedzone:
                    return None
                odwiedzone.add(klucz)
                klucz = wynik[klucz]
            return klucz

        splaszczone: dict[str, str] = {}
        for alias, kanon in wynik.items():
            cel = punkt_staly(alias)
            if cel is None:
                splaszczone[alias] = kanon  # cykl z uszkodzonych danych — bez zmian
            elif cel != alias:
                splaszczone[alias] = cel
        return splaszczone

    def _waliduj_zmiany(
        self,
        obecne: dict[str, WpisNumeru],
        zmiany: list[ZmianaWpisu],
        usuniete: set[str],
        aliasy: dict[str, str],
    ) -> tuple[list[dict], dict[str, str]]:
        """Walidacja zestawu — patrz zapisz_zmiany. Zwraca (znormalizowane
        zmiany jako słowniki, tabela aliasów po zapisie — patrz
        _aliasy_po_zmianach) albo podnosi ZmianyOdrzucone ze WSZYSTKIMI
        błędami."""
        bledy: list[BladZmiany] = []
        gotowe: list[dict] = []
        zmieniane_klucze: set[str] = set()

        def opis(nazwa: str, nip: str, klucz: str | None, pozycja) -> str:
            tekst = f"„{nazwa or klucz or '(bez nazwy)'}”"
            szczegoly = [f"poz. {pozycja}"] if pozycja is not None else []
            if nip:
                szczegoly.append(f"NIP {nip}")
            return tekst + (f" ({', '.join(szczegoly)})" if szczegoly else "")

        for z in zmiany:
            stary = obecne.get(z.stary_klucz) if z.stary_klucz is not None else None
            if z.stary_klucz is not None:
                if stary is None or z.stary_klucz in usuniete or z.stary_klucz in zmieniane_klucze:
                    bledy.append(BladZmiany(
                        z.id, "nazwa",
                        "Tego wpisu nie ma już w rejestrze (zmieniony w międzyczasie) — "
                        "zamknij okno bez zapisu i otwórz ponownie.",
                    ))
                    continue
                zmieniane_klucze.add(z.stary_klucz)
            pozycja = self._parsuj_pozycje(z.pozycja)
            if (
                pozycja is None and stary is not None and stary.pozycja >= 1
                and self._liczba_z_komorki(z.pozycja) == stary.pozycja
            ):
                # limit zakresu dotyczy WPISYWANEJ pozycji — numer ponad
                # POZYCJA_MAX zapisany wcześniej (np. z synchronizacji) i nie
                # zmieniony nie blokuje zapisu innych pól tego wpisu
                pozycja = stary.pozycja
            if pozycja is None:
                surowa = str(z.pozycja).strip()
                liczba = self._liczba_z_komorki(z.pozycja)
                if liczba is not None and liczba > POZYCJA_MAX:
                    komunikat = (
                        f"Pozycja „{surowa}” poza zakresem — numer karty to liczba "
                        f"całkowita od 1 do {_POZYCJA_MAX_TEKST}."
                    )
                else:
                    komunikat = (
                        f"Pozycja „{surowa}” — musi być liczbą całkowitą od 1 do "
                        f"{_POZYCJA_MAX_TEKST}."
                    )
                bledy.append(BladZmiany(z.id, "pozycja", komunikat))
            nip = _digits(z.nip)
            nazwa = (z.nazwa or "").strip()
            miejscowosc = (z.miejscowosc or "").strip()
            status = (z.status or "").strip()
            if status not in STATUSY:
                bledy.append(BladZmiany(
                    z.id, "status",
                    f"Status „{status}” — dozwolone: {', '.join(STATUSY)}.",
                ))
            klucz = None
            if not nazwa and not nip:
                bledy.append(BladZmiany(z.id, "nazwa", "Podaj NIP lub nazwę kontrahenta."))
            elif not nip and czy_nazwa_zastepcza(nazwa):
                # wiersz z „Dodaj wiersz”, w którym nic nie wpisano (np. Esc,
                # a potem „Zapisz” przy zamknięciu okna) — zająłby numer max+1
                # i przesunął numerację kolejnego eksportu
                bledy.append(BladZmiany(
                    z.id, "nazwa",
                    f"„{nazwa}” to nazwa zastępcza nowego wiersza — wpisz nazwę "
                    "kontrahenta albo NIP; jeśli wiersz dodano przez pomyłkę, usuń go "
                    "(zaznacz wiersz i wciśnij Del).",
                ))
            else:
                klucz = _klucz_seed(nazwa, nip, miejscowosc)
            gotowe.append({
                "id": z.id,
                "stary_klucz": z.stary_klucz,
                "klucz": klucz,
                "pozycja": pozycja,
                "nip": nip,
                "nazwa": nazwa,
                "miejscowosc": miejscowosc,
                "status": status,
                "nowa_pozycja": stary is None or pozycja != stary.pozycja,
                "nowy_klucz": stary is None or klucz != stary.klucz,
            })

        # stan końcowy: wpisy bez zmian + wpisy po zmianach (bez usuniętych)
        koncowe: list[dict] = [
            {
                "id": None, "klucz": w.klucz, "pozycja": w.pozycja, "nip": w.nip,
                "nazwa": w.nazwa, "status": w.status, "nowa_pozycja": False,
                "nowy_klucz": False,
            }
            for k, w in obecne.items()
            if k not in usuniete and k not in zmieniane_klucze
        ] + gotowe

        po_pozycji: dict[int, list[dict]] = {}
        po_kluczu: dict[str, list[dict]] = {}
        for e in koncowe:
            if e["pozycja"] is not None:
                po_pozycji.setdefault(e["pozycja"], []).append(e)
            if e["klucz"] is not None:
                po_kluczu.setdefault(e["klucz"], []).append(e)

        for pozycja, grupa in po_pozycji.items():
            if len(grupa) < 2:
                continue
            for e in grupa:
                if e["id"] is None or not e["nowa_pozycja"]:
                    continue
                inne = ", ".join(
                    opis(x["nazwa"], x["nip"], x["klucz"], None) for x in grupa if x is not e
                )
                bledy.append(BladZmiany(
                    e["id"], "pozycja", f"Pozycja {pozycja} jest już przypisana do: {inne}.",
                ))

        for klucz, grupa in po_kluczu.items():
            if len(grupa) < 2:
                continue
            for e in grupa:
                if e["id"] is None or not e["nowy_klucz"]:
                    continue
                inne = ", ".join(
                    opis(x["nazwa"], x["nip"], x["klucz"], x["pozycja"])
                    for x in grupa if x is not e
                )
                if e["nip"]:
                    bledy.append(BladZmiany(
                        e["id"], "nip", f"Kontrahent z NIP {e['nip']} już jest w rejestrze: {inne}.",
                    ))
                else:
                    bledy.append(BladZmiany(
                        e["id"], "nazwa",
                        f"Kontrahent bez NIP o tej nazwie i miejscowości już jest w rejestrze: {inne}.",
                    ))

        # aliasy po zapisie + (6) klucz nowego/zmienionego wiersza równy
        # kluczowi ALIASU: kanoniczny() zawsze wybierze alias, więc taki wiersz
        # byłby martwy (faktury szłyby dalej na kartę celu). Odrzucamy — poza
        # statusem duplikat, dla którego klucz-alias to stan zamierzony (karta
        # nadal w FK, numer zajęty, faktury na kartę celu scalenia).
        rename = {
            g["stary_klucz"]: g["klucz"]
            for g in gotowe
            if g["stary_klucz"] is not None and g["klucz"] is not None
            and g["klucz"] != g["stary_klucz"]
        }
        statusy_koncowe = {e["klucz"]: e["status"] for e in koncowe if e["klucz"] is not None}
        aliasy_koncowe = self._aliasy_po_zmianach(aliasy, rename, usuniete, statusy_koncowe)
        wpis_koncowy = {e["klucz"]: e for e in koncowe if e["klucz"] is not None}
        for g in gotowe:
            if g["klucz"] is None or not g["nowy_klucz"] or g["status"] == "duplikat":
                continue
            cel = aliasy_koncowe.get(g["klucz"])
            if cel is None:
                continue
            w = wpis_koncowy.get(cel)
            karta = opis(w["nazwa"], w["nip"], cel, w["pozycja"]) if w else f"„{cel}”"
            if g["nip"]:
                komunikat = (
                    f"Faktury z NIP {g['nip']} trafiają na kartę {karta} (scalenie albo "
                    "wcześniejsza zmiana NIP-u tej karty) — wiersz z tym NIP-em nie byłby "
                    "używany. Sprawdź NIP; jeśli to ta sama firma, popraw dane w wierszu "
                    "tej karty zamiast dodawać nowy."
                )
                bledy.append(BladZmiany(g["id"], "nip", komunikat))
            else:
                komunikat = (
                    f"Faktury kontrahenta o tej nazwie i miejscowości trafiają na kartę "
                    f"{karta} (scalenie albo wcześniejsza zmiana nazwy tej karty) — ten "
                    "wiersz nie byłby używany. Zmień nazwę/miejscowość; jeśli to ta sama "
                    "firma, popraw dane w wierszu tej karty zamiast dodawać nowy."
                )
                bledy.append(BladZmiany(g["id"], "nazwa", komunikat))

        if bledy:
            raise ZmianyOdrzucone(bledy)
        return gotowe, aliasy_koncowe

    def _zastosuj_zmiany(
        self, profil_nip: str, gotowe: list[dict], usuniete: set[str]
    ) -> dict[str, str]:
        """Zapis zwalidowanego zestawu (tabela numery) — wołane wewnątrz
        transakcji zapisz_zmiany (bez commit/rollback tutaj). Aliasy zapisuje
        zapisz_zmiany (wyliczone w walidacji — _aliasy_po_zmianach)."""
        # 1) usunięcia — wpis numeru (aliasy: kaskada w _aliasy_po_zmianach)
        for klucz in usuniete:
            self._conn.execute(
                "DELETE FROM numery WHERE profil_nip=? AND klucz=?", (profil_nip, klucz)
            )

        istniejace = [g for g in gotowe if g["stary_klucz"] is not None]
        # 2) wszystkie zmieniane wpisy na tymczasowe ujemne pozycje — unikalny
        # indeks (profil_nip, pozycja) nie może zablokować zamiany numerów
        row = self._conn.execute(
            "SELECT MIN(pozycja) FROM numery WHERE profil_nip=?", (profil_nip,)
        ).fetchone()
        temp = min(0, row[0] or 0) - 1
        for g in istniejace:
            self._conn.execute(
                "UPDATE numery SET pozycja=? WHERE profil_nip=? AND klucz=?",
                (temp, profil_nip, g["stary_klucz"]),
            )
            temp -= 1

        # 3) zmiany klucza: najpierw usuń WSZYSTKIE stare wiersze (łańcuch
        # A: X->Y, B: Y->Z nie może zderzyć się na kluczu Y), zachowując added_at
        rename = {
            g["stary_klucz"]: g["klucz"] for g in istniejace if g["klucz"] != g["stary_klucz"]
        }
        added_at: dict[str, str] = {}
        for stary in rename:
            wiersz = self._conn.execute(
                "SELECT added_at FROM numery WHERE profil_nip=? AND klucz=?", (profil_nip, stary)
            ).fetchone()
            added_at[stary] = wiersz[0] if wiersz and wiersz[0] else _now()
            self._conn.execute(
                "DELETE FROM numery WHERE profil_nip=? AND klucz=?", (profil_nip, stary)
            )

        # 4) wpisy bez zmiany klucza — UPDATE wszystkich pól
        for g in istniejace:
            if g["stary_klucz"] in rename:
                continue
            self._conn.execute(
                "UPDATE numery SET pozycja=?, nazwa=?, nip=?, miejscowosc=?, status=? "
                "WHERE profil_nip=? AND klucz=?",
                (g["pozycja"], g["nazwa"], g["nip"], g["miejscowosc"], g["status"],
                 profil_nip, g["stary_klucz"]),
            )

        # 5) wpisy pod nowym kluczem i zupełnie nowe wpisy
        for g in gotowe:
            if g["stary_klucz"] is not None and g["stary_klucz"] not in rename:
                continue
            kiedy = added_at.get(g["stary_klucz"], _now()) if g["stary_klucz"] else _now()
            self._conn.execute(
                "INSERT INTO numery(profil_nip, klucz, pozycja, nazwa, nip, miejscowosc, "
                "status, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (profil_nip, g["klucz"], g["pozycja"], g["nazwa"], g["nip"], g["miejscowosc"],
                 g["status"], kiedy),
            )

        return {g["id"]: g["klucz"] for g in gotowe}

    def utworz_wpis(
        self,
        profil_nip: str,
        *,
        pozycja: int | None = None,
        nip: str = "",
        nazwa: str = "",
        miejscowosc: str = "",
        status: str = "manual",
    ) -> tuple[str, int]:
        """Nowy wpis dodany ręcznie w rejestrze-arkuszu. Bez podanej pozycji bierze
        kolejny numer (max+1, jak przydziel()). Zwraca (klucz, pozycja)."""
        nip = _digits(nip)
        nazwa = (nazwa or "").strip()
        miejscowosc = (miejscowosc or "").strip()
        if not nazwa and not nip:
            raise ValueError("Podaj NIP lub nazwę")
        klucz = _klucz_seed(nazwa, nip, miejscowosc)

        if pozycja is None:
            pozycja = self.nastepny_numer(profil_nip)
        elif not isinstance(pozycja, int) or pozycja < 1:
            raise ValueError("Pozycja musi być liczbą całkowitą >= 1")

        istniejacy = self._conn.execute(
            "SELECT pozycja FROM numery WHERE profil_nip=? AND klucz=?",
            (profil_nip, klucz),
        ).fetchone()
        if istniejacy is not None:
            raise ValueError(
                f"Kontrahent o tym NIP/kluczu już istnieje (poz. {istniejacy[0]})."
            )
        konflikt = self._conn.execute(
            "SELECT klucz FROM numery WHERE profil_nip=? AND pozycja=?",
            (profil_nip, pozycja),
        ).fetchone()
        if konflikt is not None:
            raise ValueError(
                f"Pozycja {pozycja} jest już zajęta przez kontrahenta o kluczu "
                f"'{konflikt[0]}'."
            )

        self._conn.execute(
            "INSERT INTO numery(profil_nip, klucz, pozycja, nazwa, nip, miejscowosc, "
            "status, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (profil_nip, klucz, pozycja, nazwa, nip, miejscowosc, status, _now()),
        )
        self._conn.commit()
        return klucz, pozycja

    def wyczysc_profil(self, profil_nip: str) -> int:
        """Procedura naprawcza po eksporcie bez seedu: rejestr zdążył przydzielić
        numery 'auto' rozjeżdżające się z kartoteką FK. Usuwa WSZYSTKIE wpisy
        numerów i aliasy danego profilu (np. przed ponownym importem kartoteki).
        Zwraca liczbę usuniętych wpisów numery."""
        kursor = self._conn.execute(
            "DELETE FROM numery WHERE profil_nip=?", (profil_nip,)
        )
        usuniete = kursor.rowcount if kursor.rowcount and kursor.rowcount > 0 else 0
        self._conn.execute("DELETE FROM aliasy WHERE profil_nip=?", (profil_nip,))
        self._conn.commit()
        return usuniete

    def usun(self, profil_nip: str, klucz: str) -> int:
        """Usuwa wpis numeru oraz kaskadowo wiszące aliasy (jako cel scalenia lub
        jako alias) — zwraca liczbę usuniętych aliasów."""
        self._conn.execute(
            "DELETE FROM numery WHERE profil_nip=? AND klucz=?", (profil_nip, klucz)
        )
        kursor = self._conn.execute(
            "DELETE FROM aliasy WHERE profil_nip=? AND (klucz_kanoniczny=? OR klucz_alias=?)",
            (profil_nip, klucz, klucz),
        )
        usuniete = kursor.rowcount if kursor.rowcount and kursor.rowcount > 0 else 0
        self._conn.commit()
        return usuniete

    # ------------------------------------------------------------------ #
    # aliasy (scalanie duplikatów)

    def dodaj_alias(
        self, profil_nip: str, klucz_alias: str, klucz_kanoniczny: str, zwolnij_numer: bool
    ) -> None:
        # rozwiąż cel do punktu stałego PRZED zapisem — tabela aliasy ma nigdy
        # nie zawierać łańcuchów (patrz kanoniczny())
        kanon_final = self.kanoniczny(profil_nip, klucz_kanoniczny)
        if kanon_final == klucz_alias:
            raise ValueError("Scalenie utworzyłoby cykl aliasów")
        self._conn.execute(
            "INSERT OR REPLACE INTO aliasy(profil_nip, klucz_alias, klucz_kanoniczny) "
            "VALUES (?, ?, ?)",
            (profil_nip, klucz_alias, kanon_final),
        )
        # spłaszczenie: istniejące aliasy wskazujące dotąd na klucz_alias
        # przepinamy wprost na nowy kanoniczny, żeby nie powstał łańcuch
        self._conn.execute(
            "UPDATE aliasy SET klucz_kanoniczny=? WHERE profil_nip=? AND klucz_kanoniczny=?",
            (kanon_final, profil_nip, klucz_alias),
        )
        if zwolnij_numer:
            # karta-duplikat USUNIĘTA w FK -> numer wraca do puli
            self._conn.execute(
                "DELETE FROM numery WHERE profil_nip=? AND klucz=?", (profil_nip, klucz_alias)
            )
        else:
            # karta zostaje w FK -> numer nadal zajęty, wpis nieemitowany
            self._conn.execute(
                "UPDATE numery SET status='duplikat' WHERE profil_nip=? AND klucz=?",
                (profil_nip, klucz_alias),
            )
        self._conn.commit()

    def usun_alias(self, profil_nip: str, klucz_alias: str) -> None:
        self._conn.execute(
            "DELETE FROM aliasy WHERE profil_nip=? AND klucz_alias=?", (profil_nip, klucz_alias)
        )
        self._conn.commit()

    def aliasy(self, profil_nip: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT klucz_alias, klucz_kanoniczny FROM aliasy WHERE profil_nip=?",
            (profil_nip,),
        ).fetchall()
        return {alias: kanon for alias, kanon in rows}

    # ------------------------------------------------------------------ #
    # przegląd / dopasowanie

    def lista(self, profil_nip: str) -> list[WpisNumeru]:
        rows = self._conn.execute(
            "SELECT klucz, pozycja, nazwa, nip, miejscowosc, status FROM numery "
            "WHERE profil_nip=? ORDER BY pozycja",
            (profil_nip,),
        ).fetchall()
        return [WpisNumeru(*row) for row in rows]

    def podobni(
        self, profil_nip: str, nazwa: str, prog: float = 0.87
    ) -> list[tuple[WpisNumeru, float]]:
        """Dopasowania fuzzy po nazwie. Pełna nazwa (lub jej tokeny w innej
        kolejności) równa -> 1.0. W przeciwnym razie porównanie po RDZENIU (bez
        formy prawnej/generycznych słów) — dla krótkich rdzeni (<6 znaków) wymaga
        równości, dla dłuższych liczy SequenceMatcher.ratio() >= prog. Chroni to
        przed fałszywymi trafieniami typu "ABC"/"ABD Sp. z o.o." czy "PPHU Max"/
        "PPHU Mix", gdzie wspólny, długi dopisek zawyżałby ratio całej nazwy."""
        norm_a = _norm(nazwa)
        wyniki: list[tuple[WpisNumeru, float]] = []
        if not norm_a:
            return wyniki
        tokeny_a = sorted(norm_a.split())
        rdzen_a = _rdzen(nazwa)
        for wpis in self.lista(profil_nip):
            norm_b = _norm(wpis.nazwa)
            if not norm_b:
                continue
            if norm_a == norm_b:
                ratio = 1.0
            elif tokeny_a == sorted(norm_b.split()):
                ratio = 1.0
            else:
                rdzen_b = _rdzen(wpis.nazwa)
                if not rdzen_a or not rdzen_b:
                    continue
                if min(len(rdzen_a), len(rdzen_b)) < 6:
                    if rdzen_a != rdzen_b:
                        continue
                    ratio = 1.0
                else:
                    ratio = SequenceMatcher(None, rdzen_a, rdzen_b).ratio()
                    if ratio < prog:
                        continue
            wyniki.append((wpis, ratio))
        wyniki.sort(key=lambda t: t[1], reverse=True)
        return wyniki

    # ------------------------------------------------------------------ #
    # import kartoteki FK / eksport

    def importuj_kartoteke(self, profil_nip: str, sciezka: Path) -> tuple[int, list[str]]:
        sciezka = Path(sciezka)
        wiersze = self._wczytaj_wiersze(sciezka)
        dodane = 0
        ostrzezenia: list[str] = []
        for i, wiersz in enumerate(wiersze, start=2):  # wiersz 1 = nagłówek
            wynik, blad = _parsuj_wiersz_kartoteki(wiersz, i)
            if wynik is None:
                ostrzezenia.append(blad)
                continue
            pozycja, nazwa, nip, miejscowosc = wynik

            klucz = _klucz_seed(nazwa, nip, miejscowosc)

            if self.pozycja(profil_nip, klucz) is not None:
                ostrzezenia.append(
                    f"Wiersz {i}: klucz {klucz} już istnieje w rejestrze — pominięto"
                )
                continue
            zajeta = self._conn.execute(
                "SELECT klucz FROM numery WHERE profil_nip=? AND pozycja=?",
                (profil_nip, pozycja),
            ).fetchone()
            if zajeta is not None:
                ostrzezenia.append(
                    f"Wiersz {i}: pozycja {pozycja} już zajęta przez {zajeta[0]} — pominięto"
                )
                continue

            if not nip:
                ostrzezenia.append(
                    f"Wiersz {i} '{nazwa}': brak NIP — klucz przybliżony; po pierwszym "
                    f"eksporcie sprawdź w rejestrze, czy nie powstał duplikat, i w razie "
                    f"potrzeby scal"
                )

            self._conn.execute(
                "INSERT INTO numery(profil_nip, klucz, pozycja, nazwa, nip, miejscowosc, "
                "status, added_at) VALUES (?, ?, ?, ?, ?, ?, 'seed', ?)",
                (profil_nip, klucz, pozycja, nazwa, nip, miejscowosc, _now()),
            )
            dodane += 1
        self._conn.commit()
        return dodane, ostrzezenia

    def synchronizuj_z_kartoteka(
        self, profil_nip: str, sciezka: Path, wymus: bool = False
    ) -> RaportSync:
        """Synchronizuje rejestr ze świeżym eksportem kartoteki z FK — kartoteka
        jest tu ŹRÓDŁEM PRAWDY (użytkownik zakłada karty też ręcznie w FK, z
        pominięciem tego eksportu, więc rejestr z czasem się rozjeżdża od
        rzeczywistego stanu). Wczytuje plik tym samym parserem co
        importuj_kartoteke i dla każdego klucza z kartoteki:
        - brak w rejestrze -> dodaje wpis (status 'seed');
        - ta sama pozycja co w rejestrze -> uzupełnia TYLKO puste pola (nazwa/
          nip/miejscowość) — nie nadpisuje danych już wypełnionych; wyjątek:
          nazwę kontrahenta z NIP-em zawsze bierze z FK (eksport wysyła ją
          do Symfonii — musi być równa nazwie karty);
        - inna pozycja -> NADPISUJE pozycję wartością z FK i zgłasza w
          rozjazdy (kolizje z pozycjami już zajętymi w rejestrze — także przez
          wpisy nieobecne w kartotece — rozwiązywane przez tymczasowe ujemne
          pozycje, w jednej transakcji, więc unikalny indeks
          (profil_nip, pozycja) nigdy nie jest chwilowo naruszony).

        WYJĄTEK — wpisy "manual" (numer ustawiony ręcznie przez księgową po
        sprawdzeniu w Symfonii; rejestr jest dla nich źródłem prawdy): nie są
        zmieniane w ŻADEN sposób (pozycja, puste pola, ewakuacja); różnica
        pozycji trafia do pominiete_reczne. Klucz z kartoteki, którego pozycja
        FK jest zajęta przez taki wpis (albo przez wpis przez to zablokowany),
        nie jest dodawany/przesuwany — trafia do zablokowane.

        Wpisy obecne w rejestrze, ale NIEobecne w kartotece FK, NIE są usuwane
        (mogą to być karty-duplikaty scalone aliasem, albo świeżo przydzielone
        i jeszcze niewyeksportowane do FK) — trafiają do brak_w_fk. Gdy taki
        wpis akurat blokuje pozycję potrzebną kluczowi z kartoteki, zostaje
        PRZESUNIĘTY (nie usunięty) na pierwszą wolną pozycję po pozostałych
        zmianach, z ostrzeżeniem. Aliasy pozostają nietknięte. Cała operacja
        jest transakcyjna: błąd w trakcie -> rollback, rejestr wraca do stanu
        sprzed wywołania. Idempotentna: ponowne uruchomienie na tym samym
        pliku nic więcej nie zmienia.

        BEZPIECZNIK: przed jakąkolwiek zmianą sprawdzana jest zgodność źródła
        z profilem (patrz _sprawdz_zgodnosc_z_fk) — niezgodna kartoteka podnosi
        SynchronizacjaOdrzucona bez dotknięcia rejestru. wymus=True świadomie
        pomija tę kontrolę (GUI na razie z tego nie korzysta)."""
        sciezka = Path(sciezka)
        wiersze = self._wczytaj_wiersze(sciezka)
        ostrzezenia: list[str] = []

        # 1) parsuj wiersze FK, z ochroną przed zduplikowanym kluczem/pozycją
        # WEWNĄTRZ samego pliku (błąd źródła — nie powinien się zdarzyć w
        # prawdziwym eksporcie kartoteki, ale nie może wywalić synchronizacji)
        fk: dict[str, tuple[int, str, str, str]] = {}
        pozycje_fk: dict[int, str] = {}
        skroty: dict[str, str] = {}
        for i, wiersz in enumerate(wiersze, start=2):
            wynik, blad = _parsuj_wiersz_kartoteki(wiersz, i)
            if wynik is None:
                ostrzezenia.append(blad)
                continue
            pozycja, nazwa, nip, miejscowosc = wynik
            klucz = _klucz_seed(nazwa, nip, miejscowosc)
            if klucz in fk:
                ostrzezenia.append(
                    f"Wiersz {i}: klucz {klucz} zduplikowany w pliku kartoteki — pominięto"
                )
                continue
            if pozycja in pozycje_fk:
                ostrzezenia.append(
                    f"Wiersz {i}: pozycja {pozycja} zduplikowana w pliku kartoteki "
                    f"(już użyta przez {pozycje_fk[pozycja]}) — pominięto"
                )
                continue
            fk[klucz] = (pozycja, nazwa, nip, miejscowosc)
            pozycje_fk[pozycja] = klucz
            if "kod" in wiersz:   # plik bez kolumny Kod — kodów nie ruszamy
                skroty[klucz] = (wiersz.get("kod") or "").strip()

        return self._synchronizuj_z_fk(
            profil_nip, fk, ostrzezenia, wymus=wymus, skroty=skroty
        )

    def synchronizuj_z_sql(self, profil_nip: str, baza: str, wymus: bool = False) -> RaportSync:
        """Odpowiednik synchronizuj_z_kartoteka, ale bez pośredniego pliku
        eksportu — czyta kartotekę FK.fk_kontrahenci wprost z żywej bazy
        Symfonii (SQL, tylko odczyt — patrz app.symfonia_sql.czytaj_kartoteke)
        dla wskazanej bazy firmowej. Reguły synchronizacji (dodane/
        zaktualizowane/rozjazdy/brak_w_fk, ochrona przed zduplikowanym
        kluczem/pozycją w źródle, bezpiecznik zgodności — wymus) identyczne
        jak w synchronizuj_z_kartoteka — patrz jej docstring po pełny opis.

        NIP z SQL (app.symfonia_sql.czytaj_kartoteke) jest tylko .strip()-owany
        (może więc zawierać prefiks 'PL', spacje/myślniki w zagranicznym VAT-UE
        itp.) — tu, tak samo jak _parsuj_wiersz_kartoteki robi to dla ścieżki
        plikowej, przepuszczamy go przez _digits PRZED _klucz_seed, żeby klucz
        wyszedł identyczny jak z plikowego seedu/emisji hmffk (bez tego wpisy
        z SQL dostawały klucze z surowym NIP-em, np. 'PL9100010037', rozjeżdżające
        się z kluczem kanonicznym '9100010037' i duplikujące karty w FK)."""
        wiersze = _czytaj_kartoteke_sql(baza)
        ostrzezenia: list[str] = []

        fk: dict[str, tuple[int, str, str, str]] = {}
        pozycje_fk: dict[int, str] = {}
        skroty: dict[str, str] = {}
        for w in wiersze:
            pozycja, nazwa, miejscowosc = w["pozycja"], w["nazwa"], w["miejscowosc"]
            nip = _digits(w["nip"])
            klucz = _klucz_seed(nazwa, nip, miejscowosc)
            if klucz in fk:
                ostrzezenia.append(
                    f"Pozycja {pozycja}: klucz {klucz} zduplikowany w kartotece SQL — pominięto"
                )
                continue
            if pozycja in pozycje_fk:
                ostrzezenia.append(
                    f"Pozycja {pozycja}: zduplikowana w kartotece SQL "
                    f"(już użyta przez {pozycje_fk[pozycja]}) — pominięto"
                )
                continue
            fk[klucz] = (pozycja, nazwa, nip, miejscowosc)
            pozycje_fk[pozycja] = klucz
            skroty[klucz] = w["kod"]

        return self._synchronizuj_z_fk(
            profil_nip, fk, ostrzezenia, wymus=wymus, skroty=skroty
        )

    def _sprawdz_zgodnosc_z_fk(
        self,
        profil_nip: str,
        obecne: dict[str, WpisNumeru],
        fk: dict[str, tuple[int, str, str, str]],
    ) -> None:
        """BEZPIECZNIK przed pomyłkowym wskazaniem kartoteki/bazy INNEJ firmy
        (patrz SynchronizacjaOdrzucona) — wołane jako pierwszy krok
        _synchronizuj_z_fk, przed jakąkolwiek zmianą w rejestrze.

        Kontrola włącza się tylko, gdy profil ma już >= 20 wpisów z NIP-em —
        profil pusty albo z garstką wpisów to normalny seed startowy, dla
        którego "niska zgodność" nie ma sensu (nie ma z czym porównywać).
        Przy włączonej kontroli liczymy, jaki odsetek NIP-ów JUŻ w rejestrze
        (po _digits — bez PL/spacji/myślników) występuje wśród NIP-ów w
        nadchodzącym źródle (fk) — też po _digits, obronnie i idempotentnie,
        niezależnie od tego, czy źródło (plik albo synchronizuj_z_sql) dostarczyło
        NIP już samymi cyframi.
        Odsetek < 60% -> kartoteka nie pasuje do tego profilu -> odrzucamy
        całą synchronizację, zanim cokolwiek zostanie zapisane."""
        nipy_profilu = {d for w in obecne.values() if (d := _digits(w.nip))}
        if len(nipy_profilu) < _MIN_WPISOW_NIP_DO_KONTROLI:
            return
        nipy_zrodla = {d for (_, _, nip, _) in fk.values() if (d := _digits(nip))}
        wspolne = nipy_profilu & nipy_zrodla
        liczba_wspolnych = len(wspolne)
        liczba_profilu = len(nipy_profilu)
        odsetek = liczba_wspolnych / liczba_profilu
        if odsetek < _PROG_ZGODNOSCI_SYNC:
            procent = round(odsetek * 100)
            raise SynchronizacjaOdrzucona(
                f"Kartoteka nie pasuje do firmy: tylko {liczba_wspolnych} z "
                f"{liczba_profilu} kontrahentów profilu występuje w wybranym źródle "
                f"({procent}%). To wygląda na kartotekę INNEJ firmy — sprawdź wybraną "
                f"bazę/plik. Jeśli podmiana jest zamierzona, najpierw użyj 'Wyczyść "
                f"rejestr profilu'."
            )

    def _synchronizuj_z_fk(
        self,
        profil_nip: str,
        fk: dict[str, tuple[int, str, str, str]],
        ostrzezenia: list[str],
        wymus: bool = False,
        skroty: dict[str, str] | None = None,
    ) -> RaportSync:
        # skroty: klucz -> kod karty w FK ('' = karta bez kodu); klucza brak
        # w słowniku = źródło nie niesie kodów, zapamiętany kod zostaje
        """Rdzeń synchronizacji rejestru z kartoteką FK już sparsowaną do
        słownika klucz -> (pozycja, nazwa, nip, miejscowosc) — wspólny dla
        synchronizuj_z_kartoteka (plik) i synchronizuj_z_sql (żywa baza).
        `ostrzezenia` to lista ostrzeżeń zebranych już na etapie parsowania
        źródła (wywołujący przekazuje ją tutaj, funkcja dopisuje własne).
        Reguły — patrz docstring synchronizuj_z_kartoteka.

        Zanim cokolwiek zostanie zmienione, wołany jest bezpiecznik
        _sprawdz_zgodnosc_z_fk (patrz tam) — chyba że wymus=True świadomie go
        pomija. Odrzucenie (SynchronizacjaOdrzucona) nie modyfikuje rejestru
        w ŻADEN sposób."""
        # 2) stan rejestru PRZED zmianami
        obecne = {w.klucz: w for w in self.lista(profil_nip)}
        if not wymus:
            self._sprawdz_zgodnosc_z_fk(profil_nip, obecne, fk)
        zajete_pozycje = {w.pozycja: w.klucz for w in obecne.values()}

        dodane_kandydaci: dict[str, tuple[int, str, str, str]] = {}
        fill_wiersze: list[tuple[str, str, str, str]] = []
        reposition: dict[str, tuple[int, str, str, str]] = {}
        pominiete_reczne: list[tuple[str, int, int]] = []
        zablokowane: list[tuple[str, int, str]] = []

        # REGUŁA "manual": wpis ze statusem manual to numer ustawiony ręcznie
        # przez księgową w oknie Kontrahenci… (po sprawdzeniu w Symfonii) —
        # rejestr jest dla niego źródłem prawdy, więc synchronizacja NIE zmienia
        # go w żaden sposób (ani pozycji, ani pustych pól, ani ewakuacji);
        # różnica pozycji trafia do raportu (pominiete_reczne)
        reczne = {k for k, w in obecne.items() if w.status == "manual"}

        for klucz, (pozycja_fk, nazwa, nip, miejscowosc) in fk.items():
            wpis = obecne.get(klucz)
            if wpis is None:
                dodane_kandydaci[klucz] = (pozycja_fk, nazwa, nip, miejscowosc)
            elif klucz in reczne:
                if wpis.pozycja != pozycja_fk:
                    pominiete_reczne.append((klucz, wpis.pozycja, pozycja_fk))
            elif wpis.pozycja == pozycja_fk:
                fill_wiersze.append((klucz, nazwa, nip, miejscowosc))
            else:
                reposition[klucz] = (pozycja_fk, nazwa, nip, miejscowosc)

        # pozycje, które na pewno zostają na miejscu: wpisy manual (w kartotece
        # albo nie) i wpisy już na właściwej pozycji. Klucz z kartoteki, którego
        # pozycja FK jest zajęta przez taki wpis, NIE może jej dostać (nie
        # dodajemy go / nie przesuwamy) — a jeśli sam jest w rejestrze, zostaje
        # na swojej obecnej pozycji i blokuje ją z kolei dla innych (stąd pętla
        # do punktu stałego zamiast jednego przejścia)
        stale: dict[int, str] = {obecne[k].pozycja: k for k in reczne}
        stale.update({obecne[k].pozycja: k for k, _, _, _ in fill_wiersze})
        zmiana = True
        while zmiana:
            zmiana = False
            for klucz, (pozycja_fk, _, _, _) in list(dodane_kandydaci.items()) + list(
                reposition.items()
            ):
                zajmujacy = stale.get(pozycja_fk)
                if zajmujacy is None or zajmujacy == klucz:
                    continue
                zablokowane.append((klucz, pozycja_fk, zajmujacy))
                if klucz in reposition:
                    del reposition[klucz]
                    stale[obecne[klucz].pozycja] = klucz
                else:
                    del dodane_kandydaci[klucz]
                zmiana = True

        dodane_wiersze: list[tuple[str, int, str, str, str]] = [
            (klucz, pozycja, nazwa, nip, miejscowosc)
            for klucz, (pozycja, nazwa, nip, miejscowosc) in dodane_kandydaci.items()
        ]
        rozjazdy: list[tuple[str, int, int]] = [
            (klucz, obecne[klucz].pozycja, dane[0]) for klucz, dane in reposition.items()
        ]

        brak_w_fk_klucze = [k for k in obecne if k not in fk]

        try:
            # 3) kolizje: klucze, których OBECNA pozycja jest celem (dla
            # dodania albo repozycjonowania) innego klucza, a same nie są
            # repozycjonowane -> trzeba je ewakuować, żeby nie zablokowały
            # zapisu (obejmuje też wpisy nieobecne w kartotece — brak_w_fk)
            cele = {p for _, p, _, _, _ in dodane_wiersze} | {
                p for p, _, _, _ in reposition.values()
            }
            do_ewakuacji = [
                zajmujacy
                for pozycja_docelowa in cele
                if (zajmujacy := zajete_pozycje.get(pozycja_docelowa)) is not None
                and zajmujacy not in reposition
                # obrona w głąb: cele zajęte przez wpisy manual/stałe zostały
                # już wyłączone powyżej — takiego wpisu nigdy nie przesuwamy
                and zajmujacy not in reczne
            ]
            if do_ewakuacji:
                ostrzezenia.append(
                    "Pozycje zajęte w rejestrze przez wpisy nieobecne w kartotece FK "
                    "zostały przesunięte (nie usunięte), żeby zrobić miejsce kluczom "
                    "z kartoteki: " + ", ".join(do_ewakuacji)
                )

            # tymczasowe ujemne pozycje dla WSZYSTKICH przenoszonych — usuwa
            # ryzyko chwilowego konfliktu (profil_nip, pozycja) bez względu na
            # kolejność (obsługuje też zamianę miejscami A<->B)
            temp = -1
            for klucz in list(reposition.keys()) + do_ewakuacji:
                self._conn.execute(
                    "UPDATE numery SET pozycja=? WHERE profil_nip=? AND klucz=?",
                    (temp, profil_nip, klucz),
                )
                temp -= 1

            # 4) rozjazdy: nadpisz pozycję wartością z FK + uzupełnij puste pola
            for klucz, (nowa_pozycja, nazwa, nip, miejscowosc) in reposition.items():
                wpis = obecne[klucz]
                self._conn.execute(
                    "UPDATE numery SET pozycja=?, nazwa=?, nip=?, miejscowosc=? "
                    "WHERE profil_nip=? AND klucz=?",
                    (
                        nowa_pozycja,
                        _nazwa_po_sync(wpis, nazwa, nip),
                        wpis.nip or nip,
                        wpis.miejscowosc or miejscowosc,
                        profil_nip,
                        klucz,
                    ),
                )

            # 5) nowe wpisy (seed) — pozycja docelowa już wolna (patrz p. 3)
            for klucz, pozycja, nazwa, nip, miejscowosc in dodane_wiersze:
                self._conn.execute(
                    "INSERT INTO numery(profil_nip, klucz, pozycja, nazwa, nip, "
                    "miejscowosc, status, added_at) VALUES (?, ?, ?, ?, ?, ?, 'seed', ?)",
                    (profil_nip, klucz, pozycja, nazwa, nip, miejscowosc, _now()),
                )

            # 6) ewakuowani -> pierwsza naprawdę wolna pozycja PO wszystkich
            # powyższych zmianach (nie usuwamy ich, tylko przenosimy)
            if do_ewakuacji:
                row = self._conn.execute(
                    "SELECT MAX(pozycja) FROM numery WHERE profil_nip=? AND pozycja>0",
                    (profil_nip,),
                ).fetchone()
                nastepna = (row[0] or 0) + 1
                for klucz in do_ewakuacji:
                    self._conn.execute(
                        "UPDATE numery SET pozycja=? WHERE profil_nip=? AND klucz=?",
                        (nastepna, profil_nip, klucz),
                    )
                    nastepna += 1

            policzone: set[str] = set()
            # 7) ta sama pozycja co w FK -> uzupełnij TYLKO puste pola; wyjątek:
            # NAZWA kontrahenta z NIP-em idzie z FK zawsze (eksport bierze ją
            # z rejestru do pliku importu — nazwa z pierwszej faktury, np.
            # z dopiskiem „BDO: …”, albo sprzed poprawki karty w FK dawała
            # w Symfonii błąd dostawcy). Klucz z NIP-u nie zależy od nazwy,
            # więc nadpisanie nie rusza numeracji; wpisy bez NIP-u mają klucz
            # z nazwy FK, więc tu i tak mają ją równą (patrz _klucz_seed)
            zaktualizowane = 0
            for klucz, nazwa, nip, miejscowosc in fill_wiersze:
                wpis = obecne[klucz]
                nowa_nazwa = _nazwa_po_sync(wpis, nazwa, nip)
                nowy_nip = wpis.nip or nip
                nowa_miejscowosc = wpis.miejscowosc or miejscowosc
                if (nowa_nazwa, nowy_nip, nowa_miejscowosc) == (
                    wpis.nazwa, wpis.nip, wpis.miejscowosc
                ):
                    continue  # nic do uzupełnienia (idempotencja)
                self._conn.execute(
                    "UPDATE numery SET nazwa=?, nip=?, miejscowosc=? "
                    "WHERE profil_nip=? AND klucz=?",
                    (nowa_nazwa, nowy_nip, nowa_miejscowosc, profil_nip, klucz),
                )
                zaktualizowane += 1
                policzone.add(klucz)

            # 8) kod karty z FK (także pusty — karta bez kodu) z pozycją karty,
            # z której pochodzi; manual nietknięte, a wpis z inną pozycją niż
            # karta (zablokowany) — też nie (warunek na pozycję)
            for klucz, skrot in (skroty or {}).items():
                if klucz not in fk or klucz in reczne:
                    continue
                poz_fk = fk[klucz][0]
                przed = self._conn.execute(
                    "SELECT skrot, skrot_poz, pozycja FROM numery "
                    "WHERE profil_nip=? AND klucz=?",
                    (profil_nip, klucz),
                ).fetchone()
                if przed is None or przed[2] != poz_fk:
                    continue
                if ((przed[0] or ""), przed[1]) == (skrot.strip(), poz_fk):
                    continue  # idempotencja
                self._conn.execute(
                    "UPDATE numery SET skrot=?, skrot_poz=? WHERE profil_nip=? AND klucz=?",
                    (skrot.strip(), poz_fk, profil_nip, klucz),
                )
                zmieniony_kod = (przed[0] or "") != skrot.strip() or (
                    skrot.strip() and przed[1] != poz_fk
                )
                if (
                    zmieniony_kod and klucz in obecne and klucz not in policzone
                    and klucz not in reposition
                ):
                    zaktualizowane += 1
                    policzone.add(klucz)
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()

        # brak_w_fk raportujemy z pozycją PO ewentualnej ewakuacji (p. 6) —
        # wpis mógł zostać przesunięty, więc stara pozycja byłaby myląca
        brak_w_fk = sorted(
            ((k, self.pozycja(profil_nip, k)) for k in brak_w_fk_klucze),
            key=lambda t: t[1],
        )

        return RaportSync(
            dodane=len(dodane_wiersze),
            zaktualizowane=zaktualizowane,
            rozjazdy=rozjazdy,
            brak_w_fk=brak_w_fk,
            ostrzezenia=ostrzezenia,
            pominiete_reczne=pominiete_reczne,
            zablokowane=zablokowane,
            reczne=len(reczne),
        )

    def _wczytaj_wiersze(self, sciezka: Path) -> list[dict[str, str]]:
        if sciezka.suffix.lower() == ".xlsx":
            return self._wczytaj_xlsx(sciezka)
        return self._wczytaj_csv(sciezka)

    def _wczytaj_xlsx(self, sciezka: Path) -> list[dict[str, str]]:
        """Wczytuje arkusz xlsx z odporną detekcją nagłówka: pierwszy wiersz
        zawierający jednocześnie kolumny 'Pozycja' i 'Nazwa' (patrz
        _czy_wiersz_naglowka) — wiersze przed nim (np. pusty pierwszy wiersz)
        są ignorowane. Kolumny mapowane po nazwach z tego wiersza; kolumny bez
        nazwy w nagłówku są ignorowane (patrz _mapuj_wiersze_danych)."""
        from openpyxl import load_workbook

        wb = load_workbook(sciezka, read_only=True, data_only=True)
        try:
            ws = wb.active
            wiersze = [list(row) for row in ws.iter_rows(values_only=True)]
        finally:
            wb.close()

        limit = min(_MAX_WIERSZY_SZUKAJ_NAGLOWKA, len(wiersze))
        for i in range(limit):
            naglowki = _naglowki_z_wiersza(wiersze[i])
            if _czy_wiersz_naglowka(naglowki):
                return _mapuj_wiersze_danych(wiersze[i + 1 :], naglowki)
        raise _blad_brak_naglowka("arkuszu xlsx", limit)

    def _wczytaj_csv(self, sciezka: Path) -> list[dict[str, str]]:
        """Wczytuje CSV z odporną detekcją nagłówka i separatora: separator
        ([';', ',', '\\t']) jest dobierany na podstawie WIERSZA NAGŁÓWKA (nie
        pierwszego wiersza pliku, który może być śmieciowy — np. eksport z
        Arkuszy Google), a nagłówek to pierwszy wiersz z kolumnami 'Pozycja' i
        'Nazwa' (patrz _czy_wiersz_naglowka). Kodowanie utf-8-sig (BOM) z
        fallbackiem na cp1250, gdyby plik nie był UTF-8."""
        surowe = sciezka.read_bytes()
        try:
            tekst = surowe.decode("utf-8-sig")
        except UnicodeDecodeError:
            tekst = surowe.decode("cp1250")

        sprawdzono = 0
        for separator in _SEPARATORY_CSV:
            wiersze = list(csv.reader(io.StringIO(tekst), delimiter=separator))
            limit = min(_MAX_WIERSZY_SZUKAJ_NAGLOWKA, len(wiersze))
            sprawdzono = max(sprawdzono, limit)
            for i in range(limit):
                naglowki = _naglowki_z_wiersza(wiersze[i])
                if _czy_wiersz_naglowka(naglowki):
                    return _mapuj_wiersze_danych(wiersze[i + 1 :], naglowki)
        raise _blad_brak_naglowka("pliku CSV", sprawdzono)

    def eksportuj_csv(self, profil_nip: str, sciezka: Path) -> None:
        sciezka = Path(sciezka)
        with sciezka.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["Pozycja", "Klucz", "NIP", "Nazwa", "Miejscowosc", "Status"])
            for wpis in self.lista(profil_nip):
                writer.writerow(
                    [wpis.pozycja, wpis.klucz, wpis.nip, wpis.nazwa, wpis.miejscowosc, wpis.status]
                )

    def close(self) -> None:
        self._conn.close()
