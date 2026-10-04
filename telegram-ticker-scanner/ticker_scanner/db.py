"""SQLite storage for scanner entries and their results."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable

from .models import STATUS_LIQ, STATUS_PAIR, Detection, Entry, Result

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tickers TEXT NOT NULL,
    chains TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    scan_since REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    chain TEXT NOT NULL,
    result_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    token_address TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    pair_address TEXT,
    pool_kind TEXT,
    dex TEXT NOT NULL,
    quote_symbol TEXT,
    quote_address TEXT,
    source TEXT NOT NULL,
    tx_hash TEXT,
    found_at REAL NOT NULL,
    liq_at REAL,
    liquidity_usd REAL,
    market_cap REAL,
    updated_at REAL,
    notify_message_id INTEGER,
    UNIQUE(entry_id, chain, result_key)
);
CREATE INDEX IF NOT EXISTS results_by_key ON results(chain, result_key);
CREATE INDEX IF NOT EXISTS results_by_status ON results(status, found_at);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Pairs that already existed when a ticker started being watched (DexScreener).
CREATE TABLE IF NOT EXISTS baseline_meta (
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    complete INTEGER NOT NULL,
    taken_at REAL NOT NULL,
    PRIMARY KEY (entry_id, ticker)
);
CREATE TABLE IF NOT EXISTS baseline (
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    chain TEXT NOT NULL,
    result_key TEXT NOT NULL,
    PRIMARY KEY (entry_id, chain, result_key)
);
"""

_RESULT_COLS = (
    "id, entry_id, chain, result_key, kind, status, token_address, symbol, name, pair_address, "
    "pool_kind, dex, quote_symbol, quote_address, source, tx_hash, found_at, liq_at, "
    "liquidity_usd, market_cap, updated_at, notify_message_id"
)


