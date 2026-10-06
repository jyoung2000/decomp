"""Persisted, revisioned project plan with exact progress derived from durable job/feature state.

Plan items have stable IDs. Revisions are snapshots with reason+timestamp. Progress has real denominators or None.
"""
from __future__ import annotations

import json
from typing import Any

from .events import EventLog
from .ids import new_id, now_iso, now_ts
from .jobs import JobState, JobStore
from .ledger import FeatureLedger
from .store.db import Database, loads

STATUS = {"queued", "running", "completed", "blocked", "failed", "cancelled", "needs_retest"}
KINDS = {"milestone", "deliverable", "feature", "discovery", "deferred", "unsupported"}

# Stable baseline milestone ids published before substantive work starts.
BASELINE = [
    ("M-ANALYSIS", "Analysis", "Inventory the installation, detect formats, build the dependency graph", []),
    ("M-ASSETS", "Assets & dependencies", "Recover assets, resources and dependency manifests", ["M-ANALYSIS"]),
    ("M-RECOVERY", "Code recovery", "Recover code/structure with the selected backends", ["M-ANALYSIS"]),
    ("M-FEATURES", "Feature discovery", "Derive the feature ledger from static and runtime evidence", ["M-RECOVERY"]),
    ("M-IMPL", "Implementation", "Reconstruct the application in the requested language", ["M-FEATURES", "M-ASSETS"]),
    ("M-BUILD", "Builds", "Build runnable candidates", ["M-IMPL"]),
    ("M-COMPARE", "Comparisons", "Compare candidate against original evidence per feature", ["M-BUILD"]),
    ("M-FIX", "Fixes", "Repair mismatches and rebuild", ["M-COMPARE"]),
    ("M-PACKAGE", "Packaging", "Package the requested output type", ["M-COMPARE"]),
    ("M-DELIVER", "Final delivery", "Export source/dist/evidence/reports and the parity report", ["M-PACKAGE", "M-FIX"]),
    ("D-UNKNOWN", "Undiscovered scope", "Features not yet discovered; this plan is not complete until discovery finishes", ["M-ANALYSIS"]),
]


