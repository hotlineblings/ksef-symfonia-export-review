"""Rejestr pobranych faktur (dedup) — SQLite z WAL.

Dedup jest ważny funkcjonalnie: ponowny import tego samego dokumentu w Symfonii
(#POWIELONE_NAZWY "1") wywołuje interaktywne pytanie u księgowej.

W tej samej bazie żyją (wszystko per NIP profilu, trwale — przeżywa zamknięcie
aplikacji; patrz `app.exporter`, spóźnione faktury):

- `sync_points` — punkt kontynuacji synchronizacji przyrostowej per (NIP,
  rodzaj Subject1/Subject2): moment w czasie KSeF (data trwałego zapisu, HWM),
  do którego aplikacja sprawdziła już, co wpłynęło do KSeF; `watch_from` =
  pierwszy zapisany punkt (od kiedy aplikacja obserwuje tę firmę i rodzaj —
  dolna granica liczenia czasu oczekiwania dla bezpiecznika 60 dni);
- `export_ranges` — zakresy dat wystawienia, które już wyeksportowano (per NIP
  × rodzaj, wszystkie, także częściowe); miesiąc jest DOMKNIĘTY, gdy jeden
  zakres obejmuje go w całości (`is_covered`) — tylko faktura z miesiąca
  domkniętego, której nie ma w rejestrze, jest „spóźniona”;
- `late_invoices` — spóźnione faktury i zakres eksportu, do którego je
  dołączono (ponowny pełny eksport tego zakresu dokłada je znowu); status
  `do_recznej_obslugi` = spóźniona, której nie dało się przetworzyć (błąd
  mapowania) — trwały ślad, bo punkt kontynuacji ją przeskakuje;
- `pending_new_contractors` — nowi kontrahenci (nadany numer karty), o których
  ostrzeżenie jeszcze nie trafiło do raportu udanego eksportu;
- `legacy_start` — per NIP moment najstarszego eksportu starszej wersji (bez
  zakresów i punktu), zapamiętany przy migracji: od niego startuje pierwsze
  sprawdzenie spóźnionych (N3).
"""

from __future__ import annotations

import calendar
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath

from app.config import app_data_dir

# nazwa pliku zakresu ze starszej wersji: hmffk_<podfolder>_<od>_<do>_<stempel>.TXT
# albo paczka_<sprzedaz|koszt>_<od>_<do>_<stempel>.TXT (patrz app.exporter)
_PLIK_ZAKRESU = re.compile(
    r"^(?P<przed>(?:hmffk|paczka)_.*?)_(?P<od>\d{4}-\d{2}-\d{2})_(?P<do>\d{4}-\d{2}-\d{2})"
    r"_(?P<stempel>\d{8})-\d{6}\.TXT$",
    re.IGNORECASE,
)
ZRODLO_STARSZA_WERSJA = "nazwa pliku (starsza wersja)"

STATUS_W_PLIKU = "w_pliku"
STATUS_DO_RECZNEJ = "do_recznej_obslugi"


@dataclass(frozen=True)
class LateRecord:
    """Spóźniona faktura zapamiętana z zakresem eksportu, do którego ją dołączono."""

    ksef_number: str
    subject_type: str
    invoice_number: str
    issue_date: str
    acquisition_date: str | None
    permanent_storage_date: str | None
    range_from: date
    range_to: date
    status: str
    error: str | None


def _czas(s: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s) if s else None
    except ValueError:
        return None
    return dt if dt is not None and dt.tzinfo is not None else None


def _nazwa_pliku(sciezka: str) -> str:
    # ścieżki z Windows (C:\...) i z innych systemów
    return PureWindowsPath(sciezka).name if "\\" in sciezka else Path(sciezka).name


