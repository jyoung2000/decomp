"""SQLite-backed durable store with migrations and atomic transactions.

One Database per process. Thread-safe via a lock around a single connection in WAL mode.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

# Ordered migrations: (version, sql). Version 1 is the base schema.
MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA),
]


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._tx_depth = 0
        self.migrate()

    # -- migrations -----------------------------------------------------
    def migrate(self) -> None:
        # executescript() implicitly commits, so migrations run outside the re-entrant transaction wrapper.
        with self._lock:
            self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            current = int(row["value"]) if row else 0
            for version, sql in MIGRATIONS:
                if version > current:
                    self._conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
                    self._conn.execute(
                        "INSERT INTO meta(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(version),),
                    )
                    current = version

    # -- transactions ---------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Re-entrant atomic transaction. Inner calls join the outer transaction."""
        with self._lock:
            if self._tx_depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield self._conn
            except BaseException:
                self._tx_depth -= 1
                if self._tx_depth == 0:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._tx_depth -= 1
                if self._tx_depth == 0:
                    self._conn.execute("COMMIT")

    # -- helpers --------------------------------------------------------
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    def insert(self, table: str, row: dict[str, Any]) -> None:
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        vals = tuple(_enc(v) for v in row.values())
        self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", vals)

    def upsert(self, table: str, row: dict[str, Any], key: str) -> None:
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(f"{c}=excluded.{c}" for c in row if c != key)
        vals = tuple(_enc(v) for v in row.values())
        self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT({key}) DO UPDATE SET {updates}", vals)

    def update(self, table: str, key: str, key_value: Any, fields: dict[str, Any]) -> int:
        sets = ", ".join(f"{c}=?" for c in fields)
        vals = tuple(_enc(v) for v in fields.values()) + (key_value,)
        return self.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", vals).rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _enc(v: Any) -> Any:
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, sort_keys=True, default=str)
    if isinstance(v, bool):
        return int(v)
    return v


def loads(v: Any, default: Any = None) -> Any:
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def open_database(path: Path | str) -> Database:
    return Database(path)