class PlanStore:
    def __init__(self, db: Database, events: EventLog, jobs: JobStore, ledger: FeatureLedger):
        self.db = db
        self.events = events
        self.jobs = jobs
        self.ledger = ledger

    # -- initialisation -------------------------------------------------
    def initialize(self, case_id: str, case: dict[str, Any]) -> int:
        ts = now_iso()
        for i, (iid, title, outcome, deps) in enumerate(BASELINE):
            kind = "discovery" if iid.startswith("D-") else "milestone"
            self.db.upsert("plan_items", {"item_id": f"{case_id}:{iid}", "case_id": case_id, "parent_id": None, "title": title, "outcome": outcome,
                                          "kind": kind, "status": "queued", "owner": "controller", "depends_on": [f"{case_id}:{d}" for d in deps],
                                          "acceptance": _acceptance_for(iid, case), "evidence_ids": [], "files": [], "preview_id": None,
                                          "blockers": [], "feature_id": None, "job_ids": [], "sort_order": i, "created_at": ts, "updated_at": ts}, "item_id")
        return self.revise(case_id, "initial plan published before reconstruction starts")

    # -- items ----------------------------------------------------------
    def add_item(self, case_id: str, *, title: str, outcome: str, kind: str, parent_id: str | None = None, depends_on: list[str] | None = None,
                 acceptance: list[str] | None = None, feature_id: str | None = None, owner: str = "controller", item_id: str | None = None,
                 reason: str = "discovery") -> dict[str, Any]:
        assert kind in KINDS
        iid = item_id or new_id("item")
        ts = now_iso()
        n = self.db.query_one("SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM plan_items WHERE case_id=?", (case_id,))["n"]
        self.db.upsert("plan_items", {"item_id": iid, "case_id": case_id, "parent_id": parent_id, "title": title, "outcome": outcome, "kind": kind,
                                      "status": "queued", "owner": owner, "depends_on": depends_on or [], "acceptance": acceptance or [],
                                      "evidence_ids": [], "files": [], "preview_id": None, "blockers": [], "feature_id": feature_id, "job_ids": [],
                                      "sort_order": n, "created_at": ts, "updated_at": ts}, "item_id")
        item = self.get_item(iid)
        self.events.emit("plan.item", {"item": item, "reason": reason}, case_id=case_id)
        return item

    def get_item(self, item_id: str) -> dict[str, Any]:
        r = self.db.query_one("SELECT * FROM plan_items WHERE item_id=?", (item_id,))
        if not r:
            raise KeyError(item_id)
        for k in ("depends_on", "acceptance", "evidence_ids", "files", "blockers", "job_ids"):
            r[k] = loads(r[k], [])
        return r

    def items(self, case_id: str) -> list[dict[str, Any]]:
        return [self.get_item(r["item_id"]) for r in self.db.query("SELECT item_id FROM plan_items WHERE case_id=? ORDER BY sort_order", (case_id,))]

    def update_item(self, item_id: str, **fields: Any) -> dict[str, Any]:
        if "status" in fields:
            assert fields["status"] in STATUS
        fields["updated_at"] = now_iso()
        self.db.update("plan_items", "item_id", item_id, fields)
        item = self.get_item(item_id)
        self.events.emit("plan.item", {"item": item}, case_id=item["case_id"])
        return item

    def link_job(self, item_id: str, job_id: str) -> None:
        item = self.get_item(item_id)
        if job_id not in item["job_ids"]:
            self.update_item(item_id, job_ids=item["job_ids"] + [job_id])

    def add_evidence(self, item_id: str, evidence_id: str) -> None:
        item = self.get_item(item_id)
        if evidence_id not in item["evidence_ids"]:
            self.update_item(item_id, evidence_ids=item["evidence_ids"] + [evidence_id])

    def milestone_id(self, case_id: str, short: str) -> str:
        return f"{case_id}:{short}"

    # -- status derivation ---------------------------------------------
    def refresh_statuses(self, case_id: str) -> None:
        """Derive item statuses from linked jobs (durable state), never from timers."""
        for item in self.items(case_id):
            if not item["job_ids"]:
                continue
            states = []
            for jid in item["job_ids"]:
                try:
                    states.append(self.jobs.get(jid).state)
                except KeyError:
                    pass
            if not states:
                continue
            if any(s == JobState.RUNNING for s in states):
                st = "running"
            elif any(s == JobState.FAILED for s in states):
                st = "failed"
            elif any(s == JobState.BLOCKED for s in states):
                st = "blocked"
            elif any(s == JobState.NEEDS_RETEST for s in states):
                st = "needs_retest"
            elif any(s == JobState.CANCELLED for s in states):
                st = "cancelled"
            elif all(s == JobState.COMPLETED for s in states):
                st = "completed"
            else:
                st = "queued"
            blockers = [j.blocker for jid in item["job_ids"] for j in [self._safe_job(jid)] if j and j.blocker]
            if st != item["status"] or blockers != item["blockers"]:
                self.update_item(item["item_id"], status=st, blockers=blockers)

    def _safe_job(self, jid: str):
        try:
            return self.jobs.get(jid)
        except KeyError:
            return None

    # -- revisions ------------------------------------------------------
    def current_revision(self, case_id: str) -> int:
        r = self.db.query_one("SELECT MAX(revision) AS r FROM plan_revisions WHERE case_id=?", (case_id,))
        return int(r["r"] or 0) if r else 0

    def revise(self, case_id: str, reason: str) -> int:
        rev = self.current_revision(case_id) + 1
        snap = {"items": self.items(case_id), "progress": self.progress(case_id)}
        self.db.insert("plan_revisions", {"revision": rev, "case_id": case_id, "reason": reason, "snapshot": snap, "created_at": now_iso()})
        self.events.emit("plan.revised", {"revision": rev, "reason": reason, "item_count": len(snap["items"])}, case_id=case_id)
        return rev

    def revisions(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT revision, reason, created_at FROM plan_revisions WHERE case_id=? ORDER BY revision", (case_id,))
        return rows

    def revision_snapshot(self, case_id: str, revision: int) -> dict[str, Any]:
        r = self.db.query_one("SELECT snapshot FROM plan_revisions WHERE case_id=? AND revision=?", (case_id, revision))
        return loads(r["snapshot"], {}) if r else {}

    # -- user actions ---------------------------------------------------
    def prioritize(self, case_id: str, item_id: str) -> dict[str, Any]:
        item = self.get_item(item_id)
        for jid in item["job_ids"]:
            self.db.update("jobs", "job_id", jid, {"priority": 1})
        self.update_item(item_id, sort_order=-1)
        self.revise(case_id, f"user prioritized {item['title']}")
        return self.get_item(item_id)

    def request_change(self, case_id: str, item_id: str, request: str, reason: str) -> dict[str, Any]:
        """Record a user change request; returns affected items/tests. Scope reductions are explicit decisions."""
        item = self.get_item(item_id)
        affected = [i["item_id"] for i in self.items(case_id) if item_id in i["depends_on"]]
        change = self.add_item(case_id, title=f"Change request: {request[:80]}", outcome=request, kind="deliverable", parent_id=item_id,
                               depends_on=[item_id], acceptance=["user-specified change verified by comparator"], owner="controller", reason=reason)
        self.revise(case_id, f"user change request on {item['title']}: {reason}")
        return {"change_item": change, "affected_items": affected, "affected_tests": [a for a in item["acceptance"]]}

    # -- exact progress -------------------------------------------------
    def progress(self, case_id: str) -> dict[str, Any]:
        counts = self.jobs.counts(case_id)
        by_stage: dict[str, dict[str, int]] = {}
        for j in self.jobs.list(case_id):
            d = by_stage.setdefault(j.stage, {"done": 0, "total": 0, "failed": 0, "running": 0})
            d["total"] += 1
            if j.state == JobState.COMPLETED:
                d["done"] += 1
            elif j.state == JobState.FAILED:
                d["failed"] += 1
            elif j.state == JobState.RUNNING:
                d["running"] += 1
        feats = self.ledger.summary(case_id)
        groups = {
            "discovery": _group(by_stage, ("inventory", "detect", "dependency_graph", "discover_features")),
            "recovery": _group(by_stage, ("analyze_module", "decompile", "recover_assets", "recover_managed", "recover_engine", "recover_web")),
            "implementation": {"done": feats["impl"]["runnable"], "total": feats["total"] or None, "unit": "features",
                               "note": "features with a runnable implementation; not verified parity"},
            "build": _group(by_stage, ("build_candidate", "package")),
            "verification": {"done": feats["verify"]["verified"], "total": feats["total"] or None, "unit": "features",
                             "failed": feats["verify"]["failed"], "partial": feats["verify"]["partial"], "stale": feats["verify"]["stale"]},
        }
        known = any(i["kind"] == "discovery" and i["status"] != "completed" for i in self.items(case_id))
        return {"jobs": counts, "groups": groups, "features": feats, "scope_known": not known,
                "eta": self._eta(case_id), "measured_at": now_iso()}

    def _eta(self, case_id: str) -> dict[str, Any] | None:
        """Estimate from measured attempt durations of the same stages; None when no history."""
        rows = self.db.query("SELECT j.stage, a.started_at, a.finished_at FROM job_attempts a JOIN jobs j ON j.job_id=a.job_id WHERE j.case_id=? AND a.outcome='completed'", (case_id,))
        if len(rows) < 3:
            return None
        import datetime as dt
        durs: dict[str, list[float]] = {}
        for r in rows:
            try:
                s = dt.datetime.fromisoformat(r["started_at"].replace("Z", "+00:00")); e = dt.datetime.fromisoformat(r["finished_at"].replace("Z", "+00:00"))
                durs.setdefault(r["stage"], []).append((e - s).total_seconds())
            except Exception:
                continue
        remaining = self.jobs.list(case_id, [JobState.QUEUED, JobState.BLOCKED, JobState.RUNNING])
        est, unknown = 0.0, 0
        for j in remaining:
            d = durs.get(j.stage)
            if d:
                est += sum(d) / len(d)
            else:
                unknown += 1
        if unknown == len(remaining) and remaining:
            return None
        spread = max((max(d) - min(d)) for d in durs.values() if d) if durs else 0
        return {"seconds": est, "uncertainty_seconds": spread * max(1, len(remaining)), "unknown_jobs": unknown,
                "updated_at": now_iso(), "label": "estimate from measured stage history; does not cover undiscovered scope"}

    def export(self, case_id: str) -> dict[str, Any]:
        return {"case_id": case_id, "revision": self.current_revision(case_id), "exported_at": now_iso(), "items": self.items(case_id),
                "revisions": self.revisions(case_id), "progress": self.progress(case_id)}


def _group(by_stage: dict[str, dict[str, int]], stages: tuple[str, ...]) -> dict[str, Any]:
    done = sum(by_stage.get(s, {}).get("done", 0) for s in stages)
    total = sum(by_stage.get(s, {}).get("total", 0) for s in stages)
    failed = sum(by_stage.get(s, {}).get("failed", 0) for s in stages)
    running = sum(by_stage.get(s, {}).get("running", 0) for s in stages)
    return {"done": done, "total": total if total else None, "failed": failed, "running": running, "unit": "jobs"}


def _acceptance_for(iid: str, case: dict[str, Any]) -> list[str]:
    return {
        "M-ANALYSIS": ["inventory evidence recorded with disclosed skips/truncation", "profile detected with reasons"],
        "M-ASSETS": ["asset/dependency manifest evidence recorded"],
        "M-RECOVERY": ["recovery evidence per module with backend version"],
        "M-FEATURES": ["feature ledger populated with origin per feature"],
        "M-IMPL": [f"candidate source in {case.get('target_language')} present"],
        "M-BUILD": ["candidate builds from delivered contents without case workspace"],
        "M-COMPARE": ["comparator verdicts recorded per feature by the verifier"],
        "M-FIX": ["no failed comparison on the latest candidate or explicit blocker recorded"],
        "M-PACKAGE": [f"{case.get('output_type')} output present in dist/"],
        "M-DELIVER": ["source/ dist/ evidence/ reports/ published atomically; manifest covers every shipped file"],
        "D-UNKNOWN": ["discovery complete: runtime observation and static analysis both finished"],
    }.get(iid, [])
