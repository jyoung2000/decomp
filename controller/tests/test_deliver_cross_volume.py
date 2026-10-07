"""Delivery to an output folder on a different drive than the data folder.

Found on a genuine Windows install: the data folder lives in %LOCALAPPDATA% (C:) and the user picked an output folder on D:;
publishing staged under the data folder and os.replace() across drives failed with WinError 17. Delivery now stages inside the
output folder. This test uses a real second volume when one exists (skips otherwise)."""
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

from rebuild_controller.jobs import JobState

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "webapp" / "original"


def _other_volume_dir() -> Path | None:
    temp_drive = Path(tempfile.gettempdir()).drive.upper()
    if os.name != "nt" or not temp_drive:
        return None
    for letter in "DEFGH":
        root = Path(f"{letter}:\\")
        if f"{letter}:" != temp_drive and root.exists():
            try:
                d = root / f"rs-xvol-test-{uuid.uuid4().hex[:8]}"
                d.mkdir()
                return d
            except OSError:
                continue
    return None


def test_delivery_to_another_drive_than_the_data_folder(settings):
    out_parent = _other_volume_dir()
    if out_parent is None:
        pytest.skip("needs Windows with a second writable drive")
    from rebuild_controller.services import StudioServices
    settings.limits.max_stage_seconds = 600
    settings.limits.lease_timeout_seconds = 120
    assert Path(settings.data_dir).drive.upper() != out_parent.drive.upper()
    st = StudioServices(settings)
    try:
        out = out_parent / "out folder with spaces"
        case = st.create_case(name="xvol", source_root=str(FIX), output_root=str(out), target_language="web", output_type="web",
                              ai_policy={"mode": "no_ai"})
        cid = case["case_id"]
        st.start_rebuild(cid)
        for _ in range(400):
            if st.runner.run_pending() == 0 and not st.jobs.list(None, [JobState.QUEUED, JobState.RUNNING]):
                break
        deliver = [j for j in st.jobs.list(cid) if j.stage == "deliver"]
        assert deliver and deliver[0].state == JobState.COMPLETED, [(j.stage, j.state, j.error) for j in st.jobs.list(cid)]
        assert (out / "manifest.json").is_file() and (out / "source").is_dir() and (out / "dist").is_dir()
        assert not (out / ".rebuild-studio-publishing").exists()   # staging removed after publication
    finally:
        st.stop()
        shutil.rmtree(out_parent, ignore_errors=True)
