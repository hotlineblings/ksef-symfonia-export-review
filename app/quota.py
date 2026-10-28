"""Trwały licznik limitów API KSeF (SQLite w %APPDATA%).

Limity KSeF są rozliczane po stronie serwera per NIP i godzina zegarowa,
NIEZALEŻNIE od naszych uruchomień. Licznik w pamięci procesu nie wystarcza:
dwa eksporty pod rząd widziały świeży limit i wpadały w HTTP 429 z blokadą
~30 min (zaobserwowane 2026-07-08). Stąd zapis znaczników czasu na dysku
+ zapamiętywanie blokad narzuconych przez serwer (Retry-After).

Tu też żyją niedokończone zlecenia paczek eksportu (numer referencyjny + klucz
AES/IV): paczka, której KSeF nie przygotował w czasie jednego eksportu, jest
odbierana w kolejnym zamiast zamawiania nowej. Klucz chroni wyłącznie transfer
faktur, które i tak trafiają na ten dysk; wpis żyje najwyżej 6 h (KSeF
trzyma status eksportu 7 dni) i jest usuwany po odebraniu albo błędzie paczki.
"""

from __future__ import annotations

import base64
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple

from app.config import app_data_dir

# niedokończone zlecenie paczki odbieramy najwyżej przez 6 h od zamówienia
# (zegar komputera — tylko do wygaszania wpisu; KSeF trzyma status 7 dni).
# Zlecenie, którego odbiór znów kończy się timeoutem, jest uznawane za
# zawieszone wcześniej — po ZLECENIE_ZAWIESZONE_PO (ksef_client, zegar KSeF)
PENDING_EXPORT_MAX_AGE_S = 6 * 3600


class ZlecenieEksportu(NamedTuple):
    reference: str
    aes_key: bytes
    iv: bytes
    ordered_at: float              # time.time() komputera — wyłącznie do wygaszania
    ordered_ksef: str | None       # moment zlecenia wg zegara KSeF (nagłówek Date), ISO UTC


class QuotaStore:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path else app_data_dir() / "quota.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS events(nip TEXT, grp TEXT, ts REAL)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events ON events(nip, grp, ts)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS blocks(nip TEXT, grp TEXT, until_ts REAL, "
            "PRIMARY KEY (nip, grp))"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS pending_exports(nip TEXT, zakres TEXT, reference TEXT, "
            "aes_key TEXT, iv TEXT, ordered_at REAL, ordered_ksef TEXT, "
            "PRIMARY KEY (nip, zakres))"
        )
        kolumny = {r[1] for r in self._conn.execute("PRAGMA table_info(pending_exports)")}
        if "ordered_ksef" not in kolumny:
            self._conn.execute("ALTER TABLE pending_exports ADD COLUMN ordered_ksef TEXT")
        self._conn.commit()
        self._prune()

    def record(self, nip: str, group: str) -> None:
        self._conn.execute(
            "INSERT INTO events(nip, grp, ts) VALUES (?, ?, ?)", (nip, group, time.time())
        )
        self._conn.commit()

    def recent(self, nip: str, group: str, window_s: float) -> list[float]:
        rows = self._conn.execute(
            "SELECT ts FROM events WHERE nip=? AND grp=? AND ts>? ORDER BY ts",
            (nip, group, time.time() - window_s),
        ).fetchall()
        return [r[0] for r in rows]

    def set_block(self, nip: str, group: str, until_ts: float) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO blocks(nip, grp, until_ts) VALUES (?, ?, ?)",
            (nip, group, until_ts),
        )
        self._conn.commit()

    def block_remaining(self, nip: str, group: str) -> float:
        row = self._conn.execute(
            "SELECT until_ts FROM blocks WHERE nip=? AND grp=?", (nip, group)
        ).fetchone()
        if row is None:
            return 0.0
        return max(0.0, row[0] - time.time())

    # -- niedokończone zlecenia paczek eksportu ---------------------------------

    def save_pending_export(
        self, nip: str, zakres: str, reference: str, aes_key: bytes, iv: bytes,
        ordered_ksef: str | None = None,
    ) -> None:
        """`zakres` — klucz zlecenia (rodzaj, typ daty, okno od–do);
        `ordered_ksef` — moment zlecenia wg zegara KSeF (ISO UTC) albo None."""
        self._conn.execute(
            "INSERT OR REPLACE INTO pending_exports(nip, zakres, reference, aes_key, iv, "
            "ordered_at, ordered_ksef) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (nip, zakres, reference, base64.b64encode(aes_key).decode(),
             base64.b64encode(iv).decode(), time.time(), ordered_ksef),
        )
        self._conn.commit()

    def pending_export(self, nip: str, zakres: str) -> ZlecenieEksportu | None:
        """Zapamiętane zlecenie albo None; wpis starszy niż
        PENDING_EXPORT_MAX_AGE_S (zegar komputera) jest usuwany."""
        row = self._conn.execute(
            "SELECT reference, aes_key, iv, ordered_at, ordered_ksef FROM pending_exports "
            "WHERE nip=? AND zakres=?", (nip, zakres),
        ).fetchone()
        if row is None:
            return None
        if row[3] < time.time() - PENDING_EXPORT_MAX_AGE_S:
            self.drop_pending_export(nip, zakres)
            return None
        return ZlecenieEksportu(row[0], base64.b64decode(row[1]), base64.b64decode(row[2]),
                                row[3], row[4])

    def drop_pending_export(self, nip: str, zakres: str) -> None:
        self._conn.execute(
            "DELETE FROM pending_exports WHERE nip=? AND zakres=?", (nip, zakres)
        )
        self._conn.commit()

    def _prune(self) -> None:
        cutoff = time.time() - 2 * 3600
        self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        self._conn.execute("DELETE FROM blocks WHERE until_ts < ?", (time.time(),))
        self._conn.execute(
            "DELETE FROM pending_exports WHERE ordered_at < ?",
            (time.time() - PENDING_EXPORT_MAX_AGE_S,),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
