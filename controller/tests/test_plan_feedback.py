"""Plan / preview / feedback workflow against the real controller on the web fixture (no AI, real Playwright comparisons)."""
import shutil
from pathlib import Path

import pytest

from rebuild_controller.jobs import JobState

FIX = Path(__file__).resolve().parents[2] / "fixtures"
pytestmark = pytest.mark.e2e


@pytest.fixture
def studio(settings):
    from rebuild_controller.services import StudioServices
    settings.limits.max_stage_seconds = 900
    settings.limits.lease_timeout_seconds = 120   # browser comparisons outlive the 2 s test default lease
    s = StudioServices(settings)
    yield s
    s.stop()


def drain(studio):
    for _ in range(400):
        n = studio.runner.run_pending()
        if n == 0 and not studio.jobs.list(None, [JobState.QUEUED, JobState.RUNNING]):
            return
    raise AssertionError("did not settle")


@pytest.mark.skipif(not (FIX / "webapp" / "original" / "index.html").exists() or not shutil.which("node"), reason="web fixture/node missing")
def test_preview_feedback_retest_cycle(studio, tmp_path, settings):
    import urllib.request
    case = studio.create_case(name="webapp", source_root=str(FIX / "webapp" / "original"), output_root=str(tmp_path / "out"), target_language="web", output_type="pwa",
                              launch_profile={"execute_original": True, "kind": "web", "launch": {"type": "web", "root": ".", "entry": "index.html"},
                                              "scenarios": [{"id": "load", "feature_id": "webapp.render_notes", "title": "Notes view renders", "critical": True, "text_selectors": ["h1", "#app"], "actions": [{"type": "wait_sw"}]}]})
    cid = case["case_id"]
    rev0 = studio.plan.current_revision(cid)
    studio.start_rebuild(cid)
    drain(studio)
    assert studio.plan.current_revision(cid) > rev0
    # a real, versioned preview exists for the built candidate
    previews = studio.previews.list(cid)
    diag = [(r["channel"], r["rule"], r["verdict"], str(r["details"])[-700:]) for r in studio.db.query("SELECT * FROM comparisons WHERE case_id=?", (cid,)) if r["verdict"] != "pass"]
    diag += [(j.stage, j.state.value, (j.error or "")[:300]) for j in studio.jobs.list(cid) if j.state != JobState.COMPLETED]
    assert previews and previews[0]["kind"] == "real" and previews[0]["build_hash"] and previews[0]["verification"] == "verified", diag
    opened = studio.previews.open(previews[0]["preview_id"])
    assert opened["opened"] and opened["kind"] == "browser"
    html = urllib.request.urlopen(opened["url"].split("#")[0], timeout=10).read().decode()
    assert "<html" in html.lower()
    assert studio.previews.stop(opened["instance_id"]) is True
    assert studio.previews.running() == []
    # feedback tied to the exact candidate/plan revision is persisted and survives a restart
    cand = studio.candidates.list(cid)[-1]
    fb = studio.feedback.create(cid, target_kind="preview", target_id=previews[0]["preview_id"], candidate_id=cand["candidate_id"], classification="bug", priority="high",
                                comment="title should be bold", expected="bold", actual="regular")
    assert fb["status"] == "received" and fb["candidate_id"] == cand["candidate_id"] and fb["plan_revision"] == studio.plan.current_revision(cid)
    fb = studio.feedback.triage(fb["feedback_id"], create_work=True)
    assert fb["status"] == "queued" and fb["linked_items"]
    item = studio.plan.get_item(fb["linked_items"][0])
    assert item["kind"] == "deliverable" and item["acceptance"]
    # user acceptance and machine verification are separate properties
    feat = studio.ledger.get("webapp.render_notes")
    assert feat["verify_status"] == "verified" and feat["user_review"] is None
    studio.ledger.set_user_review("webapp.render_notes", "rejected")
    assert studio.ledger.get("webapp.render_notes")["verify_status"] == "verified"  # feedback never rewrites verdicts
    # a new candidate makes earlier previews historical and open feedback ready-to-retest; verdicts of the old candidate go stale
    studio.feedback.set_status(fb["feedback_id"], "in_progress", "fix underway")
    new = studio.candidates.propose(cid, {"site/extra.txt": "x"}, note="fix", author="user", base_candidate=cand["candidate_id"], plan_revision=studio.plan.current_revision(cid))
    b = studio.jobs.create(cid, "build_candidate", "build", {"candidate_id": new["candidate_id"]})
    drain(studio)
    assert studio.jobs.get(b.job_id).state == JobState.COMPLETED
    assert studio.feedback.get(fb["feedback_id"])["status"] == "ready_to_retest"
    assert studio.previews.get(previews[0]["preview_id"])["stale"] is True
    assert studio.ledger.get("webapp.render_notes")["verify_status"] == "stale"
    # restart: everything is still there
    from rebuild_controller.services import StudioServices
    st2 = StudioServices(settings)
    try:
        assert st2.feedback.get(fb["feedback_id"])["status"] == "ready_to_retest"
        assert len(st2.previews.list(cid)) == 2 and st2.plan.current_revision(cid) >= 3
        exp = __import__("rebuild_controller.export.report", fromlist=["export_plan"]).export_plan(st2, cid, tmp_path / "exp")
        assert Path(exp["json"]).exists() and Path(exp["html"]).exists()
        reopened = st2.feedback.reopen(fb["feedback_id"], "still wrong")
        assert reopened["status"] == "reopened" and len(reopened["history"]) >= 5
    finally:
        st2.stop()
