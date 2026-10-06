"""User-declared scenarios: CRUD, consent gate, recording a NEW baseline revision from a real run of the original, verification counts."""
import os
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rebuild_controller.api.server import create_app
from rebuild_controller.services import StudioServices

TOKEN = "t0k3n"
FIX = Path(__file__).resolve().parents[2] / "fixtures" / "pecli" / "original"
PECLI = FIX / "pecli.exe"


@pytest.fixture
def client(settings):
    st = StudioServices(settings)
    app = create_app(st, TOKEN)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}", "Origin": "http://localhost:5173"})
        yield c, st
    st.stop()


def _case(c, tmp_path, src, **lp):
    out = tmp_path / "out"
    r = c.post("/cases", json={"name": "us", "source_root": str(src), "output_root": str(out), "target_language": "rust", "output_type": "exe", "launch_profile": lp or {"execute_original": False}})
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def _py_original(tmp_path) -> Path:
    """A tiny original: a Python script launched through the current interpreter (works on any host)."""
    d = tmp_path / "orig"; d.mkdir()
    (d / "app.py").write_text("import sys,datetime\nprint('hello '+' '.join(sys.argv[1:]))\nprint(sys.stdin.read().upper(), end='')\nprint('at', datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))\nsys.exit(3)\n")
    return d


BODY = {"title": "Greets", "steps": [{"args": ["a", "b"], "stdin": "x"}], "compare": {"exit_code": True, "output": True, "files": False}}


