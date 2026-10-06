import json, sys, time
from pathlib import Path
sys.path.insert(0, "/home/user/decomp/controller")
from rebuild_controller.config import Settings, Limits, set_settings
from rebuild_controller.services import StudioServices
from rebuild_controller.jobs import JobState
FIX = Path("/home/user/decomp/fixtures")
data = Path("/tmp/claude-0/-home-user-decomp/c456138f-a927-5bb9-bf2d-fa1845ce6ed1/scratchpad/dotnet-demo")
s = Settings(data_dir=data, limits=Limits(max_stage_seconds=1500, lease_timeout_seconds=120)); set_settings(s); s.ensure_dirs()
st = StudioServices(s)
case = st.create_case(name="dotnetapp", source_root=str(FIX/"dotnetapp"/"original"), output_root=str(data/"out"), target_language="rust", output_type="exe",
    ai_policy={"mode": "no_ai"}, launch_profile={"baseline_file": str(FIX/"dotnetapp"/"expected"/"scenarios.json")}, settings={"decompile_limit": 120})
cid = case["case_id"]; st.start_rebuild(cid)
while st.runner.run_pending(max_jobs=1) or st.jobs.list(cid, [JobState.QUEUED, JobState.RUNNING]): pass
print(json.dumps(st.jobs.counts(cid)))
print("case", cid, [ (m["module_id"], m["rel_path"]) for m in st.cases.modules(cid)])
print("packet", [e["evidence_id"] for e in st.cases.list_evidence(cid, kind="ai_task_packet")])
print("M-IMPL", st.plan.get_item(st.plan.milestone_id(cid, "M-IMPL"))["blockers"])
st.stop()
