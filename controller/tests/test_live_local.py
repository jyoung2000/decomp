"""LIVE local-model proof (opt-in): a real Ollama server on this PC, the real connection store / router / implement loop / cargo /
verifier. Nothing is mocked and nothing leaves the machine.

Run:  REBUILD_LIVE_LOCAL=1 python -m pytest -m live tests/test_live_local.py -s
Env:  REBUILD_LIVE_OLLAMA (default http://127.0.0.1:11434/v1), REBUILD_LIVE_LOCAL_MODEL (default qwen2.5:14b),
      REBUILD_LIVE_LOCAL_ATTEMPTS (default 3). Models are never pulled by these tests.

The repair task: ``tests/data/tinycalc`` - a 60-line Rust CLI. ``buggy/`` is the reference with ONE injected bug (``max`` computes
the minimum); ``expected/scenarios.json`` is the frozen oracle recorded from the correct ``reference/`` (3 CLI scenarios). The
model only ever sees the buggy files and the scenarios. Whether it fixes the bug is reported, not assumed.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

DATA = Path(__file__).resolve().parent / "data" / "tinycalc"
OLLAMA = os.environ.get("REBUILD_LIVE_OLLAMA", "http://127.0.0.1:11434/v1")
MODEL = os.environ.get("REBUILD_LIVE_LOCAL_MODEL", "qwen2.5:14b")
MISSING = "qwen9-does-not-exist:1b"


def _ollama_up() -> bool:
    try:
        return httpx.get(OLLAMA.rstrip("/").removesuffix("/v1") + "/api/tags", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("REBUILD_LIVE_LOCAL") != "1", reason="opt-in: set REBUILD_LIVE_LOCAL=1 (needs a local Ollama)"),
              pytest.mark.skipif(os.environ.get("REBUILD_LIVE_LOCAL") == "1" and not _ollama_up(), reason=f"no Ollama at {OLLAMA}"),
              pytest.mark.skipif(not shutil.which("cargo") and not (Path.home() / ".cargo" / "bin").exists(), reason="cargo missing")]


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def studio(settings):
    from rebuild_controller.services import StudioServices
    settings.limits.max_stage_seconds = 1800
    settings.limits.lease_timeout_seconds = 600
    s = StudioServices(settings)
    s.ai.advisor = None
    yield s
    s.stop()


def report(name: str, doc: dict[str, Any]) -> None:
    print(f"\n[live-local] {name}: " + json.dumps(doc, indent=1, default=str))
    if not os.environ.get("REBUILD_LIVE_REPORT_DIR"):
        return
    out = Path(os.environ["REBUILD_LIVE_REPORT_DIR"])
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{name}.json").write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    except OSError:
        pass


def local_connection(studio, *, capabilities: bool = False, model: str | None = None) -> dict[str, Any]:
    conn = studio.connections.create("local", "Ollama (this PC)", endpoint=OLLAMA, auth_mode="local")
    return studio.connections.probe(conn["connection_id"], capabilities=capabilities, model=model)


def drain(studio, rounds: int = 400) -> None:
    from rebuild_controller.jobs import JobState
    for _ in range(rounds):
        n = studio.runner.run_pending()
        if n == 0 and not studio.jobs.list(None, [JobState.QUEUED, JobState.RUNNING]):
            return
    raise AssertionError("jobs did not settle")


# ----------------------------------------------------------------------------------------------- (a) connection + discovery
def test_live_local_connection_discovers_models_capabilities_and_context(studio):
    t0 = time.time()
    p = local_connection(studio)
    by = {m["id"]: m for m in p["models"]}
    assert p["state"] == "ok" and p["limits"].get("server") == "ollama" and p["capabilities"]["discovery"] == "supported"
    assert MODEL in by, f"{MODEL} is not pulled on this Ollama (models: {sorted(by)})"
    assert by[MODEL]["context_window"] and by[MODEL]["capabilities"]["completion"] is True
    vision = sorted(m for m, e in by.items() if (e.get("capabilities") or {}).get("vision"))
    from rebuild_controller.providers.ladder import ladder_view, model_catalog, preset
    pv = preset(studio.connections, "all_local", apply=False)
    report("discovery", {"seconds": round(time.time() - t0, 2), "server_version": p["limits"].get("server_version"), "models": {
        k: {"context_window": v.get("context_window"), "capabilities": v.get("capabilities"), "details": (v.get("meta") or {}).get("ollama")}
        for k, v in sorted(by.items())}, "vision_models": vision,
        "preset_all_local_preview": {t: [e["model"] for e in v["entries"]] for t, v in pv["tasks"].items()}, "preset_warnings": pv["warnings"]})
    assert all(e["locality"] == "local" and e["free"] for t in pv["tasks"].values() for e in t["entries"])
    assert all(r["locality"] == "local" for r in model_catalog(studio.connections))
    # a capability probe against a small local model costs nothing and needs no budget
    small = "qwen2.5:3b" if "qwen2.5:3b" in by else MODEL
    t1 = time.time()
    p2 = studio.connections.probe(p["connection_id"], capabilities=True, model=small)
    report("capability_probe", {"model": small, "seconds": round(time.time() - t1, 1), "state": p2["state"],
                                "capabilities": {k: v for k, v in p2["capabilities"].items() if k != "probe_detail"},
                                "detail": p2["capabilities"].get("probe_detail")})
    assert p2["capabilities"]["streaming"] == "supported"


# ----------------------------------------------------------------------------------------------- fallback with reason (router)
def test_live_local_missing_model_falls_back_with_a_reason(studio):
    from rebuild_controller.providers.base import Message, Request
    from rebuild_controller.providers.ladder import put_ladder
    p = local_connection(studio)
    small = "qwen2.5:3b" if any(m["id"] == "qwen2.5:3b" for m in p["models"]) else MODEL
    put_ladder(studio.connections, "knowledge", [{"connection_id": p["connection_id"], "model": MISSING},
                                                 {"connection_id": p["connection_id"], "model": small}])
    t0 = time.time()
    res = studio.ai.call("knowledge", Request(model="", messages=[Message.user("Reply with exactly: pong")], max_output_tokens=20, stream=False),
                         policy={"mode": "assisted", "locality": "local_only"})
    a = res.attempts
    report("fallback_router", {"seconds": round(time.time() - t0, 1), "winner": res.model, "answer": res.response.text[:80],
                               "attempts": [{k: x.get(k) for k in ("position", "model", "outcome", "reason", "locality")} for x in a],
                               "cost_usd": res.cost_usd, "tokens": [res.response.usage.input_tokens, res.response.usage.output_tokens]})
    assert [(x["position"], x["outcome"]) for x in a] == [(1, "model_unavailable"), (2, "ok")]
    assert a[0]["reason"] == f"{MISSING} is not available on Ollama (this PC) (model not found)"
    assert res.cost_usd == 0 and res.locality == "local" and res.took_over_from[0]["outcome"] == "model_unavailable"
    flagged = [m for m in studio.connections.get(p["connection_id"])["models"] if m["id"] == MISSING][0]
    assert flagged["availability"]["state"] == "model_unavailable"


# ----------------------------------------------------------------------------------------------- (b) real repair through the loop
def test_live_local_model_repairs_a_tiny_rust_cli_through_the_real_implement_loop(studio, tmp_path):
    from rebuild_controller.implement import mismatch_digest
    from rebuild_controller.jobs import JobState
    from rebuild_controller.outcome import case_outcome
    from rebuild_controller.providers.ladder import activity, put_ladder
    from rebuild_controller.reconstruct import _task_packet
    t_all = time.time()
    p = local_connection(studio)
    cid_conn = p["connection_id"]
    rev = put_ladder(studio.connections, "implementation", [{"connection_id": cid_conn, "model": MISSING},
                                                           {"connection_id": cid_conn, "model": MODEL}])["config_revision"]
    orig = tmp_path / "original"
    orig.mkdir()
    (orig / "README.txt").write_text("tinycalc: sum | max | avg of integers\n")
    attempts_cap = int(os.environ.get("REBUILD_LIVE_LOCAL_ATTEMPTS", "3"))
    case = studio.create_case(name="tinycalc", source_root=str(orig), output_root=str(tmp_path / "out"), target_language="rust", output_type="exe",
                              ai_policy={"mode": "assisted", "budget_usd": 0, "max_attempts": attempts_cap, "max_output_tokens": 3000,
                                         "retry_backoff_s": 0.5, "request_timeout_s": 900, "locality": "local_only"},
                              launch_profile={"baseline_file": str(DATA / "expected" / "scenarios.json")})
    cid = case["case_id"]
    d = studio.jobs.create(cid, "discover_features", "discover", {}, milestone_id="M-FEATURES")
    studio.jobs.create(cid, "capture_original", "capture", {}, depends_on=[d.job_id], milestone_id="M-FEATURES")
    drain(studio)
    # the remake under repair: the reference with one injected bug, verified first by the real build + verifier stages
    files = {"Cargo.toml": (DATA / "buggy" / "Cargo.toml").read_text(), "src/main.rs": (DATA / "buggy" / "src" / "main.rs").read_text()}
    buggy = studio.candidates.propose(cid, files, note="remake with a reported behaviour bug", author="user", base_candidate=None,
                                      plan_revision=studio.plan.current_revision(cid))
    b = studio.jobs.create(cid, "build_candidate", "build buggy", {"candidate_id": buggy["candidate_id"]})
    studio.jobs.create(cid, "compare_candidate", "compare buggy", {"candidate_id": buggy["candidate_id"]}, depends_on=[b.job_id])
    drain(studio)
    before = studio.candidates.get(buggy["candidate_id"])["verification"]
    packet = _task_packet(studio, studio.cases.get_case(cid), "rust", "native")
    packet["current_candidate_verification"] = mismatch_digest(studio, cid, buggy["candidate_id"])
    packet["task"] = "The CURRENT FILES already implement this program but fail some declared scenarios. Repair them."
    pev = studio.cases.add_evidence(cid, "ai_task_packet", "Repair packet (tinycalc)", body=packet, meta={"untrusted": True})
    loop = studio.jobs.create(cid, "implement_loop", "AI repair (live local)", {"candidate_id": buggy["candidate_id"], "packet": pev["evidence_id"]},
                              milestone_id="M-IMPL", max_attempts=1)
    t_loop = time.time()
    drain(studio)
    loop = studio.jobs.get(loop.job_id)
    assert loop.state == JobState.COMPLETED, (loop.state, loop.error, loop.blocker)
    res = loop.result
    recs = []
    for ev in studio.cases.list_evidence(cid, kind="ai_attempt"):
        if ev["meta"].get("counted"):
            body = studio.cases.evidence_body(ev["evidence_id"])
            c = body["call"]
            recs.append({"attempt": body["attempt"], "model": c.get("model"), "build": body["build"]["status"],
                         "verdict": (body.get("verdict") or {}).get("state"), "passed": (body.get("verdict") or {}).get("passed"),
                         "tokens": c.get("usage"), "prompt_chars": c.get("prompt_chars"), "cost_usd": c.get("cost_usd"),
                         "router": [(r["position"], r["model"], r["outcome"]) for r in c.get("router_attempts") or []],
                         "seconds": _secs(body.get("started_at"), body.get("finished_at"))})
    feed = [a["text"] for a in activity(studio.events, cid)]
    report("repair_loop", {"model": MODEL, "verified": res["verified"], "stop_reason": res["stop_reason"], "buggy_before": before,
                           "attempts": recs, "loop_seconds": round(time.time() - t_loop, 1), "total_seconds": round(time.time() - t_all, 1),
                           "spent_usd": res["spent_usd"], "outcome": case_outcome(studio, cid).get("state"), "activity": feed})
    # provider-independent invariants (the model's success is reported, not assumed)
    assert before in ("partial", "failed")
    assert recs and recs[0]["router"][:2] == [(1, MISSING, "model_unavailable"), (2, MODEL, "ok")]
    assert all(r["cost_usd"] == 0 for r in recs) and res["spent_usd"] == 0
    assert any("is not available on Ollama (this PC) (model not found); trying " + MODEL in t for t in feed)
    assert (case_outcome(studio, cid)["state"] == "fully_matched") == bool(res["verified"])   # the verifier decides
    assert all(r["router"][0][2] == "model_unavailable" for r in recs)                        # the flag never hides the entry; each try is free
    assert studio.connections.config_revision() == rev


def _secs(a: str | None, b: str | None) -> float | None:
    import datetime as dt
    try:
        return round((dt.datetime.fromisoformat(b.replace("Z", "+00:00")) - dt.datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds(), 1)
    except Exception:
        return None
