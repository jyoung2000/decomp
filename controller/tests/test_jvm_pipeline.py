"""A Java application goes through the real pipeline (inventory -> recover_jvm) instead of 'unsupported'. Marked e2e."""
import hashlib
import shutil
import tempfile
import urllib.request
from pathlib import Path

import pytest

from rebuild_controller.backends import jvm
from rebuild_controller.jobs import JobState

FIX = Path(__file__).resolve().parents[2] / "fixtures"
CACHE = Path(tempfile.gettempdir()) / "rebuild-studio-test-tools"   # shared with test_jvm.py
pytestmark = pytest.mark.e2e


def _cfr_tools_dir() -> Path:
    jar = CACHE / "cfr" / "cfr-0.152.jar"
    if not jar.exists():
        jar.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(jvm.CFR_URL, timeout=120) as r:   # noqa: S310 - pinned https URL from the lock
                data = r.read()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"CFR not cached and download failed: {e}")
        assert hashlib.sha256(data).hexdigest() == jvm.CFR_SHA256
        jar.write_bytes(data)
    return CACHE


@pytest.fixture
def studio(settings):
    from rebuild_controller.services import StudioServices
    if not shutil.which("java"):
        pytest.skip("no Java runtime on this host")
    settings.tools_dir = _cfr_tools_dir()
    settings.limits.max_stage_seconds = 600
    s = StudioServices(settings)
    yield s
    s.stop()


def drain(studio, max_rounds=200):
    for _ in range(max_rounds):
        n = studio.runner.run_pending()
        if n == 0 and not studio.jobs.list(None, [JobState.QUEUED, JobState.RUNNING]):
            return
    raise AssertionError("pipeline did not settle")


def test_java_app_is_recovered_through_the_pipeline(studio, tmp_path):
    orig = FIX / "javacli" / "original"
    case = studio.create_case(name="javacli", source_root=str(orig), output_root=str(tmp_path / "out"), target_language="rust",
                              output_type="exe", ai_policy={"mode": "no_ai"})
    cid = case["case_id"]
    studio.start_rebuild(cid)
    drain(studio)
    jobs = {j.stage: j for j in studio.jobs.list(cid)}
    assert "recover_jvm" in jobs, sorted(jobs)
    assert jobs["recover_jvm"].state == JobState.COMPLETED, (jobs["recover_jvm"].state, jobs["recover_jvm"].error)
    reports = [e for e in studio.cases.list_evidence(cid, kind="module_report") if e["title"].startswith("JVM recovery")]
    assert reports, "no JVM recovery evidence"
    recovered = list((studio.cases.case_root(cid) / "recovered").rglob("*.java"))
    assert len(recovered) >= 4, recovered
