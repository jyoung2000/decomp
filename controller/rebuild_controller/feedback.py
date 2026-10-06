"""Feedback: persisted before acknowledgement; linked to exact candidate/plan revision; convertible to plan work."""
from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

from .cases import CaseStore
from .events import EventLog
from .ids import new_id, now_iso
from .plan import PlanStore
from .store.db import Database, loads

STATUSES = ["received", "triaged", "queued", "in_progress", "ready_to_retest", "resolved", "reopened"]
CLASSES = {"bug", "change", "question", "acceptance"}
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{10,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._\-]{16,})")


class FeedbackStore:
    def __init__(self, db: Database, events: EventLog, plan: PlanStore, cases: CaseStore):
        self.db = db
        self.events = events
        self.plan = plan
        self.cases = cases

    def create(self, case_id: str, *, target_kind: str, target_id: str, classification: str, priority: str, comment: str,
               expected: str = "", actual: str = "", candidate_id: str | None = None, attachments: list[dict[str, Any]] | None = None,
               context: dict[str, Any] | None = None) -> dict[str, Any]:
        assert classification in CLASSES
        fid = new_id("fb")
        ts = now_iso()
        stored = []
        for att in attachments or []:
            data = base64.b64decode(att.get("bytes_b64", ""))
            if len(data) > MAX_ATTACHMENT_BYTES:
                raise ValueError(f"attachment {att.get('name')} exceeds {MAX_ATTACHMENT_BYTES} bytes")
            sha = self.cases.blobs.put_bytes(data)
            stored.append({"name": _safe_name(att.get("name", "attachment")), "sha256": sha, "size": len(data)})
        ctx = {"plan_revision": self.plan.current_revision(case_id), "candidate_id": candidate_id, **(context or {})}
        row = {"feedback_id": fid, "case_id": case_id, "target_kind": target_kind, "target_id": target_id, "candidate_id": candidate_id,
               "plan_revision": ctx["plan_revision"], "classification": classification, "priority": priority,
               "comment": _SECRET_RE.sub("[redacted]", comment), "expected": _SECRET_RE.sub("[redacted]", expected),
               "actual": _SECRET_RE.sub("[redacted]", actual), "attachments": stored, "context": ctx, "status": "received", "linked_items": [],
               "history": [{"ts": ts, "status": "received", "note": "persisted"}], "created_at": ts, "updated_at": ts}
        with self.db.transaction():  # persisted before we return/acknowledge
            self.db.insert("feedback", row)
        fb = self.get(fid)
        self.events.emit("feedback.created", {"feedback": fb}, case_id=case_id)
        return fb

    def get(self, feedback_id: str) -> dict[str, Any]:
        r = self.db.query_one("SELECT * FROM feedback WHERE feedback_id=?", (feedback_id,))
        if not r:
            raise KeyError(feedback_id)
        for k in ("attachments", "context", "linked_items", "history"):
            r[k] = loads(r[k], [] if k != "context" else {})
        return r

    def list(self, case_id: str) -> list[dict[str, Any]]:
        return [self.get(r["feedback_id"]) for r in self.db.query("SELECT feedback_id FROM feedback WHERE case_id=? ORDER BY created_at DESC", (case_id,))]

    def set_status(self, feedback_id: str, status: str, note: str = "", *, by: str = "controller") -> dict[str, Any]:
        assert status in STATUSES
        fb = self.get(feedback_id)
        hist = fb["history"] + [{"ts": now_iso(), "status": status, "note": note, "by": by}]
        self.db.update("feedback", "feedback_id", feedback_id, {"status": status, "history": hist, "updated_at": now_iso()})
        fb = self.get(feedback_id)
        self.events.emit("feedback.updated", {"feedback": fb}, case_id=fb["case_id"])
        return fb

    def triage(self, feedback_id: str, *, status: str = "triaged", note: str = "", create_work: bool = False) -> dict[str, Any]:
        """Triage; optionally convert into linked plan work. Feedback never edits baselines or verdicts."""
        fb = self.get(feedback_id)
        if create_work:
            kind = "deliverable"
            title = ("Fix: " if fb["classification"] == "bug" else "Change: ") + fb["comment"][:70]
            item = self.plan.add_item(fb["case_id"], title=title, outcome=fb["comment"], kind=kind,
                                      acceptance=[f"comparison for {fb['target_kind']} {fb['target_id']} passes on a new candidate",
                                                  "affected regression tests re-run"], reason=f"feedback {feedback_id}")
            self.plan.update_item(item["item_id"], status="queued")
            linked = fb["linked_items"] + [item["item_id"]]
            self.db.update("feedback", "feedback_id", feedback_id, {"linked_items": linked})
            self.plan.revise(fb["case_id"], f"feedback {feedback_id} converted to plan work ({fb['classification']})")
            status = "queued"
        return self.set_status(feedback_id, status, note)

    def reopen(self, feedback_id: str, note: str = "") -> dict[str, Any]:
        return self.set_status(feedback_id, "reopened", note, by="user")

    def mark_stale_for_candidate(self, case_id: str, new_candidate_id: str) -> int:
        """Feedback tied to an older candidate becomes 'ready_to_retest' only when its linked work completed; else just annotated."""
        n = 0
        for fb in self.list(case_id):
            if fb["candidate_id"] and fb["candidate_id"] != new_candidate_id and fb["status"] in ("in_progress", "queued"):
                self.set_status(fb["feedback_id"], "ready_to_retest", f"new candidate {new_candidate_id} published")
                n += 1
        return n


def _safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^A-Za-z0-9._\-]", "_", name)[:120] or "attachment"
