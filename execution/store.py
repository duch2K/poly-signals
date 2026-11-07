"""SQLite: signal_id dedup, legs/positions, daily PnL. Survives restarts."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'received'
);
CREATE TABLE IF NOT EXISTS legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    market TEXT NOT NULL,
    token_id TEXT NOT NULL,
    size REAL NOT NULL,
    price REAL NOT NULL,
    state TEXT NOT NULL,          -- unhedged | hedged | unwound | stuck
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pnl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    kind TEXT NOT NULL,           -- realized (unwound leg) | locked (hedged pair)
    amount REAL NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class Store:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    # --- dedup ---
    def claim_signal(self, signal_id: str) -> bool:
        """True - new signal. False - already seen (duplicate)."""
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO signals(signal_id, received_at) VALUES (?, ?)",
                    (signal_id, _now()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def set_signal_status(self, signal_id: str, status: str) -> None:
        with self._lock:
            self._db.execute("UPDATE signals SET status=? WHERE signal_id=?", (status, signal_id))

    def get_signal_status(self, signal_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT status FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
            return row[0] if row else None

    # --- legs ---
    def add_leg(self, signal_id: str, market: str, token_id: str, size: float, price: float) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO legs(signal_id, market, token_id, size, price, state, created_at)"
                " VALUES (?,?,?,?,?, 'unhedged', ?)",
                (signal_id, market, token_id, size, price, _now()),
            )
            return int(cur.lastrowid)

    def set_leg_state(self, leg_id: int, state: str) -> None:
        with self._lock:
            self._db.execute("UPDATE legs SET state=? WHERE id=?", (state, leg_id))

    def unhedged_count(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM legs WHERE state IN ('unhedged','stuck')"
            ).fetchone()
            return int(row[0])

    def market_exposure(self, market: str) -> float:
        """USDC already committed to this market (legs not yet unwound)."""
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(size*price),0) FROM legs"
                " WHERE market=? AND state IN ('unhedged','hedged','stuck')",
                (market,),
            ).fetchone()
            return float(row[0])

    # --- PnL ---
    def record_pnl(self, signal_id: str, kind: str, amount: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO pnl(day, signal_id, kind, amount) VALUES (?,?,?,?)",
                (_today(), signal_id, kind, amount),
            )

    def daily_realized_loss(self) -> float:
        """Positive number = today's (UTC) realized loss from unwinds."""
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM pnl WHERE day=? AND kind='realized'",
                (_today(),),
            ).fetchone()
            return max(0.0, -float(row[0]))
