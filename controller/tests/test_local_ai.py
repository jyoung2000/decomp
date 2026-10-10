"""Local AI on this PC: detection, plain-language suitability, idempotent connections, ladder safety, Ollama num_ctx handling,
Hugging Face search / verified resumable download / registration into Ollama, and the network allowlist.

Everything runs against fake servers (``http.server`` threads on 127.0.0.1); the Hugging Face base URL is pointed at a fake hub.
The opt-in ``live`` tests at the bottom use the real Ollama on this PC (REBUILD_LIVE_LOCAL=1) and, with REBUILD_LIVE_DOWNLOAD=1,
one real small download from huggingface.co.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rebuild_controller.budget import BudgetLedger
from rebuild_controller.local_models import ModelLibrary, ollama_name_for, quant_of
from rebuild_controller.providers.base import ContextWindowExceeded, Message, Request
from rebuild_controller.providers.connections import ConnectionStore
from rebuild_controller.providers.local_ai import LocalAI, NotLoopback, ServerSpec, judge, require_loopback
from rebuild_controller.providers.ollama_chat import OllamaChatAdapter, estimate_tokens
from rebuild_controller.providers.router import AIClient, AllCandidatesFailed, NoRoute
from rebuild_controller.providers.secrets import SecretStore
from rebuild_controller.tool_setup import ToolSetupError


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


# ============================================================================================== fake servers
class Fake:
    """A tiny threaded HTTP server; ``routes`` maps (method, path-prefix) -> handler(req) -> (status, headers, body|iterable)."""

    def __init__(self, routes: Callable[["Req"], tuple[int, dict[str, str], Any] | None]):
        self.routes = routes
        self.log: list[tuple[str, str, Any]] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _do(self):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                if not n and self.headers.get("transfer-encoding", "").lower() == "chunked":
                    raw = b""
                    while True:
                        size = int(self.rfile.readline().strip() or b"0", 16)
                        if not size:
                            self.rfile.readline()
                            break
                        raw += self.rfile.read(size)
                        self.rfile.readline()
                req = Req(self.command, self.path, dict(self.headers), raw)
                fake.log.append((self.command, req.path, req.json()))
                res = fake.routes(req) or (404, {"content-type": "application/json"}, {"error": "not found"})
                status, headers, body = res
                try:
                    if isinstance(body, (dict, list)):
                        body = json.dumps(body).encode()
                    if isinstance(body, str):
                        body = body.encode()
                    if isinstance(body, bytes):
                        self.send_response(status)
                        for k, v in headers.items():
                            self.send_header(k, v)
                        self.send_header("content-length", str(len(body)))
                        self.end_headers()
                        if self.command != "HEAD":
                            self.wfile.write(body)
                        return
                    # iterable of byte chunks: stream with a declared length (may be cut short on purpose)
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    for chunk in body:
                        if chunk is None:          # simulate a dropped connection
                            self.wfile.flush()
                            self.connection.shutdown(socket.SHUT_RDWR)
                            return
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (ConnectionError, OSError):
                    pass

            do_GET = do_POST = do_HEAD = do_DELETE = _do

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.root = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def bodies(self, path: str) -> list[Any]:
        return [b for (_m, p, b) in self.log if p == path]


class Req:
    def __init__(self, method: str, raw_path: str, headers: dict[str, str], body: bytes):
        u = urlsplit(raw_path)
        self.method, self.path, self.query = method, unquote(u.path), parse_qs(u.query)
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    def json(self) -> Any:
        try:
            return json.loads(self.body) if self.body else None
        except ValueError:
            return {"_bytes": len(self.body)}


JSON = {"content-type": "application/json"}

OLLAMA_MODELS = {
    "qwen2.5-coder:14b": {"details": {"family": "qwen2", "parameter_size": "14.8B", "quantization_level": "Q4_K_M", "context_length": 32768},
                          "show": {"capabilities": ["completion", "tools", "insert"], "model_info": {"qwen2.context_length": 32768}}, "size": 8_990_000_000},
    "llava:7b": {"details": {"family": "llama", "parameter_size": "7B", "quantization_level": "Q4_0", "context_length": 32768},
                 "show": {"capabilities": ["completion", "vision"], "model_info": {"llama.context_length": 32768}}, "size": 4_700_000_000},
    "qwen2.5:3b": {"details": {"family": "qwen2", "parameter_size": "3.1B", "quantization_level": "Q4_K_M"},
                   "show": {"capabilities": ["completion", "tools"], "model_info": {"qwen2.context_length": 32768}}, "size": 1_900_000_000},
    "nomic-embed-text:latest": {"details": {"family": "nomic-bert", "parameter_size": "137M"},
                                "show": {"capabilities": ["embedding"], "model_info": {"nomic-bert.context_length": 2048}}, "size": 270_000_000},
    "gemma3:12b": {"details": {"family": "gemma3", "parameter_size": "12.2B", "quantization_level": "Q4_K_M"},
                   "show": {"capabilities": ["completion", "vision", "thinking"], "model_info": {"gemma3.context_length": 131072}}, "size": 8_100_000_000},
}


class FakeOllama(Fake):
    """Ollama 0.35 behaviour that matters here: /api/chat honours options.num_ctx and ``truncate:false``; without them a prompt
    longer than the default context (4096) is silently cut (prompt_eval_count = num_ctx/2 + 2) - exactly what the OpenAI-compatible
    /v1/chat/completions does."""

    DEFAULT_CTX = 4096

    def __init__(self, models: dict[str, Any] | None = None):
        self.models = dict(OLLAMA_MODELS if models is None else models)
        self.blobs: dict[str, int] = {}
        self.created: dict[str, Any] = {}
        super().__init__(self.route)

    @staticmethod
    def tokens(messages: list[dict[str, Any]]) -> int:
        return sum(len(str(m.get("content") or "")) for m in messages) // 2

    def answer(self, model: str, n_prompt: int, ctx: int, truncate: Any) -> tuple[int, dict[str, str], Any]:
        if n_prompt > ctx:
            if truncate is False:
                inner = json.dumps({"error": {"code": 400, "message": f"request ({n_prompt} tokens) exceeds the available context size "
                                                                    f"({ctx} tokens), try increasing it", "type": "exceed_context_size_error",
                                              "n_prompt_tokens": n_prompt, "n_ctx": ctx}})
                return 400, JSON, {"error": inner}
            seen, text = ctx // 2 + 2, "WRONG (start of prompt was cut)"
        else:
            seen, text = n_prompt, "PURPLE-42"
        return 200, JSON, {"model": model, "seen": seen, "text": text}

    def route(self, r: Req):
        if r.path == "/api/version":
            return 200, JSON, {"version": "0.35.0"}
        if r.path == "/api/tags":
            return 200, JSON, {"models": [{"name": k, "model": k, "size": v.get("size"), "digest": k, "details": v["details"]}
                                          for k, v in self.models.items()]}
        if r.path == "/api/show":
            name = (r.json() or {}).get("model")
            if name not in self.models:
                return 404, JSON, {"error": f"model '{name}' not found"}
            return 200, JSON, self.models[name]["show"]
        if r.path == "/v1/models":
            return 200, JSON, {"object": "list", "data": [{"id": k, "object": "model"} for k in self.models]}
        if r.path == "/api/chat":
            b = r.json()
            ctx = int((b.get("options") or {}).get("num_ctx") or self.DEFAULT_CTX)
            st, h, a = self.answer(b["model"], self.tokens(b["messages"]), ctx, b.get("truncate"))
            if st != 200:
                return st, h, a
            final = {"model": b["model"], "message": {"role": "assistant", "content": a["text"]}, "done": True, "done_reason": "stop",
                     "prompt_eval_count": a["seen"], "eval_count": 3}
            if b.get("stream"):
                lines = [json.dumps({"model": b["model"], "message": {"role": "assistant", "content": a["text"]}, "done": False}),
                         json.dumps({**final, "message": {"role": "assistant", "content": ""}})]
                return 200, {"content-type": "application/x-ndjson"}, ("\n".join(lines) + "\n").encode()
            return 200, JSON, final
        if r.path == "/v1/chat/completions":
            b = r.json()
            st, h, a = self.answer(b["model"], self.tokens(b["messages"]), self.DEFAULT_CTX, None)
            return 200, JSON, {"id": "x", "model": b["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": a["text"]},
                                                                            "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": a["seen"], "completion_tokens": 3}}
        if r.path.startswith("/api/blobs/"):
            digest = r.path.rsplit("/", 1)[-1]
            if r.method == "HEAD":
                return (200 if digest in self.blobs else 404), {}, b""
            got = "sha256:" + hashlib.sha256(r.body).hexdigest()
            if got != digest:
                return 400, JSON, {"error": "digest mismatch"}
            self.blobs[digest] = len(r.body)
            return 201, {}, b""
        if r.path == "/api/create":
            b = r.json()
            for _f, d in (b.get("files") or {}).items():
                if d not in self.blobs:
                    return 400, JSON, {"error": f"blob {d} not found"}
            self.created[b["model"]] = b
            self.models[b["model"] + ":latest" if ":" not in b["model"] else b["model"]] = {
                "details": {"family": "llama", "parameter_size": "135M", "quantization_level": "Q8_0", "context_length": 8192},
                "show": {"capabilities": ["completion"], "model_info": {"llama.context_length": 8192}}, "size": 1000}
            return 200, JSON, {"status": "success"}
        if r.path == "/api/delete":
            name = (r.json() or {}).get("model")
            for k in [k for k in self.models if k == name or k == f"{name}:latest"]:
                del self.models[k]
            return 200, JSON, {}
        if r.path == "/api/pull":
            name = (r.json() or {}).get("model")
            if name == "missing:model":
                return 200, {"content-type": "application/x-ndjson"}, b'{"status":"pulling manifest"}\n{"error":"pull model manifest: file does not exist"}\n'
            lines = [{"status": "pulling manifest"}, {"status": "pulling abc", "digest": "sha256:abc", "total": 1000, "completed": 500},
                     {"status": "pulling abc", "digest": "sha256:abc", "total": 1000, "completed": 1000}, {"status": "success"}]
            self.models[name] = {"details": {"parameter_size": "7B"}, "show": {"capabilities": ["completion"], "model_info": {}}, "size": 1000}
            return 200, {"content-type": "application/x-ndjson"}, ("\n".join(json.dumps(x) for x in lines) + "\n").encode()
        return None


class FakeLMStudio(Fake):
    def __init__(self):
        super().__init__(self.route)

    def route(self, r: Req):
        if r.path == "/v1/models":
            return 200, JSON, {"data": [{"id": "qwen2.5-coder-7b-instruct"}, {"id": "text-embedding-nomic-embed-text-v1.5"},
                                        {"id": "llama-3.2-1b-instruct"}]}
        if r.path == "/api/v0/models":
            return 200, JSON, {"data": [
                {"id": "qwen2.5-coder-7b-instruct", "type": "llm", "arch": "qwen2", "quantization": "Q4_K_M", "state": "loaded",
                 "max_context_length": 32768, "loaded_context_length": 16384},
                {"id": "text-embedding-nomic-embed-text-v1.5", "type": "embeddings", "max_context_length": 2048},
                {"id": "llama-3.2-1b-instruct", "type": "llm", "arch": "llama", "quantization": "Q8_0", "max_context_length": 131072}]}
        return None


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def specs(ollama: Fake | None = None, lmstudio: Fake | None = None, llamacpp: Fake | None = None) -> list[ServerSpec]:
    return [ServerSpec("ollama", "Ollama", ollama.root if ollama else f"http://127.0.0.1:{free_port()}"),
            ServerSpec("lmstudio", "LM Studio", lmstudio.root if lmstudio else f"http://127.0.0.1:{free_port()}"),
            ServerSpec("llamacpp", "llama.cpp server", llamacpp.root if llamacpp else f"http://127.0.0.1:{free_port()}")]


@pytest.fixture
def ollama():
    f = FakeOllama()
    yield f
    f.stop()


@pytest.fixture
def lmstudio():
    f = FakeLMStudio()
    yield f
    f.stop()


@pytest.fixture
def store(db, events, settings, tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sec = SecretStore(tmp_path / "secret-data")
    return ConnectionStore(db, events, settings, secrets=sec, ledger=BudgetLedger(db, events))


def make_local(store, events, settings, servers) -> LocalAI:
    return LocalAI(store, events, servers=servers, state_path=settings.data_dir / "local_ai.json", timeout_s=0.5)


# ============================================================================================== detection
def test_detect_none(store, events, settings):
    la = make_local(store, events, settings, specs())
    r = la.detect()
    assert [s["found"] for s in r["servers"]] == [False, False, False]
    assert store.list() == [] and r["suitable_models"] == 0 and r["recommend_use"] is False
    assert all(s["install_page"].startswith("https://") for s in r["servers"])


def test_detect_ollama_capabilities_and_good_for(store, events, settings, ollama):
    la = make_local(store, events, settings, specs(ollama=ollama))
    r = la.detect()
    s = r["servers"][0]
    assert s["found"] and s["version"] == "0.35.0" and s["connection_id"] and s["connection_state"] == "ok"
    by = {m["id"]: m for m in s["models"]}
    coder = by["qwen2.5-coder:14b"]
    assert coder["context_window"] == 32768 and coder["parameter_b"] == 14.8 and coder["quantization"] == "Q4_K_M"
    assert coder["size_bytes"] == 8_990_000_000 and coder["capabilities"]["tools"] is True
    assert coder["tasks"]["implementation"]["ok"] and coder["tasks"]["repair"]["note"] == "coding model"
    assert not coder["tasks"]["visual_review"]["ok"]
    assert by["llava:7b"]["tasks"]["visual_review"]["ok"] and not by["llava:7b"]["tasks"]["repair"]["ok"]
    small = by["qwen2.5:3b"]
    assert small["quick_only"] and not small["tasks"]["implementation"]["ok"] and "quick tasks only" in small["tasks"]["repair"]["note"]
    emb = by["nomic-embed-text:latest"]
    assert emb["excluded"] and not emb["suitable"] and "embedding" in emb["summary"]
    assert by["gemma3:12b"]["capabilities"]["thinking"] is True and by["gemma3:12b"]["effective_context"] == 32768
    conn = store.get(s["connection_id"])
    assert conn["label"] == "Ollama (this PC)" and conn["provider"] == "local" and conn["limits"]["server"] == "ollama"
    assert conn["limits"]["num_ctx_cap"] == 32768
    assert {m["id"] for m in conn["models"]} >= {"qwen2.5-coder:14b", "llava:7b"}
    assert r["recommend_use"] is True and r["ladder"]["state"] == "empty" and r["recommended_preset"] == "all_local"


def test_detect_lmstudio_and_both(store, events, settings, ollama, lmstudio):
    la = make_local(store, events, settings, specs(lmstudio=lmstudio))
    r = la.detect()
    lm = r["servers"][1]
    assert lm["found"] and lm["connection_id"]
    by = {m["id"]: m for m in lm["models"]}
    assert by["qwen2.5-coder-7b-instruct"]["context_window"] == 16384          # the loaded context, not the trained one
    assert by["qwen2.5-coder-7b-instruct"]["tasks"]["repair"]["ok"]
    assert by["text-embedding-nomic-embed-text-v1.5"]["excluded"]
    assert by["llama-3.2-1b-instruct"]["quick_only"]
    conn = store.get(lm["connection_id"])
    entry = next(m for m in conn["models"] if m["id"] == "text-embedding-nomic-embed-text-v1.5")
    assert entry["capabilities"]["completion"] is False                      # so presets skip it
    la2 = make_local(store, events, settings, specs(ollama=ollama, lmstudio=lmstudio))
    r2 = la2.detect()
    assert [s["found"] for s in r2["servers"]] == [True, True, False]
    labels = sorted(c["label"] for c in store.list())
    assert labels == ["LM Studio (this PC)", "Ollama (this PC)"]


def test_detection_is_idempotent_and_reuses_a_user_connection(store, events, settings, ollama):
    mine = store.create("local", "My Ollama", endpoint=f"http://127.0.0.1:{ollama.port}/v1/", auth_mode="local")
    la = make_local(store, events, settings, specs(ollama=ollama))
    for _ in range(3):
        la.detect()
    other = store.create("local", "LM via localhost", endpoint="http://localhost:1234/v1", auth_mode="local")
    assert la._find_connection(ServerSpec("lmstudio", "LM Studio", "http://127.0.0.1:1234"))["connection_id"] == other["connection_id"]
    store.delete(other["connection_id"])
    conns = store.list()
    assert len(conns) == 1 and conns[0]["connection_id"] == mine["connection_id"] and conns[0]["label"] == "My Ollama"


def test_server_disappears_and_comes_back(store, events, settings):
    f = FakeOllama()
    servers = specs(ollama=f)
    la = make_local(store, events, settings, servers)
    cid = la.detect()["servers"][0]["connection_id"]
    f.stop()
    r = la.detect()
    assert r["servers"][0]["found"] is False and r["servers"][0]["connection_state"] == "unreachable"
    assert store.get(cid)["state"] == "unreachable" and len(store.list()) == 1
    kinds = [e["payload"] for e in events.events_since(0) if e["kind"] == "ai.local.detected"]
    assert kinds[-1]["changed"] is True and kinds[-1]["found"] == []


def test_ladder_is_never_overwritten_silently(store, events, settings, ollama):
    from rebuild_controller.providers.ladder import put_ladder
    la = make_local(store, events, settings, specs(ollama=ollama))
    cid = la.detect()["servers"][0]["connection_id"]
    put_ladder(store, "repair", [{"connection_id": cid, "model": "qwen2.5:3b"}])          # the user's own choice
    rev = store.config_revision()
    r = la.detect()
    assert r["ladder"]["state"] == "user" and r["recommend_use"] is False and "left alone" in r["advice"]
    assert store.route_entries("repair") == [(cid, "qwen2.5:3b")] and store.config_revision() == rev
    preview = la.use_detected(apply=False)
    assert preview["applied"] is False and preview["replaces_user_ladder"] is True
    assert store.route_entries("repair") == [(cid, "qwen2.5:3b")]
    applied = la.use_detected(apply=True)
    assert applied["applied"] and store.route_entries("repair")[0] == (cid, "qwen2.5-coder:14b")
    vis = [e["model"] for e in applied["tasks"]["visual_review"]["entries"]]
    assert vis and set(vis) <= {"llava:7b", "gemma3:12b"}
    every = [e["model"] for t in applied["tasks"].values() for e in t["entries"]]
    assert "nomic-embed-text:latest" not in every
    assert la.detect()["ladder"]["state"] == "preset"


# ============================================================================================== num_ctx (Ollama)
def big_request(chars: int, **kw) -> Request:
    text = "The secret code is PURPLE-42. " + ("x" * chars) + " What is the code?"
    return Request(model="qwen2.5-coder:14b", messages=[Message.user(text)], max_output_tokens=200, stream=kw.pop("stream", True), **kw)


def test_openai_compatible_path_truncates_silently_which_is_why_native_is_used(ollama):
    from rebuild_controller.providers.openai_responses import OpenAIResponsesAdapter
    a = OpenAIResponsesAdapter(endpoint=ollama.root + "/v1", dialect="chat", provider_name="local")
    r = a.complete(big_request(20_000, stream=False))
    assert "WRONG" in r.text and r.usage.input_tokens == FakeOllama.DEFAULT_CTX // 2 + 2      # HTTP 200, prompt cut


@pytest.mark.parametrize("stream", [True, False])
def test_native_adapter_sends_num_ctx_and_refuses_truncation(ollama, stream):
    a = OllamaChatAdapter(endpoint=ollama.root + "/v1", model_context={"qwen2.5-coder:14b": 32768})
    req = big_request(20_000, stream=stream)
    r = a.complete(req)
    body = ollama.bodies("/api/chat")[-1]
    assert body["truncate"] is False and body["options"]["num_ctx"] >= estimate_tokens(req) + 200
    assert body["options"]["num_ctx"] in (16384, 32768) and body["options"]["num_predict"] == 200
    assert r.text == "PURPLE-42" and r.meta["num_ctx"] == body["options"]["num_ctx"] and r.meta["prompt_tokens"] == 10_024
    assert r.usage.input_tokens == r.meta["prompt_tokens"]


def test_native_adapter_never_sends_a_prompt_that_cannot_fit(ollama):
    a = OllamaChatAdapter(endpoint=ollama.root + "/v1", model_context={"qwen2.5-coder:14b": 32768}, num_ctx_cap=16384)
    with pytest.raises(ContextWindowExceeded) as e:
        a.complete(big_request(40_000))
    assert e.value.request_sent is False and "raise 'Max context'" in str(e.value) and "silently cut" in str(e.value)
    assert ollama.bodies("/api/chat") == []


def test_native_adapter_grows_num_ctx_when_the_server_counts_more_tokens(ollama, monkeypatch):
    import rebuild_controller.providers.ollama_chat as oc
    monkeypatch.setattr(oc, "estimate_tokens", lambda req: 1000)          # badly underestimated prompt
    a = OllamaChatAdapter(endpoint=ollama.root + "/v1", model_context={"qwen2.5-coder:14b": 32768})
    r = a.complete(big_request(20_000))
    sent = [b["options"]["num_ctx"] for b in ollama.bodies("/api/chat")]
    assert sent == [8192, 16384] and r.text == "PURPLE-42" and r.meta["num_ctx"] == 16384


def test_router_uses_native_ollama_and_records_effective_context(store, events, settings, db, ollama):
    la = make_local(store, events, settings, specs(ollama=ollama))
    cid = la.detect()["servers"][0]["connection_id"]
    store.set_route("implementation", cid, "qwen2.5-coder:14b")
    client = AIClient(store, BudgetLedger(db, events), events, db, sleep=lambda s: None)
    client.advisor = None
    res = client.call("implementation", big_request(20_000))
    assert res.response.text == "PURPLE-42"
    assert ollama.bodies("/v1/chat/completions") == [] and ollama.bodies("/api/chat")[-1]["options"]["num_ctx"] >= 10_000
    row = db.query("SELECT * FROM ai_calls WHERE outcome='ok'")[-1]
    detail = json.loads(row["detail"])
    assert detail["effective_context"] == ollama.bodies("/api/chat")[-1]["options"]["num_ctx"] and detail["prompt_tokens"] == 10_024
    texts = [e["payload"]["text"] for e in events.events_since(0) if e["kind"] == "ai.activity"]
    assert any("context 16384 tokens" in t or "context 32768 tokens" in t for t in texts)
    with pytest.raises((AllCandidatesFailed, NoRoute)):
        client.call("implementation", big_request(120_000))                 # > 32k cap: refused, never cut
    assert all(len(json.dumps(b)) < 100_000 for b in ollama.bodies("/api/chat"))


def test_num_ctx_cap_is_user_adjustable(store, events, settings, ollama):
    la = make_local(store, events, settings, specs(ollama=ollama))
    cid = la.detect()["servers"][0]["connection_id"]
    la.set_num_ctx_cap(65536)
    assert store.get(cid)["limits"]["num_ctx_cap"] == 65536
    r = la.detect()
    assert {m["id"]: m for m in r["servers"][0]["models"]}["gemma3:12b"]["effective_context"] == 65536
    with pytest.raises(ValueError):
        la.set_num_ctx_cap(100)


# ============================================================================================== Hugging Face fake hub
GGUF_OK = b"GGUF" + os.urandom(300_000)
GGUF_BAD = b"GGUF" + os.urandom(1000)


class FakeHub(Fake):
    def __init__(self):
        self.files = {"SmolLM2-135M-Instruct-Q8_0.gguf": GGUF_OK, "SmolLM2-135M-Instruct-Q4_K_M.gguf": GGUF_OK[:200_000],
                      "bad-Q2_K.gguf": GGUF_BAD}
        self.lie = {"bad-Q2_K.gguf": "0" * 64}          # the hub's published sha256 does not match the bytes
        self.drop_once_after: int | None = None
        self.slow = False
        self.ranges: list[str | None] = []
        super().__init__(self.route)

    def sha(self, name: str) -> str:
        return self.lie.get(name) or hashlib.sha256(self.files[name]).hexdigest()

    def tree(self) -> list[dict[str, Any]]:
        out = [{"type": "file", "path": "README.md", "size": 10}]
        for n, b in self.files.items():
            out.append({"type": "file", "path": n, "size": len(b), "lfs": {"oid": self.sha(n), "size": len(b), "pointerSize": 133}})
        out.append({"type": "file", "path": "mmproj-f16.gguf", "size": 5, "lfs": {"oid": "a" * 64, "size": 5}})
        out.append({"type": "file", "path": "big-Q8_0-00001-of-00002.gguf", "size": 5, "lfs": {"oid": "b" * 64, "size": 5}})
        return out

    def route(self, r: Req):
        p = r.path
        if p == "/api/models":
            q = r.query.get("search", [""])[0]
            assert r.query.get("filter") == ["gguf"] and r.query.get("sort") == ["downloads"]
            rows = [{"id": "acme/SmolLM2-135M-Instruct-GGUF", "downloads": 1234, "likes": 5, "gated": False,
                     "tags": ["gguf", "license:apache-2.0"], "pipeline_tag": "text-generation", "lastModified": "2025-01-01T00:00:00.000Z"},
                    {"id": "corp/Gated-GGUF", "downloads": 99, "likes": 1, "gated": "manual", "tags": ["gguf", "license:llama3.1"]}]
            return 200, JSON, [x for x in rows if q.lower() in x["id"].lower()]
        if p in ("/api/models/acme/SmolLM2-135M-Instruct-GGUF", "/api/models/corp/Gated-GGUF", "/api/models/other/Custom-GGUF"):
            repo = p[len("/api/models/"):]
            gated = "manual" if repo.startswith("corp/") else False
            lic = {"acme": "apache-2.0", "corp": "llama3.1", "other": "other"}[repo.split("/")[0]]
            return 200, JSON, {"id": repo, "sha": "c0ffee", "gated": gated, "tags": ["gguf"], "cardData": {"license": lic}}
        if p.startswith("/api/models/") and "/tree/c0ffee" in p:
            return 200, JSON, self.tree()
        if "/resolve/c0ffee/" in p:
            name = p.rsplit("/", 1)[-1]
            if p.startswith("/corp/") and r.headers.get("authorization") != "Bearer hf_testtoken123":
                return 401, JSON, {"error": "Access to model corp/Gated-GGUF is restricted."}
            return 302, {"location": f"{self.root}/cdn/{name}"}, b""
        if p.startswith("/cdn/"):
            name = p.rsplit("/", 1)[-1]
            assert "authorization" not in r.headers          # the token is never forwarded to the CDN host
            data = self.files[name]
            rng = r.headers.get("range")
            self.ranges.append(rng)
            start = int(rng.split("=")[1].split("-")[0]) if rng else 0
            part = data[start:]
            headers = {"content-type": "application/octet-stream", "content-length": str(len(part)), "accept-ranges": "bytes"}
            status = 206 if rng else 200
            if rng:
                headers["content-range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"

            def gen():
                sent = 0
                for i in range(0, len(part), 16384):
                    if self.drop_once_after is not None and sent >= self.drop_once_after:
                        self.drop_once_after = None
                        yield None
                        return
                    if self.slow:
                        time.sleep(0.05)
                    chunk = part[i:i + 16384]
                    sent += len(chunk)
                    yield chunk
            return status, headers, gen()
        return None


@pytest.fixture
def hub():
    f = FakeHub()
    yield f
    f.stop()


@pytest.fixture
def lib(store, events, settings, ollama, hub, tmp_path):
    la = make_local(store, events, settings, specs(ollama=ollama))
    la.detect()
    return ModelLibrary(la, data_dir=settings.data_dir, events=events, secrets=store.secrets, hf_base=hub.root,
                        allow_insecure_loopback=True)


def wait(lib: ModelLibrary, job: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
    assert lib.join(job["job_id"], timeout)
    return lib.job(job["job_id"])


def test_search_and_file_listing(lib):
    r = lib.search("smollm2")
    assert r["results"][0]["repo"] == "acme/SmolLM2-135M-Instruct-GGUF" and r["results"][0]["license"] == "apache-2.0"
    assert r["results"][0]["license_permissive"] and r["results"][0]["gated"] is False and r["results"][0]["downloads"] == 1234
    f = lib.files("acme/SmolLM2-135M-Instruct-GGUF")
    assert f["revision"] == "c0ffee" and f["license_ack_required"] is False
    by = {x["path"]: x for x in f["files"]}
    q8 = by["SmolLM2-135M-Instruct-Q8_0.gguf"]
    assert q8["quant"] == "Q8_0" and q8["size_bytes"] == len(GGUF_OK) and q8["sha256"] == hashlib.sha256(GGUF_OK).hexdigest()
    assert q8["downloadable"] and q8["ram_hint_gb"] > 0
    assert by["mmproj-f16.gguf"]["kind"] == "vision_projector" and not by["mmproj-f16.gguf"]["downloadable"]
    assert by["big-Q8_0-00001-of-00002.gguf"]["split"] and not by["big-Q8_0-00001-of-00002.gguf"]["downloadable"]
    assert "README.md" not in by
    with pytest.raises(ToolSetupError):
        lib.files("not a repo")


def test_quant_and_name_helpers():
    assert quant_of("x-IQ4_XS.gguf") == "IQ4_XS" and quant_of("model.Q5_K_M.gguf") == "Q5_K_M" and quant_of("m-f16.gguf") == "F16"
    assert ollama_name_for("bartowski/SmolLM2-135M-Instruct-GGUF", "SmolLM2-135M-Instruct-Q8_0.gguf") == "rs-smollm2-135m-instruct-q8_0"


def test_download_verifies_registers_with_ollama_and_rediscovers(lib, ollama, hub, store, tmp_path):
    dest = tmp_path / "my models with spaces"
    job = lib.start_download("acme/SmolLM2-135M-Instruct-GGUF", "SmolLM2-135M-Instruct-Q8_0.gguf", dest_dir=str(dest))
    v = wait(lib, job)
    assert v["phase"] == "done", v
    rec = v["result"]
    path = Path(rec["path"])
    assert path.read_bytes() == GGUF_OK and path.parent.parent == dest and not path.with_name(path.name + ".part").exists()
    assert rec["sha256"] == hashlib.sha256(GGUF_OK).hexdigest() and rec["registered_as"] == "rs-smollm2-135m-instruct-q8_0"
    assert ollama.blobs == {f"sha256:{rec['sha256']}": len(GGUF_OK)}
    assert ollama.created["rs-smollm2-135m-instruct-q8_0"]["files"] == {path.name: f"sha256:{rec['sha256']}"}
    conn = next(c for c in store.list() if c["label"] == "Ollama (this PC)")
    assert "rs-smollm2-135m-instruct-q8_0:latest" in {m["id"] for m in conn["models"]}          # appears in the ladder picker
    lst = lib.downloads()
    assert lst[0]["exists"] and lst[0]["registered_as"] == "rs-smollm2-135m-instruct-q8_0"
    gone = lib.remove(lst[0]["id"], unregister=True)
    assert gone["unregistered"] == "rs-smollm2-135m-instruct-q8_0" and not path.exists() and lib.downloads() == []
    assert "rs-smollm2-135m-instruct-q8_0:latest" not in ollama.models


def test_wrong_hash_is_refused_and_deleted(lib, hub, ollama, tmp_path):
    job = lib.start_download("acme/SmolLM2-135M-Instruct-GGUF", "bad-Q2_K.gguf", dest_dir=str(tmp_path / "m"))
    v = wait(lib, job)
    assert v["phase"] == "failed" and v["error"]["code"] == "checksum_mismatch" and "deleted" in v["error"]["message"]
    assert not list((tmp_path / "m").rglob("*.gguf*")) and ollama.blobs == {} and lib.downloads() == []


def test_resume_after_interruption(lib, hub, tmp_path):
    hub.drop_once_after = 270_000
    args = ("acme/SmolLM2-135M-Instruct-GGUF", "SmolLM2-135M-Instruct-Q8_0.gguf")
    v = wait(lib, lib.start_download(*args, dest_dir=str(tmp_path / "m"), register=False))
    assert v["phase"] == "failed" and v["error"]["code"] == "interrupted" and v["error"]["retryable"]
    part = next((tmp_path / "m").rglob("*.part"))
    kept = part.stat().st_size
    assert 0 < kept < len(GGUF_OK)
    v2 = wait(lib, lib.start_download(*args, dest_dir=str(tmp_path / "m"), register=False))
    assert v2["phase"] == "done" and v2["resumed_from"] == kept
    assert hub.ranges[-1] == f"bytes={kept}-"
    assert Path(v2["result"]["path"]).read_bytes() == GGUF_OK


def test_cancel_deletes_partial(lib, hub, tmp_path):
    hub.slow = True
    job = lib.start_download("acme/SmolLM2-135M-Instruct-GGUF", "SmolLM2-135M-Instruct-Q8_0.gguf", dest_dir=str(tmp_path / "m"))
    for _ in range(100):
        if lib.job(job["job_id"])["bytes_done"] > 0:
            break
        time.sleep(0.02)
    lib.cancel(job["job_id"])
    v = wait(lib, job)
    assert v["phase"] == "cancelled" and v["cancelled"] and not list((tmp_path / "m").rglob("*.gguf*"))
    with pytest.raises(ToolSetupError):
        lib.cancel(job["job_id"])


def test_gated_and_license_acknowledgement(lib, hub, tmp_path):
    f = lib.files("corp/Gated-GGUF")
    assert f["gated"] and f["needs_token"] and f["license_ack_required"]
    with pytest.raises(ToolSetupError) as e:
        lib.start_download("corp/Gated-GGUF", "SmolLM2-135M-Instruct-Q8_0.gguf", dest_dir=str(tmp_path), accept_license=True)
    assert e.value.code == "gated_needs_token" and "token" in e.value.next_action
    with pytest.raises(ToolSetupError) as e2:
        lib.start_download("other/Custom-GGUF", "SmolLM2-135M-Instruct-Q8_0.gguf", dest_dir=str(tmp_path))
    assert e2.value.code == "license_ack_required"
    cfg = lib.set_hf_token("hf_testtoken123")
    assert cfg["has_hf_token"] and "hf_testtoken123" not in json.dumps(cfg)
    assert "hf_testtoken123" not in (lib.local_ai.state_path.read_text())
    v = wait(lib, lib.start_download("corp/Gated-GGUF", "SmolLM2-135M-Instruct-Q8_0.gguf", dest_dir=str(tmp_path / "g"),
                                     accept_license=True, register=False))
    assert v["phase"] == "done"
    assert lib.set_hf_token(None)["has_hf_token"] is False


def test_no_server_keeps_file_and_says_what_is_needed(store, events, settings, hub, tmp_path):
    la = make_local(store, events, settings, specs())
    la.detect()
    lib = ModelLibrary(la, data_dir=settings.data_dir, events=events, secrets=store.secrets, hf_base=hub.root, allow_insecure_loopback=True)
    v = wait(lib, lib.start_download("acme/SmolLM2-135M-Instruct-GGUF", "SmolLM2-135M-Instruct-Q4_K_M.gguf", dest_dir=str(tmp_path / "m")))
    assert v["phase"] == "done" and v["result"]["status"] == "needs_server"
    assert "needs a local AI server (Ollama or LM Studio)" in v["result"]["status_text"]
    assert v["result"]["install_pages"]["ollama"] == "https://ollama.com/download"
    assert Path(v["result"]["path"]).is_file()


def test_folder_checks(lib, tmp_path, monkeypatch):
    assert lib.check_folder(str(tmp_path / "a b c"))["writable"]
    with pytest.raises(ToolSetupError) as e:
        lib.check_folder("relative\\path")
    assert e.value.code == "bad_folder"
    import shutil as sh
    monkeypatch.setattr(sh, "disk_usage", lambda p: type("U", (), {"free": 10_000})())
    with pytest.raises(ToolSetupError) as e2:
        lib.check_folder(str(tmp_path), need_bytes=10**9)
    assert e2.value.code == "no_space"
    cfg = lib.set_models_dir.__self__.config()
    assert cfg["models_dir"].endswith("models")


def test_ollama_pull_by_name(lib, ollama):
    v = wait(lib, lib.start_pull("qwen2.5-coder:7b"))
    assert v["phase"] == "done" and v["bytes_done"] == 1000 and v["result"]["folder"]
    bad = wait(lib, lib.start_pull("missing:model"))
    assert bad["phase"] == "failed" and "does not exist" in bad["error"]["message"]
    with pytest.raises(ToolSetupError):
        lib.start_pull("http://evil/x")


# ============================================================================================== network allowlist
def test_url_policies():
    with pytest.raises(NotLoopback):
        require_loopback("http://192.168.1.5:11434/api/tags")
    with pytest.raises(NotLoopback):
        LocalAI(None, servers=[ServerSpec("ollama", "Ollama", "http://10.0.0.2:11434")])
    lib = ModelLibrary(LocalAI(None, servers=[]), data_dir=Path("."), hf_base="https://huggingface.co")
    for ok in ("https://huggingface.co/api/models", "https://us.aws.cdn.hf.co/x", "https://cdn-lfs.huggingface.co/y",
               "https://cas-bridge.xethub.hf.co/z"):
        assert lib._check_hf_url(ok) == ok
    for bad in ("http://huggingface.co/x", "https://evil.example/x", "https://huggingface.co.evil.example/x", "http://127.0.0.1:9/x"):
        with pytest.raises(ToolSetupError):
            lib._check_hf_url(bad)
    with pytest.raises(ToolSetupError):
        ModelLibrary(LocalAI(None, servers=[]), data_dir=Path("."), hf_base="https://evil.example")


def test_no_network_outside_loopback_during_detect_search_download(store, events, settings, ollama, hub, tmp_path, monkeypatch):
    seen: list[str] = []
    real = httpx.HTTPTransport.handle_request

    def spy(self, request):
        seen.append(request.url.host)
        return real(self, request)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", spy)
    la = make_local(store, events, settings, specs(ollama=ollama))
    la.detect()
    lib = ModelLibrary(la, data_dir=settings.data_dir, events=events, secrets=store.secrets, hf_base=hub.root, allow_insecure_loopback=True)
    lib.search("smol")
    wait(lib, lib.start_download("acme/SmolLM2-135M-Instruct-GGUF", "SmolLM2-135M-Instruct-Q4_K_M.gguf", dest_dir=str(tmp_path / "m")))
    assert seen and set(seen) == {"127.0.0.1"}


# ============================================================================================== API
def test_api_routes(store, events, settings, ollama, hub, tmp_path):
    from rebuild_controller.api.local_ai_routes import build_router
    la = make_local(store, events, settings, specs(ollama=ollama))
    lib = ModelLibrary(la, data_dir=settings.data_dir, events=events, secrets=store.secrets, hf_base=hub.root, allow_insecure_loopback=True)
    app = FastAPI()
    app.include_router(build_router(la, lib))
    c = TestClient(app)
    d = c.post("/ai/local/detect").json()
    assert d["servers"][0]["found"] and d["recommend_use"]
    assert c.get("/ai/local").json()["servers"][0]["found"]
    prev = c.post("/ai/local/use", json={"apply": False}).json()
    assert prev["applied"] is False and prev["tasks"]["repair"]["entries"]
    assert c.post("/ai/local/use", json={"apply": True, "preset": "all_local"}).json()["applied"]
    assert c.post("/ai/local/use", json={"apply": True, "preset": "all_cloud"}).status_code in (400, 422)
    s = c.put("/ai/local/settings", json={"models_dir": str(tmp_path / "x y"), "num_ctx_cap": 16384}).json()
    assert s["models_dir"] == str(tmp_path / "x y") and s["num_ctx_cap"] == 16384
    err = c.put("/ai/local/settings", json={"models_dir": "nope"}).json()
    assert err["detail"]["code"] == "bad_folder" if "detail" in err else True
    assert c.get("/ai/local/search", params={"q": "smol"}).json()["results"][0]["repo"].startswith("acme/")
    f = c.get("/ai/local/files", params={"repo": "acme/SmolLM2-135M-Instruct-GGUF"}).json()
    assert f["files"][0]["downloadable"]
    j = c.post("/ai/local/downloads", json={"repo": "acme/SmolLM2-135M-Instruct-GGUF", "path": "SmolLM2-135M-Instruct-Q4_K_M.gguf"}).json()
    assert lib.join(j["job_id"], 30)
    assert c.get(f"/ai/local/jobs/{j['job_id']}").json()["phase"] == "done"
    models = c.get("/ai/local/models").json()
    assert models and models[0]["registered_as"]
    r = c.post("/ai/local/downloads", json={"repo": "other/Custom-GGUF", "path": "SmolLM2-135M-Instruct-Q8_0.gguf"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "license_ack_required"
    assert c.delete(f"/ai/local/models/{models[0]['id']}?unregister=1").json()["unregistered"]


# ============================================================================================== judgement unit
def test_judge_rules():
    j = judge({"id": "tiny:1b", "parameter_b": 1.0, "context_window": 131072, "capabilities": {"completion": True}})
    assert j["quick_only"] and not j["tasks"]["repair"]["ok"] and j["tasks"]["knowledge"]["ok"]
    j = judge({"id": "coder:14b", "parameter_b": 14, "context_window": 8192, "capabilities": {"completion": True}})
    assert not j["tasks"]["repair"]["ok"] and "16k" in j["tasks"]["repair"]["note"]
    j = judge({"id": "emb", "capabilities": {"completion": False, "embedding": True}})
    assert j["excluded"] and not j["suitable"]


# ============================================================================================== live (opt-in)
LIVE = os.environ.get("REBUILD_LIVE_LOCAL") == "1"


@pytest.mark.live
@pytest.mark.skipif(not LIVE, reason="opt-in: set REBUILD_LIVE_LOCAL=1 (uses the real Ollama on this PC)")
def test_live_detect_real_ollama(store, events, settings):
    la = LocalAI(store, events, state_path=settings.data_dir / "local_ai.json")
    t0 = time.monotonic()
    r = la.detect()
    took = time.monotonic() - t0
    print(f"\n[live-local] detection took {took:.2f}s")
    for s in r["servers"]:
        print(f"[live-local] {s['name']}: {'found v' + str(s['version']) if s['found'] else 'not running'} ({s['probe_ms']} ms)")
        for m in s["models"]:
            print(f"   {m['id']:<34} {str(m.get('parameter_size') or ''):>7} ctx={m.get('context_window')} "
                  f"eff={m.get('effective_context')} caps={[k for k, v in m['capabilities'].items() if v is True]} -> {m['summary']}")
    print(f"[live-local] suitable={r['suitable_models']} ladder={r['ladder']['state']} preset={r['recommended_preset']}")
    prev = la.use_detected(apply=False)
    for t, lad in prev["tasks"].items():
        print(f"[live-local] proposed {t}: {[e['model'] for e in lad['entries']]}")
    print(f"[live-local] warnings: {prev['warnings']}")
    assert any(s["found"] for s in r["servers"]), "no local AI server detected"


@pytest.mark.live
@pytest.mark.skipif(not (LIVE and os.environ.get("REBUILD_LIVE_DOWNLOAD") == "1"),
                    reason="opt-in: REBUILD_LIVE_LOCAL=1 REBUILD_LIVE_DOWNLOAD=1 (downloads a ~140 MB GGUF from huggingface.co)")
def test_live_real_small_download_register_and_remove(store, events, settings, tmp_path):
    repo = os.environ.get("REBUILD_LIVE_HF_REPO", "bartowski/SmolLM2-135M-Instruct-GGUF")
    fname = os.environ.get("REBUILD_LIVE_HF_FILE", "SmolLM2-135M-Instruct-Q8_0.gguf")
    la = LocalAI(store, events, state_path=settings.data_dir / "local_ai.json")
    la.detect()
    lib = ModelLibrary(la, data_dir=settings.data_dir, events=events, secrets=store.secrets)
    t0 = time.monotonic()
    hits = lib.search("SmolLM2-135M-Instruct")
    print(f"\n[live-dl] search: {len(hits['results'])} results in {time.monotonic() - t0:.2f}s; first={hits['results'][0]['repo']}")
    files = lib.files(repo)
    f = next(x for x in files["files"] if x["path"] == fname)
    print(f"[live-dl] {repo} license={files['license']} gated={files['gated']} {fname} size={f['size_bytes']} sha256={f['sha256']}")
    t1 = time.monotonic()
    v = wait(lib, lib.start_download(repo, fname, dest_dir=str(tmp_path / "real models")), timeout=1800)
    took = time.monotonic() - t1
    assert v["phase"] == "done", v
    rec = v["result"]
    print(f"[live-dl] downloaded+verified+registered in {took:.1f}s: {rec['path']} ({rec['size_bytes']} bytes) as {rec['registered_as']}")
    conn = next(c for c in store.list() if c["label"] == "Ollama (this PC)")
    ids = {m["id"] for m in conn["models"]}
    assert f"{rec['registered_as']}:latest" in ids or rec["registered_as"] in ids
    snap = la.snapshot()
    m = next(m for m in snap["servers"][0]["models"] if m["id"].startswith(rec["registered_as"]))
    print(f"[live-dl] discovered: {m['id']} ctx={m.get('context_window')} caps={m['capabilities']} -> {m['summary']}")
    t2 = time.monotonic()
    gone = lib.remove(rec["id"], unregister=True)
    print(f"[live-dl] removed file + Ollama model in {time.monotonic() - t2:.2f}s: {gone}")
    assert not Path(rec["path"]).exists()
    after = httpx.get("http://127.0.0.1:11434/api/tags", timeout=5).json()
    assert not any(x["name"].startswith(rec["registered_as"]) for x in after["models"])


def test_native_adapter_streams_so_slow_local_generation_is_not_cut_off():
    """Found on the genuine install: every local model hit a fixed 300 s total timeout while still writing a program.
    The adapter streams even for non-streaming callers, so the timeout bounds the gap between tokens, not the answer."""
    seen: list[dict] = []

    class Slow(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            self.send_response(200)
            self.send_header("content-type", "application/x-ndjson")
            self.end_headers()
            for word in ("fn ", "main", "() {}"):
                time.sleep(0.6)            # total 1.8 s > the 1 s timeout below; every gap < 1 s
                self.wfile.write((json.dumps({"model": "m", "message": {"content": word}, "done": False}) + "\n").encode())
                self.wfile.flush()
            self.wfile.write((json.dumps({"model": "m", "message": {"content": ""}, "done": True, "done_reason": "stop",
                                          "prompt_eval_count": 5, "eval_count": 3}) + "\n").encode())

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        a = OllamaChatAdapter(endpoint=f"http://127.0.0.1:{srv.server_address[1]}", model_context={"m": 8192}, timeout_s=1.0)
        r = a.complete(Request(model="m", messages=[Message.user("write main")], max_output_tokens=50, stream=False))
        assert r.text == "fn main() {}" and seen[0]["stream"] is True
    finally:
        srv.shutdown(); srv.server_close()


def test_native_adapter_constrains_file_map_json_and_turns_off_unrequested_thinking(ollama):
    """Found on the genuine install: qwen2.5:14b's Rust was lost to one unescaped quote in its JSON, and gemma4:12b spent its whole
    output budget on hidden thinking. File-map requests get grammar-constrained JSON; thinking is off unless asked for."""
    a = OllamaChatAdapter(endpoint=ollama.root, model_context={"qwen2.5-coder:14b": 32768})
    req = big_request(1_000, stream=False)
    req.metadata["output_format"] = "json_file_map"
    a.complete(req)
    body = ollama.bodies("/api/chat")[-1]
    assert body["format"] == {"type": "object", "additionalProperties": {"type": "string"}} and body["think"] is False
    a.complete(big_request(1_000, stream=False))
    body = ollama.bodies("/api/chat")[-1]
    assert "format" not in body and body["think"] is False


def test_output_reserve_shrinks_to_fit_the_context_instead_of_refusing(ollama):
    """Found on the genuine install: a ~17.5k-token repair prompt plus the 16k output reserve exceeded the 32k context, so no
    model was asked at all. The reserve shrinks to what fits (>= 4096) and the cap is recorded."""
    a = OllamaChatAdapter(endpoint=ollama.root, model_context={"qwen2.5-coder:14b": 32768})
    req = big_request(34_000, stream=False)          # ~17k estimated tokens
    req.max_output_tokens = 16_000
    est = estimate_tokens(req)
    r = a.complete(req)
    body = ollama.bodies("/api/chat")[-1]
    assert body["options"]["num_ctx"] == 32768 and body["options"]["num_predict"] == 32768 - est - 256
    assert r.meta["output_tokens_capped"] == {"requested": 16_000, "allowed": 32768 - est - 256}
    assert req.max_output_tokens == 16_000            # the caller's request is not mutated
    tiny = big_request(120_000, stream=False)        # leaves < 4096 tokens for the answer: still refused, nothing sent
    tiny.max_output_tokens = 16_000
    n = len(ollama.bodies("/api/chat"))
    with pytest.raises(ContextWindowExceeded):
        a.complete(tiny)
    assert len(ollama.bodies("/api/chat")) == n