class Ledger:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path else app_data_dir() / "ledger.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS exports(
                   nip TEXT NOT NULL,
                   ksef_number TEXT NOT NULL,
                   txt_path TEXT NOT NULL,
                   exported_at TEXT NOT NULL,
                   PRIMARY KEY (nip, ksef_number)
               )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS sync_points(
                   nip TEXT NOT NULL,
                   subject_type TEXT NOT NULL,
                   point TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   watch_from TEXT,
                   PRIMARY KEY (nip, subject_type)
               )"""
        )
        kolumny = {r[1] for r in self._conn.execute("PRAGMA table_info(sync_points)")}
        if "watch_from" not in kolumny:
            # baza z wersji bez tej kolumny: najlepsze przybliżenie początku
            # obserwacji to obecny punkt
            self._conn.execute("ALTER TABLE sync_points ADD COLUMN watch_from TEXT")
            self._conn.execute("UPDATE sync_points SET watch_from = point WHERE watch_from IS NULL")
        nowe_zakresy = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='export_ranges'"
        ).fetchone() is None
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS export_ranges(
                   nip TEXT NOT NULL,
                   subject_type TEXT NOT NULL,
                   date_from TEXT NOT NULL,
                   date_to TEXT NOT NULL,
                   recorded_at TEXT NOT NULL,
                   source TEXT NOT NULL,
                   PRIMARY KEY (nip, subject_type, date_from, date_to)
               )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS late_invoices(
                   nip TEXT NOT NULL,
                   ksef_number TEXT NOT NULL,
                   subject_type TEXT NOT NULL,
                   invoice_number TEXT NOT NULL,
                   issue_date TEXT NOT NULL,
                   acquisition_date TEXT,
                   permanent_storage_date TEXT,
                   range_from TEXT NOT NULL,
                   range_to TEXT NOT NULL,
                   status TEXT NOT NULL,
                   error TEXT,
                   recorded_at TEXT NOT NULL,
                   PRIMARY KEY (nip, ksef_number)
               )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS pending_new_contractors(
                   nip TEXT NOT NULL,
                   card_no INTEGER NOT NULL,
                   name TEXT NOT NULL,
                   contractor_nip TEXT NOT NULL,
                   accounts TEXT NOT NULL,
                   recorded_at TEXT NOT NULL,
                   PRIMARY KEY (nip, card_no)
               )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS legacy_start(
                   nip TEXT PRIMARY KEY,
                   oldest_export_at TEXT NOT NULL
               )"""
        )
        if nowe_zakresy:
            self._zakresy_ze_starszej_wersji()
        self._conn.commit()

    @staticmethod
    def _teraz() -> str:
        return datetime.now(timezone.utc).isoformat()

    # -- rejestr pobranych faktur -----------------------------------------------

    def is_done(self, nip: str, ksef_number: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM exports WHERE nip=? AND ksef_number=?",
            (nip, ksef_number),
        ).fetchone()
        return row is not None

    def mark_done(self, nip: str, ksef_number: str, txt_path: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO exports(nip, ksef_number, txt_path, exported_at) "
            "VALUES (?, ?, ?, ?)",
            (nip, ksef_number, txt_path, self._teraz()),
        )
        self._conn.commit()

    def count(self, nip: str) -> int:
        (n,) = self._conn.execute(
            "SELECT COUNT(*) FROM exports WHERE nip=?", (nip,)
        ).fetchone()
        return n

    # -- punkt kontynuacji ------------------------------------------------------

    def sync_point(self, nip: str, subject_type: str) -> str | None:
        """Punkt kontynuacji (znacznik czasu KSeF z offsetem, dokładnie w postaci
        zwróconej przez KSeF) albo None, gdy dla tej pary jeszcze nie sprawdzano."""
        row = self._conn.execute(
            "SELECT point FROM sync_points WHERE nip=? AND subject_type=?",
            (nip, subject_type),
        ).fetchone()
        return row[0] if row else None

    def watch_from(self, nip: str, subject_type: str) -> str | None:
        """Pierwszy zapisany punkt dla (NIP, rodzaj) — od kiedy aplikacja
        obserwuje, co wpływa do KSeF. None = jeszcze nie obserwuje."""
        row = self._conn.execute(
            "SELECT watch_from FROM sync_points WHERE nip=? AND subject_type=?",
            (nip, subject_type),
        ).fetchone()
        return row[0] if row else None

    def set_sync_point(
        self, nip: str, subject_type: str, point: str, *, observed_at: str | None = None,
    ) -> None:
        """Zapisuje punkt. Przy PIERWSZYM zapisie dla (NIP, rodzaj) ustala też
        początek obserwacji: `observed_at` (moment sprawdzenia w czasie KSeF —
        HWM), a gdy nie podano, sam punkt. Punkt bywa cofnięty do wstrzymanej
        faktury, więc nie nadaje się na początek obserwacji."""
        self._conn.execute(
            "INSERT INTO sync_points(nip, subject_type, point, updated_at, watch_from) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(nip, subject_type) DO UPDATE SET point=excluded.point, "
            "updated_at=excluded.updated_at, "
            "watch_from=COALESCE(sync_points.watch_from, excluded.watch_from)",
            (nip, subject_type, point, self._teraz(), observed_at or point),
        )
        self._conn.commit()

    # -- zakresy już wyeksportowane (W2) ---------------------------------------

    def record_range(
        self, nip: str, subject_type: str, date_from: date, date_to: date,
        *, source: str = "eksport",
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO export_ranges(nip, subject_type, date_from, date_to, "
            "recorded_at, source) VALUES (?, ?, ?, ?, ?, ?)",
            (nip, subject_type, date_from.isoformat(), date_to.isoformat(), self._teraz(), source),
        )
        self._conn.commit()

    def is_covered(self, nip: str, subject_type: str, day: date) -> bool:
        """Czy miesiąc daty wystawienia `day` jest DOMKNIĘTY: JEDEN zapisany
        zakres eksportu obejmuje cały miesiąc kalendarzowy (od 1. do ostatniego
        dnia; zakres wielomiesięczny domyka każdy w pełni objęty miesiąc).
        Zakresy „na bieżąco” (01–07.09, 01–15.08…) miesiąca nie domykają, także
        gdy razem go pokrywają — decyzja właściciela 28.09: domknięcie to osobny
        eksport całego miesiąca."""
        pierwszy = day.replace(day=1).isoformat()
        ostatni = day.replace(day=calendar.monthrange(day.year, day.month)[1]).isoformat()
        row = self._conn.execute(
            "SELECT 1 FROM export_ranges WHERE nip=? AND subject_type=? "
            "AND date_from<=? AND date_to>=? LIMIT 1",
            (nip, subject_type, pierwszy, ostatni),
        ).fetchone()
        return row is not None

    def ranges(self, nip: str, subject_type: str) -> list[tuple[date, date]]:
        return [
            (date.fromisoformat(od), date.fromisoformat(do))
            for od, do in self._conn.execute(
                "SELECT date_from, date_to FROM export_ranges WHERE nip=? AND subject_type=? "
                "ORDER BY date_from, date_to",
                (nip, subject_type),
            )
        ]

    def _zakresy_ze_starszej_wersji(self) -> None:
        """Jednorazowo przy pierwszym otwarciu bazy z tabelą zakresów: starsze
        wersje nie zapisywały zakresów, ale nazwy plików w rejestrze pobranych je
        zawierają (hmffk_… / paczka_…_<od>_<do>_<stempel>.TXT). Bez tego okresy
        wyeksportowane przed aktualizacją uchodziłyby za niewyeksportowane i ich
        spóźnione faktury czekałyby na bezpiecznik 60 dni. Pomijane: pliki
        wyszukiwania (_numery, _kontrahenci) i pliki per faktura. Rodzaj: z nazwy
        paczki; w pliku hmffk z numeru KSeF (zaczyna się NIP-em sprzedawcy —
        NIP profilu = sprzedaż)."""
        zakresy: set[tuple[str, str, str, str]] = set()
        najstarszy: dict[str, tuple[datetime, str]] = {}
        for nip, ksef_number, txt_path, exported_at in self._conn.execute(
            "SELECT nip, ksef_number, txt_path, exported_at FROM exports"
        ).fetchall():
            kiedy = _czas(exported_at)
            if kiedy is not None and (nip not in najstarszy or kiedy < najstarszy[nip][0]):
                najstarszy[nip] = (kiedy, exported_at)
            m = _PLIK_ZAKRESU.match(_nazwa_pliku(txt_path or ""))
            if m is None:
                continue
            przed = m.group("przed").lower()
            if przed.endswith("_numery") or przed.endswith("_kontrahenci"):
                continue
            try:
                od, do = date.fromisoformat(m.group("od")), date.fromisoformat(m.group("do"))
                s = m.group("stempel")
                stempel = date(int(s[:4]), int(s[4:6]), int(s[6:]))
            except ValueError:
                continue
            # N2: plik zrobiony w trakcie zakresu (np. 01–30.09 zrobiony 16.09)
            # obejmuje naprawdę tylko dni przed dniem eksportu (stempel = czas
            # lokalny komputera księgowej)
            do = min(do, stempel - timedelta(days=1))
            if od > do:
                continue
            if przed.startswith("paczka_sprzedaz"):
                rodzaj = "Subject1"
            elif przed.startswith("paczka_koszt"):
                rodzaj = "Subject2"
            else:
                rodzaj = "Subject1" if ksef_number.split("-", 1)[0] == nip else "Subject2"
            zakresy.add((nip, rodzaj, od.isoformat(), do.isoformat()))
        teraz = self._teraz()
        self._conn.executemany(
            "INSERT OR IGNORE INTO export_ranges(nip, subject_type, date_from, date_to, "
            "recorded_at, source) VALUES (?, ?, ?, ?, ?, ?)",
            [(*z, teraz, ZRODLO_STARSZA_WERSJA) for z in sorted(zakresy)],
        )
        self._conn.executemany(
            "INSERT OR IGNORE INTO legacy_start(nip, oldest_export_at) VALUES (?, ?)",
            [(nip, surowy) for nip, (_, surowy) in sorted(najstarszy.items())],
        )

    def legacy_start(self, nip: str) -> str | None:
        """Moment najstarszego eksportu starszej wersji dla tej firmy (tak, jak
        zapisała go starsza wersja — UTC z offsetem) albo None."""
        row = self._conn.execute(
            "SELECT oldest_export_at FROM legacy_start WHERE nip=?", (nip,)
        ).fetchone()
        return row[0] if row else None

    def legacy_horizon(self, nip: str, subject_type: str) -> date | None:
        """Pierwszy dzień najwcześniejszego miesiąca objętego zakresami starszej
        wersji (N3) — miesiące wcześniejsze to czas sprzed pracy aplikacji."""
        row = self._conn.execute(
            "SELECT MIN(date_from) FROM export_ranges WHERE nip=? AND subject_type=? "
            "AND source=?",
            (nip, subject_type, ZRODLO_STARSZA_WERSJA),
        ).fetchone()
        return date.fromisoformat(row[0]).replace(day=1) if row and row[0] else None

    # -- spóźnione dołączone do eksportu (W3) + do ręcznej obsługi (9) ----------

    def record_late(
        self, nip: str, subject_type: str, ksef_number: str, *,
        invoice_number: str, issue_date: str,
        acquisition_date: str | None, permanent_storage_date: str | None,
        range_from: date, range_to: date,
        status: str = STATUS_W_PLIKU, error: str | None = None,
    ) -> None:
        """Pierwsze dołączenie wygrywa (zakres zostaje); późniejsze wywołanie może
        tylko zmienić status z „do ręcznej obsługi” na „w pliku”."""
        wiersz = self._conn.execute(
            "SELECT status FROM late_invoices WHERE nip=? AND ksef_number=?",
            (nip, ksef_number),
        ).fetchone()
        if wiersz is None:
            self._conn.execute(
                "INSERT INTO late_invoices(nip, ksef_number, subject_type, invoice_number, "
                "issue_date, acquisition_date, permanent_storage_date, range_from, range_to, "
                "status, error, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (nip, ksef_number, subject_type, invoice_number, issue_date,
                 acquisition_date, permanent_storage_date,
                 range_from.isoformat(), range_to.isoformat(), status, error, self._teraz()),
            )
        elif wiersz[0] == STATUS_DO_RECZNEJ and status == STATUS_W_PLIKU:
            self._conn.execute(
                "UPDATE late_invoices SET status=?, error=NULL WHERE nip=? AND ksef_number=?",
                (STATUS_W_PLIKU, nip, ksef_number),
            )
        self._conn.commit()

    def _late(self, where: str, params: tuple) -> list[LateRecord]:
        return [
            LateRecord(
                ksef_number=r[0], subject_type=r[1], invoice_number=r[2], issue_date=r[3],
                acquisition_date=r[4], permanent_storage_date=r[5],
                range_from=date.fromisoformat(r[6]), range_to=date.fromisoformat(r[7]),
                status=r[8], error=r[9],
            )
            for r in self._conn.execute(
                "SELECT ksef_number, subject_type, invoice_number, issue_date, "
                "acquisition_date, permanent_storage_date, range_from, range_to, status, error "
                f"FROM late_invoices WHERE {where} ORDER BY recorded_at, ksef_number",
                params,
            )
        ]

    def late_within(
        self, nip: str, subject_type: str, date_from: date, date_to: date,
    ) -> list[LateRecord]:
        """Spóźnione dołączone do eksportu, którego zakres mieści się w
        `date_from`–`date_to` (ten sam zakres albo np. domknięcie miesiąca)."""
        return self._late(
            "nip=? AND subject_type=? AND range_from>=? AND range_to<=?",
            (nip, subject_type, date_from.isoformat(), date_to.isoformat()),
        )

    def manual_list(self, nip: str) -> list[LateRecord]:
        """Spóźnione, których nie dało się przetworzyć (do ręcznej obsługi)."""
        return self._late("nip=? AND status=?", (nip, STATUS_DO_RECZNEJ))

    # -- nowi kontrahenci czekający na zgłoszenie (10) -------------------------

    def add_pending_contractors(
        self, nip: str, nowi: list[tuple[str, str, int, str]],
    ) -> None:
        teraz = self._teraz()
        self._conn.executemany(
            "INSERT OR REPLACE INTO pending_new_contractors(nip, card_no, name, "
            "contractor_nip, accounts, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(nip, nr, nazwa, nip_k or "", konta, teraz) for nazwa, nip_k, nr, konta in nowi],
        )
        self._conn.commit()

    def take_pending_contractors(self, nip: str) -> list[tuple[str, str, int, str]]:
        """Zwraca i usuwa czekające ostrzeżenia (w kolejności numerów kart)."""
        wynik = [
            (nazwa, nip_k, nr, konta)
            for nazwa, nip_k, nr, konta in self._conn.execute(
                "SELECT name, contractor_nip, card_no, accounts FROM pending_new_contractors "
                "WHERE nip=? ORDER BY card_no",
                (nip,),
            ).fetchall()
        ]
        self._conn.execute("DELETE FROM pending_new_contractors WHERE nip=?", (nip,))
        self._conn.commit()
        return wynik

    def close(self) -> None:
        self._conn.close()
