"""Durable, sequenced controller events with in-process subscribers.

Every state change the UI shows is an event row first (seq, ts, kind, payload), then fanned out.
Reconnecting clients replay `events_since(seq)`; seq is strictly increasing so clients dedupe.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .ids import now_iso
from .store.db import Database

Subscriber = Callable[[dict[str, Any]], None]


class EventLog:
    def __init__(self, db: Database):
        self.db = db
        self._subs: list[Subscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)

        return unsubscribe

    def emit(self, kind: str, payload: dict[str, Any] | None = None, *, case_id: str | None = None,
             job_id: str | None = None) -> dict[str, Any]:
        payload = payload or {}
        ts = now_iso()
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO events(ts, case_id, job_id, kind, payload) VALUES (?,?,?,?,?)",
                (ts, case_id, job_id, kind, json.dumps(payload, default=str)),
            )
            seq = cur.lastrowid
        ev = {"seq": seq, "ts": ts, "case_id": case_id, "job_id": job_id, "kind": kind, "payload": payload}
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(ev)
            except Exception:  # subscriber failures must never break the controller
                pass
        return ev

    def events_since(self, seq: int, *, case_id: str | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        if case_id:
            rows = self.db.query(
                "SELECT * FROM events WHERE seq>? AND (case_id=? OR case_id IS NULL) ORDER BY seq LIMIT ?",
                (seq, case_id, limit),
            )
        else:
            rows = self.db.query("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (seq, limit))
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def latest_seq(self) -> int:
        row = self.db.query_one("SELECT MAX(seq) AS s FROM events")
        return int(row["s"] or 0) if row else 0
