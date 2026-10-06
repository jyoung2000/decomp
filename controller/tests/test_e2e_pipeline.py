"""End-to-end rebuild on real fixtures through the durable DAG (no AI). Marked e2e: slow."""
import shutil
from pathlib import Path

import pytest

from rebuild_controller.jobs import JobState

FIX = Path(__file__).resolve().parents[2] / "fixtures"
pytestmark = pytest.mark.e2e


@pytest.fixture
def studio(settings):
    from rebuild_controller.services import StudioServices
    settings.limits.max_stage_seconds = 1500
    settings.limits.lease_timeout_seconds = 120
    s = StudioServices(settings)
    yield s
    s.stop()


def drain(studio, max_rounds=400):
    for _ in range(max_rounds):
        n = studio.runner.run_pending()
        pending = studio.jobs.list(None, [JobState.QUEUED, JobState.RUNNING])
        if n == 0 and not pending:
            return
    raise AssertionError("pipeline did not settle")


@pytest.mark.skipif(not (FIX / "pecli" / "original" / "pecli.exe").exists() or not shutil.which("wine"), reason="fixture/wine missing")
def test_pecli_full_pipeline_no_ai_is_honest(studio, tmp_path):
    orig = FIX / "pecli" / "original"
    out = tmp_path / "out"
    case = studio.create_case(name="pecli", source_root=str(orig), output_root=str(out), target_language="rust", output_type="exe",
                              ai_policy={"mode": "no_ai"}, launch_profile={"baseline_file": str(FIX / "pecli" / "expected" / "scenarios.json"),
                                                                            "scenarios": [{"id": "s", "feature_id": "pecli.file_roundtrip", "title": "init creates empty store", "critical": True}]},
                              settings={"decompile_limit": 30})
    cid = case["case_id"]
    studio.start_rebuild(cid)
    drain(studio)
    jobs = {j.stage: j for j in studio.jobs.list(cid)}
    assert jobs["inventory"].state == JobState.COMPLETED and jobs["analyze_module"].state == JobState.COMPLETED, {k: (v.state, v.error) for k, v in jobs.items()}
    rep = jobs["analyze_module"].result
    assert rep["functions_total"] > 20 and rep["decompiled"] > 0
    assert jobs["capture_original"].state == JobState.COMPLETED and jobs["reconstruct"].state == JobState.COMPLETED
    assert jobs["build_candidate"].state == JobState.COMPLETED  # scaffold builds
    assert jobs["compare_candidate"].state == JobState.COMPLETED
    assert jobs["deliver"].state == JobState.COMPLETED, jobs["deliver"].error
    # honest outcome: scaffold is not a remake → every scenario failed, M-IMPL blocked with next action, no parity
    cand = studio.candidates.list(cid)[-1]
    assert cand["verification"] == "failed"
    summary = studio.ledger.summary(cid)
    assert summary["full_parity"] is False
    impl = studio.plan.get_item(studio.plan.milestone_id(cid, "M-IMPL"))
    assert impl["status"] == "blocked" and "propose_candidate" in impl["blockers"][0]
    # delivered layout + manifest covering every file
    assert (out / "source" / "Cargo.toml").exists() and (out / "dist").is_dir() and (out / "reports" / "parity-report.md").exists()
    assert (out / "evidence" / "comparisons.json").exists() and (out / "reports" / "project-plan.html").exists()
    import json
    man = json.loads((out / "manifest.json").read_text())
    shipped = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert shipped == {f["path"] for f in man["files"]}
    md = (out / "reports" / "parity-report.md").read_text()
    assert "**Full parity:** NO" in md and "no AI calls were made" in md
    # progress is exact: known denominators for jobs, features counted separately
    prog = studio.plan.progress(cid)
    assert prog["groups"]["recovery"]["total"] == 1 and prog["groups"]["verification"]["total"] == 1
    # task packet exists for external clients and is bounded
    pk = studio.cases.list_evidence(cid, kind="ai_task_packet")
    assert pk and pk[0]["meta"]["bytes"] <= 200_000 + 2000


@pytest.mark.skipif(not (FIX / "pecli" / "original" / "pecli.exe").exists(), reason="fixture missing")
def test_crash_and_resume_keeps_progress(studio, tmp_path, settings):
    orig = FIX / "pecli" / "original"
    case = studio.create_case(name="pecli2", source_root=str(orig), output_root=str(tmp_path / "out"), target_language="rust", output_type="exe",
                              launch_profile={"baseline_file": str(FIX / "pecli" / "expected" / "scenarios.json")}, settings={"decompile_limit": 5})
    cid = case["case_id"]
    studio.start_rebuild(cid)
    studio.runner.run_pending(max_jobs=1)  # inventory done
    assert studio.jobs.list(cid, [JobState.COMPLETED])[0].stage == "inventory"
    # simulate a crash mid-run: claim a job and never heartbeat, then "restart"
    j = studio.jobs.claim_next("dying-worker")
    assert j is not None
    from rebuild_controller.services import StudioServices
    studio.db.close() if False else None
    st2 = StudioServices(settings)
    try:
        recovered = st2.jobs.recover_stale(now=__import__("time").time() + 10_000)
        assert j.job_id in recovered
        ok, why = st2.cases.is_resumable(cid)
        assert ok, why
        assert len(st2.jobs.list(cid, [JobState.COMPLETED])) == 1  # completed work preserved
        assert st2.plan.current_revision(cid) >= 2 and st2.cases.list_evidence(cid, kind="inventory")
        drain(st2)
        assert {j.stage for j in st2.jobs.list(cid, [JobState.COMPLETED])} >= {"inventory", "analyze_module", "deliver"}
    finally:
        st2.stop()
