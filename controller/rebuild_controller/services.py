"""StudioServices: the single internal service API used by GUI (api/server.py), CLI, Cutter plugin and MCP.

All model-facing operations go through here. Models can request work and read bounded evidence; they can never write
verification verdicts, immutable originals, or the trusted baseline.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .adapters.registry import BackendRegistry
from .cases import CaseStore
from .config import Settings, get_settings
from .events import EventLog
from .jobs import JobRunner, JobState, JobStore, StageRegistry
from .store.db import Database


class StudioServices:
    def __init__(self, settings: Settings | None = None, *, start_runner: bool = False):
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.db = Database(self.settings.db_path)
        self.events = EventLog(self.db)
        self.jobs = JobStore(self.db, self.events, max_queue=self.settings.limits.max_queue_length,
                             lease_timeout=self.settings.limits.lease_timeout_seconds)
        self.cases = CaseStore(self.db, self.events, self.settings)
        self.registry = BackendRegistry(self.settings.data_dir)
        self.stages = StageRegistry()
        self.services: dict[str, Any] = {"studio": self}
        self.runner = JobRunner(self.jobs, self.events, self.stages, self.settings.limits, self.services)
        self._lock = threading.RLock()
        self._wire()
        if start_runner:
            self.runner.start()

    def _wire(self) -> None:
        """Register backends, stages and sub-services. Imports are local so optional components fail soft."""
        from .backends import register_backends
        from .stages import register_stages
        register_backends(self.registry, self.settings)
        register_stages(self.stages)
        from .plan import PlanStore
        from .feedback import FeedbackStore
        from .ledger import FeatureLedger
        from .verifier import Verifier
        from .candidates import CandidateStore
        from .previews import PreviewManager
        from .knowledge.store import KnowledgeStore
        self.ledger = FeatureLedger(self.db, self.events)
        self.plan = PlanStore(self.db, self.events, self.jobs, self.ledger)
        self.feedback = FeedbackStore(self.db, self.events, self.plan, self.cases)
        self.candidates = CandidateStore(self.db, self.events, self.cases)
        self.verifier = Verifier(self.db, self.events, self.cases, self.candidates, self.ledger)
        self.previews = PreviewManager(self.db, self.events, self.candidates, self.cases)
        self.knowledge = KnowledgeStore(self.db, self.events, self.settings)
        self.events.subscribe(self._settle_case_status)
        # Optional sub-services (fail soft so the core keeps working; doctor reports them).
        self.budgets = self.connections = self.ai = None
        self.optional_errors: dict[str, str] = {}
        try:
            from .budget import BudgetLedger
            self.budgets = BudgetLedger(self.db, self.events)
        except Exception as e:  # pragma: no cover
            self.optional_errors["budget"] = f"{type(e).__name__}: {e}"
        try:
            from .providers.connections import ConnectionStore
            self.connections = ConnectionStore(self.db, self.events, self.settings)
        except Exception as e:  # pragma: no cover
            self.optional_errors["connections"] = f"{type(e).__name__}: {e}"
        try:
            from .providers.router import AIClient
            from .providers.jev import JeVRouter
            advisor = None
            try:
                advisor = JeVRouter(self.budgets, self.events, self.settings.data_dir)
            except Exception as e:  # advisory only
                self.optional_errors["jev"] = f"{type(e).__name__}: {e}"
            self.ai = AIClient(self.connections, self.budgets, self.events, self.db, advisor=advisor)
            try:
                self.budgets.sweep_stale(3600)
            except Exception:
                pass
        except Exception as e:  # pragma: no cover
            self.optional_errors["ai"] = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.runner.start()

    def stop(self) -> None:
        try:
            self.previews.stop_all()
        except Exception:
            pass
        self.runner.stop()
        for bid in self.registry.ids():
            b = self.registry.get(bid)
            if hasattr(b, "close"):
                try:
                    b.close()
                except Exception:
                    pass
        self.db.close()

    # ------------------------------------------------------------------ typed operations (MCP/CLI/API share these)
    def create_case(self, **kwargs: Any) -> dict[str, Any]:
        case = self.cases.create_case(**kwargs)
        self.plan.initialize(case["case_id"], case)
        case["implementation_forecast"] = self.implementation_forecast(case)
        return case

    def implementation_forecast(self, case: dict[str, Any]) -> dict[str, Any]:
        """Plain-language statement, before the case starts, of whether an implementation can be produced for this profile/target/AI mode."""
        from .implement import forecast_for_case
        return forecast_for_case(self, case)

    def start_rebuild(self, case_id: str) -> dict[str, Any]:
        from .pipeline import schedule_rebuild
        out = schedule_rebuild(self, case_id)
        out["implementation_forecast"] = self.implementation_forecast(self.cases.get_case(case_id))
        return out

    def doctor(self, smoke: bool = False, verify: bool = False) -> dict[str, Any]:
        return self.registry.doctor(smoke=smoke, verify=verify)

    def job_status(self, job_id: str) -> dict[str, Any]:
        return self.jobs.get(job_id).to_dict()

    def cancel(self, job_id: str | None = None, case_id: str | None = None) -> list[str]:
        if job_id:
            return self.jobs.cancel(job_id)
        if case_id:
            ids = self.jobs.cancel_case(case_id)
            self.cases.set_case_status(case_id, "cancelled")
            return ids
        return []

    def _settle_case_status(self, ev: dict[str, Any]) -> None:
        """When the last active job of a running case ends without delivery, move the case out of 'running':
        'failed' if any job failed, else 'blocked' if any job is blocked. Found on a real install: a failed delivery left
        the case 'running' forever with Resume disabled. Never raises (event-bus subscriber)."""
        try:
            if ev.get("kind") not in ("job.failed", "job.blocked", "job.cancelled") or not ev.get("case_id"):
                return
            cid = ev["case_id"]
            if self.cases.get_case(cid).get("status") != "running":
                return
            states = {j.state for j in self.jobs.list(cid)}
            if states & {JobState.QUEUED, JobState.RUNNING}:
                return
            if JobState.FAILED in states:
                self.cases.set_case_status(cid, "failed")
            elif JobState.BLOCKED in states:
                self.cases.set_case_status(cid, "blocked")
        except Exception:  # noqa: BLE001 - status is advisory; never break event delivery
            pass

    def resume(self, job_id: str | None = None, case_id: str | None = None) -> list[str]:
        if job_id:
            return [self.jobs.resume(job_id).job_id]
        if case_id:
            ok, why = self.cases.is_resumable(case_id)
            if not ok:
                raise ValueError(f"case cannot be resumed: {why}")
            out = [self.jobs.resume(j.job_id).job_id for j in self.jobs.list(case_id, [JobState.FAILED, JobState.CANCELLED, JobState.NEEDS_RETEST, JobState.BLOCKED])]
            self.cases.set_case_status(case_id, "running")
            return out
        return []

    def search_evidence(self, case_id: str, query: str, kinds: list[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.cases.search_evidence(case_id, query, kinds=kinds, limit=limit)

    def get_evidence(self, evidence_id: str, max_bytes: int | None = None) -> dict[str, Any]:
        ev = self.cases.get_evidence(evidence_id)
        ev["body"] = self.cases.evidence_body(evidence_id, max_bytes=max_bytes or self.settings.limits.max_context_bytes)
        return ev

    def get_function_briefing(self, case_id: str, module_id: str, function: str) -> dict[str, Any]:
        from .backends.native import function_briefing
        return function_briefing(self, case_id, module_id, function)

    def propose_candidate(self, case_id: str, files: dict[str, str], note: str = "", *, author: str = "model",
                          base_candidate: str | None = None) -> dict[str, Any]:
        """Models propose source files; they land in a new staged candidate (never in the original or the baseline)."""
        return self.candidates.propose(case_id, files, note=note, author=author, base_candidate=base_candidate,
                                       plan_revision=self.plan.current_revision(case_id))

    def build_candidate(self, case_id: str, candidate_id: str) -> dict[str, Any]:
        job = self.jobs.create(case_id, "build_candidate", f"Build candidate {candidate_id}", {"candidate_id": candidate_id})
        return job.to_dict()

    def compare_candidate(self, case_id: str, candidate_id: str, feature_ids: list[str] | None = None) -> dict[str, Any]:
        job = self.jobs.create(case_id, "compare_candidate", f"Compare candidate {candidate_id}",
                               {"candidate_id": candidate_id, "feature_ids": feature_ids or []})
        return job.to_dict()

    def propose_knowledge(self, **kwargs: Any) -> dict[str, Any]:
        return self.knowledge.propose(**kwargs)

    def validate_knowledge(self, knowledge_id: str) -> dict[str, Any]:
        return self.knowledge.validate(knowledge_id)

    def deliver(self, case_id: str, candidate_id: str | None = None) -> dict[str, Any]:
        """Publish a candidate (default: last known good, else newest built) into the output root."""
        if candidate_id is None:
            lkg = self.candidates.last_known_good(case_id)
            built = [c for c in self.candidates.list(case_id) if c["build_status"] == "built"]
            if lkg:
                candidate_id = lkg["candidate_id"]
            elif built:
                candidate_id = built[-1]["candidate_id"]
            else:
                raise ValueError("no built candidate to deliver")
        job = self.jobs.create(case_id, "deliver", "Publish source/dist/evidence/reports", {"candidate_id": candidate_id}, milestone_id="M-DELIVER", max_attempts=1)
        self.plan.link_job(self.plan.milestone_id(case_id, "M-DELIVER"), job.job_id)
        return job.to_dict()

    def capture_original(self, case_id: str, scenario_id: str | None = None) -> dict[str, Any]:
        job = self.jobs.create(case_id, "capture_original", "Capture original behaviour", {"scenario_id": scenario_id})
        return job.to_dict()
