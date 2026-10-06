import subprocess
from pathlib import Path

import pytest

from rebuild_controller.verifier import BaselineError

C_SRC = r'''
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv){
  if(argc<2){fprintf(stderr,"usage\n");return 1;}
  if(!strcmp(argv[1],"hello")){puts("hello world");return 0;}
  if(!strcmp(argv[1],"write")){FILE*f=fopen(argv[2],"wb");fputs("DATA1",f);fclose(f);puts("ok");return 0;}
  if(!strcmp(argv[1],"fail")){fprintf(stderr,"bad\n");return 2;}
  return 1;}
'''
C_WRONG = C_SRC.replace('"hello world"', '"hello wrold"').replace("return 2;", "return 3;")


def _build(tmp: Path, name: str, src: str, *, pe: bool) -> Path:
    s = tmp / f"{name}.c"; s.write_text(src)
    out = tmp / name / ("app.exe" if pe else "app"); out.parent.mkdir(parents=True, exist_ok=True)
    cc = ["x86_64-w64-mingw32-gcc"] if pe else ["gcc"]
    subprocess.run(cc + ["-O1", "-o", str(out), str(s)], check=True)
    return out.parent


@pytest.fixture
def studio(settings):
    from rebuild_controller.services import StudioServices
    s = StudioServices(settings)
    yield s
    s.stop()


def _case(studio, tmp_path, src_dir):
    out = tmp_path / "out"
    return studio.create_case(name="v", source_root=str(src_dir), output_root=str(out), target_language="rust", output_type="exe")


def _baseline_from_run(studio, case_id, root: Path, launch: dict, scenarios: list[dict], work: Path):
    from rebuild_controller.comparators.cli import run_steps, snapshot_work
    bl = {"kind": "cli", "launch": launch, "scenarios": []}
    for sc in scenarios:
        w = work / sc["id"]
        runs = run_steps(launch, root, sc["steps"], w)
        bl["scenarios"].append({**sc, "expected": {"steps": runs, "files": snapshot_work(w)}})
    return studio.verifier.freeze_baseline(case_id, bl, producer="capture_original")


SCENARIOS = [
    {"id": "hello", "feature_id": "f.hello", "steps": [{"args": ["hello"]}]},
    {"id": "write", "feature_id": "f.write", "steps": [{"args": ["write", "{work}/o.txt"]}]},
    {"id": "fail", "feature_id": "f.fail", "steps": [{"args": ["fail"]}], "channels": ["exit_code", "stdout", "stderr"]},
]


def test_verifier_accepts_correct_and_rejects_wrong_remake(studio, tmp_path):
    orig = _build(tmp_path, "orig", C_SRC, pe=True)           # original is a PE (run via wine)
    good = _build(tmp_path, "good", C_SRC, pe=False)          # remake as native ELF
    wrong = _build(tmp_path, "wrong", C_WRONG, pe=False)
    case = _case(studio, tmp_path, orig)
    cid = case["case_id"]
    for f in ("f.hello", "f.write", "f.fail"):
        studio.ledger.add(cid, f, feature_id=f, critical=(f == "f.hello"))
    _baseline_from_run(studio, cid, orig, {"type": "exe", "path": "app.exe"}, SCENARIOS, tmp_path / "bw")
    for name, root in (("good", good), ("wrong", wrong)):
        c = studio.candidates.create(cid, target_language="rust", output_type="exe", plan_revision=1, source_dir=root)
        c = studio.candidates.mark_built(c["candidate_id"], root)
        studio.db.update("candidates", "candidate_id", c["candidate_id"], {"meta": {"launch": {"type": "exe", "path": "app"}}})
        rep = studio.verifier.verify_candidate(cid, c["candidate_id"])
        if name == "good":
            assert rep["summary"] == {"scenarios": 3, "passed": 3, "failed": 0, "errors": 0}
            assert studio.candidates.get(c["candidate_id"])["verification"] == "verified"
            assert studio.candidates.last_known_good(cid)["candidate_id"] == c["candidate_id"]
            assert all(f["verify_status"] == "verified" for f in studio.ledger.list(cid))
            assert studio.ledger.summary(cid)["full_parity"] is True
        else:
            assert rep["summary"]["failed"] == 2 and rep["summary"]["passed"] == 1
            assert rep["feature_verdicts"] == {"f.hello": "partial", "f.write": "verified", "f.fail": "partial"}
            assert studio.candidates.get(c["candidate_id"])["verification"] == "partial"
            s = studio.ledger.summary(cid)
            assert s["full_parity"] is False and s["critical_incomplete"]
    comps = studio.verifier.comparisons(cid)
    assert all(r["writer"] == "verifier" for r in comps)
    assert any(r["channel"] == "stdout" and r["verdict"] == "fail" and r["details"]["diff_at"] == 7 for r in comps)
    # the last-known-good is still the good candidate after the wrong one
    assert studio.candidates.last_known_good(cid)["meta"]["launch"]["path"] == "app"


def test_ai_cannot_write_verdicts(studio, tmp_path, src_out):
    src, out = src_out
    case = studio.create_case(name="x", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe")
    f = studio.ledger.add(case["case_id"], "feat")
    with pytest.raises(PermissionError):
        studio.ledger.set_verification(f["feature_id"], "verified", candidate_id=None, evidence_id=None, writer="model")
    c = studio.candidates.create(case["case_id"], target_language="rust", output_type="exe", plan_revision=1)
    with pytest.raises(PermissionError):
        studio.candidates.set_verification(c["candidate_id"], "verified", writer="model")
    with pytest.raises(BaselineError):
        studio.verifier.freeze_baseline(case["case_id"], {"scenarios": []}, producer="model")
    # evidence a model stores as 'baseline' is ignored by the verifier
    studio.cases.add_evidence(case["case_id"], "baseline", "fake", body={"kind": "cli", "scenarios": [], "frozen": True}, producer="model", meta={"frozen": True})
    with pytest.raises(BaselineError):
        studio.verifier.load_baseline(case["case_id"])


def test_invalidate_marks_stale(studio, tmp_path):
    orig = _build(tmp_path, "orig", C_SRC, pe=False)
    case = _case(studio, tmp_path, orig); cid = case["case_id"]
    studio.ledger.add(cid, "f.hello", feature_id="f.hello")
    _baseline_from_run(studio, cid, orig, {"type": "exe", "path": "app"}, SCENARIOS[:1], tmp_path / "bw")
    c = studio.candidates.create(cid, target_language="rust", output_type="exe", plan_revision=1, source_dir=orig)
    c = studio.candidates.mark_built(c["candidate_id"], orig)
    studio.db.update("candidates", "candidate_id", c["candidate_id"], {"meta": {"launch": {"type": "exe", "path": "app"}}})
    studio.verifier.verify_candidate(cid, c["candidate_id"])
    assert studio.ledger.get("f.hello")["verify_status"] == "verified"
    assert studio.verifier.invalidate(cid, "original files changed") == 1
    assert studio.ledger.get("f.hello")["verify_status"] == "stale"
    assert studio.candidates.get(c["candidate_id"])["verification"] == "stale"
