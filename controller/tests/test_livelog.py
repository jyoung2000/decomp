"""Live log: ctx.log rate limiting + redaction, GET /cases/{id}/log, and a real (no-AI) fixture run."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rebuild_controller.api.server import create_app
from rebuild_controller.jobs import JobState, StageError
from rebuild_controller.livelog import LogLimiter
from rebuild_controller.services import StudioServices

TOKEN = "t0k3n"
FIX = Path(__file__).resolve().parents[2] / "fixtures"
FAKE_KEY = "sk-" + "A1b2C3d4E5f6G7h8I9j0KLMNOP"
FAKE_AUTH = "Authorization: Bearer " + "zzTOPSECRETtokenvalue987654"
FAKE_BASIC = "Authorization: Basic " + "dXNlcjpwYXNzd29yZDEyMw=="
SECRETS = [FAKE_KEY, "zzTOPSECRETtokenvalue987654", "dXNlcjpwYXNzd29yZDEyMw=="]


@pytest.fixture
def client(settings):
    st = StudioServices(settings)
    app = create_app(st, TOKEN)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}", "Origin": "http://localhost:5173"})
        yield c, st
    st.stop()


def _case(st, src_out):
    src, out = src_out
    return st.create_case(name="t", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe")["case_id"]


def _log_events(events, job_id=None):
    return [e for e in events.events_since(0) if e["kind"] == "job.log" and (job_id is None or e["job_id"] == job_id)]


def _run(st, cid, name, fn, **kw):
    st.stages.add(name, fn)
    j = st.jobs.create(cid, name, f"{name} job", {}, milestone_id="M-ANALYSIS", **kw)
    st.runner.run_pending()
    return st.jobs.get(j.job_id)


# ------------------------------------------------------------------------------------------------ rate limiting
def test_rate_limit_caps_info_lines_and_coalesces_progress(client, src_out):
    c, st = client
    cid = _case(st, src_out)
    clock = {"t": 100.0}

    def stage(ctx):
        ctx._limiter = LogLimiter(clock=lambda: clock["t"])
        for i in range(200):
            ctx.log(f"Scanning… {i} files", key="scan")           # one burst inside a single second
        assert len(_log_events(st.events, ctx.job.job_id)) <= 5
        clock["t"] += 1.1                                              # next window: the held latest line is released first
        ctx.log("Scanned: 200 files")
        return {}
    j = _run(st, cid, "burst", stage)
    assert j.state == JobState.COMPLETED, j.error
    texts = [e["payload"]["text"] for e in _log_events(st.events, j.job_id)]
    assert len(texts) <= 7
    assert "Scanning… 199 files" in texts            # the latest progress was coalesced, not lost
    assert texts[-1] == "Scanned: 200 files"
    assert "Scanning… 100 files" not in texts


def test_rate_limit_never_starves_problems_and_flushes_at_job_end(client, src_out):
    c, st = client
    cid = _case(st, src_out)

    def stage(ctx):
        ctx._limiter = LogLimiter(clock=lambda: 5.0)    # frozen clock: a single window
        for i in range(30):
            ctx.log(f"step {i}", key="p")
        ctx.log("build failed: first error", "error")
        ctx.log("last progress line", key="p")           # held, must be flushed when the job finishes
        return {}
    j = _run(st, cid, "problems", stage)
    ev = _log_events(st.events, j.job_id)
    levels = [e["payload"]["level"] for e in ev]
    assert levels.count("info") <= 6 and "error" in levels
    assert ev[-1]["payload"]["text"] == "last progress line"
    p = ev[0]["payload"]
    assert p["job_id"] == j.job_id and p["stage"] == "problems" and p["milestone"] == "M-ANALYSIS" and p["at"] and p["text"] == p["message"]


# ------------------------------------------------------------------------------------------------ redaction
def test_secrets_never_reach_events_or_api(client, src_out):
    c, st = client
    cid = _case(st, src_out)
    stderr = f"curl: (22) 401\n> {FAKE_AUTH}\n> {FAKE_BASIC}\nx-api-key: abcdef123456\nusing {FAKE_KEY}\nfinal line"

    def stage(ctx):
        ctx.log(f"calling the tool with {FAKE_KEY}", "warn", detail=f"{FAKE_AUTH}")
        ctx.log("with extras", prompt="SYSTEM PROMPT SHOULD NOT BE STORED", messages=[{"role": "user", "content": "RAW PROMPT"}], authorization=FAKE_AUTH, note=FAKE_KEY)
        ctx.log_failure("The helper tool failed", stderr)
        raise StageError("helper exited 22:\n" + stderr)
    j = _run(st, cid, "leaky", stage)
    assert j.state == JobState.FAILED
    blob = json.dumps(st.events.events_since(0)) + json.dumps(c.get(f"/cases/{cid}/log").json()) + json.dumps(c.get(f"/cases/{cid}/jobs").json())
    for s in SECRETS + ["abcdef123456", "SYSTEM PROMPT SHOULD NOT BE STORED", "RAW PROMPT"]:
        assert s not in blob, s
    entries = c.get(f"/cases/{cid}/log").json()["entries"]
    fail = [e for e in entries if e["text"] == "The helper tool failed"][0]
    assert fail["level"] == "error" and "final line" in fail["detail"] and fail["detail"].count("\n") <= 4     # last few lines only, bounded
    assert any(e["level"] == "error" and e["text"].startswith("Failed: leaky job") for e in entries)


def test_text_and_detail_are_bounded(client, src_out):
    c, st = client
    cid = _case(st, src_out)
    j = _run(st, cid, "big", lambda ctx: ctx.log("x" * 100_000, "info", "y" * 100_000) or {})
    p = _log_events(st.events, j.job_id)[0]["payload"]
    assert len(p["text"]) <= 400 and len(p["detail"]) <= 1500


# ------------------------------------------------------------------------------------------------ GET /cases/{id}/log
def test_log_endpoint_merges_transitions_filters_and_pages(client, src_out):
    c, st = client
    cid = _case(st, src_out)
    st.stages.add("ok", lambda ctx: ctx.log("Scanning the installation folder… 12 files") or {})
    st.stages.add("boom", lambda ctx: (ctx.log("Build failed: error[E0425]", "error", "line1\nline2"), (_ for _ in ()).throw(StageError("cargo build failed:\nerror[E0425]: nope")))[1])
    a = st.jobs.create(cid, "ok", "Scan files", {}, milestone_id="M-ANALYSIS")
    b = st.jobs.create(cid, "boom", "Build candidate", {}, depends_on=[a.job_id], milestone_id="M-BUILD")
    d = st.jobs.create(cid, "ok", "Compare", {}, depends_on=[b.job_id], milestone_id="M-COMPARE")
    st.runner.run_pending()
    st.events.emit("ai.activity", {"at": "2026-01-01T00:00:00Z", "kind": "proposal", "text": "The model proposed 3 files", "prompt": "RAW", "provider": "p", "model": "m"},
                   case_id=cid, job_id=b.job_id)
    r = c.get(f"/cases/{cid}/log")
    assert r.status_code == 200
    body = r.json()
    texts = [e["text"] for e in body["entries"]]
    assert texts[:3] == ["Started: Scan files", "Scanning the installation folder… 12 files", "Finished: Scan files"]
    assert any(t.startswith("Failed: Build candidate") for t in texts)
    assert any(t.startswith("Waiting: Compare") for t in texts)           # dependency block rendered as text
    assert body["entries"][-1]["kind"] == "ai" and "RAW" not in json.dumps(body)
    seqs = [e["seq"] for e in body["entries"]]
    assert seqs == sorted(seqs) and body["latest_seq"] >= seqs[-1]
    # level filter keeps that level and worse
    problems = c.get(f"/cases/{cid}/log", params={"level": "warn"}).json()["entries"]
    assert problems and all(e["level"] in ("warn", "error") for e in problems)
    assert [e["level"] for e in c.get(f"/cases/{cid}/log", params={"level": "error"}).json()["entries"]] == ["error", "error"]
    # stage filter
    assert {e["stage"] for e in c.get(f"/cases/{cid}/log", params={"stage": "ok"}).json()["entries"]} == {"ok"}
    # since + limit: forward paging; limit without since = newest N
    first = c.get(f"/cases/{cid}/log", params={"since": 0, "limit": 2}).json()["entries"]
    assert [e["seq"] for e in first] == seqs[-2:]
    page = c.get(f"/cases/{cid}/log", params={"since": seqs[2], "limit": 2}).json()["entries"]
    assert [e["seq"] for e in page] == seqs[3:5]
    assert c.get(f"/cases/{cid}/log", params={"since": seqs[-1]}).json()["entries"] == []
    assert c.get(f"/cases/{cid}/log", params={"level": "loud"}).status_code == 400
    assert c.get(f"/cases/{cid}/log", headers={"Authorization": "Bearer wrong"}).status_code == 401


# ------------------------------------------------------------------------------------------------ real fixture run
@pytest.mark.e2e
@pytest.mark.skipif(not (FIX / "webapp" / "original" / "index.html").exists(), reason="webapp fixture missing")
def test_webapp_fixture_run_logs_meaningful_lines_in_order(client, tmp_path):
    c, st = client
    st.settings.limits.max_stage_seconds = 600
    case = st.create_case(name="webapp", source_root=str(FIX / "webapp" / "original"), output_root=str(tmp_path / "out"), target_language="web", output_type="web",
                          ai_policy={"mode": "no_ai"}, launch_profile={"execute_original": True, "kind": "web"})
    cid = case["case_id"]
    st.start_rebuild(cid)
    for _ in range(400):
        n = st.runner.run_pending()
        if n == 0 and not st.jobs.list(None, [JobState.QUEUED, JobState.RUNNING]):
            break
    jobs = {j.stage: j for j in st.jobs.list(cid)}
    if jobs["capture_original"].state != JobState.COMPLETED:
        pytest.skip(f"headless browser not available here: {jobs['capture_original'].error or jobs['capture_original'].blocker}")
    assert jobs["deliver"].state == JobState.COMPLETED, jobs["deliver"].error
    entries = c.get(f"/cases/{cid}/log", params={"limit": 500}).json()["entries"]
    texts = [e["text"] for e in entries]

    def first(prefix):
        for i, t in enumerate(texts):
            if t.startswith(prefix):
                return i
        raise AssertionError(f"no log line starting {prefix!r} in:\n" + "\n".join(texts))
    order = [first("Scanning the installation folder"), first("Scanned the installation folder:"), first("Recovered the web app"),
             first("Running the original in an isolated process: scenario 1 of 1"), first("Porting the recovered web site"), first("Built the web candidate"),
             first("Running the rebuilt program: scenario 1 of 1"), first("Comparison finished: 1 of 1 scenarios match"), first("Delivered to ")]
    assert order == sorted(order) and len(set(order)) == len(order)
    scanned = texts[order[1]]
    assert "files" in scanned and "modules" in scanned
    assert all(e["stage"] and e["milestone"] for e in entries if e["kind"] in ("log", "job"))
    assert not [e for e in entries if e["level"] == "error"]
