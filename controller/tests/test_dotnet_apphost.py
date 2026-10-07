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
