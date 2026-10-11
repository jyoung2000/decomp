"""R5 AI-assisted RE quality: context fit, slim repair prompts (failing slices + line edits), model-proposed annotations
(accept / reject / confirm / rollback), per-function AI naming, the model scoreboard and the preset recommendation that uses it.

No cloud call: models are the scripted fake OpenAI-compatible server (test_implement_loop.FakeOpenAI) or fakes; the real-rizin
round trip skips when rizin is not installed."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rebuild_controller import prompt_fit as pf
from rebuild_controller import scoreboard
from rebuild_controller.adapters.contract import OperationResult
from rebuild_controller.backends import re_annotations as ann

REPO = Path(__file__).resolve().parents[2]
TOKEN = "q" * 32


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def studio(settings):
    from rebuild_controller.services import StudioServices
    s = StudioServices(settings)
    s.ai.advisor = None
    yield s
    s.stop()


# =============================================================================================== (d) context fit
def _packet(n_ex: int = 6, body: int = 20_000) -> dict:
    return {"case_id": "c", "target": "rust", "scenarios": [{"id": f"s{i}", "original_behaviour": {"steps": [{"stdout": "x" * 5000, "stderr": ""}]}}
                                                             for i in range(4)],
            "evidence_index": [{"id": f"e{i}", "kind": "strings", "title": "t" * 80} for i in range(300)],
            "excerpts": [{"id": f"x{i}", "kind": "native.briefing", "title": f"Function briefing f{i}", "body": {"text": "y" * body}}
                         for i in range(n_ex)]}


def test_fit_packet_keeps_valid_json_and_drops_lowest_priority_functions_first():
    p = _packet()
    big = len(json.dumps(p))
    out = pf.fit_packet(p, 40_000)
    s = json.dumps(out)
    assert json.loads(s) and len(s) <= 40_000 and out["fit"]["trimmed"] and out["fit"]["fits"]
    assert out["fit"]["before_chars"] == big and out["fit"]["index_dropped"] == 300
    titles = [e["title"] for e in out["excerpts"]]
    assert titles == [f"Function briefing f{i}" for i in range(len(titles))]          # kept in priority order, whole units dropped from the end
    assert all(t.startswith("Function briefing f") for t in out["fit"]["excerpts_dropped"])
    assert p["excerpts"][0]["body"] == {"text": "y" * 20_000}                          # the input is not modified
    small = pf.fit_packet(_packet(1, 100), 10**6)
    assert small["fit"]["trimmed"] is False and small["excerpts"][0]["body"] == {"text": "y" * 100}


def test_prompt_budget_uses_the_first_rung_context_and_the_ollama_cap(studio):
    c = studio.connections.create("local", "Ollama (this PC)", endpoint="http://127.0.0.1:11434/v1", auth_mode="local", models=["qwen2.5-coder:14b"])
    studio.db.update("connections", "connection_id", c["connection_id"], {"models": [{"id": "qwen2.5-coder:14b", "source": "explicit", "context_window": 32768}]})
    studio.connections.set_limits(c["connection_id"], server="ollama", num_ctx_cap=16384)
    studio.connections.set_route("repair", c["connection_id"], "qwen2.5-coder:14b")
    b = pf.prompt_budget(studio, "repair", {"mode": "assisted"}, max_output_tokens=16000, system_chars=1000)
    # min(32768 model, 16384 cap) - max(4096, 16384//4) answer reserve - 512 margin = 11776 tokens * 2 chars - 1000 system chars
    assert b["context_tokens"] == 16384 and b["model"] == "qwen2.5-coder:14b" and b["chars"] == 11776 * 2 - 1000
    assert "num_ctx_cap 16384" in b["basis"]
    none = pf.prompt_budget(studio, "knowledge", {"mode": "assisted"}, max_output_tokens=1000)
    assert none["chars"] is None and none["model"] is None


# =============================================================================================== (a) slim repair prompts
RUST_LOG = """   Compiling app v0.1.0
error[E0308]: mismatched types
 --> src/main.rs:52:18
  |
52 |     let x: i32 = "not a number";
  |            ---   ^^^^^^^^^^^^^^ expected `i32`, found `&str`
