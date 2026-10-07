"""The Verifier: the only writer of comparison verdicts, feature verification status and candidate verification.

Inputs: a frozen baseline (evidence kind 'baseline' produced by a trusted producer) and a built candidate.
AI output never reaches this module except as a candidate to test. Verdicts are cleared when inputs change.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .candidates import CandidateStore
from .cases import CaseStore
from .comparators import compare_cli_scenario, compare_web_capture, run_web_scenario
from .comparators.base import ComparisonResult
from .events import EventLog
from .ids import new_id, now_iso, stable_json_hash
from .ledger import FeatureLedger
from .previews import _free_port, _QuietHandler
from .store.db import Database, loads

TRUSTED_BASELINE_PRODUCERS = {"capture_original", "fixture_oracle", "verifier"}


class BaselineError(ValueError):
    pass


class Verifier:
    writer = "verifier"

    def __init__(self, db: Database, events: EventLog, cases: CaseStore, candidates: CandidateStore, ledger: FeatureLedger):
        self.db = db
        self.events = events
        self.cases = cases
        self.candidates = candidates
        self.ledger = ledger

    # -- baselines ------------------------------------------------------
    def freeze_baseline(self, case_id: str, baseline: dict[str, Any], *, producer: str, title: str = "Frozen baseline") -> dict[str, Any]:
        if producer not in TRUSTED_BASELINE_PRODUCERS:
            raise BaselineError(f"producer {producer!r} may not write baselines")
        body = dict(baseline); body["frozen"] = True; body["frozen_at"] = now_iso()
        ev = self.cases.add_evidence(case_id, "baseline", title, body=body, inputs={"baseline_hash": stable_json_hash(baseline)}, producer=producer,
                                     meta={"frozen": True, "scenarios": len(baseline.get("scenarios", [])), "kind": baseline.get("kind")})
        return ev

    def load_baseline(self, case_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        evs = [e for e in self.cases.list_evidence(case_id, kind="baseline") if e["producer"] in TRUSTED_BASELINE_PRODUCERS and e["meta"].get("frozen")]
        if not evs:
            raise BaselineError("no frozen baseline for this case; run capture_original first")
        ev = evs[-1]
        return ev, self.cases.evidence_body(ev["evidence_id"])

    # -- verification ---------------------------------------------------
    def verify_candidate(self, case_id: str, candidate_id: str, *, feature_ids: list[str] | None = None, progress=None, log=None) -> dict[str, Any]:
        cand = self.candidates.get(candidate_id)
        if cand["build_status"] != "built" or not cand["dist_dir"]:
            raise ValueError("candidate is not built; nothing to verify")
        ev, baseline = self.load_baseline(case_id)
        launch = cand["meta"].get("launch")
        if not launch:
            raise ValueError("candidate has no launch spec from the builder")
        env = _environment()
        results_by_feature: dict[str, list[ComparisonResult]] = {}
        scenario_results: list[dict[str, Any]] = []
        artifacts_root = self.cases.case_root(case_id) / "verify" / candidate_id
        if artifacts_root.exists():
            shutil.rmtree(artifacts_root)
        artifacts_root.mkdir(parents=True)
        scenarios = baseline.get("scenarios", [])
        wanted = set(feature_ids or [])
        todo = [sc for sc in scenarios if not wanted or sc.get("feature_id") in wanted]
        say = log or (lambda *a, **k: None)
        say(f"Comparing the rebuilt program with the original: {len(todo)} scenario(s) to run")
        for i, sc in enumerate(scenarios):
            fid = sc.get("feature_id")
            if wanted and fid not in wanted:
                continue
            say(f"Running the rebuilt program: scenario {len(scenario_results) + 1} of {len(todo)} ({sc.get('title') or sc['id']})", key="verify")
            work = artifacts_root / f"scenario-{sc['id']}"
            try:
                if baseline.get("kind") == "web":
                    comps = self._verify_web(sc, launch, Path(cand["dist_dir"]), work, baseline.get("tolerance", {}))
                else:
                    comps = compare_cli_scenario(sc, sc["expected"], launch, Path(cand["dist_dir"]), work, timeout=float(sc.get("timeout", 60)))
            except Exception as e:
                comps = [ComparisonResult("exit_code", "run", "error", {"error": f"{type(e).__name__}: {e}"[:2000]})]
            recorded = [self._record(case_id, candidate_id, fid, c, env, sc["id"]) for c in comps]
            scenario_results.append({"scenario": sc["id"], "feature_id": fid, "comparisons": recorded,
                                     "verdict": "pass" if all(c.verdict == "pass" for c in comps) else ("error" if any(c.verdict == "error" for c in comps) else "fail")})
            results_by_feature.setdefault(fid or f"scenario:{sc['id']}", []).extend(comps)
            ok_n = sum(1 for r in scenario_results if r["verdict"] == "pass")
            say(f"Comparing: {ok_n} of {len(scenario_results)} scenarios match so far", "warn" if scenario_results[-1]["verdict"] != "pass" else "info", key="compare")
            if scenario_results[-1]["verdict"] != "pass":
                bad = [c for c in comps if c.verdict != "pass"][:1]
                if bad:
                    say(f"Scenario '{sc.get('title') or sc['id']}' differs in {bad[0].channel} ({bad[0].verdict})", "warn")
            if progress:
                progress({"scenarios_done": i + 1, "scenarios_total": len(scenarios), "unit": "scenarios"})
        # per-feature verdicts (only this module writes them)
        feature_verdicts: dict[str, str] = {}
        report_ev = None
        for fid, comps in results_by_feature.items():
            if not fid.startswith("scenario:"):
                verdict = "verified" if all(c.verdict == "pass" for c in comps) else ("partial" if any(c.verdict == "pass" for c in comps) and not any(c.verdict == "error" for c in comps) else "failed")
                feature_verdicts[fid] = verdict
        report = {"candidate_id": candidate_id, "build_hash": cand["build_hash"], "baseline_evidence": ev["evidence_id"], "environment": env,
                  "scenarios": scenario_results, "feature_verdicts": feature_verdicts, "verified_at": now_iso(),
                  "summary": {"scenarios": len(scenario_results), "passed": sum(1 for s in scenario_results if s["verdict"] == "pass"),
                              "failed": sum(1 for s in scenario_results if s["verdict"] == "fail"), "errors": sum(1 for s in scenario_results if s["verdict"] == "error")}}
        report_ev = self.cases.add_evidence(case_id, "verification_report", f"Verification of {candidate_id}", body=report,
                                            inputs={"candidate": candidate_id, "build_hash": cand["build_hash"], "baseline": ev["evidence_id"]}, producer="verifier")
        known = {f["feature_id"] for f in self.ledger.list(case_id)}
        for fid, verdict in feature_verdicts.items():
            if fid in known:
                self.ledger.set_verification(fid, verdict, candidate_id=candidate_id, evidence_id=report_ev["evidence_id"], writer=self.writer)
                if verdict in ("verified", "partial"):
                    # a measured pass proves a runnable implementation exists for this feature (impl state, not a verdict)
                    self.ledger.set_impl(fid, "runnable", evidence_ids=[report_ev["evidence_id"]])
        all_pass = bool(scenario_results) and all(s["verdict"] == "pass" for s in scenario_results)
        if not scenario_results:
            verdict = "untested"   # no scenarios declared: nothing was measured, never 'verified'
        elif all_pass:
            verdict = "verified"
        elif any(s["verdict"] == "pass" for s in scenario_results):
            verdict = "partial"
        else:
            verdict = "failed"
        self.candidates.set_verification(candidate_id, verdict, writer=self.writer)
        self.events.emit("verification.completed", {"candidate_id": candidate_id, "summary": report["summary"], "evidence_id": report_ev["evidence_id"]}, case_id=case_id)
        report["evidence_id"] = report_ev["evidence_id"]
        return report

    def _verify_web(self, sc: dict[str, Any], launch: dict[str, Any], dist: Path, work: Path, tolerance: dict[str, Any]) -> list[ComparisonResult]:
        import http.server, threading
        from functools import partial
        root = dist / launch.get("root", ".")
        port = _free_port()
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), partial(_QuietHandler, directory=str(root)))
        t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
        try:
            url = f"http://127.0.0.1:{port}/{launch.get('entry', 'index.html')}"
            rec = run_web_scenario(url, sc, work / "candidate")
        finally:
            srv.shutdown(); srv.server_close()
        base = dict(sc["expected"])
        # baseline screenshot path may be stored as evidence blob sha
        if base.get("screenshot_sha"):
            p = self.cases.blobs.path_for(base["screenshot_sha"])
            if p.exists():
                base["screenshot"] = str(p)
        return compare_web_capture(base, rec, tolerance={**tolerance, **sc.get("tolerance", {})}, artifacts_dir=work)

    def _record(self, case_id: str, candidate_id: str, feature_id: str | None, c: ComparisonResult, env: dict[str, Any], scenario_id: str) -> dict[str, Any]:
        cid = new_id("cmp")
        row = {"comparison_id": cid, "case_id": case_id, "candidate_id": candidate_id, "feature_id": feature_id, "channel": c.channel, "rule": c.rule,
               "tolerance": c.tolerance, "verdict": c.verdict, "original_hash": c.original_hash, "candidate_hash": c.candidate_hash,
               "details": {**c.details, "scenario": scenario_id}, "command": c.command, "environment": env, "artifacts": c.artifacts, "created_at": now_iso(), "writer": self.writer}
        self.db.insert("comparisons", row)
        self.events.emit("comparison.recorded", {"comparison_id": cid, "candidate_id": candidate_id, "feature_id": feature_id, "channel": c.channel, "verdict": c.verdict, "scenario": scenario_id}, case_id=case_id)
        return {"comparison_id": cid, "channel": c.channel, "rule": c.rule, "verdict": c.verdict}

    def comparisons(self, case_id: str, candidate_id: str | None = None, feature_id: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM comparisons WHERE case_id=?", [case_id]
        if candidate_id:
            sql += " AND candidate_id=?"; params.append(candidate_id)
        if feature_id:
            sql += " AND feature_id=?"; params.append(feature_id)
        rows = self.db.query(sql + " ORDER BY created_at", tuple(params))
        for r in rows:
            for k in ("tolerance", "details", "environment", "artifacts"):
                r[k] = loads(r[k], {} if k != "artifacts" else [])
        return rows

    def invalidate(self, case_id: str, reason: str, *, candidate_id: str | None = None) -> int:
        """Inputs changed (original files, assets, toolchain, baseline): verified verdicts become stale."""
        n = self.ledger.mark_stale(case_id, reason, candidate_id=candidate_id)
        if candidate_id:
            self.db.update("candidates", "candidate_id", candidate_id, {"verification": "stale"})
        else:
            self.db.execute("UPDATE candidates SET verification='stale' WHERE case_id=? AND verification IN ('verified','partial','failed')", (case_id,))
        self.events.emit("verification.invalidated", {"reason": reason, "features": n, "candidate_id": candidate_id}, case_id=case_id)
        return n


def _environment() -> dict[str, Any]:
    return {"os": platform.system(), "os_release": platform.release(), "machine": platform.machine(), "python": platform.python_version(),
            "locale": os.environ.get("LANG", ""), "tz": os.environ.get("TZ", "UTC"), "wine_available": shutil.which("wine") is not None, "host_certifies_windows": os.name == "nt",
            "note": "the runner actually used per comparison is recorded in each comparison's details.runner"}
