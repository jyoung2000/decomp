"""Runs the verifier against the real fixtures: correct original (self-check) and the deliberately wrong remake."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

FIX = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.fixture
def studio(settings):
    from rebuild_controller.services import StudioServices
    s = StudioServices(settings)
    yield s
    s.stop()


@pytest.mark.skipif(not (FIX / "pecli" / "original" / "pecli.exe").exists() or (os.name != "nt" and not shutil.which("wine")) or not shutil.which("cargo"), reason="pecli fixture, cargo or (non-Windows) wine missing")
def test_pecli_oracle_rejects_wrong_remake_and_accepts_original(studio, tmp_path):
    from rebuild_controller.fixture_oracle import load_baseline_file
    orig = FIX / "pecli" / "original"
    case = studio.create_case(name="pecli", source_root=str(orig), output_root=str(tmp_path / "out"), target_language="rust", output_type="exe",
                              launch_profile={"baseline_file": str(FIX / "pecli" / "expected" / "scenarios.json")})
    cid = case["case_id"]
    bl = load_baseline_file(FIX / "pecli" / "expected" / "scenarios.json", orig)
    assert bl["kind"] == "cli" and len(bl["scenarios"]) == 8
    studio.verifier.freeze_baseline(cid, bl, producer="fixture_oracle")
    for sc in bl["scenarios"]:
        studio.ledger.add(cid, sc["feature_id"], feature_id=sc["feature_id"])
    # control 1: the original itself (via wine) must pass every scenario
    c = studio.candidates.create(cid, target_language="rust", output_type="exe", plan_revision=1, source_dir=orig)
    c = studio.candidates.mark_built(c["candidate_id"], orig)
    studio.db.update("candidates", "candidate_id", c["candidate_id"], {"meta": {"launch": {"type": "exe", "path": "pecli.exe"}}})
    rep = studio.verifier.verify_candidate(cid, c["candidate_id"])
    assert rep["summary"]["passed"] == 8 and rep["summary"]["failed"] == 0, rep["summary"]
    # control 2: the deliberately wrong Rust remake must be rejected
    wrong = FIX / "pecli" / "wrong_remake"
    build = tmp_path / "wrong"
    shutil.copytree(wrong, build, ignore=shutil.ignore_patterns("target"))
    subprocess.run(["cargo", "build", "--release", "-q"], cwd=build, check=True)
    exe = next((build / "target" / "release").glob("*"), None)
    rel = build / "target" / "release"
    if os.name == "nt":
        bins = [rel / "pecli.exe"] if (rel / "pecli.exe").is_file() else [p for p in rel.glob("*.exe")]   # no exec bit on Windows
        wrong_name = "pecli.exe"
    else:
        bins = [p for p in rel.iterdir() if p.is_file() and p.stat().st_mode & 0o111 and not p.suffix]
        wrong_name = "pecli"
    dist = tmp_path / "wrong-dist"; dist.mkdir(); shutil.copy2(bins[0], dist / wrong_name)
    w = studio.candidates.create(cid, target_language="rust", output_type="exe", plan_revision=1, source_dir=build)
    w = studio.candidates.mark_built(w["candidate_id"], dist)
    studio.db.update("candidates", "candidate_id", w["candidate_id"], {"meta": {"launch": {"type": "exe", "path": wrong_name}}})
    rep = studio.verifier.verify_candidate(cid, w["candidate_id"])
    assert rep["summary"]["passed"] == 0 and rep["summary"]["failed"] == 8, rep["summary"]
    assert studio.candidates.get(w["candidate_id"])["verification"] == "failed"
    assert studio.candidates.last_known_good(cid)["candidate_id"] == c["candidate_id"]
    assert studio.ledger.summary(cid)["full_parity"] is False


@pytest.mark.skipif(not (FIX / "dotnetapp" / "original" / "dotnetapp.dll").exists() or not shutil.which("dotnet"), reason="dotnet fixture missing")
def test_dotnetapp_oracle_self_check(studio, tmp_path):
    from rebuild_controller.fixture_oracle import load_baseline_file
    orig = FIX / "dotnetapp" / "original"
    case = studio.create_case(name="dotnetapp", source_root=str(orig), output_root=str(tmp_path / "out"), target_language="rust", output_type="exe")
    cid = case["case_id"]
    bl = load_baseline_file(FIX / "dotnetapp" / "expected" / "scenarios.json", orig)
    studio.verifier.freeze_baseline(cid, bl, producer="fixture_oracle")
    c = studio.candidates.create(cid, target_language="rust", output_type="exe", plan_revision=1, source_dir=orig)
    c = studio.candidates.mark_built(c["candidate_id"], orig)
    studio.db.update("candidates", "candidate_id", c["candidate_id"], {"meta": {"launch": {"type": "dotnet", "path": "dotnetapp.dll"}}})
    rep = studio.verifier.verify_candidate(cid, c["candidate_id"])
    assert rep["summary"]["passed"] == len(bl["scenarios"]) == 9, rep["summary"]