error: could not compile `app` (bin "app") due to 1 previous error
"""


def _big_main() -> str:
    filler = "".join(f"fn helper_{i}() -> u32 {{\n    {i}\n}}\n\n" for i in range(12))
    target = 'fn compute(a: u32) -> u32 {\n    let y = a + 1;\n    let x: i32 = "not a number";\n    y\n}\n\n'
    rest = "".join(f"fn tail_{i}() {{\n    println!(\"{i}\");\n}}\n" for i in range(50))
    return filler + target + rest + "fn main() {\n    compute(1);\n}\n"


def test_compiler_errors_and_failing_function_slice():
    main = _big_main()
    lines = main.splitlines()
    err_line = next(i for i, ln in enumerate(lines, 1) if "not a number" in ln)
    log = RUST_LOG.replace("src/main.rs:52:18", f"src/main.rs:{err_line}:18")
    errs = pf.compiler_errors(log, "rust")
    assert len(errs) == 1 and errs[0]["code"] == "E0308" and errs[0]["line"] == err_line and "expected `i32`" in errs[0]["block"]
    sl = pf.failing_slices({"src/main.rs": main, "Cargo.toml": "[package]\n"}, log, "rust")
    (s,) = sl["slices"]
    assert s["path"] == "src/main.rs" and s["function"].startswith("fn compute") and not s["whole"]
    assert lines[s["start"] - 1].startswith("fn compute") and lines[s["end"] - 1] == "}" and s["end"] - s["start"] == 4
    prompt = pf.slim_repair_prompt(attempt=2, max_attempts=3, tool="cargo build", files={"src/main.rs": main, "Cargo.toml": "[package]\n"},
                                   sl=sl, history=["attempt 1: build failed"])
    assert "build_failed" in prompt and "E0308" in prompt and f"{err_line}|     let x: i32" in prompt and '"edits"' in prompt
    assert "helper_3" not in prompt and "tail_7" not in prompt                       # only the failing function travels
    assert "Cargo.toml (10 chars)" in prompt and len(prompt) < len(main)


def test_csharp_and_java_errors_map_to_files():
    cs = "\n".join(f"// l{i}" for i in range(10))
    log = r"C:\sandbox\cand\NotesApp\Menus.cs(4,9): error CS0246: The type or namespace name 'List<>' could not be found [C:\sandbox\cand\app.csproj]"
    sl = pf.failing_slices({"NotesApp/Menus.cs": cs}, log, "csharp")
    assert sl["slices"][0]["path"] == "NotesApp/Menus.cs" and sl["slices"][0]["whole"] and "CS0246" in sl["errors"][0]["block"]
    jlog = "src/dev/App.java:3: error: cannot find symbol\n  symbol:   class Scanner\n"
    jl = pf.failing_slices({"src/dev/App.java": "a\nb\nc\n"}, jlog, "java")
    assert jl["slices"][0]["path"] == "src/dev/App.java" and "Scanner" in jl["errors"][0]["block"]


def test_apply_edits_replace_insert_delete_and_refusals():
    cur = {"a.rs": "one\ntwo\nthree\nfour\n"}
    files, prob = pf.apply_edits([{"path": "a.rs", "start_line": 2, "end_line": 2, "text": "TWO"},
                                  {"path": "a.rs", "start_line": 4, "end_line": 4, "text": ""},
                                  {"path": "a.rs", "start_line": 1, "end_line": 0, "text": "zero"}], cur.get)
    assert prob is None and files == {"a.rs": "zero\none\nTWO\nthree\n"}
    assert pf.apply_edits([{"path": "a.rs", "start_line": 2, "end_line": 9, "text": ""}], cur.get)[1].startswith("edit for a.rs: lines 2-9")
    assert "overlap" in pf.apply_edits([{"path": "a.rs", "start_line": 1, "end_line": 2, "text": ""},
                                        {"path": "a.rs", "start_line": 2, "end_line": 3, "text": ""}], cur.get)[1]
    assert "no such file" in pf.apply_edits([{"path": "b.rs", "start_line": 1, "end_line": 1, "text": ""}], cur.get)[1]
    assert pf.apply_edits([{"path": "a.rs", "start_line": "1", "end_line": 1, "text": ""}], cur.get)[0] == {}
    from rebuild_controller.implement import parse_answer
    f2, p2 = parse_answer('```json\n{"edits": [{"path": "a.rs", "start_line": 1, "end_line": 1, "text": "ONE"}]}\n```', cur.get)
    assert p2 is None and f2["a.rs"].startswith("ONE\ntwo")
    f3, _ = parse_answer('{"src/main.rs": "fn main() {}"}', cur.get)                  # whole files still accepted
    assert f3 == {"src/main.rs": "fn main() {}"}


def test_ollama_adapter_constrains_naming_and_edit_answers():
    from rebuild_controller.providers.base import Message, Request
    from rebuild_controller.providers.ollama_chat import OllamaChatAdapter
    a = OllamaChatAdapter(endpoint="http://127.0.0.1:11434/v1")
    for fmt, key in (("json_edits", "edits"), ("json_naming", "name")):
        body = a._body(Request(model="m", messages=[Message.user("x")], metadata={"output_format": fmt}), 8192)
        assert key in body["format"]["properties"] and body["format"]["required"][0] == key


# =============================================================================================== (b) annotations: model-proposed
def _module(cases, tmp_path):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "a.exe").write_bytes(b"MZ" + b"\0" * 64)
    case = cases.create_case(name="t", source_root=str(src), output_root=str(tmp_path / "out"), target_language="auto", output_type="exe")
    mid = cases.add_module(case["case_id"], "a.exe", "0" * 64, 66, "pe", "native_pe")
    return case["case_id"], mid


def _save(cases, cid, mid, state, change, author, prov=None):
    _, before, parent = ann.load(cases, cid, mid)
    ann.mark(state, before, change, author=author, provenance=prov)
    return ann.save(cases, cid, mid, state, change, parent=parent, author=author)


def test_model_proposals_are_labelled_until_reviewed_and_can_be_rejected_or_rolled_back(cases, tmp_path):
    cid, mid = _module(cases, tmp_path)
    s = ann.empty()
    s["functions"]["0x401000"] = {"name": "user_name"}
    _save(cases, cid, mid, s, {"action": "rename", "kind": "function", "addr": "0x401000", "old": "fcn.00401000", "new": "user_name"}, "user")
    rev_user = ann.load(cases, cid, mid)[0]
    s = ann.load(cases, cid, mid)[1]
    s["functions"]["0x401000"] = {**s["functions"]["0x401000"], "name": "parse_args"}
    prov = {"model": "qwen2.5-coder:14b", "task": "naming", "confidence": 0.7}
    _save(cases, cid, mid, s, {"action": "rename", "kind": "function", "addr": "0x401000", "old": "user_name", "new": "parse_args"}, "model", prov)
    s = ann.load(cases, cid, mid)[1]
    s["functions"]["0x402000"] = {"name": "crc32_table"}
    _save(cases, cid, mid, s, {"action": "rename", "kind": "function", "addr": "0x402000", "old": "fcn.00402000", "new": "crc32_table"}, "model", prov)
    s = ann.load(cases, cid, mid)[1]
    s["comments"]["0x402000"] = {"text": "builds the CRC table"}
    _save(cases, cid, mid, s, {"action": "comment", "addr": "0x402000", "text": "builds the CRC table"}, "model", prov)
    st = ann.load(cases, cid, mid)[1]
    props = ann.proposals(st)
    assert [(p["kind"], p["key"], p["label"]) for p in props] == [("comment", "0x402000", "model-proposed"), ("function", "0x401000", "model-proposed"),
                                                                  ("function", "0x402000", "model-proposed")]
    f1 = next(p for p in props if p["key"] == "0x401000" and p["kind"] == "function")
    assert f1["model"] == "qwen2.5-coder:14b" and f1["replaced"]["name"] == "user_name"
    f2 = next(p for p in props if p["key"] == "0x402000" and p["kind"] == "function")
    assert f2["replaced"] is None and f2["analysis_name"] == "fcn.00402000"
    lines = dict(ann.comments_in(st, 0x400000, 0x403000))
    assert lines[0x402000].startswith("[model-proposed]")
    assert any("function name parse_args (not yet accepted)" in t for _, t in ann.comments_in(st, 0x401000, 0x401001))
    # a person accepts one, a test confirms one, and a person rejects the one that replaced their own name
    st2 = ann.mutated(st)
    ch = ann.review(st2, "function", "0x402000", "accept")
    assert ch["decision"] == "accept" and st2["functions"]["0x402000"]["prov"]["status"] == "accepted"
    with pytest.raises(ann.AnnotationError):
        ann.review(st2, "comment", "0x402000", "confirm")                             # a confirmation must name its test
    ann.review(st2, "comment", "0x402000", "confirm", evidence="tests/test_bench_names.py::crc")
    ch = ann.review(st2, "function", "0x401000", "reject")
    assert st2["functions"]["0x401000"]["name"] == "user_name" and st2["functions"]["0x401000"]["prov"]["origin"] == "user"
    assert ch["restored"]["name"] == "user_name"
    assert [p["label"] for p in ann.proposals(st2)] == ["confirmed by a test", "accepted by you"]
    assert not ann.comments_in(st2, 0x401000, 0x401001)                               # nothing labelled any more there
    with pytest.raises(ann.AnnotationError):
        ann.review(st2, "function", "0x401000", "accept")                             # a user entry is not a proposal
    # rollback to the revision before any model proposal
    old, _ = ann.state_at(cases, cid, mid, rev_user)
    assert old["functions"] == {"0x401000": {"name": "user_name", "prov": old["functions"]["0x401000"]["prov"]}} and not old["comments"]
    with pytest.raises(ann.AnnotationError):
        ann.state_at(cases, cid, mid, 999)


def test_backend_review_and_rollback_ops_refuse_models_and_reset_the_session(cases, tmp_path, settings):
    from rebuild_controller.backends.rizin_worker import RizinBackend
    cid, mid = _module(cases, tmp_path)
    s = ann.empty()
    s["functions"]["0x401000"] = {"name": "do_work"}
    _save(cases, cid, mid, s, {"action": "rename", "kind": "function", "addr": "0x401000", "old": "fcn.00401000", "new": "do_work"}, "model",
          {"model": "m"})
    b = RizinBackend(settings)
    assert not b.op_review_annotation(cases, case_id=cid, module_id=mid, kind="function", key="0x401000", decision="accept", author="model").ok
    assert not b.op_review_annotation(cases, case_id=cid, module_id=mid, kind="function", key="0x401000", decision="accept", author="test").ok
    r = b.op_review_annotation(cases, case_id=cid, module_id=mid, kind="function", key="0x401000", decision="reject")
    assert r.ok and r.data["change"]["decision"] == "reject" and ann.load(cases, cid, mid)[1]["functions"] == {}
    r = b.op_rollback_annotations(cases, case_id=cid, module_id=mid, revision=1)
    assert r.ok and r.data["change"]["to_revision"] == 1 and ann.load(cases, cid, mid)[1]["functions"]["0x401000"]["name"] == "do_work"
    assert [h["change"]["action"] for h in ann.history(cases, cid, mid)] == ["rename", "review", "rollback"]
    assert not b.op_rollback_annotations(cases, case_id=cid, module_id=mid, revision=1, author="model").ok
    data = b.op_annotations(cases, case_id=cid, module_id=mid).data
    assert data["open_proposals"] == 1 and data["proposals"][0]["label"] == "model-proposed"


def test_parse_naming_validates_identifiers():
    from rebuild_controller.ai_naming import is_auto_name, parse_naming
    ok, _ = parse_naming('{"name": "crc32_update", "comment": "Updates a CRC.\\nTwo lines", "locals": {"var_8h": "crc", "bad": "1x", "x": "x"}, "confidence": 0.8}')
    assert ok == {"name": "crc32_update", "comment": "Updates a CRC. Two lines", "locals": {"var_8h": "crc"}, "confidence": 0.8}
    assert parse_naming('{"name": "rm -rf; x", "comment": ""}')[0] is None
    assert parse_naming('{"name": "fcn.00401000"}')[0] is None and parse_naming("no json")[0] is None
    assert is_auto_name("fcn.140001190") and is_auto_name("sub_401000") and not is_auto_name("main")


# =============================================================================================== (b) naming job, fake backend
class FakeBackend:
    """Implements the four ops the naming stage uses on top of the real annotation store (no rizin)."""

    def __init__(self, cases):
        self.cases = cases
        self.funcs = [{"offset": 0x401000, "name": "fcn.00401000", "size": 300}, {"offset": 0x402000, "name": "main", "size": 900},
                      {"offset": 0x403000, "name": "fcn.00403000", "size": 120}]

    def op_functions(self, ctx, *, case_id, module_id):
        return OperationResult(ok=True, data={"functions": [{**f, "addr": f"0x{f['offset']:x}"} for f in self.funcs]})

    def op_decompile(self, ctx, *, case_id, module_id, function):
        return OperationResult(ok=True, data={"decompiled": {"untrusted": True, "text": f"uint32_t {function}(char *var_8h) {{ return crc(var_8h); }}"}})

    def _mut(self, case_id, module_id, change, apply, author, prov):
        _, st, parent = ann.load(self.cases, case_id, module_id)
        new = ann.mutated(st)
        apply(new)
        ann.mark(new, st, change, author=author, provenance=prov)
        ev = ann.save(self.cases, case_id, module_id, new, change, parent=parent, author=author)
        return OperationResult(ok=True, data={"change": change, "revision": ev["revision"]})

    def op_rename(self, ctx, *, case_id, module_id, kind, target, new_name, function=None, author="user", provenance=None):
        ann.check_ident(new_name)
        if kind == "local":
            fk = function
            ch = {"action": "rename", "kind": "local", "function": fk, "old": target, "new": new_name, "original": target}
            return self._mut(case_id, module_id, ch, lambda s: s["locals"].setdefault(fk, {}).setdefault(target, {}).update(name=new_name), author, provenance)
        ch = {"action": "rename", "kind": "function", "addr": target, "old": "fcn", "new": new_name}
        return self._mut(case_id, module_id, ch, lambda s: s["functions"].setdefault(target, {}).update(name=new_name), author, provenance)

    def op_add_comment(self, ctx, *, case_id, module_id, addr, text, author="user", provenance=None):
        ch = {"action": "comment", "addr": addr, "text": text}
        return self._mut(case_id, module_id, ch, lambda s: s["comments"].__setitem__(addr, {"text": text}), author, provenance)


def _drain(st, rounds=100):
    from rebuild_controller.jobs import JobState
    for _ in range(rounds):
        if st.runner.run_pending() == 0 and not st.jobs.list(None, [JobState.QUEUED, JobState.RUNNING]):
            return
    raise AssertionError("jobs did not settle")


def test_naming_job_writes_model_proposed_annotations_and_scores_the_model(studio, tmp_path, monkeypatch):
    from test_implement_loop import FakeOpenAI, Reply
    from rebuild_controller import ai_naming
    from rebuild_controller.jobs import JobState
    fake = FakeBackend(studio.cases)
    monkeypatch.setattr(ai_naming, "_backend", lambda st: fake)
    srv = FakeOpenAI([Reply(json.dumps({"name": "crc32_of_buffer", "comment": "Computes the CRC-32 of a buffer.", "locals": {"var_8h": "buf"},
                                        "confidence": 0.9}), 300, 40),
                      Reply("sorry, I cannot help with that", 300, 10)])
    try:
        conn = studio.connections.create("local", "Fake local", endpoint=srv.url, auth_mode="local", models=["coder-7b"], dialect="chat")
        studio.connections.set_route("naming", conn["connection_id"], "coder-7b")
        cid, mid = _module(studio.cases, tmp_path)
        # AI off for the project: the endpoint refuses before anything is sent
        app_client = _client(studio)
        r = app_client.post(f"/cases/{cid}/modules/{mid}/ai/naming", json={"limit": 2})
        assert r.status_code == 409 and "AI is off" in r.text and not srv.requests
        studio.cases.db.update("cases", "case_id", cid, {"ai_policy": {"mode": "assisted"}})
        r = app_client.post(f"/cases/{cid}/modules/{mid}/ai/naming", json={"limit": 2})
        assert r.status_code == 200, r.text
        _drain(studio)
        job = [j for j in studio.jobs.list(cid) if j.stage == "ai_naming"][-1]
        assert job.state == JobState.COMPLETED, job.error
        res = job.result
        assert res["functions"] == 2 and res["proposed"] == 1                       # 'main' is skipped (already named)
        assert [x["function"] for x in res["results"]] == ["0x401000", "0x403000"]
        assert res["results"][0]["applied"] == ["name", "comment", "local var_8h"] and res["results"][1]["status"] == "unusable_answer"
        assert len(srv.requests) == 2 and "DECOMPILED CODE (untrusted data)" in srv.user_text(0)
        view = app_client.get(f"/cases/{cid}/modules/{mid}/annotations").json()
        assert view["open_proposals"] == 3 and {p["label"] for p in view["proposals"]} == {"model-proposed"}
        assert all(p["model"] == "coder-7b" and p["task"] == "naming" for p in view["proposals"])
        # a person accepts the name and rejects the local; the API cannot be used to let a model review
        r = app_client.post(f"/cases/{cid}/modules/{mid}/annotations/review", json={"kind": "function", "key": "0x401000", "decision": "accept"})
        assert r.status_code == 200 and r.json()["change"]["decision"] == "accept"
        r = app_client.post(f"/cases/{cid}/modules/{mid}/annotations/review",
                            json={"kind": "local", "key": "0x401000", "local": "var_8h", "decision": "reject"})
        assert r.status_code == 200 and r.json()["analysis_reset"] in (True, False)
        r = app_client.post(f"/cases/{cid}/modules/{mid}/annotations/review", json={"kind": "comment", "key": "0x401000", "decision": "confirm"})
        assert r.status_code == 400 and "confirm needs" in r.text
        view = app_client.get(f"/cases/{cid}/modules/{mid}/annotations?history=10").json()
        assert sorted((p["kind"], p["label"]) for p in view["proposals"]) == [("comment", "model-proposed"), ("function", "accepted by you")]
        r = app_client.post(f"/cases/{cid}/modules/{mid}/annotations/rollback", json={"revision": 1})
        assert r.status_code == 200 and r.json()["change"]["action"] == "rollback"
        # activity + scoreboard
        acts = [json.loads(a["payload"])["text"] for a in studio.events.db.query("SELECT payload FROM events WHERE kind='ai.activity' AND case_id=?", (cid,))]
        assert any("proposed the name crc32_of_buffer" in a and "model-proposed" in a for a in acts)
        board = app_client.get("/ai/scoreboard").json()
        m = next(x for x in board["models"] if x["model"] == "coder-7b")
        assert m["tasks"]["naming"]["runs"] == 2 and m["tasks"]["naming"]["ok"] == 1 and m["locality"] == "local"
        assert all(r_["task"] == "naming" for r_ in board["recent"])
    finally:
        srv.close()


def _client(studio):
    from rebuild_controller.api.server import create_app
    c = TestClient(create_app(studio, TOKEN))
    c.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return c


# =============================================================================================== (c) scoreboard + preset recommendation
def test_scoreboard_summary_rank_and_import(db):
    for ok, comp in ((True, False), (True, False), (True, False)):
        scoreboard.record(db, task="implementation", model="big-general:14b", ok=ok, compiled=comp, verified=False, latency_ms=400_000, locality="local")
    scoreboard.record(db, task="repair", model="coder:7b", ok=True, compiled=True, verified=True, latency_ms=60_000, locality="local")
    out = scoreboard.import_rows(db, [{"task": "naming", "model": "coder:7b", "ok": True, "truth_match": True, "row": "c_msvc_x64_o2", "latency_ms": 9000},
                                      {"task": "naming", "model": "coder:7b", "ok": True, "truth_match": False, "row": "c_msvc_x64_o2"},
                                      {"bad": 1}])
    assert out["imported"] == 2 and out["problems"] == ["row 2: needs task, model and ok"]
    s = scoreboard.summary(db)
    big = next(m for m in s["models"] if m["model"] == "big-general:14b")
    coder = next(m for m in s["models"] if m["model"] == "coder:7b")
    assert big["code_runs"] == 3 and big["ever_compiled"] is False and big["tasks"]["implementation"]["median_latency_ms"] == 400_000
    assert coder["ever_compiled"] and coder["tasks"]["naming"]["truth_rate"] == 0.5 and coder["tasks"]["naming"]["score"] == 2.0
    assert scoreboard.rank_key(s, "implementation", "coder:7b")[0] == 0          # repair success counts for implementation
    assert scoreboard.rank_key(s, "implementation", "big-general:14b") == (2, 0.0)
    assert scoreboard.rank_key(s, "implementation", "unknown:3b") == (1, 0.0)
    assert "3 code attempt(s) here: 0 compiled" in scoreboard.describe("implementation", scoreboard.measured(s, "implementation", "big-general:14b"))
    assert scoreboard.record(db, task="naming", model=None, ok=True) is None


def test_preset_proposes_the_measured_best_local_model_and_warns_about_never_compiled(studio):
    from rebuild_controller.providers.ladder import preset
    c = studio.connections.create("local", "Ollama (this PC)", endpoint="http://127.0.0.1:11434/v1", auth_mode="local",
                                  models=["qwen2.5:14b", "deepseek-coder-v2:16b", "qwen2.5-coder:7b"])
    # heuristic alone: the bigger coder model first
    before = preset(studio.connections, "all_local")
    assert before["tasks"]["implementation"]["entries"][0]["model"] == "deepseek-coder-v2:16b"
    assert before["tasks"]["implementation"]["recommendation"]["basis"] == "heuristic"
    assert any("deepseek-coder-v2:16b has never compiled anything here yet" in w for w in before["warnings"])
    for _ in range(3):
        scoreboard.record(studio.db, task="implementation", model="deepseek-coder-v2:16b", ok=True, compiled=False, locality="local")
    scoreboard.record(studio.db, task="repair", model="qwen2.5-coder:7b", ok=True, compiled=True, verified=True, locality="local")
    after = preset(studio.connections, "all_local")
    order = [e["model"] for e in after["tasks"]["implementation"]["entries"]]
    assert order == ["qwen2.5-coder:7b", "qwen2.5:14b", "deepseek-coder-v2:16b"]      # measured best, unmeasured, never compiled
    rec = after["tasks"]["implementation"]["recommendation"]
    assert rec["model"] == "qwen2.5-coder:7b" and rec["basis"] == "measured" and "1 compiled, 1 verified" in rec["note"]
    assert any("deepseek-coder-v2:16b was tried 3 time(s) for code here and never produced anything that compiles" in w for w in after["warnings"])
    assert after["tasks"]["implementation"]["measured"][0]["basis"] == "measured"
    # the API returns the same and use_detected (local AI) goes through the same preset
    cl = _client(studio)
    r = cl.post("/ai/ladder/preset", json={"preset": "all_local", "apply": False}).json()
    assert r["tasks"]["implementation"]["entries"][0]["model"] == "qwen2.5-coder:7b"
    assert c["connection_id"]


# =============================================================================================== real rizin round trip
def _rizin():
    from rebuild_controller.backends.rizin_worker import find_rizin
    from rebuild_controller.config import Settings
    return find_rizin(Settings()) is not None


BENCH = REPO / "fixtures" / "bench" / "c_msvc_x64_o2"


@pytest.mark.skipif(not _rizin() or not (BENCH / "bin" / "benchc.exe").is_file(), reason="needs rizin and the bench corpus")
def test_naming_round_trip_with_real_rizin_shows_labels_in_decompile_and_survives_reject(studio, tmp_path):
    from test_implement_loop import FakeOpenAI, Hook, Reply
    from rebuild_controller.jobs import JobState

    def answer(body):
        user = next(m["content"] for m in body["messages"] if m["role"] == "user")
        head = json.loads(user.split("\n", 1)[1].split("\n\n", 1)[0])
        return Reply(json.dumps({"name": "proposed_" + head["address"][2:], "comment": "Model guess.", "confidence": 0.3}), 200, 30)
    srv = FakeOpenAI([Hook(answer), Hook(answer)])
    try:
        conn = studio.connections.create("local", "Fake local", endpoint=srv.url, auth_mode="local", models=["coder-7b"], dialect="chat")
        studio.connections.set_route("naming", conn["connection_id"], "coder-7b")
        sess = studio.re.open(path=str(BENCH / "bin" / "benchc.exe"))
        cid, mid = sess["case_id"], sess["module_id"]
        studio.cases.db.update("cases", "case_id", cid, {"ai_policy": {"mode": "assisted"}})
        cl = _client(studio)
        r = cl.post(f"/cases/{cid}/modules/{mid}/ai/naming", json={"limit": 2})
        assert r.status_code == 200, r.text
        _drain(studio, 200)
        job = [j for j in studio.jobs.list(cid) if j.stage == "ai_naming"][-1]
        assert job.state == JobState.COMPLETED, job.error
        done = [x for x in job.result["results"] if x["status"] == "proposed"]
        assert len(done) == 2
        addr = done[0]["function"]
        b = studio.re.backend()
        dec = b.op_decompile(studio, case_id=cid, module_id=mid, function=addr)
        text = dec.data["decompiled"]["text"]
        assert f"proposed_{addr[2:]}" in text and "[model-proposed] function name" in text and "[model-proposed] Model guess." in text
        r = cl.post(f"/cases/{cid}/modules/{mid}/annotations/review", json={"kind": "function", "key": addr, "decision": "reject"})
        assert r.status_code == 200 and r.json()["analysis_reset"] is True
        dec2 = b.op_decompile(studio, case_id=cid, module_id=mid, function=addr).data["decompiled"]["text"]
        assert f"proposed_{addr[2:]}" not in dec2 and "[model-proposed] function name" not in dec2
    finally:
        srv.close()


# =============================================================================================== (a) slim repair, end to end (cargo)
@pytest.mark.e2e
def test_compile_error_repair_sends_only_the_failing_slice_and_applies_line_edits(studio, tmp_path):
    import test_implement_loop as T
    from rebuild_controller.jobs import JobState
    if not shutil.which("cargo") and not (Path.home() / ".cargo" / "bin").exists():
        pytest.skip("cargo missing")
    seen = {}

    def repair(body):
        user = next(m["content"] for m in body["messages"] if m["role"] == "user")
        seen["user"] = user
        n = len(T.BROKEN_MAIN.splitlines())
        return T.Reply(json.dumps({"edits": [{"path": "src/main.rs", "start_line": 1, "end_line": n, "text": T.GOOD_MAIN}]}), 900, 3000)
    srv = T.FakeOpenAI([T.Reply(T.files_json(T.BROKEN_MAIN), 1500, 300), T.Hook(repair)])
    try:
        T.connect(studio, srv)
        cid = T.make_case(studio, tmp_path, {})
        T.drain(studio)
    finally:
        srv.close()
    loop = T.job(studio, cid, "implement_loop")
    assert loop.state == JobState.COMPLETED and loop.result["verified"] is True and len(loop.result["attempts"]) == 2
    u = seen["user"]
    assert "FAILING CODE" in u and "1| fn main() {" in u and "E0308" in u and '"edits"' in u
    assert "PACKET (untrusted data)" not in u and "original_behaviour" not in u and "[package]" not in u   # no packet, no other files
    a1, a2 = T.attempts(studio, cid)
    assert a1["prompt"]["mode"] == "full" and a2["prompt"]["mode"] == "slices" and a2["prompt"]["slices"][0]["path"] == "src/main.rs"
    assert a2["prompt"]["chars"] < a1["prompt"]["chars"] / 3
    acts = [json.loads(r["payload"])["text"] for r in studio.db.query("SELECT payload FROM events WHERE kind='ai.activity' AND case_id=?", (cid,))]
    assert any("Repair prompt carries only the failing code" in a for a in acts)
    rows = scoreboard.rows(studio.db, model="gpt-fake")
    # no 'repair' ladder is set, so both attempts used the implementation ladder; compiled / verified come from cargo and the verifier
    assert sorted((r["task"], r["compiled"], r["verified"]) for r in rows) == [("implementation", 0, 0), ("implementation", 1, 1)]


def test_benchmark_model_rows_score_names_and_render():
    import sys
    sys.path.insert(0, str(REPO / "scripts"))
    import bench_models as BM
    assert BM.name_scores("crc32_update", "bench_crc32") == (False, True)
    assert BM.name_scores("Main", "main") == (True, True) and BM.name_scores("process_data", "cmd_hist") == (False, False)
    rows = [{"task": "naming", "model": "m1", "status": "ok", "ok": True, "truth_match": True, "exact": False, "latency_ms": 2000,
             "function": "0x1", "truth": "bench_crc32", "proposed": "crc32_update"},
            {"task": "naming", "model": "m1", "status": "ok", "ok": False, "truth_match": False, "exact": False, "latency_ms": 4000,
             "function": "0x2", "truth": "usage", "proposed": None, "problem": "unusable name"},
            {"task": "repair", "model": "m1", "status": "ok", "ok": True, "compiled": True, "verified": True, "passed": 9, "scenarios": 9,
             "start_passed": 8, "build": "built", "prompt_mode": "full", "prompt_chars": 30000, "latency_ms": 120000, "row": "dotnetapp"},
            {"task": "naming", "model": "m2", "status": "not run: m2 is not pulled on this Ollama"}]
    summ = BM.summarize(rows)
    assert [(s["model"], s["task"], s["runs"], s["ok"], s["match"]) for s in summ] == [("m1", "naming", 2, 1, 1), ("m1", "repair", 1, 1, 0)]
    md = "\n".join(BM.render({"rows": rows, "summary": summ, "generated_utc": "t", "endpoint": "e", "naming_row": "r", "naming_functions": 2}))
    assert "## Model scoreboard (R5)" in md and "| m1 | naming | 2 | 1 | 1 | 0 | - | - | 3.0 s |" in md
    assert "`bench_crc32` -> `crc32_update` (match)" in md and "verifier 9/9 (was 8/9)" in md and "m2 is not pulled" in md
    imp = scoreboard.import_rows  # rows are importable as they are
    assert callable(imp)
