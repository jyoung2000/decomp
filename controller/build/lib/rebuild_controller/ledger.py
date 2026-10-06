"""Feature ledger: semantic features with separate implementation, verification and user-review properties.

verify_status is written only through Verifier (it calls set_verification with writer='verifier').
"""
from __future__ import annotations

from typing import Any

from .events import EventLog
from .ids import new_id, now_iso
from .store.db import Database, loads

IMPL = {"unplanned", "planned", "in_progress", "runnable", "blocked", "unsupported", "deferred"}
VERIFY = {"untested", "verified", "partial", "failed", "stale"}


class FeatureLedger:
    def __init__(self, db: Database, events: EventLog):
        self.db = db
        self.events = events

    def add(self, case_id: str, title: str, *, description: str = "", origin: str = "static", critical: bool = False,
            impl_status: str = "planned", evidence_ids: list[str] | None = None, feature_id: str | None = None) -> dict[str, Any]:
        assert impl_status in IMPL
        fid = feature_id or new_id("feat")
        ts = now_iso()
        existing = self.db.query_one("SELECT feature_id FROM features WHERE feature_id=?", (fid,))
        row = {"feature_id": fid, "case_id": case_id, "title": title, "description": description, "origin": origin,
               "critical": int(critical), "impl_status": impl_status, "verify_status": "untested", "evidence_ids": evidence_ids or [],
               "created_at": ts, "updated_at": ts}
        if existing:
            self.db.update("features", "feature_id", fid, {k: v for k, v in row.items() if k not in ("created_at", "verify_status")})
        else:
            self.db.insert("features", row)
        f = self.get(fid)
        self.events.emit("feature.updated", {"feature": f}, case_id=case_id)
        return f

    def get(self, feature_id: str) -> dict[str, Any]:
        r = self.db.query_one("SELECT * FROM features WHERE feature_id=?", (feature_id,))
        if not r:
            raise KeyError(feature_id)
        r["evidence_ids"] = loads(r["evidence_ids"], [])
        r["critical"] = bool(r["critical"])
        return r

    def list(self, case_id: str) -> list[dict[str, Any]]:
        return [self.get(r["feature_id"]) for r in self.db.query("SELECT feature_id FROM features WHERE case_id=? ORDER BY created_at", (case_id,))]

    def set_impl(self, feature_id: str, impl_status: str, *, evidence_ids: list[str] | None = None) -> dict[str, Any]:
        assert impl_status in IMPL
        f = self.get(feature_id)
        fields: dict[str, Any] = {"impl_status": impl_status, "updated_at": now_iso()}
        if evidence_ids:
            fields["evidence_ids"] = sorted(set(f["evidence_ids"]) | set(evidence_ids))
        self.db.update("features", "feature_id", feature_id, fields)
        f = self.get(feature_id)
        self.events.emit("feature.updated", {"feature": f}, case_id=f["case_id"])
        return f

    def set_verification(self, feature_id: str, status: str, *, candidate_id: str | None, evidence_id: str | None, writer: str) -> dict[str, Any]:
        if writer != "verifier":
            raise PermissionError("only the verifier may write verification status")
        assert status in VERIFY
        self.db.update("features", "feature_id", feature_id, {"verify_status": status, "verify_candidate": candidate_id,
                                                              "verify_evidence": evidence_id, "updated_at": now_iso()})
        f = self.get(feature_id)
        self.events.emit("feature.updated", {"feature": f}, case_id=f["case_id"])
        return f

    def set_user_review(self, feature_id: str, review: str | None) -> dict[str, Any]:
        assert review in (None, "accepted", "rejected")
        self.db.update("features", "feature_id", feature_id, {"user_review": review, "updated_at": now_iso()})
        f = self.get(feature_id)
        self.events.emit("feature.updated", {"feature": f}, case_id=f["case_id"])
        return f

    def mark_stale(self, case_id: str, reason: str, *, candidate_id: str | None = None) -> int:
        sql, params = "UPDATE features SET verify_status='stale', updated_at=? WHERE case_id=? AND verify_status IN ('verified','partial','failed')", [now_iso(), case_id]
        if candidate_id:
            sql += " AND verify_candidate=?"; params.append(candidate_id)
        n = self.db.execute(sql, tuple(params)).rowcount
        if n:
            self.events.emit("feature.stale", {"count": n, "reason": reason}, case_id=case_id)
        return n

    def summary(self, case_id: str) -> dict[str, Any]:
        feats = self.list(case_id)
        impl = {k: 0 for k in IMPL}; ver = {k: 0 for k in VERIFY}
        critical_incomplete = []
        for f in feats:
            impl[f["impl_status"]] += 1; ver[f["verify_status"]] += 1
            if f["critical"] and (f["impl_status"] in ("blocked", "unsupported", "unplanned") or f["verify_status"] != "verified"):
                critical_incomplete.append({"feature_id": f["feature_id"], "title": f["title"], "impl_status": f["impl_status"], "verify_status": f["verify_status"]})
        total = len(feats)
        return {"total": total, "impl": impl, "verify": ver, "critical_incomplete": critical_incomplete,
                "full_parity": total > 0 and ver["verified"] == total and not critical_incomplete,
                "note": "counts are semantic features, not files/bytes/functions"}