SCHEMA_VERSION = 2  # v2: meta + baseline tables (created by _SCHEMA)


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise SystemExit(f"{path} vient d'une version plus récente du scanner (schéma {version})")
        self.conn.executescript(_SCHEMA)
        # Future schema changes go here: `if version < 2: ALTER TABLE …`
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- entries -------------------------------------------------------
    @staticmethod
    def _entry(row) -> Entry:
        return Entry(
            id=row[0], name=row[1], tickers=json.loads(row[2]), chains=json.loads(row[3]),
            paused=bool(row[4]), created_at=row[5], scan_since=row[6],
        )

    def create_entry(self, name: str, tickers: list[str], chains: list[str]) -> Entry:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO entries(name, tickers, chains, paused, created_at, scan_since) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            (name, json.dumps(tickers), json.dumps(chains), now, now),
        )
        self.conn.commit()
        return self.get_entry(cur.lastrowid)

    def get_entry(self, entry_id: int) -> Entry | None:
        row = self.conn.execute(
            "SELECT id, name, tickers, chains, paused, created_at, scan_since FROM entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        return self._entry(row) if row else None

    def list_entries(self) -> list[Entry]:
        rows = self.conn.execute(
            "SELECT id, name, tickers, chains, paused, created_at, scan_since FROM entries ORDER BY id"
        ).fetchall()
        return [self._entry(r) for r in rows]

    def active_entries(self) -> list[Entry]:
        return [e for e in self.list_entries() if e.active]

    def update_entry(self, entry_id: int, *, name: str | None = None, tickers: list[str] | None = None,
                     chains: list[str] | None = None, paused: bool | None = None) -> Entry | None:
        sets, args = [], []
        if name is not None:
            sets.append("name = ?")
            args.append(name)
        if tickers is not None:
            sets.append("tickers = ?")
            args.append(json.dumps(tickers))
        if chains is not None:
            sets.append("chains = ?")
            args.append(json.dumps(chains))
        if tickers is not None or chains is not None:
            # Do not flood the user with pairs that existed before the edit.
            sets.append("scan_since = ?")
            args.append(time.time())
            self.conn.execute("DELETE FROM baseline WHERE entry_id = ?", (entry_id,))
            self.conn.execute("DELETE FROM baseline_meta WHERE entry_id = ?", (entry_id,))
        if paused is not None:
            sets.append("paused = ?")
            args.append(1 if paused else 0)
        if sets:
            self.conn.execute(f"UPDATE entries SET {', '.join(sets)} WHERE id = ?", (*args, entry_id))
            self.conn.commit()
        return self.get_entry(entry_id)

    def delete_entry(self, entry_id: int) -> None:
        self.conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        self.conn.commit()

    # ---- meta ----------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.conn.commit()

    # ---- baseline (pairs pre-existing when a ticker started being watched)
    def baseline_state(self, entry_id: int, ticker: str) -> bool | None:
        """None: no snapshot yet; True/False: snapshot complete or truncated."""
        row = self.conn.execute(
            "SELECT complete FROM baseline_meta WHERE entry_id = ? AND ticker = ?", (entry_id, ticker)
        ).fetchone()
        return None if row is None else bool(row[0])

    def save_baseline(self, entry_id: int, ticker: str, keys: Iterable[tuple[str, str]], complete: bool) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO baseline(entry_id, chain, result_key) VALUES (?, ?, ?)",
            [(entry_id, chain, key) for chain, key in keys],
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO baseline_meta(entry_id, ticker, complete, taken_at) VALUES (?, ?, ?, ?)",
            (entry_id, ticker, 1 if complete else 0, time.time()),
        )
        self.conn.commit()

    def in_baseline(self, entry_id: int, chain: str, key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM baseline WHERE entry_id = ? AND chain = ? AND result_key = ?", (entry_id, chain, key)
        ).fetchone() is not None

    # ---- results -------------------------------------------------------
    @staticmethod
    def _result(row) -> Result:
        return Result(*row)

    def get_result(self, entry_id: int, chain: str, key: str) -> Result | None:
        row = self.conn.execute(
            f"SELECT {_RESULT_COLS} FROM results WHERE entry_id = ? AND chain = ? AND result_key = ?",
            (entry_id, chain, key),
        ).fetchone()
        return self._result(row) if row else None

    def get_result_by_id(self, result_id: int) -> Result | None:
        row = self.conn.execute(f"SELECT {_RESULT_COLS} FROM results WHERE id = ?", (result_id,)).fetchone()
        return self._result(row) if row else None

    def find_results(self, chain: str, key: str) -> list[Result]:
        rows = self.conn.execute(
            f"SELECT {_RESULT_COLS} FROM results WHERE chain = ? AND result_key = ?", (chain, key)
        ).fetchall()
        return [self._result(r) for r in rows]

    def has_launch(self, chain: str, token: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM results WHERE chain = ? AND result_key = ? LIMIT 1", (chain, f"launch:{token}")
        ).fetchone()
        return row is not None

    def insert_result(self, entry_id: int, det: Detection, status: str) -> Result:
        try:
            return self._insert_result(entry_id, det, status)
        except sqlite3.IntegrityError:
            # Already stored (same entry / chain / key): return the existing row.
            self.conn.rollback()
            existing = self.get_result(entry_id, det.chain, det.result_key)
            if existing is None:
                raise
            return existing

    def _insert_result(self, entry_id: int, det: Detection, status: str) -> Result:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO results(entry_id, chain, result_key, kind, status, token_address, symbol, name, "
            "pair_address, pool_kind, dex, quote_symbol, quote_address, source, tx_hash, found_at, liq_at, "
            "liquidity_usd, market_cap, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry_id, det.chain, det.result_key, det.kind, status, det.token_address, det.symbol,
                det.name, det.pair_address, det.pool_kind, det.dex, det.quote_symbol, det.quote_address,
                det.source, det.tx_hash, now, now if status == STATUS_LIQ else None,
                det.liquidity_usd, det.market_cap, now if (det.liquidity_usd or det.market_cap) else None,
            ),
        )
        self.conn.commit()
        return self.get_result_by_id(cur.lastrowid)

    def mark_liquidity(self, result_id: int, liquidity_usd: float | None, market_cap: float | None) -> None:
        now = time.time()
        self.conn.execute(
            "UPDATE results SET status = ?, liq_at = COALESCE(liq_at, ?), "
            "liquidity_usd = COALESCE(?, liquidity_usd), market_cap = COALESCE(?, market_cap), "
            "updated_at = ? WHERE id = ?",
            (STATUS_LIQ, now, liquidity_usd, market_cap, now, result_id),
        )
        self.conn.commit()

    def update_metrics(self, result_id: int, liquidity_usd: float | None, market_cap: float | None) -> None:
        self.conn.execute(
            "UPDATE results SET liquidity_usd = ?, market_cap = ?, updated_at = ? WHERE id = ?",
            (liquidity_usd, market_cap, time.time(), result_id),
        )
        self.conn.commit()

    def set_notify_message(self, result_id: int, message_id: int) -> None:
        self.conn.execute("UPDATE results SET notify_message_id = ? WHERE id = ?", (message_id, result_id))
        self.conn.commit()

    def results_for_entry(self, entry_id: int) -> list[Result]:
        rows = self.conn.execute(
            f"SELECT {_RESULT_COLS} FROM results WHERE entry_id = ? ORDER BY found_at DESC, id DESC",
            (entry_id,),
        ).fetchall()
        return [self._result(r) for r in rows]

    def count_results(self, entry_id: int) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status != ? THEN 1 ELSE 0 END) FROM results WHERE entry_id = ?",
            (STATUS_PAIR, entry_id),
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def pending_results(self, max_age_s: float, chains: Iterable[str] | None = None) -> list[Result]:
        """Pairs created without liquidity yet (from active entries only)."""
        rows = self.conn.execute(
            f"SELECT {', '.join('r.' + c.strip() for c in _RESULT_COLS.split(','))} FROM results r "
            "JOIN entries e ON e.id = r.entry_id "
            "WHERE r.status = ? AND r.found_at >= ? AND e.paused = 0",
            (STATUS_PAIR, time.time() - max_age_s),
        ).fetchall()
        out = [self._result(r) for r in rows]
        if chains is not None:
            wanted = set(chains)
            out = [r for r in out if r.chain in wanted]
        return out

    def recent_liquid_results(self, max_age_s: float) -> list[Result]:
        rows = self.conn.execute(
            f"SELECT {_RESULT_COLS} FROM results WHERE status != ? AND found_at >= ?",
            (STATUS_PAIR, time.time() - max_age_s),
        ).fetchall()
        return [self._result(r) for r in rows]
