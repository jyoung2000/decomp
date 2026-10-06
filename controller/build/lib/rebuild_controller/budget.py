"""Budget ledger: atomic reserve -> settle/release against the ``budgets`` and ``reservations`` tables.

* ``reserve`` is atomic (BEGIN IMMEDIATE) and rejects a repeated ``request_key`` (UNIQUE) with ``DuplicateReservation``.
* Money held by a reservation counts against the limit until it is settled or released, so concurrent calls cannot
  jointly overspend.
* ``settle`` may record ``actual`` above the reserved amount (a ceiling can be wrong); the overrun is recorded and the
  budget then reads as exhausted - it is never silently clamped.
* Subscription quotas are NOT dollars. They are tracked separately with ``quota_known`` (default False): when a vendor
  does not publish a machine-readable quota we say "unknown" rather than invent a number.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from typing import Any

from .events import EventLog
from .ids import new_id, now_iso
from .store.db import Database, loads

EPS = 1e-9


class BudgetError(Exception):
    pass


class BudgetNotFound(BudgetError):
    pass


class BudgetExhausted(BudgetError):
    def __init__(self, budget_id: str, requested: float, available: float):
        super().__init__(f"budget {budget_id} exhausted: requested ${requested:.6f}, available ${max(available, 0):.6f}")
        self.budget_id, self.requested, self.available = budget_id, requested, available


class DuplicateReservation(BudgetError):
    def __init__(self, request_key: str):
        super().__init__(f"reservation for request_key {request_key!r} already exists")
        self.request_key = request_key


class ReservationStateError(BudgetError):
    pass


def _check_amount(x: float, name: str) -> float:
    if not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) or x < 0:
        raise ValueError(f"{name} must be a finite number >= 0 (got {x!r})")
    return float(x)


class BudgetLedger:
    def __init__(self, db: Database, events: EventLog):
        self.db = db
        self.events = events

    # ------------------------------------------------------------------ budgets
    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        remaining = row["limit_usd"] - row["reserved_usd"] - row["spent_usd"]
        row = dict(row)
        row["remaining_usd"] = remaining
        row["exhausted"] = remaining <= EPS
        row["kind"] = "usd"
        return row

    def create(self, budget_id: str, scope: str, limit_usd: float, *, currency: str = "USD", exist_ok: bool = False) -> dict[str, Any]:
        limit = _check_amount(limit_usd, "limit_usd")
        with self.db.transaction():
            row = self.db.query_one("SELECT * FROM budgets WHERE budget_id=?", (budget_id,))
            if row:
                if not exist_ok:
                    raise BudgetError(f"budget {budget_id} already exists")
                return self._view(row)
            self.db.insert("budgets", {"budget_id": budget_id, "scope": scope, "limit_usd": limit, "reserved_usd": 0.0,
                                       "spent_usd": 0.0, "currency": currency, "updated_at": now_iso()})
            row = self.db.query_one("SELECT * FROM budgets WHERE budget_id=?", (budget_id,))
        self._emit(row, "create")
        return self._view(row)

    def ensure(self, budget_id: str, scope: str, limit_usd: float) -> dict[str, Any]:
        """Create if missing; never changes an existing budget's limit."""
        return self.create(budget_id, scope, limit_usd, exist_ok=True)

    def get(self, budget_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM budgets WHERE budget_id=?", (budget_id,))
        if row is None:
            raise BudgetNotFound(budget_id)
        return self._view(row)

    def exists(self, budget_id: str) -> bool:
        return self.db.query_one("SELECT 1 AS x FROM budgets WHERE budget_id=?", (budget_id,)) is not None

    def list(self, prefix: str | None = None) -> list[dict[str, Any]]:
        if prefix:
            rows = self.db.query("SELECT * FROM budgets WHERE budget_id LIKE ? ESCAPE '\\' ORDER BY budget_id",
                                 (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",))
        else:
            rows = self.db.query("SELECT * FROM budgets ORDER BY budget_id")
        return [self._view(r) for r in rows]

    def set_limit(self, budget_id: str, limit_usd: float) -> dict[str, Any]:
        limit = _check_amount(limit_usd, "limit_usd")
        with self.db.transaction():
            if not self.db.update("budgets", "budget_id", budget_id, {"limit_usd": limit, "updated_at": now_iso()}):
                raise BudgetNotFound(budget_id)
            row = self.db.query_one("SELECT * FROM budgets WHERE budget_id=?", (budget_id,))
        self._emit(row, "limit")
        return self._view(row)

    # ------------------------------------------------------------------ reservations
    def reserve(self, budget_id: str, amount: float, request_key: str) -> dict[str, Any]:
        amount = _check_amount(amount, "amount")
        if not request_key:
            raise ValueError("request_key is required")
        rid = new_id("rsv")
        now = now_iso()
        with self.db.transaction():
            if self.db.query_one("SELECT 1 AS x FROM reservations WHERE request_key=?", (request_key,)):
                raise DuplicateReservation(request_key)
            row = self.db.query_one("SELECT * FROM budgets WHERE budget_id=?", (budget_id,))
            if row is None:
                raise BudgetNotFound(budget_id)
            available = row["limit_usd"] - row["reserved_usd"] - row["spent_usd"]
            if amount > available + EPS:
                raise BudgetExhausted(budget_id, amount, available)
            try:
                self.db.insert("reservations", {"reservation_id": rid, "budget_id": budget_id, "amount_usd": amount,
                                                "state": "held", "request_key": request_key, "actual_usd": None,
                                                "usage": {}, "created_at": now, "updated_at": now})
            except sqlite3.IntegrityError as e:  # UNIQUE(request_key) is the race-proof guard
                raise DuplicateReservation(request_key) from e
            self.db.execute("UPDATE budgets SET reserved_usd=reserved_usd+?, updated_at=? WHERE budget_id=?",
                            (amount, now, budget_id))
            brow = self.db.query_one("SELECT * FROM budgets WHERE budget_id=?", (budget_id,))
        self._emit(brow, "reserve", reservation_id=rid)
        return self.get_reservation(rid)

    def get_reservation(self, reservation_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,))
        if row is None:
            raise BudgetNotFound(f"reservation {reservation_id}")
        row["usage"] = loads(row["usage"], {})
        return row

    def reservations(self, budget_id: str, state: str | None = None) -> list[dict[str, Any]]:
        if state:
            rows = self.db.query("SELECT * FROM reservations WHERE budget_id=? AND state=? ORDER BY created_at", (budget_id, state))
        else:
            rows = self.db.query("SELECT * FROM reservations WHERE budget_id=? ORDER BY created_at", (budget_id,))
        for r in rows:
            r["usage"] = loads(r["usage"], {})
        return rows

    def _close(self, reservation_id: str, new_state: str, spend: float, usage: dict[str, Any] | None) -> dict[str, Any]:
        now = now_iso()
        with self.db.transaction():
            rsv = self.db.query_one("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,))
            if rsv is None:
                raise BudgetNotFound(f"reservation {reservation_id}")
            if rsv["state"] != "held":
                raise ReservationStateError(f"reservation {reservation_id} is {rsv['state']}, not held")
            self.db.execute("UPDATE reservations SET state=?, actual_usd=?, usage=?, updated_at=? WHERE reservation_id=?",
                            (new_state, spend, json.dumps(usage or {}, sort_keys=True, default=str), now, reservation_id))
            self.db.execute("UPDATE budgets SET reserved_usd=MAX(0, reserved_usd-?), spent_usd=spent_usd+?, updated_at=? WHERE budget_id=?",
                            (rsv["amount_usd"], spend, now, rsv["budget_id"]))
            brow = self.db.query_one("SELECT * FROM budgets WHERE budget_id=?", (rsv["budget_id"],))
        self._emit(brow, new_state if new_state != "settled" else "settle", reservation_id=reservation_id,
                   overrun=max(0.0, spend - rsv["amount_usd"]))
        return self.get_reservation(reservation_id)

    def settle(self, reservation_id: str, actual: float, usage: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._close(reservation_id, "settled", _check_amount(actual, "actual"), usage)

    def release(self, reservation_id: str) -> dict[str, Any]:
        """Nothing was spent (request provably not processed)."""
        return self._close(reservation_id, "released", 0.0, {"reason": "released"})

    def sweep_stale(self, max_age_s: float, *, now: float | None = None) -> list[str]:
        """Held reservations older than ``max_age_s`` (crash between send and settle) are settled at their full amount:
        we cannot prove the provider did not bill, so the conservative assumption is that it did."""
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime((now if now is not None else time.time()) - max_age_s))
        out = []
        for r in self.db.query("SELECT reservation_id, amount_usd FROM reservations WHERE state='held' AND created_at<?", (cutoff,)):
            try:
                self.settle(r["reservation_id"], r["amount_usd"], {"reason": "stale_swept_assumed_spent"})
                out.append(r["reservation_id"])
            except ReservationStateError:
                pass
        return out

    # ------------------------------------------------------------------ subscription quotas (not dollars)
    def set_quota(self, connection_id: str, *, quota_known: bool = False, unit: str | None = None, limit: float | None = None,
                  used: float | None = None, resets_at: str | None = None, source: str = "") -> dict[str, Any]:
        if quota_known and (unit is None or limit is None):
            raise ValueError("quota_known=True requires unit and limit")
        doc = {"connection_id": connection_id, "quota_known": bool(quota_known), "unit": unit, "limit": limit, "used": used,
               "resets_at": resets_at, "source": source, "updated_at": now_iso()}
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (f"quota:{connection_id}", json.dumps(doc)))
        self._emit_raw({"kind": "quota", **doc})
        return doc

    def get_quota(self, connection_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT value FROM meta WHERE key=?", (f"quota:{connection_id}",))
        if row:
            return json.loads(row["value"])
        return {"connection_id": connection_id, "quota_known": False, "unit": None, "limit": None, "used": None,
                "resets_at": None, "source": "", "usd_tracked": False}

    def snapshot(self) -> dict[str, Any]:
        quotas = []
        for r in self.db.query("SELECT value FROM meta WHERE key LIKE 'quota:%' ORDER BY key"):
            quotas.append(json.loads(r["value"]))
        return {"budgets": self.list(), "quotas": quotas}

    # ------------------------------------------------------------------ events
    def _emit(self, row: dict[str, Any], reason: str, **extra: Any) -> None:
        v = self._view(row)
        scope = row["scope"]
        case_id = scope.split(":", 1)[1] if scope.startswith("case:") else None
        job_id = scope.split(":", 1)[1] if scope.startswith("job:") else None
        self.events.emit("budget.updated", {"budget_id": row["budget_id"], "scope": scope, "limit_usd": v["limit_usd"],
                                            "reserved_usd": v["reserved_usd"], "spent_usd": v["spent_usd"],
                                            "remaining_usd": v["remaining_usd"], "exhausted": v["exhausted"],
                                            "reason": reason, **extra}, case_id=case_id, job_id=job_id)

    def _emit_raw(self, payload: dict[str, Any]) -> None:
        self.events.emit("budget.updated", payload)
