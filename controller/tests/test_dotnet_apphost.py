"""A .NET app's native launcher (app.exe next to app.dll) must not route to native analysis: found on a genuine install where a
missing Rizin blocked recovery of a .NET app whose C# ILSpy could recover."""
from pathlib import Path

import pytest

from rebuild_controller.jobs import JobState

ORIG = Path(__file__).resolve().parents[2] / "fixtures" / "dotnetapp" / "original"


@pytest.mark.skipif(not (ORIG / "dotnetapp.dll").exists(), reason="dotnet fixture missing")
def test_dotnet_apphost_is_not_sent_to_native_analysis(settings, tmp_path):
    from rebuild_controller.services import StudioServices
    st = StudioServices(settings)
    try:
        case = st.create_case(name="apphost", source_root=str(ORIG), output_root=str(tmp_path / "out"), target_language="rust",
                              output_type="exe", ai_policy={"mode": "no_ai"})
        cid = case["case_id"]
        inv = st.jobs.create(cid, "inventory", "inventory", {})
        for _ in range(20):
            if st.runner.run_pending() == 0:
                break
        assert st.jobs.get(inv.job_id).state == JobState.COMPLETED
        mods = {m["module_id"]: m["rel_path"] for m in st.cases.modules(cid)}
        jobs = [(j.stage, (j.inputs or {}).get("module_id")) for j in st.jobs.list(cid)]
        analysed = [mods.get(mid) for stage, mid in jobs if stage == "analyze_module"]
        assert not any(str(p).lower().endswith("dotnetapp.exe") for p in analysed), analysed
        assert any(stage == "recover_managed" for stage, _ in jobs)
    finally:
        st.stop()


def test_failed_job_settles_case_status_and_resume_requeues(settings, tmp_path):
    """Found on a real install: a failed delivery left the case 'running' forever with Resume disabled."""
    from rebuild_controller.services import StudioServices
    st = StudioServices(settings)
    try:
        src = tmp_path / "src"; src.mkdir(); (src / "a.txt").write_text("x")
        cid = st.create_case(name="settle", source_root=str(src), output_root=str(tmp_path / "out"), target_language="web",
                             output_type="web", ai_policy={"mode": "no_ai"})["case_id"]
        st.cases.set_case_status(cid, "running")
        j = st.jobs.create(cid, "inventory", "inventory", {})
        claimed = st.jobs.claim_next("w1")
        assert claimed and claimed.job_id == j.job_id
        st.jobs.fail(j.job_id, "w1", "boom", retry=False)
        assert st.cases.get_case(cid)["status"] == "failed"
        resumed = st.resume(case_id=cid)
        assert j.job_id in resumed and st.jobs.get(j.job_id).state == JobState.QUEUED
        assert st.cases.get_case(cid)["status"] == "running"
    finally:
        st.stop()


def test_forecast_counts_recorded_user_scenarios_as_verifiable(settings, tmp_path):
    """Found on a real install: a project with 7 recorded user scenarios was told 'No baseline or scenarios are declared'."""
    from rebuild_controller.services import StudioServices
    st = StudioServices(settings)
    try:
        src = tmp_path / "src"; src.mkdir(); (src / "a.txt").write_text("x")
        case = st.create_case(name="fc", source_root=str(src), output_root=str(tmp_path / "out"), target_language="rust",
                              output_type="exe", ai_policy={"mode": "no_ai"})
        assert case["implementation_forecast"]["verifiable"] is False
        st.cases.add_evidence(case["case_id"], "baseline", "user scenarios", body={"frozen": True, "scenarios": [{"id": "s1"}]})
        fc = st.implementation_forecast(st.cases.get_case(case["case_id"]))
        assert fc["verifiable"] is True
        assert not any("No baseline or scenarios" in d for d in fc["details"])
    finally:
        st.stop()


def test_capture_stage_reuses_baseline_recorded_from_user_scenarios(settings, tmp_path):
    """Found on a real install: scenarios recorded in the Scenarios tab (program chosen there) left the rebuild blocked at
    'configure how the original is started'. The stage must reuse that frozen baseline, not block or overwrite it."""
    from rebuild_controller.services import StudioServices
    st = StudioServices(settings)
    try:
        src = tmp_path / "src"; src.mkdir(); (src / "app.exe").write_bytes(b"MZ")
        cid = st.create_case(name="reuse", source_root=str(src), output_root=str(tmp_path / "out"), target_language="rust",
                             output_type="exe", ai_policy={"mode": "no_ai"})["case_id"]
        st.cases.set_original_execution_consent(cid, True, via="test")
        bl = {"kind": "cli", "launch": {"type": "exe", "path": "app.exe"}, "tolerance": {},
              "scenarios": [{"id": "usc_1", "steps": [{"args": [], "stdin": ""}], "expected": {"steps": [], "files": {}}}]}
        frozen = st.verifier.freeze_baseline(cid, bl, producer="capture_original", title="Baseline revision 1 (with your scenarios)")
        job = st.jobs.create(cid, "capture_original", "Capture original behaviour (authorized)", {})
        for _ in range(5):
            if st.runner.run_pending() == 0:
                break
        j = st.jobs.get(job.job_id)
        assert j.state == JobState.COMPLETED, (j.state, j.blocker, j.error)
        ev, kept = st.verifier.load_baseline(cid)
        assert ev["evidence_id"] == frozen["evidence_id"] and [s["id"] for s in kept["scenarios"]] == ["usc_1"]
    finally:
        st.stop()
