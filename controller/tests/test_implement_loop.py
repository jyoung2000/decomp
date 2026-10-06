"""AI implement -> build -> verify -> repair loop, end to end WITHOUT network.

A scripted fake OpenAI-compatible HTTP server (threaded http.server on 127.0.0.1) plays the model. The pecli fixture (native PE CLI with
a frozen oracle of 8 scenarios) is the program to remake; the "correct remake" the fake returns is the previously verified candidate
source from examples/pecli-rust-from-evidence. Everything else is real: connection store + secret store, budget ledger, router,
job DAG, cargo (sandboxed), verifier, outcome, delivery.

Marked e2e (cargo builds). Needs the pecli fixture and cargo.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

from rebuild_controller.jobs import JobState
from rebuild_controller.outcome import case_outcome

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "fixtures" / "pecli"
EXAMPLE = ROOT / "examples" / "pecli-rust-from-evidence"
API_KEY = "sk-test-FAKEKEY-1234567890abcdef"
pytestmark = [pytest.mark.e2e,
              pytest.mark.skipif(not (FIX / "original" / "pecli.exe").exists() or not (EXAMPLE / "src" / "main.rs").exists() or not shutil.which("cargo")
                                 and not (Path.home() / ".cargo" / "bin").exists(), reason="pecli fixture/example or cargo missing")]

GOOD_MAIN = (EXAMPLE / "src" / "main.rs").read_text("utf-8")
GOOD_TOML = (EXAMPLE / "Cargo.toml").read_text("utf-8")
BROKEN_MAIN = 'fn main() {\n    let x: i32 = "not a number";\n    println!("{}", x);\n}\n'
# compiles, but two scenarios (update_and_remove, err_missing_file) no longer match the oracle
PARTIAL_MAIN = (GOOD_MAIN.replace('out(format!("removed {} ({} records)\\n"', 'out(format!("deleted {} ({} records)\\n"')
                .replace('error: file not found: {}\\n", path));\n            return Err(3);', 'error: no such file: {}\\n", path));\n            return Err(3);', 1))
WRONG_MAIN = 'fn main() {\n    println!("ok");\n}\n'


def files_json(main: str, toml: str = GOOD_TOML, extra: dict[str, str] | None = None) -> str:
    return json.dumps({"Cargo.toml": toml, "src/main.rs": main, **(extra or {})})


# ----------------------------------------------------------------------------------------- fake OpenAI-compatible server
class Reply:
    def __init__(self, text: str, prompt_tokens: int = 1200, completion_tokens: int = 800):
        self.text, self.pt, self.ct = text, prompt_tokens, completion_tokens


class Status:
    def __init__(self, code: int, retry_after: str | None = None):
        self.code, self.retry_after = code, retry_after


class Hook:
    """Called with the parsed request body while the (single-threaded per request) server handles it; returns the element to play."""

    def __init__(self, fn: Callable[[dict[str, Any]], Any]):
        self.fn = fn


class FakeOpenAI:
    def __init__(self, script: list[Any]):
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self._lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code: int, doc: Any, headers: dict[str, str] | None = None) -> None:
                data = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, {"data": [{"id": "gpt-fake"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                with outer._lock:
                    outer.requests.append(body)
                    outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                    item = outer.script.pop(0) if outer.script else Status(500)
                if isinstance(item, Hook):
                    item = item.fn(body)
                if isinstance(item, Status):
                    self._send(item.code, {"error": {"message": f"scripted {item.code}", "type": "server_error"}},
                               {"retry-after": item.retry_after} if item.retry_after is not None else None)
                    return
                self._send(200, {"id": "chatcmpl-fake", "model": "gpt-fake",
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": item.text}, "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": item.pt, "completion_tokens": item.ct}})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def user_text(self, i: int) -> str:
        return next(m["content"] for m in self.requests[i]["messages"] if m["role"] == "user")

    def close(self) -> None:
        self.httpd.shutdown(); self.httpd.server_close()


# ----------------------------------------------------------------------------------------- harness
@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def studio(settings):
    from rebuild_controller.services import StudioServices
    settings.limits.max_stage_seconds = 900
    settings.limits.lease_timeout_seconds = 120
    s = StudioServices(settings)
    s.ai.advisor = None           # no advisory network hop in tests
    yield s
    s.stop()


@pytest.fixture
def servers():
    made: list[FakeOpenAI] = []

    def make(script):
        f = FakeOpenAI(script)
        made.append(f)
        return f
    yield make
    for f in made:
        f.close()


PRICE = {"input_per_mtok": 1.0, "output_per_mtok": 2.0}


def connect(studio, server: FakeOpenAI, *, price: dict | None = PRICE, label: str = "Fake OpenAI", provider: str = "openai", route: bool = True) -> dict[str, Any]:
    model: Any = {"id": "gpt-fake", **({"price": price} if price else {})}
    if provider == "local":
        conn = studio.connections.create("local", label, endpoint=server.url, auth_mode="local", models=[model], dialect="chat")
    else:
        conn = studio.connections.create("openai", label, endpoint=server.url, auth_mode="api_key", api_key=API_KEY, models=[model], dialect="chat")
    if route:
        studio.connections.set_route("interpretation", conn["connection_id"], "gpt-fake")
    return conn


def make_case(studio, tmp_path, policy: dict[str, Any]) -> str:
    pol = {"mode": "assisted", "budget_usd": 5.0, "max_attempts": 3, "retry_backoff_s": 0.01, **policy}
    case = studio.create_case(name="pecli", source_root=str(FIX / "original"), output_root=str(tmp_path / "out"), target_language="rust", output_type="exe",
                              ai_policy=pol, launch_profile={"baseline_file": str(FIX / "expected" / "scenarios.json")})
    cid = case["case_id"]
    d = studio.jobs.create(cid, "discover_features", "discover", {}, milestone_id="M-FEATURES")
    c = studio.jobs.create(cid, "capture_original", "capture", {}, depends_on=[d.job_id], milestone_id="M-FEATURES")
    studio.jobs.create(cid, "reconstruct", "reconstruct", {}, depends_on=[c.job_id], milestone_id="M-IMPL")
    studio.cases.set_case_status(cid, "running")
    return cid


def drain(studio, rounds: int = 200) -> None:
    for _ in range(rounds):
        n = studio.runner.run_pending()
        if n == 0 and not studio.jobs.list(None, [JobState.QUEUED, JobState.RUNNING]):
            return
    raise AssertionError("did not settle")


def job(studio, cid, stage):
    js = [j for j in studio.jobs.list(cid) if j.stage == stage]
    return js[-1] if js else None


def attempts(studio, cid) -> list[dict[str, Any]]:
    out = []
    for ev in studio.cases.list_evidence(cid, kind="ai_attempt"):
        if ev["meta"].get("counted"):
            out.append(studio.cases.evidence_body(ev["evidence_id"]))
    return sorted(out, key=lambda a: a["attempt"])


def cost_of(pt: int, ct: int) -> float:
    return pt * PRICE["input_per_mtok"] / 1e6 + ct * PRICE["output_per_mtok"] / 1e6


# ----------------------------------------------------------------------------------------- the headline e2e
def test_three_attempts_compile_error_then_two_failures_then_verified(studio, servers, tmp_path):
    t0 = time.time()
    srv = servers([Reply(files_json(BROKEN_MAIN), 1500, 300), Reply(files_json(PARTIAL_MAIN), 1800, 5000), Reply(files_json(GOOD_MAIN), 2600, 4000)])
    connect(studio, srv)
    cid = make_case(studio, tmp_path, {})
    drain(studio)
    loop = job(studio, cid, "implement_loop")
    assert loop.state == JobState.COMPLETED, (loop.state, loop.error)
    res = loop.result
    assert res["stop_reason"] == "verified" and res["verified"] is True and len(res["attempts"]) == 3 and len(srv.requests) == 3

    # attempt 2's request carries the cargo error; attempt 3's carries the failing scenario diffs (expected vs actual)
    u2, u3 = srv.user_text(1), srv.user_text(2)
    assert "cargo build failed" in u2 and "E0308" in u2 and "build_failed" in u2
    assert "scenario_mismatches" in u3 and "update_and_remove" in u3 and "err_missing_file" in u3
    assert "removed owner" in u3 and "deleted owner" in u3          # expected vs actual excerpts
    assert "init_roundtrip" in u3 and "scenarios_already_passing" in u3
    assert "not a number" in u2 and "deleted" in u3                  # the model sees its own previous files
    # the first prompt includes the scenarios with the original's recorded behaviour
    u1 = srv.user_text(0)
    assert "original_behaviour" in u1 and "initialized store.dat" in u1

    # every attempt is evidence: prompt hash, model, tokens, cost estimate, build log, verdict
    a1, a2, a3 = attempts(studio, cid)
    assert [a["build"]["status"] for a in (a1, a2, a3)] == ["failed", "built", "built"]
    assert a1["verdict"] is None and "E0308" in a1["build"]["log_tail"]
    assert a2["verdict"]["state"] == "partial" and a2["verdict"]["passed"] == 6 and a2["verdict"]["scenarios"] == 8
    assert a3["verdict"]["state"] == "verified" and a3["verdict"]["passed"] == 8 and a3["verdict"]["written_by"] == "verifier"
    hashes = [a["call"]["prompt_sha256"] for a in (a1, a2, a3)]
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes) and len(set(hashes)) == 3
    for a, (pt, ct) in zip((a1, a2, a3), [(1500, 300), (1800, 5000), (2600, 4000)]):
        assert a["call"]["model"] == "gpt-fake" and a["call"]["usage"]["input_tokens"] == pt and a["call"]["usage"]["output_tokens"] == ct
        assert a["call"]["cost_usd"] == pytest.approx(cost_of(pt, ct)) and a["call"]["cost_known"] is True
    rows = studio.db.query("SELECT outcome, cost_usd, input_tokens, output_tokens FROM ai_calls WHERE case_id=? ORDER BY created_at", (cid,))
    assert [r["outcome"] for r in rows] == ["ok"] * 3 and sum(r["output_tokens"] for r in rows) == 9300
    budget = studio.budgets.get(f"case:{cid}")
    assert budget["spent_usd"] == pytest.approx(cost_of(1500, 300) + cost_of(1800, 5000) + cost_of(2600, 4000)) and budget["reserved_usd"] == 0

    # verdict: verified 8/8 within declared coverage; outcome fully_matched; the candidate is last-known-good
    final = studio.candidates.get(res["final_candidate"])
    assert final["verification"] == "verified" and final["last_known_good"] and final["meta"]["author"] == "model"
    rep = studio.cases.evidence_body(a3["verdict"]["report_evidence"])
    assert rep["summary"] == {"scenarios": 8, "passed": 8, "failed": 0, "errors": 0}
    out = case_outcome(studio, cid)
    assert out["state"] == "fully_matched" and out["verification"]["passed"] == 8 and out["verification"]["declared"] == 8 and out["scaffold_only"] is False
    assert out["verification"]["scope"] == "declared_scenarios_only"
    assert studio.ledger.summary(cid)["full_parity"] is True

    # delivery: final candidate published, not marked scaffold, attempts listed in the report
    d = job(studio, cid, "deliver")
    assert d.state == JobState.COMPLETED and d.result["scaffold_only"] is False and d.result["outcome"]["state"] == "fully_matched"
    outdir = tmp_path / "out"
    assert not (outdir / "SCAFFOLD_NOT_IMPLEMENTED.txt").exists()
    md = (outdir / "reports" / "parity-report.md").read_text("utf-8")
    assert "## AI attempts" in md and "Fully matched within declared coverage" in md
    man = json.loads((outdir / "manifest.json").read_text())
    assert {p.relative_to(outdir).as_posix() for p in outdir.rglob("*") if p.is_file()} == {f["path"] for f in man["files"]}

    # the key never leaves the secret store: not in the DB, events, evidence blobs, delivered files
    blob = json.dumps([dict(r) for r in studio.db.query("SELECT payload FROM events")]) + json.dumps([dict(r) for r in studio.db.query("SELECT * FROM ai_calls")]) \
        + json.dumps([dict(r) for r in studio.db.query("SELECT * FROM connections")])
    for ev in studio.cases.list_evidence(cid, include_stale=True):
        if ev["blob_sha"]:
            blob += studio.cases.blobs.get_bytes(ev["blob_sha"]).decode("utf-8", "replace")
    for p in outdir.rglob("*"):
        if p.is_file() and p.stat().st_size < 5_000_000 and p.suffix in (".json", ".md", ".html", ".txt", ".rs", ".toml"):
            blob += p.read_text("utf-8", "replace")
    assert API_KEY not in blob
    assert srv.headers[0]["authorization"] == f"Bearer {API_KEY}"
    print(f"\n[e2e timing] three-attempt loop incl. 3 requests, 2 cargo builds, 2 verifications, delivery: {time.time() - t0:.1f}s")


# ----------------------------------------------------------------------------------------- budget / pricing
def test_budget_is_reserved_before_each_call_and_stops_the_loop(studio, servers, tmp_path):
    # ceiling per call ~ 0.038 USD; the first answer is billed ~0.0306, which leaves too little for a second call under a 0.05 budget
    srv = servers([Reply(files_json(BROKEN_MAIN), 1000, 15000), Reply(files_json(GOOD_MAIN)), Reply(files_json(GOOD_MAIN))])
    connect(studio, srv)
    cid = make_case(studio, tmp_path, {"budget_usd": 0.05})
    drain(studio)
    loop = job(studio, cid, "implement_loop")
    res = loop.result
    assert loop.state == JobState.COMPLETED and res["stop_reason"] == "budget_exhausted" and res["verified"] is False
    assert len(srv.requests) == 1                                     # the second call never left the process
    assert "budget" in res["blocker"].lower() and "remains" in res["blocker"]
    b = studio.budgets.get(f"case:{cid}")
    assert b["spent_usd"] <= 0.05 and b["reserved_usd"] == 0
    impl = studio.plan.get_item(studio.plan.milestone_id(cid, "M-IMPL"))
    fix = studio.plan.get_item(studio.plan.milestone_id(cid, "M-FIX"))
    assert fix["status"] == "blocked" and "budget" in fix["blockers"][0].lower()
    out = case_outcome(studio, cid)
    assert out["state"] != "fully_matched" and out["state"] == "scaffolded" and out["can_claim_complete"] is False
    d = job(studio, cid, "deliver")
    assert d.state == JobState.COMPLETED and d.result["scaffold_only"] is True
    assert (tmp_path / "out" / "SCAFFOLD_NOT_IMPLEMENTED.txt").exists() and (tmp_path / "out" / "dist" / "SCAFFOLD_NOT_IMPLEMENTED.txt").exists()
    assert "SCAFFOLD ONLY" in (tmp_path / "out" / "reports" / "parity-report.md").read_text("utf-8")


def test_zero_budget_sends_nothing(studio, servers, tmp_path):
    srv = servers([Reply(files_json(GOOD_MAIN))])
    connect(studio, srv)
    cid = make_case(studio, tmp_path, {"budget_usd": 0})
    drain(studio)
    res = job(studio, cid, "reconstruct").result
    # a metered route with no budget is refused before the loop is even scheduled: scaffold path with the reason on the plan
    assert srv.requests == [] and "implement_job" not in res
    impl = studio.plan.get_item(studio.plan.milestone_id(cid, "M-IMPL"))
    assert impl["status"] == "blocked" and "budget" in impl["blockers"][0].lower()
    assert case_outcome(studio, cid)["state"] == "scaffolded"


def test_unknown_pricing_is_not_free_and_blocks_with_a_clear_message(studio, servers, tmp_path):
    srv = servers([Reply(files_json(GOOD_MAIN))])
    connect(studio, srv, price=None)
    fc = studio.implementation_forecast({"target_language": "rust", "output_type": "exe", "ai_policy": {"mode": "assisted", "budget_usd": 5.0}, "launch_profile": {}, "case_id": "x"})
    assert fc["state"] == "ai_blocked" and "unknown" in fc["summary"].lower() and fc["can_produce_implementation"] is False
    cid = make_case(studio, tmp_path, {})
    drain(studio)
    assert srv.requests == []
    impl = studio.plan.get_item(studio.plan.milestone_id(cid, "M-IMPL"))
    msg = impl["blockers"][0]
    assert impl["status"] == "blocked" and "unknown" in msg.lower() and "never treated as free" in msg and "max_output_tokens" in msg
    assert studio.db.query("SELECT * FROM ai_calls WHERE case_id=?", (cid,)) == []
    assert case_outcome(studio, cid)["state"] == "scaffolded"


def test_unknown_pricing_with_a_token_cap_runs_at_the_conservative_ceiling(studio, servers, tmp_path):
    srv = servers([Reply(files_json(GOOD_MAIN), 1000, 1000)])
    connect(studio, srv, price=None)
    cid = make_case(studio, tmp_path, {"max_output_tokens": 2000, "budget_usd": 5.0, "max_attempts": 1})
    drain(studio)
    assert len(srv.requests) == 1 and srv.requests[0]["max_completion_tokens"] == 2000
    a = attempts(studio, cid)[0]
    # never zero: charged at the conservative unknown-price ceiling (30/150 USD per Mtok), flagged as an estimate
    assert a["call"]["cost_known"] is False and a["call"]["cost_usd"] > 0.1
    assert job(studio, cid, "implement_loop").result["verified"] is True      # the (correct) model output is still judged only by the verifier


def test_local_openai_compatible_server_needs_no_budget(studio, servers, tmp_path):
    srv = servers([Reply(files_json(GOOD_MAIN), 900, 700)])
    connect(studio, srv, provider="local", price=None)
    fc = studio.implementation_forecast({"target_language": "rust", "output_type": "exe", "ai_policy": {"mode": "assisted"}, "launch_profile": {"baseline_file": "x"}, "case_id": "x"})
    assert fc["state"] == "ai_ready" and "local endpoint" in fc["summary"] and fc["pricing"] == "free"
    cid = make_case(studio, tmp_path, {"budget_usd": 0})
    drain(studio)
    res = job(studio, cid, "implement_loop").result
    assert res["verified"] is True and res["spent_usd"] == 0 and "authorization" not in srv.headers[0]
    assert case_outcome(studio, cid)["state"] == "fully_matched"


# ----------------------------------------------------------------------------------------- retries, fallback
def test_429_then_success_retries_with_backoff(studio, servers, tmp_path):
    srv = servers([Status(429, "0"), Status(503), Reply(files_json(GOOD_MAIN))])
    connect(studio, srv)
    studio.ai._sleep = lambda s: None
    cid = make_case(studio, tmp_path, {})
    drain(studio)
    res = job(studio, cid, "implement_loop").result
    assert res["verified"] is True and len(res["attempts"]) == 1 and len(srv.requests) == 3
    rows = studio.db.query("SELECT outcome, cost_usd FROM ai_calls WHERE case_id=? ORDER BY created_at", (cid,))
    assert [r["outcome"] for r in rows] == ["rate_limit", "unavailable", "ok"] and rows[0]["cost_usd"] == 0 and rows[1]["cost_usd"] == 0
    # only the successful call was billed; failed sends released their reservations
    assert studio.budgets.get(f"case:{cid}")["spent_usd"] == pytest.approx(cost_of(1200, 800)) and studio.budgets.get(f"case:{cid}")["reserved_usd"] == 0
    routers = attempts(studio, cid)[0]["call"]["router_attempts"]
    assert [x["outcome"] for x in routers] == ["rate_limit", "unavailable", "ok"]


def test_backoff_is_exponential_and_retry_after_wins(studio, servers, tmp_path):
    sleeps: list[float] = []
    srv = servers([Status(503), Status(503), Status(429, "7"), Reply(files_json(GOOD_MAIN))])
    connect(studio, srv)
    studio.ai._sleep = sleeps.append
    cid = make_case(studio, tmp_path, {"retry_backoff_s": 0.5})
    drain(studio)
    assert job(studio, cid, "implement_loop").result["verified"] is True
    assert sleeps == [0.5, 1.0, 7.0]


def test_fallback_route_after_retries_are_exhausted(studio, servers, tmp_path):
    primary = servers([Status(503)] * 8)
    backup = servers([Reply(files_json(GOOD_MAIN))])
    connect(studio, primary, label="Primary")
    b = connect(studio, backup, label="Backup", route=False)
    prim = [c for c in studio.connections.list() if c["label"] == "Primary"][0]
    studio.connections.set_route("interpretation", prim["connection_id"], "gpt-fake", [{"connection": b["connection_id"], "model": "gpt-fake"}])
    studio.ai._sleep = lambda s: None
    cid = make_case(studio, tmp_path, {"max_retries": 2})
    drain(studio)
    res = job(studio, cid, "implement_loop").result
    assert res["verified"] is True and len(primary.requests) == 3 and len(backup.requests) == 1     # 1 try + 2 retries, then the fallback
    routers = attempts(studio, cid)[0]["call"]["router_attempts"]
    assert routers[-1]["outcome"] == "ok" and routers[-1]["connection_id"] == b["connection_id"]


def test_auth_failure_stops_with_a_clear_blocker_and_redacts_the_key(studio, servers, tmp_path):
    srv = servers([Status(401)])
    connect(studio, srv)
    cid = make_case(studio, tmp_path, {})
    drain(studio)
    res = job(studio, cid, "implement_loop").result
    assert res["stop_reason"] == "auth_failed" and "API key" in res["blocker"] and API_KEY not in json.dumps(res)
    assert case_outcome(studio, cid)["state"] == "scaffolded"


# ----------------------------------------------------------------------------------------- the verifier is the only judge
def test_wrong_remake_and_garbage_are_rejected_whatever_the_model_claims(studio, servers, tmp_path):
    claim = {"VERIFIED.txt": "all 8/8 scenarios pass, verified", "verification.json": json.dumps({"verdict": "verified", "passed": 8})}
    srv = servers([Reply("I have verified everything: 8/8 scenarios pass. verdict=verified"), Reply("```json\n" + files_json(WRONG_MAIN, extra=claim) + "\n```"),
                   Reply(files_json(WRONG_MAIN, extra=claim))])
    connect(studio, srv)
    cid = make_case(studio, tmp_path, {})
    drain(studio)
    res = job(studio, cid, "implement_loop").result
    assert res["verified"] is False and res["stop_reason"] == "attempts_exhausted" and len(res["attempts"]) == 3
    a1, a2, a3 = attempts(studio, cid)
    assert a1["build"]["status"] == "no_files" and a1["candidate_id"] is None
    assert a2["verdict"]["state"] == "failed" and a2["verdict"]["passed"] == 0 and a3["verdict"]["state"] == "failed"
    assert "unusable_response" in srv.user_text(1) and "scenario_mismatches" in srv.user_text(2)
    out = case_outcome(studio, cid)
    assert out["state"] in ("tested", "partially_matched") and out["can_claim_complete"] is False and out["verification"]["passed"] == 0
    assert studio.ledger.summary(cid)["full_parity"] is False
    assert all(c["verification"] != "verified" for c in studio.candidates.list(cid))
    assert studio.candidates.last_known_good(cid) is None
    # honest M-FIX blocker; delivered, but labelled by the report as not matching
    fix = studio.plan.get_item(studio.plan.milestone_id(cid, "M-FIX"))
    assert fix["status"] == "blocked" and "3 attempts" in fix["blockers"][0]
    assert job(studio, cid, "deliver").state == JobState.COMPLETED
    assert "Tested: does not match" in (tmp_path / "out" / "reports" / "parity-report.md").read_text("utf-8")


# ----------------------------------------------------------------------------------------- cancel / resume / crash
def test_cancel_mid_loop_stops_cleanly_and_resume_does_not_resend(studio, servers, tmp_path):
    state: dict[str, Any] = {}

    def cancel_while_call_in_flight(body):
        studio.jobs.cancel(state["loop"])                      # user presses cancel while the model call is running
        return Reply(files_json(PARTIAL_MAIN), 1800, 2000)
    srv = servers([Reply(files_json(BROKEN_MAIN), 1500, 300), Hook(cancel_while_call_in_flight), Reply(files_json(GOOD_MAIN), 2600, 1000)])
    connect(studio, srv)
    cid = make_case(studio, tmp_path, {})
    studio.runner.run_pending(max_jobs=3)                      # discover, capture, reconstruct
    state["loop"] = job(studio, cid, "implement_loop").job_id
    studio.runner.run_pending(max_jobs=1)                      # runs the loop until cancelled inside attempt 2
    loop = job(studio, cid, "implement_loop")
    assert loop.state == JobState.CANCELLED and len(srv.requests) == 2
    assert [a["attempt"] for a in attempts(studio, cid)] == [1]
    # the in-flight call finished in the background: its answer is persisted and its spend settled, nothing is held
    deadline = time.time() + 20
    while time.time() < deadline and not [e for e in studio.cases.list_evidence(cid, kind="ai_response") if e["meta"]["attempt"] == 2]:
        time.sleep(0.1)
    assert [e for e in studio.cases.list_evidence(cid, kind="ai_response") if e["meta"]["attempt"] == 2]
    assert studio.budgets.get(f"case:{cid}")["reserved_usd"] == 0
    assert job(studio, cid, "deliver").state == JobState.CANCELLED          # nothing is delivered from a cancelled run
    # resume: attempt 2's stored answer is reused (no second send), attempt 3 is the only new request
    studio.resume(case_id=cid)
    drain(studio)
    loop = job(studio, cid, "implement_loop")
    assert loop.state == JobState.COMPLETED and loop.result["verified"] is True
    assert len(srv.requests) == 3
    rows = studio.db.query("SELECT outcome FROM ai_calls WHERE case_id=?", (cid,))
    assert [r["outcome"] for r in rows] == ["ok", "ok", "ok"]
    assert case_outcome(studio, cid)["state"] == "fully_matched"


def test_crash_after_send_before_answer_is_counted_as_spent_and_never_resent(studio, servers, tmp_path):
    srv = servers([Reply(files_json(PARTIAL_MAIN), 1000, 1000), Reply(files_json(GOOD_MAIN), 1000, 1000)])
    connect(studio, srv)
    cid = make_case(studio, tmp_path, {})
    studio.runner.run_pending(max_jobs=3)
    loop_id = job(studio, cid, "implement_loop").job_id
    # simulate: attempt 1 was sent and reserved, then the process died before the answer was stored
    bid = f"case:{cid}"
    studio.budgets.ensure(bid, bid, 5.0)
    rsv = studio.budgets.reserve(bid, 0.04, f"{loop_id}:a1:somconn:gpt-fake:1")
    drain(studio)
    res = job(studio, cid, "implement_loop").result
    assert len(srv.requests) == 2                              # attempts 2 and 3 only; attempt 1 was NOT sent again
    first = res["attempts"][0]
    assert first["attempt"] == 1 and first["build"] == "no_files" and first["candidate_id"] is None
    assert studio.budgets.get_reservation(rsv["reservation_id"])["state"] == "settled"      # assumed spent at its ceiling
    assert res["spent_usd"] >= 0.04 and res["verified"] is True


def test_job_retry_after_worker_crash_reuses_stored_answers(studio, servers, tmp_path):
    """Lease expiry (worker died) re-queues the loop job: stored answers and finished attempts are reused, only missing work is re-done."""
    srv = servers([Reply(files_json(BROKEN_MAIN), 1500, 300), Reply(files_json(GOOD_MAIN), 2600, 1000)])
    connect(studio, srv)
    cid = make_case(studio, tmp_path, {})
    studio.runner.run_pending(max_jobs=3)
    lp = job(studio, cid, "implement_loop")
    # a worker claims the loop job, runs attempt 1 fully (simulated by running the stage, then dying before completing the job)
    claimed = studio.jobs.claim_next("dying-worker")
    assert claimed.job_id == lp.job_id
    from rebuild_controller.implement import _run_attempt, LoopPolicy
    from rebuild_controller.jobs.runner import StageContext
    ctx = StageContext(job=claimed, jobs=studio.jobs, events=studio.events, worker="dying-worker", limits=studio.settings.limits, services=studio.services)
    case = studio.cases.get_case(cid)
    pk = studio.cases.evidence_body(claimed.inputs["packet"])
    _run_attempt(studio, ctx, case, LoopPolicy.from_case(case), n=1, loop_id=claimed.job_id, prev_id=claimed.inputs["candidate_id"], scaffold_id=claimed.inputs["candidate_id"],
                 packet=pk, feedback=None, history=[], has_baseline=True)
    assert len(srv.requests) == 1
    assert studio.jobs.recover_stale(now=time.time() + 10_000) == [lp.job_id]
    drain(studio)
    res = job(studio, cid, "implement_loop").result
    assert len(srv.requests) == 2 and res["verified"] is True and [a["attempt"] for a in res["attempts"]] == [1, 2]
    assert "cargo build failed" in srv.user_text(1)            # attempt 1's recorded build failure became attempt 2's feedback after the restart
    assert len(attempts(studio, cid)) == 2                     # attempt 1 was not duplicated


# ----------------------------------------------------------------------------------------- live (opt-in)
@pytest.mark.live
@pytest.mark.skipif(not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")), reason="needs ANTHROPIC_API_KEY or OPENAI_API_KEY (live provider)")
def test_live_provider_runs_the_same_loop_with_a_small_budget(studio, tmp_path):
    """Opt-in. Same loop against a real provider: asserts only provider-independent invariants (bounded spend, evidence, verifier decides),
    never that the model succeeds. Choose the model with REBUILD_LIVE_MODEL; set REBUILD_LIVE_PRICE_IN/OUT (USD per Mtok) for non-Anthropic models."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        provider, key, model = "anthropic", os.environ["ANTHROPIC_API_KEY"], os.environ.get("REBUILD_LIVE_MODEL", "claude-haiku-4-5")
    else:
        provider, key, model = "openai", os.environ["OPENAI_API_KEY"], os.environ.get("REBUILD_LIVE_MODEL", "gpt-4.1-mini")
    entry: dict[str, Any] = {"id": model}
    if os.environ.get("REBUILD_LIVE_PRICE_IN") and os.environ.get("REBUILD_LIVE_PRICE_OUT"):
        entry["price"] = {"input_per_mtok": float(os.environ["REBUILD_LIVE_PRICE_IN"]), "output_per_mtok": float(os.environ["REBUILD_LIVE_PRICE_OUT"])}
    conn = studio.connections.create(provider, "live", auth_mode="api_key", api_key=key, models=[entry])
    studio.connections.set_route("interpretation", conn["connection_id"], model)
    budget = float(os.environ.get("REBUILD_LIVE_BUDGET_USD", "0.50"))
    cid = make_case(studio, tmp_path, {"budget_usd": budget, "max_attempts": 2, "max_output_tokens": 8000})
    drain(studio)
    loop = job(studio, cid, "implement_loop")
    assert loop is not None and loop.state == JobState.COMPLETED, (loop and loop.error)
    res = loop.result
    assert res["stop_reason"] in ("verified", "attempts_exhausted", "budget_exhausted", "pricing_unknown")
    assert studio.budgets.get(f"case:{cid}")["spent_usd"] <= budget + 1e-9             # reservation-before-call bounds spend
    for a in attempts(studio, cid):
        assert re.fullmatch(r"[0-9a-f]{64}", a["call"]["prompt_sha256"]) and a["call"]["model"]
    out = case_outcome(studio, cid)
    assert (out["state"] == "fully_matched") == bool(res["verified"])                   # the verifier, not the model, decides
    assert key not in json.dumps([dict(r) for r in studio.db.query("SELECT payload FROM events")])