def test_create_list_edit_delete(client, tmp_path):
    c, st = client
    cid = _case(c, tmp_path, _py_original(tmp_path))
    assert c.get(f"/cases/{cid}/scenarios").json()["scenarios"] == []
    r = c.post(f"/cases/{cid}/scenarios", json=BODY)
    assert r.status_code == 200, r.text
    sid = r.json()["scenario_id"]
    assert r.json()["steps"] == [{"args": ["a", "b"], "stdin": "x"}] and r.json()["normalize"]["line_endings"] is True
    lst = c.get(f"/cases/{cid}/scenarios").json()
    assert [s["status"] for s in lst["scenarios"]] == ["no_baseline"]
    assert lst["counts"]["declared"] == 1 and lst["counts"]["no_baseline"] == 1 and lst["consent"]["allowed"] is False
    r = c.put(f"/cases/{cid}/scenarios/{sid}", json={**BODY, "title": "Greets twice", "normalize": {"ignore_timestamps": True}})
    assert r.status_code == 200 and r.json()["title"] == "Greets twice" and r.json()["normalize"]["ignore_timestamps"] is True
    assert c.get(f"/cases/{cid}/scenarios/{sid}").json()["title"] == "Greets twice"
    assert c.delete(f"/cases/{cid}/scenarios/{sid}").json() == {"deleted": sid}
    assert c.get(f"/cases/{cid}/scenarios").json()["scenarios"] == []
    r = c.get(f"/cases/{cid}/scenarios/{sid}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "scenario_not_found"


@pytest.mark.parametrize("patch,code", [
    ({"title": "  "}, "scenario_title"),
    ({"steps": []}, "scenario_steps"),
    ({"compare": {"exit_code": False, "output": False, "files": False}}, "scenario_compare"),
    ({"timeout": 0}, "scenario_timeout"),
    ({"feature_id": "nope"}, "scenario_feature"),
    ({"steps": [{"args": ["x" * 5000]}]}, "scenario_args"),
])
def test_validation_errors_use_standard_shape(client, tmp_path, patch, code):
    c, st = client
    cid = _case(c, tmp_path, _py_original(tmp_path))
    r = c.post(f"/cases/{cid}/scenarios", json={**BODY, **patch})
    assert r.status_code == 400, r.text
    e = r.json()["error"]
    assert e["code"] == code and e["message"] and e["affected"] and e["next_action"]
    assert c.get(f"/cases/{cid}/scenarios").json()["scenarios"] == []


def test_wrong_type_is_a_validation_error(client, tmp_path):
    c, st = client
    cid = _case(c, tmp_path, _py_original(tmp_path))
    r = c.post(f"/cases/{cid}/scenarios", json={**BODY, "steps": "nope"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation"


def test_record_without_consent_is_blocked_and_runs_nothing(client, tmp_path):
    c, st = client
    cid = _case(c, tmp_path, _py_original(tmp_path))
    c.post(f"/cases/{cid}/scenarios", json=BODY)
    r = c.post(f"/cases/{cid}/scenarios/record", json={"program": "app.py"})
    assert r.status_code == 409, r.text
    e = r.json()["error"]
    assert e["code"] == "original_execution_not_permitted" and e["message"].startswith("Original execution needs your permission")
    assert not (st.cases.case_root(cid) / "capture-user").exists()
    assert st.cases.list_evidence(cid, kind="baseline") == []
    # revoked consent blocks again
    assert c.put(f"/cases/{cid}/consent/original-execution", json={"allow": True}).json()["allowed"] is True
    assert c.put(f"/cases/{cid}/consent/original-execution", json={"allow": False}).json()["allowed"] is False
    assert c.post(f"/cases/{cid}/scenarios/record", json={"program": "app.py"}).status_code == 409


def test_record_needs_scenarios_and_known_program(client, tmp_path):
    c, st = client
    cid = _case(c, tmp_path, _py_original(tmp_path))
    c.put(f"/cases/{cid}/consent/original-execution", json={"allow": True})
    assert c.post(f"/cases/{cid}/scenarios/record", json={}).json()["error"]["code"] == "no_scenarios"
    c.post(f"/cases/{cid}/scenarios", json=BODY)
    assert c.post(f"/cases/{cid}/scenarios/record", json={}).json()["error"]["code"] == "launch_unknown"
    assert c.post(f"/cases/{cid}/scenarios/record", json={"program": "../x.exe"}).json()["error"]["code"] in ("program_outside", "program_missing")
    assert c.post(f"/cases/{cid}/scenarios/record", json={"program": "missing.exe"}).json()["error"]["code"] == "program_missing"


@pytest.mark.skipif(not PECLI.exists() or os.name != "nt", reason="needs the pecli.exe fixture running natively on Windows")
def test_record_with_consent_freezes_new_revision_and_verifier_counts_it(client, tmp_path):
    c, st = client
    src = tmp_path / "orig"; shutil.copytree(FIX, src)
    cid = _case(c, tmp_path, src, launch={"type": "exe", "path": "pecli.exe"})   # execute_original NOT set: no consent yet
    assert c.get(f"/cases/{cid}/consent/original-execution").json()["allowed"] is False
    s1 = c.post(f"/cases/{cid}/scenarios", json={"title": "Add then read back", "steps": [
        {"args": ["init", "{work}/s.dat"]}, {"args": ["add", "{work}/s.dat", "k", "v"]}, {"args": ["get", "{work}/s.dat", "k"]}],
        "compare": {"exit_code": True, "output": True, "files": True}}).json()["scenario_id"]
    s2 = c.post(f"/cases/{cid}/scenarios", json={"title": "No arguments", "steps": [{"args": []}]}).json()["scenario_id"]
    assert c.post(f"/cases/{cid}/scenarios/record", json={}).status_code == 409
    c.put(f"/cases/{cid}/consent/original-execution", json={"allow": True, "note": "test"})
    # first revision: only s1
    r = c.post(f"/cases/{cid}/scenarios/record", json={"scenario_ids": [s1]})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["baseline_revision"] == 1 and rec["recorded"] == [s1] and rec["scenarios_in_baseline"] == 1
    ev1, bl1 = st.verifier.load_baseline(cid)
    sc = bl1["scenarios"][0]
    assert sc["id"] == s1 and sc["user_declared"] and sc["channels"] == ["exit_code", "stdout", "stderr", "files"]
    steps = sc["expected"]["steps"]
    assert [x["exit_code"] for x in steps] == [0, 0, 0] and steps[2]["stdout"].strip() == "v" and steps[0]["role"] == "original"
    assert list(sc["expected"]["files"]) == ["s.dat"]
    # second revision extends: s1 kept, s2 added; the first revision is untouched
    r2 = c.post(f"/cases/{cid}/scenarios/record", json={"scenario_ids": [s2]}).json()
    assert r2["baseline_revision"] == 2 and r2["scenarios_in_baseline"] == 2
    revs = st.cases.list_evidence(cid, kind="baseline")
    assert len(revs) == 2 and revs[0]["evidence_id"] == ev1["evidence_id"]
    assert [s["id"] for s in st.cases.evidence_body(revs[0]["evidence_id"])["scenarios"]] == [s1]
    assert [s["id"] for s in st.verifier.load_baseline(cid)[1]["scenarios"]] == [s1, s2]
    assert st.verifier.load_baseline(cid)[1]["scenarios"][1]["expected"]["steps"][0]["exit_code"] == 1   # usage error
    assert {s["status"] for s in c.get(f"/cases/{cid}/scenarios").json()["scenarios"]} == {"not_run"}
    # candidate = a copy of the original build: verifier counts both scenarios as declared+passed
    cand_dir = tmp_path / "cand"; shutil.copytree(FIX, cand_dir)
    cand = st.candidates.create(cid, target_language="rust", output_type="exe", plan_revision=1, source_dir=cand_dir)
    cand = st.candidates.mark_built(cand["candidate_id"], cand_dir)
    st.db.update("candidates", "candidate_id", cand["candidate_id"], {"meta": {"launch": {"type": "exe", "path": "pecli.exe"}}})
    rep = st.verifier.verify_candidate(cid, cand["candidate_id"])
    assert rep["summary"] == {"scenarios": 2, "passed": 2, "failed": 0, "errors": 0}, [(x["scenario"], [(k["channel"], k["verdict"]) for k in x["comparisons"]]) for x in rep["scenarios"]] + [(r["channel"], r["verdict"], r["details"]) for r in st.verifier.comparisons(cid) if r["verdict"] != "pass"]
    from rebuild_controller.outcome import case_outcome
    v = case_outcome(st, cid)["verification"]
    assert (v["declared"], v["passed"], v["failed"], v["untested"]) == (2, 2, 0, 0)
    lst = c.get(f"/cases/{cid}/scenarios").json()
    assert {s["status"] for s in lst["scenarios"]} == {"passed"} and lst["counts"]["passed"] == 2 and lst["counts"]["recorded"] == 2
    # editing a recorded scenario does not touch the frozen baseline; it is flagged until re-recorded
    c.put(f"/cases/{cid}/scenarios/{s2}", json={"title": "No arguments", "steps": [{"args": ["list", "{work}/none.dat"]}]})
    assert c.get(f"/cases/{cid}/scenarios/{s2}").json()["status"] == "changed"
    assert st.verifier.load_baseline(cid)[1]["scenarios"][1]["steps"][0]["args"] == []
    # deleting drops it from the NEXT revision only
    c.delete(f"/cases/{cid}/scenarios/{s2}")
    r3 = c.post(f"/cases/{cid}/scenarios/record", json={"scenario_ids": [s1]}).json()
    assert r3["baseline_revision"] == 3 and [s["id"] for s in st.verifier.load_baseline(cid)[1]["scenarios"]] == [s1]
    assert len(st.cases.list_evidence(cid, kind="baseline")) == 3
    # a re-recorded baseline makes older verification stale: nothing is counted as passed
    assert c.get(f"/cases/{cid}/scenarios").json()["counts"]["passed"] == 0


@pytest.mark.skipif(os.name != "nt" and not shutil.which("python3"), reason="needs a runnable interpreter")
def test_timestamp_normalisation_and_exit_code_opt_out(client, tmp_path):
    """A program printing the current time only matches itself when 'ignore timestamps' is on; exit code can be left out."""
    c, st = client
    src = _py_original(tmp_path)
    py = sys.executable
    cid = _case(c, tmp_path, src, launch={"type": "command", "command": [py, str(src / "app.py")]}, kind="cli")
    c.put(f"/cases/{cid}/consent/original-execution", json={"allow": True})
    sid = c.post(f"/cases/{cid}/scenarios", json={"title": "time", "steps": [{"args": ["z"], "stdin": "q"}], "compare": {"exit_code": False, "output": True},
                                                  "normalize": {"ignore_timestamps": True, "line_endings": True}}).json()["scenario_id"]
    r = c.post(f"/cases/{cid}/scenarios/record", json={})
    assert r.status_code == 200, r.text
    _, bl = st.verifier.load_baseline(cid)
    assert bl["scenarios"][0]["channels"] == ["stdout", "stderr"] and any(x.startswith("mask:") for x in bl["scenarios"][0]["normalize"]["stdout"])
    from rebuild_controller.comparators.cli import compare_cli_scenario
    res = compare_cli_scenario(bl["scenarios"][0], bl["scenarios"][0]["expected"], bl["launch"], src, tmp_path / "cw")
    assert {x.channel for x in res} == {"stdout", "stderr"} and all(x.verdict == "pass" for x in res)
