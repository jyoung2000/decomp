"""Schedules the rebuild DAG for a case. One user action → many durable, resumable jobs."""
from __future__ import annotations

from typing import Any


def schedule_rebuild(studio, case_id: str) -> dict[str, Any]:
    case = studio.cases.get_case(case_id)
    jobs = studio.jobs
    plan = studio.plan
    mid = lambda s: plan.milestone_id(case_id, s)  # noqa: E731
    inv = jobs.create(case_id, "inventory", "Inventory installation root", {"root": case["source_root"]}, milestone_id="M-ANALYSIS", priority=10)
    plan.link_job(mid("M-ANALYSIS"), inv.job_id)
    dep = jobs.create(case_id, "dependency_graph", "Build dependency graph", {}, depends_on=[inv.job_id], milestone_id="M-ASSETS", priority=20)
    plan.link_job(mid("M-ASSETS"), dep.job_id)
    # feature discovery waits for the recovery jobs that inventory fans out: inventory adds them as dependencies of this summary job.
    barrier = jobs.create(case_id, "barrier", "Recovery summary", {}, depends_on=[inv.job_id, dep.job_id], milestone_id="M-RECOVERY", priority=50)
    plan.link_job(mid("M-RECOVERY"), barrier.job_id)
    feats = jobs.create(case_id, "discover_features", "Discover features", {}, depends_on=[barrier.job_id], milestone_id="M-FEATURES", priority=60)
    plan.link_job(mid("M-FEATURES"), feats.job_id)
    lp = case.get("launch_profile", {})
    deps_for_recon = [feats.job_id]
    if lp.get("execute_original") or lp.get("baseline_file"):
        cap = jobs.create(case_id, "capture_original", "Capture original behaviour (authorized)", {}, depends_on=[feats.job_id], milestone_id="M-FEATURES", priority=61)
        plan.link_job(mid("M-FEATURES"), cap.job_id)
        deps_for_recon.append(cap.job_id)
    recon = jobs.create(case_id, "reconstruct", f"Reconstruct in {case['target_language']}", {}, depends_on=deps_for_recon, milestone_id="M-IMPL", priority=70)
    plan.link_job(mid("M-IMPL"), recon.job_id)
    # build/compare/deliver are created by reconstruct (it knows the candidate id); deliver is created by compare/repair chain end.
    studio.cases.set_case_status(case_id, "running")
    plan.revise(case_id, "rebuild started: analysis and recovery jobs scheduled")
    return {"job_ids": [inv.job_id, dep.job_id, barrier.job_id, feats.job_id, recon.job_id], "case_id": case_id}


def stage_barrier(ctx) -> dict[str, Any]:
    """Runs after every recovery job the inventory fanned out has completed (dynamic dependencies). Summarises recovery."""
    from .jobs import JobState
    st = ctx.services["studio"]
    done = [j for j in st.jobs.list(ctx.job.case_id) if j.stage in ("analyze_module", "recover_managed", "recover_engine", "recover_web")]
    failed = [j.job_id for j in done if j.state == JobState.FAILED]
    st.plan.update_item(st.plan.milestone_id(ctx.job.case_id, "M-RECOVERY"), status="completed" if not failed else "failed")
    return {"recovery_jobs": len(done), "completed": sum(1 for j in done if j.state == JobState.COMPLETED), "failed": failed}
