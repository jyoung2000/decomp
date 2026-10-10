"""AI model ladder (docs/AI_LADDER.md): failure taxonomy, ladder API, project policy, plan visibility, activity feed.

No network: mock adapters, httpx.MockTransport, and (for the scripted failover) a fake OpenAI-compatible server on 127.0.0.1.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from rebuild_controller.budget import BudgetLedger
from rebuild_controller.providers import MockProvider, Request
from rebuild_controller.providers.base import (CapabilityError, ContextWindowExceeded, CreditsExhausted, ImagePart, Message, ModelUnavailable,
                                               ProviderUnavailable, TextPart, UsageLimit, classify_error)
from rebuild_controller.providers.connections import ConnectionStore, locality_of
from rebuild_controller.providers.ladder import emit_activity, policy_hash
from rebuild_controller.providers.mock import AICallAttempted, RaisingProvider
from rebuild_controller.providers.openai_responses import OpenAIResponsesAdapter
from rebuild_controller.providers.pricing import ApprovalRequired
from rebuild_controller.providers.router import AIClient, AIDisabled, AllCandidatesFailed, NoRoute
from rebuild_controller.providers.secrets import SecretStore

from test_providers import Recorder, json_response

KEY = "sk-NOT-REAL-ladder-key-0123456789abcdef"
PRICED = {"input_per_mtok": 1.0, "output_per_mtok": 2.0}
FREE = {"input_per_mtok": 0.0, "output_per_mtok": 0.0}


@pytest.fixture
def secret_store(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return SecretStore(tmp_path / "secret-data")


@pytest.fixture
def ledger(db, events):
    return BudgetLedger(db, events)


@pytest.fixture
def store(db, events, settings, secret_store, ledger):
    return ConnectionStore(db, events, settings, secrets=secret_store, ledger=ledger)


@pytest.fixture
def client(store, ledger, events, db):
    c = AIClient(store, ledger, events, db, sleep=lambda s: None)
    return c


def req(text="hello", **kw):
    kw.setdefault("max_output_tokens", 100)
    return Request(model="", messages=[Message.user(text)], **kw)


def cloud(store, label, model, price=PRICED, script=None, provider="openai"):
    c = store.create(provider, label, api_key=KEY, endpoint="https://api.example.invalid/v1", models=[{"id": model, "price": price}])
    m = MockProvider(script)
    store.set_adapter_override(c["connection_id"], m)
    return c, m


def local(store, label, model, script=None, **model_extra):
    c = store.create("local", label, endpoint="http://127.0.0.1:11434/v1", auth_mode="local", models=[{"id": model}])
    if model_extra:
        ms = c["models"]
        ms[0].update(model_extra)
        store.db.update("connections", "connection_id", c["connection_id"], {"models": ms})
    m = MockProvider(script)
    store.set_adapter_override(c["connection_id"], m)
    return store.get(c["connection_id"]), m


def ladder(store, task, *pairs):
    (c0, m0), rest = pairs[0], pairs[1:]
    return store.set_route(task, c0["connection_id"], m0, [{"connection": c["connection_id"], "model": m} for c, m in rest])


def activity_texts(events, case_id=None):
    return [e["payload"]["text"] for e in events.events_since(0) if e["kind"] == "ai.activity"
            and (case_id is None or e["case_id"] == case_id)]


# =============================================================================================== 1. taxonomy: real response shapes
@pytest.mark.parametrize("status,body,cls", [
    (402, {"error": {"message": "Insufficient credits. Add more using https://openrouter.ai/credits", "code": 402}}, CreditsExhausted),
    (402, {"error": {"message": "This request requires more credits, or fewer max_tokens.", "code": 402}}, CreditsExhausted),
    (429, {"error": {"message": "You exceeded your current quota, please check your plan and billing details.", "type": "insufficient_quota",
                     "code": "insufficient_quota"}}, CreditsExhausted),
    (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low to access the Anthropic API."}},
     CreditsExhausted),
    (404, {"error": {"message": "model \"qwen9:1b\" not found, try pulling it first", "type": "api_error", "param": None, "code": None}},
     ModelUnavailable),
    (404, {"error": {"message": "The model `gpt-9` does not exist or you do not have access to it.", "type": "invalid_request_error",
                     "code": "model_not_found"}}, ModelUnavailable),
    (404, {"type": "error", "error": {"type": "not_found_error", "message": "model: claude-3-opus-20240229"}}, ModelUnavailable),
    (404, {"error": {"code": 404, "message": "models/gemini-1.0-pro is not found for API version v1beta, or is not supported for "
                                              "generateContent.", "status": "NOT_FOUND"}}, ModelUnavailable),
    (400, {"error": {"message": "The model `text-davinci-003` has been deprecated", "code": "model_deprecated"}}, ModelUnavailable),
    (400, {"error": {"message": "This model's maximum context length is 8192 tokens. However, your messages resulted in 9000 tokens.",
                     "code": "context_length_exceeded"}}, ContextWindowExceeded),
])
def test_failure_taxonomy_from_real_response_shapes(status, body, cls):
    e = classify_error("x", status, json.dumps(body))
    assert type(e) is cls and e.retry_safe


def test_taxonomy_keeps_existing_classes_compatible():
    assert isinstance(classify_error("x", 402, '{"error":{"message":"Payment required"}}'), UsageLimit)       # CreditsExhausted is a UsageLimit
    assert type(classify_error("x", 404, "not found")).__name__ == "InvalidRequest"                          # a bare 404 is not a model claim
    assert type(classify_error("x", 429, '{"error":{"message":"daily usage limit reached"}}')) is UsageLimit
    assert issubclass(ContextWindowExceeded, CapabilityError)


def test_openai_compatible_adapter_maps_ollama_model_not_found_and_stream_error_events():
    body = {"error": {"message": "model \"nope:7b\" not found, try pulling it first", "type": "api_error"}}
    ad = OpenAIResponsesAdapter(endpoint="http://127.0.0.1:11434/v1", dialect="chat", provider_name="local",
                                transport=Recorder(lambda r: json_response(body, status=404)).transport)
    with pytest.raises(ModelUnavailable):
        ad.complete(Request(model="nope:7b", messages=[Message.user("hi")]))
    ad2 = OpenAIResponsesAdapter(endpoint="http://127.0.0.1:1/v1", dialect="chat", provider_name="openrouter", api_key="k",
                                 transport=Recorder(lambda r: json_response({"error": {"message": "Insufficient credits", "code": 402}})).transport)
    with pytest.raises(CreditsExhausted):
        ad2.complete(Request(model="m", messages=[Message.user("hi")], stream=False))


# =============================================================================================== 1. router records
def test_attempt_records_carry_reason_position_locality_revision_and_winner_took_over(client, store, ledger, db):
    a, ma = cloud(store, "OpenRouter", "m-paid", script=[CreditsExhausted("openrouter HTTP 402: Insufficient credits", status=402)])
    b, mb = local(store, "Ollama", "qwen-missing", script=[ModelUnavailable('local HTTP 404: model "qwen-missing" not found', status=404)])
    c, mc = local(store, "Ollama 2", "qwen2.5:14b", script=["fixed"])
    ladder(store, "repair", (a, "m-paid"), (b, "qwen-missing"), (c, "qwen2.5:14b"))
    rev = store.config_revision()
    ledger.create("job:j", "job:j", 1.0)
    res = client.call("repair", req(), budget="job:j", policy={"mode": "assisted"})
    assert res.response.text == "fixed" and res.position == 3 and res.locality == "local" and res.config_revision == rev
    outs = [(x["position"], x["outcome"], x["locality"]) for x in res.attempts]
    assert outs == [(1, "credits_exhausted", "cloud"), (2, "model_unavailable", "local"), (3, "ok", "local")]
    assert all(x["config_revision"] == rev and x["policy_hash"] == policy_hash({"mode": "assisted"}) and x["reason"] for x in res.attempts)
    assert res.attempts[0]["reason"] == "OpenRouter has run out of credits for m-paid"
    assert res.attempts[1]["reason"] == "qwen-missing is not available on Ollama (model not found)"
    took = res.attempts[-1]["took_over_from"]
    assert [(t["position"], t["outcome"]) for t in took] == [(1, "credits_exhausted"), (2, "model_unavailable")] and took == res.took_over_from
    # spend released for both failures; connection / ladder-entry state recorded
    assert ledger.get("job:j")["spent_usd"] == 0 and len(ledger.reservations("job:j", "released")) == 2
    assert store.get(a["connection_id"])["state"] == "no_credits"
    flagged = [m for m in store.get(b["connection_id"])["models"] if m["id"] == "qwen-missing"][0]
    assert flagged["availability"]["state"] == "model_unavailable"
    detail = json.loads(db.query("SELECT detail FROM ai_calls WHERE outcome='ok'")[0]["detail"])
    assert detail["position"] == 3 and detail["config_revision"] == rev and len(detail["took_over_from"]) == 2


def test_context_window_and_capability_are_skipped_before_the_call(client, store, ledger):
    small, ms = local(store, "Ollama", "tiny:1b", capabilities={"vision": False, "tools": False, "completion": True}, context_window=1000)
    big, mb = local(store, "Ollama big", "llava:7b", script=["seen"], capabilities={"vision": True, "tools": False, "completion": True},
                    context_window=32768)
    ladder(store, "visual_review", (small, "tiny:1b"), (big, "llava:7b"))
    img = Request(model="", messages=[Message("user", [ImagePart(data="AAAA"), TextPart("what is this")])], max_output_tokens=50)
    res = client.call("visual_review", img)
    assert ms.calls == [] and res.attempts[0]["outcome"] == "capability_unsupported" and "does not support images" in res.attempts[0]["reason"]
    ladder(store, "implementation", (small, "tiny:1b"), (big, "llava:7b"))
    mb.script = ["long ok"]
    res = client.call("implementation", req("x" * 6000, max_output_tokens=500))
    assert ms.calls == [] and res.attempts[0]["outcome"] == "capability_unsupported"
    assert "does not fit its 1000-token context window" in res.attempts[0]["reason"] and res.response.text == "long ok"


def test_free_provider_out_of_credits_never_silently_moves_to_an_unpriced_paid_model(client, store, ledger, events):
    free, mf = cloud(store, "OpenRouter free", "llama:free", price=FREE, provider="openrouter",
                     script=[CreditsExhausted("openrouter HTTP 402: free tier used up", status=402)])
    paid = store.create("openrouter", "OpenRouter paid", api_key=KEY, models=[{"id": "big-unpriced"}])
    mp = MockProvider(["should not be used"])
    store.set_adapter_override(paid["connection_id"], mp)
    ladder(store, "implementation", (free, "llama:free"), (paid, "big-unpriced"))
    ledger.create("job:j", "job:j", 5.0)
    with pytest.raises((ApprovalRequired, AllCandidatesFailed)) as ei:
        client.call("implementation", req(), budget="job:j")
    att = ei.value.attempts
    assert [a["outcome"] for a in att] == ["credits_exhausted", "approval_required"] and mp.calls == []
    assert "ran out of free credits" in att[1]["reason"] and "without your approval" in att[1]["reason"]
    assert ledger.get("job:j")["spent_usd"] == 0
    stop = activity_texts(events)[-1]
    assert "To continue:" in stop and "add credits to OpenRouter free" in stop


def test_free_provider_out_of_credits_moves_to_a_priced_paid_model_only_within_budget_and_says_so(client, store, ledger, events):
    free, mf = cloud(store, "OpenRouter free", "llama:free", price=FREE, provider="openrouter",
                     script=[CreditsExhausted("openrouter HTTP 402: free tier used up", status=402)])
    paid, mp = cloud(store, "OpenRouter paid", "priced", price=PRICED, provider="openrouter", script=["paid answer"])
    ladder(store, "implementation", (free, "llama:free"), (paid, "priced"))
    # no budget: the paid fallback is refused with a reason (never silently spent)
    with pytest.raises(AllCandidatesFailed) as ei:
        client.call("implementation", req())
    assert ei.value.attempts[-1]["outcome"] == "budget_exhausted" and "no budget" in ei.value.attempts[-1]["reason"] and mp.calls == []
    mf.script = [CreditsExhausted("openrouter HTTP 402: free tier used up", status=402)]
    ledger.create("job:j", "job:j", 1.0)
    res = client.call("implementation", req(), budget="job:j")
    assert res.response.text == "paid answer" and res.cost_usd > 0
    assert any("a paid model, up to $" in t for t in activity_texts(events))


def test_no_ai_policy_refuses_before_any_adapter_and_never_touches_the_network(client, store, ledger, monkeypatch, cases, src_out, events, db):
    c, m = cloud(store, "Cloud", "m", script=["never"])
    ladder(store, "implementation", (c, "m"))
    store.set_adapter_override(c["connection_id"], RaisingProvider())

    def no_network(*a, **k):
        raise AssertionError("network attempted under no_ai")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(httpx.Client, "send", no_network)
    made: list[str] = []
    real_adapter = store.adapter
    monkeypatch.setattr(store, "adapter", lambda *a, **k: made.append(a[0]) or real_adapter(*a, **k))
    src, out = src_out
    case = cases.create_case(name="n", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe",
                             ai_policy={"mode": "no_ai"})
    with pytest.raises(AIDisabled):
        client.call("implementation", req(), case_id=case["case_id"])                    # policy loaded from the case
    with pytest.raises(AIDisabled):
        client.call("implementation", req(), policy={"mode": "no_ai", "budget_usd": 3})  # explicit policy
    assert made == [] and db.query("SELECT COUNT(*) AS n FROM reservations")[0]["n"] == 0
    assert "AI is off for this project" in activity_texts(events, case["case_id"])[-1]


def test_local_only_policy_skips_cloud_entries_with_policy_reason(client, store, ledger):
    c, mc = cloud(store, "Cloud", "gpt-x", script=["cloud"])
    l, ml = local(store, "Ollama", "qwen2.5:3b", script=["local answer"])
    ladder(store, "implementation", (c, "gpt-x"), (l, "qwen2.5:3b"))
    res = client.call("implementation", req(), policy={"mode": "assisted", "locality": "local_only"})
    assert mc.calls == [] and res.response.text == "local answer"
    assert res.attempts[0]["outcome"] == "policy_skipped" and "local models only" in res.attempts[0]["reason"]
    with pytest.raises(NoRoute) as ei:
        client.call("implementation", req(), policy={"mode": "assisted", "locality": "cloud_only", "ladder_overrides": {"implementation": [
            {"connection_id": l["connection_id"], "model": "qwen2.5:3b"}]}})
    assert ei.value.attempts[0]["outcome"] == "policy_skipped" and "cloud models only" in ei.value.recovery + ei.value.attempts[0]["reason"]


def test_project_ladder_override_replaces_the_global_ladder_unless_inherit(client, store):
    c, mc = cloud(store, "Cloud", "gpt-x", script=["global"])
    l, ml = local(store, "Ollama", "qwen2.5:3b", script=["project"])
    ladder(store, "implementation", (c, "gpt-x"))
    ov = {"implementation": [{"connection_id": l["connection_id"], "model": "qwen2.5:3b"}]}
    assert client.call("implementation", req(), policy={"mode": "custom", "ladder_overrides": ov}).response.text == "project"
    store.db.execute("INSERT INTO budgets(budget_id, scope, limit_usd, updated_at) VALUES ('job:x','job:x',1,'t')")
    assert client.call("implementation", req(), policy={"mode": "inherit", "ladder_overrides": ov}, budget="job:x").response.text == "global"


def test_locality_rule():
    assert locality_of({"provider": "local", "endpoint": "http://192.168.1.5:1234/v1"}) == "local"
    assert locality_of({"provider": "openai", "endpoint": "http://127.0.0.1:8080/v1"}) == "local"
    assert locality_of({"provider": "openai", "endpoint": ""}) == "cloud"


# =============================================================================================== 2. Ollama enrichment
def ollama_transport(models=("qwen2.5:14b", "llava:7b", "nomic-embed-text")):
    shows = {"qwen2.5:14b": {"capabilities": ["completion", "tools"], "model_info": {"qwen2.context_length": 32768}},
             "llava:7b": {"capabilities": ["completion", "vision"], "model_info": {"llama.context_length": 32768}},
             "nomic-embed-text": {"capabilities": ["embedding"], "model_info": {"nomic-bert.context_length": 2048}}}

    def reply(r: httpx.Request):
        p = r.url.path
        if p == "/v1/models":
            return json_response({"object": "list", "data": [{"id": m, "object": "model"} for m in models]})
        if p == "/api/tags":
            return json_response({"models": [{"name": m, "details": {"parameter_size": "14.8B" if "14b" in m else "7B", "family": "x"}} for m in models]})
        if p == "/api/version":
            return json_response({"version": "0.12.0"})
        if p == "/api/show":
            name = json.loads(r.content)["model"]
            if name not in shows:
                return json_response({"error": f"model '{name}' not found"}, status=404)
            return json_response(shows[name])
        return json_response({"error": "unexpected"}, status=500)
    return Recorder(reply)


def test_probe_enriches_ollama_models_with_capabilities_and_context(store):
    rec = ollama_transport()
    store.transport = rec.transport
    c = store.create("local", "Ollama", endpoint="http://127.0.0.1:11434/v1", auth_mode="local", models=["not-pulled:1b"])
    p = store.probe(c["connection_id"], capabilities=False)
    by = {m["id"]: m for m in p["models"]}
    assert p["state"] == "ok" and p["limits"]["server"] == "ollama" and p["limits"]["server_version"] == "0.12.0"
    assert by["llava:7b"]["capabilities"]["vision"] is True and by["qwen2.5:14b"]["capabilities"]["tools"] is True
    assert by["qwen2.5:14b"]["context_window"] == 32768 and by["qwen2.5:14b"]["meta"]["ollama"]["parameter_size"] == "14.8B"
    assert by["nomic-embed-text"]["capabilities"]["completion"] is False
    assert by["not-pulled:1b"]["availability"]["state"] == "model_unavailable"
    assert "Ollama server detected" in p["capabilities"]["probe_detail"]


# =============================================================================================== 2-5. API
TOKEN = "t0k3n"


@pytest.fixture
def api(settings):
    from rebuild_controller.api.server import create_app
    from rebuild_controller.services import StudioServices
    st = StudioServices(settings)
    st.ai.advisor = None
    with TestClient(create_app(st, TOKEN)) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c, st
    st.stop()


def api_conns(st):
    rec = ollama_transport()
    st.connections.transport = rec.transport
    loc = st.connections.create("local", "Ollama", endpoint="http://127.0.0.1:11434/v1", auth_mode="local")
    st.connections.probe(loc["connection_id"], capabilities=False)
    cl = st.connections.create("anthropic", "Claude", api_key=KEY, models=["claude-haiku-4-5", "claude-sonnet-5-5"])
    return st.connections.get(loc["connection_id"]), cl


def test_ladder_api_get_put_revision_snapshots_and_routes_compat(api):
    c, st = api
    loc, cl = api_conns(st)
    r0 = c.get("/ai/ladder").json()
    assert set(r0["tasks"]) == {"implementation", "repair", "naming", "visual_review", "verification_assist", "knowledge"}
    rev0 = r0["config_revision"]
    r = c.put("/ai/ladder/repair", json={"entries": [{"connection_id": loc["connection_id"], "model": "qwen2.5:14b"},
                                                     {"connection_id": cl["connection_id"], "model": "claude-haiku-4-5"},
                                                     {"connection_id": loc["connection_id"], "model": "manual:typed"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config_revision"] == rev0 + 1 and body["rationale"] == "user"
    e0, e1, e2 = body["entries"]
    assert (e0["position"], e0["locality"], e0["free"], e0["capabilities"]["context_window"], e0["capabilities"]["tools"]) == (1, "local", True, 32768, True)
    assert (e1["position"], e1["locality"], e1["price"]["known"], e1["price"]["input_per_mtok"]) == (2, "cloud", True, 1)
    assert e2["model"] == "manual:typed" and e2["availability"]["state"] in ("ok", "unprobed")            # manual ids allowed
    assert set(e0) >= {"position", "connection_id", "connection_label", "provider", "model", "locality", "availability", "capabilities", "price", "free"}
    # /routes still works and reflects the same storage; a PUT there also bumps the revision
    routes = {x["task"]: x for x in c.get("/routes").json()}
    assert routes["repair"]["primary_model"] == "qwen2.5:14b" and len(routes["repair"]["fallbacks"]) == 2
    assert c.put("/routes/implementation", json={"primary_connection": cl["connection_id"], "primary_model": "claude-haiku-4-5"}).status_code == 200
    assert c.get("/ai/ladder").json()["config_revision"] == rev0 + 2
    snap = c.get("/ai/ladder/revisions", params={"revision": rev0 + 1}).json()
    assert snap["tasks"]["repair"]["entries"][0]["model"] == "qwen2.5:14b" and "implementation" not in snap["tasks"]
    assert c.put("/ai/ladder/nope", json={"entries": []}).status_code == 400
    assert c.put("/ai/ladder/repair", json={"entries": [{"connection_id": "conn_missing", "model": "x"}]}).status_code == 400
    assert c.put("/ai/ladder/repair", json={"entries": []}).json()["entries"] == []


def test_models_catalog_search_and_task_suitability(api):
    c, st = api
    api_conns(st)
    all_ = c.get("/ai/models").json()
    assert {m["model"] for m in all_} >= {"qwen2.5:14b", "llava:7b", "claude-haiku-4-5"}
    q = c.get("/ai/models", params={"q": "llava"}).json()
    assert [m["model"] for m in q] == ["llava:7b"] and q[0]["capabilities"]["vision"] is True and q[0]["locality"] == "local"
    vis = c.get("/ai/models", params={"task": "visual_review"}).json()
    assert vis[0]["model"] == "llava:7b" and vis[0]["suitable"] is True
    assert not [m for m in vis if m["model"] == "qwen2.5:14b"][0]["suitable"]


def test_presets_preview_then_apply_with_warnings(api):
    c, st = api
    loc, cl = api_conns(st)
    rev = c.get("/ai/ladder").json()["config_revision"]
    pv = c.post("/ai/ladder/preset", json={"preset": "local_first", "apply": False}).json()
    assert pv["applied"] is False and c.get("/ai/ladder").json()["config_revision"] == rev          # preview stores nothing
    rep = [e["model"] for e in pv["tasks"]["repair"]["entries"]]
    assert rep[0] == "qwen2.5:14b" and "nomic-embed-text" not in rep and any(m.startswith("claude") for m in rep)
    assert pv["tasks"]["visual_review"]["entries"][0]["model"] == "llava:7b"
    al = c.post("/ai/ladder/preset", json={"preset": "all_local", "apply": True}).json()
    assert al["applied"] and al["config_revision"] == rev + 1
    lad = c.get("/ai/ladder").json()
    assert all(e["locality"] == "local" for t in lad["tasks"].values() for e in t["entries"])
    assert lad["tasks"]["repair"]["rationale"] == "preset:all_local"
    cloud_only = c.post("/ai/ladder/preset", json={"preset": "all_cloud", "apply": False}).json()
    assert "No cloud model supports images; visual review has no route." in cloud_only["warnings"] or \
           any("visual review" in w for w in cloud_only["warnings"])
    off = c.post("/ai/ladder/preset", json={"preset": "no_ai", "apply": True}).json()
    assert all(not t["entries"] for t in off["tasks"].values()) and any("AI is off" in w for w in off["warnings"])
    assert c.get("/routes").json() == []
    assert c.post("/ai/ladder/preset", json={"preset": "bogus"}).status_code == 400


def make_case(c, tmp_path, policy):
    src = tmp_path / "src"; src.mkdir(exist_ok=True); (src / "a.txt").write_text("x")
    r = c.post("/cases", json={"name": "demo", "source_root": str(src), "output_root": str(tmp_path / "out"), "target_language": "rust",
                               "output_type": "exe", "ai_policy": policy})
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def test_case_ai_policy_get_put_validation_and_effective_ladder(api, tmp_path):
    c, st = api
    loc, cl = api_conns(st)
    c.put("/ai/ladder/implementation", json={"entries": [{"connection_id": cl["connection_id"], "model": "claude-haiku-4-5"},
                                                         {"connection_id": loc["connection_id"], "model": "qwen2.5:14b"}]})
    cid = make_case(c, tmp_path, {"mode": "assisted", "budget_usd": 1.0})
    g = c.get(f"/cases/{cid}/ai-policy").json()
    assert g["policy"]["mode"] == "assisted" and g["policy"]["locality"] == "any" and len(g["policy_hash"]) == 16
    assert [e["model"] for e in g["effective"]["implementation"]["entries"]] == ["claude-haiku-4-5", "qwen2.5:14b"]
    p = c.put(f"/cases/{cid}/ai-policy", json={"locality": "local_only"}).json()
    assert p["policy"]["budget_usd"] == 1.0 and p["policy_hash"] != g["policy_hash"]                 # additive merge
    ents = p["effective"]["implementation"]["entries"]
    assert ents[0]["policy_skipped"] and ents[1]["policy_skipped"] is None
    ov = {"repair": [{"connection_id": loc["connection_id"], "model": "qwen2.5:14b"}]}
    p = c.put(f"/cases/{cid}/ai-policy", json={"mode": "custom", "ladder_overrides": ov}).json()
    assert p["effective"]["repair"]["source"] == "project" and p["effective"]["implementation"]["source"] == "global"
    for bad in ({"mode": "turbo"}, {"locality": "mars"}, {"ladder_overrides": {"nope": []}},
                {"ladder_overrides": {"repair": [{"connection_id": "conn_missing", "model": "x"}]}}, {"budget_usd": -1}):
        assert c.put(f"/cases/{cid}/ai-policy", json=bad).status_code == 400, bad
    assert c.put(f"/cases/{cid}/ai-policy", json={"mode": "no_ai"}).json()["effective"]["repair"]["entries"] == []


def test_plan_exposes_ai_per_item_with_cost_and_without_ai(api, tmp_path):
    c, st = api
    loc, cl = api_conns(st)
    c.put("/ai/ladder/implementation", json={"entries": [{"connection_id": cl["connection_id"], "model": "claude-haiku-4-5"},
                                                         {"connection_id": loc["connection_id"], "model": "qwen2.5:14b"}]})
    cid = make_case(c, tmp_path, {"mode": "assisted", "budget_usd": 0.5, "max_attempts": 3, "max_output_tokens": 4000})
    plan = c.get(f"/cases/{cid}/plan").json()
    items = {i["item_id"].split(":")[-1]: i for i in plan["items"]}
    ai = items["M-IMPL"]["ai"]
    assert ai["task"] == "implementation" and ai["primary"]["model"] == "claude-haiku-4-5" and ai["primary"]["locality"] == "cloud"
    assert [f["model"] for f in ai["fallbacks"]] == ["qwen2.5:14b"] and ai["rationale"] == "user"
    assert ai["expected_cost"]["known"] is True and 0 < ai["expected_cost"]["min_usd"] <= ai["expected_cost"]["max_usd"] <= 0.5
    assert ai["runs_without_ai"] is True and "scaffold" in ai["without_ai"] and ai["budget_usd"] == 0.5
    fix = items["M-FIX"]["ai"]
    assert fix["runs_without_ai"] is False and fix["task"] == "implementation" and "repair" in fix["note"]
    assert items["M-COMPARE"]["origin"] == "verifier_decided" and items["M-IMPL"]["origin"] == "deterministic"
    assert "ai" not in items["M-ANALYSIS"]
    # unknown price in the ladder => unknown_price, never a made-up number
    unk = st.connections.create("openai", "Unpriced", api_key=KEY, models=["gpt-unknown"])
    c.put("/ai/ladder/implementation", json={"entries": [{"connection_id": unk["connection_id"], "model": "gpt-unknown"}]})
    c.put(f"/cases/{cid}/ai-policy", json={"max_output_tokens": None})
    assert c.get(f"/cases/{cid}/plan").json()["items"][[i["item_id"].endswith("M-IMPL") for i in plan["items"]].index(True)]["ai"]["expected_cost"] == \
        {"unknown_price": True, "note": "a model in the ladder has no known price; it is skipped until you set a price, an output-token cap or approve unknown pricing"}
    c.put(f"/cases/{cid}/ai-policy", json={"mode": "no_ai"})
    off = [i for i in c.get(f"/cases/{cid}/plan").json()["items"] if i["item_id"].endswith("M-IMPL")][0]["ai"]
    assert off["enabled"] is False and off["primary"] is None and "off" in off["rationale"]


# =============================================================================================== 3. pause / change / resume
def test_paused_ai_work_resumes_with_the_new_ladder_budget_and_policy(api, tmp_path):
    from rebuild_controller.implement import LoopPolicy, ask_model
    from rebuild_controller.jobs import JobState
    c, st = api
    a = st.connections.create("anthropic", "A", api_key=KEY, models=["claude-haiku-4-5"])
    b = st.connections.create("anthropic", "B", api_key=KEY + "b", models=["claude-sonnet-5-5"])
    ma, mb = MockProvider(["from A"]), MockProvider(["from B"])
    st.connections.set_adapter_override(a["connection_id"], ma)
    st.connections.set_adapter_override(b["connection_id"], mb)
    c.put("/ai/ladder/implementation", json={"entries": [{"connection_id": a["connection_id"], "model": "claude-haiku-4-5"}]})

    def stage(ctx):
        case = st.cases.get_case(ctx.job.case_id)
        out = ask_model(st, ctx, case, LoopPolicy.from_case(case), task="implementation", system="sys", prompt="please", key=f"{ctx.job.job_id}:k")
        return {k: out[k] for k in ("text", "model", "config_revision", "policy_hash")}
    st.stages.add("ai_probe_stage", stage)
    cid = make_case(c, tmp_path, {"mode": "assisted", "budget_usd": 1.0})
    old_hash = c.get(f"/cases/{cid}/ai-policy").json()["policy_hash"]
    j = st.jobs.create(cid, "ai_probe_stage", "AI work", {})
    assert c.post(f"/cases/{cid}/pause").json()["paused"] >= 1
    # while paused: the global ladder and the project budget/policy change
    rev = c.put("/ai/ladder/implementation", json={"entries": [{"connection_id": b["connection_id"], "model": "claude-sonnet-5-5"}]}).json()["config_revision"]
    new = c.put(f"/cases/{cid}/ai-policy", json={"budget_usd": 2.5, "approve_unknown_pricing": True}).json()
    assert new["policy_hash"] != old_hash
    c.post(f"/cases/{cid}/resume")
    for _ in range(50):
        st.runner.run_pending()
        if st.jobs.get(j.job_id).state in (JobState.COMPLETED, JobState.FAILED, JobState.BLOCKED):
            break
    done = st.jobs.get(j.job_id)
    assert done.state == JobState.COMPLETED, (done.state, done.error, done.blocker)
    assert done.result == {"text": "from B", "model": "claude-sonnet-5-5", "config_revision": rev, "policy_hash": new["policy_hash"]}
    assert ma.calls == [] and st.budgets.get(f"case:{cid}")["limit_usd"] == 2.5
    row = st.db.query("SELECT detail FROM ai_calls WHERE case_id=? AND outcome='ok'", (cid,))[0]
    d = json.loads(row["detail"])
    assert d["config_revision"] == rev and d["policy_hash"] == new["policy_hash"]


# =============================================================================================== 5. activity + secrets
def test_activity_feed_api_and_no_keys_or_raw_prompts_anywhere(api, tmp_path):
    c, st = api
    planted = "sk-or-v1-PLANTED-FAKE-KEY-9f8e7d6c5b4a3210"
    marker = "RAW-PROMPT-MARKER-should-never-be-stored"
    conn = st.connections.create("openrouter", "OR", api_key=planted, models=[{"id": "m1", "price": PRICED}])
    conn2 = st.connections.create("openrouter", "OR2", api_key="sk-or-v1-OTHER-FAKE-KEY-0000000000", models=[{"id": "m2", "price": PRICED}])
    err = CreditsExhausted("placeholder", status=402)
    err.args = (f"openrouter HTTP 402: key {planted} has no credits",)     # an upstream body that echoes the key
    st.connections.set_adapter_override(conn["connection_id"], MockProvider([err]))
    st.connections.set_adapter_override(conn2["connection_id"], MockProvider(["done"]))
    # (R9: a second model on the SAME connection would be skipped: OR is paused for every task after "no credits")
    st.connections.set_route("implementation", conn["connection_id"], "m1", [{"connection": conn2["connection_id"], "model": "m2"}])
    cid = make_case(c, tmp_path, {"mode": "assisted", "budget_usd": 1})
    st.budgets.ensure(f"case:{cid}", f"case:{cid}", 1.0)
    st.ai.call("implementation", req(marker), case_id=cid, budget=f"case:{cid}", activity={"subject": "module demo.exe", "plan_item_id": f"{cid}:M-IMPL"})
    feed = c.get(f"/cases/{cid}/ai/activity").json()
    texts = [f["text"] for f in feed]
    assert texts[0] == "Writing the implementation of module demo.exe with cloud model m1 (OR)"
    assert texts[1].startswith("OR has run out of credits for m1; trying m2")
    assert texts[2].startswith("m2 answered (")
    f0 = feed[0]
    assert set(f0) >= {"at", "kind", "text", "plan_item_id", "job_id", "candidate_id", "evidence_ids", "task", "provider", "model", "locality",
                       "outcome", "tokens_in", "tokens_out", "cost_usd", "cost_known", "fallback_reason", "config_revision", "origin"}
    assert f0["plan_item_id"] == f"{cid}:M-IMPL" and len(f0["prompt_sha256"]) == 64 and feed[1]["fallback_reason"]
    assert c.get(f"/cases/{cid}/ai/activity", params={"since": feed[1]["seq"]}).json()[0]["text"] == texts[2]
    dump = ""
    for (t,) in st.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        dump += json.dumps(st.db.query(f"SELECT * FROM {t}"), default=str)
    assert planted not in dump and planted not in json.dumps(feed)
    assert marker not in json.dumps(feed) and marker not in json.dumps(st.events.events_since(0))


def test_emit_activity_drops_prompt_fields_and_redacts(events):
    p = emit_activity(events, "used key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX", kind="x", prompt="secret prompt", model="m")
    assert "prompt" not in p and "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX" not in p["text"]


# =============================================================================================== 6. scripted failover over real HTTP
class FakeModels:
    """OpenAI-compatible fake on 127.0.0.1: behaviour per model id, scripted per request."""

    def __init__(self, behaviours: dict[str, list[Any]]):
        self.behaviours = {k: list(v) for k, v in behaviours.items()}
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, doc, headers=None):
                data = json.dumps(doc).encode()
                try:
                    self.send_response(code)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(data)))
                    for k, v in (headers or {}).items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def do_GET(self):
                self._send(200, {"data": [{"id": m} for m in outer.behaviours]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                with outer.lock:
                    outer.requests.append(body)
                    script = outer.behaviours.get(body.get("model"))
                    item = script.pop(0) if script else ("status", 500, {"error": {"message": "unscripted"}})
                kind = item[0]
                if kind == "status":
                    _, code, doc, *hdr = item
                    self._send(code, doc, hdr[0] if hdr else None)
                elif kind == "hang":
                    time.sleep(item[1])
                    self._send(200, {"choices": [{"message": {"content": "too late"}, "finish_reason": "stop"}]})
                else:
                    _, text, pt, ct = item
                    self._send(200, {"id": "x", "model": body.get("model"), "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                                                                        "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": pt, "completion_tokens": ct}})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def count(self, model):
        return sum(1 for r in self.requests if r.get("model") == model)

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    made = []

    def make(b):
        f = FakeModels(b)
        made.append(f)
        return f
    yield make
    for f in made:
        f.close()


def scripted_ladder(store, srv, models):
    conn = store.create("openrouter", "Fake router", endpoint=srv.url, api_key=KEY, dialect="chat",
                        models=[{"id": m, "price": p} for m, p in models])
    store.set_route("repair", conn["connection_id"], models[0][0], [{"connection": conn["connection_id"], "model": m} for m, _ in models[1:]])
    return conn


def test_scripted_failover_through_a_real_ladder_over_http(store, ledger, events, db, fake):
    srv = fake({
        "m-busy": [("status", 429, {"error": {"message": "slow down", "type": "rate_limit_error"}}, {"retry-after": "3"}),
                   ("status", 429, {"error": {"message": "slow down", "type": "rate_limit_error"}}, {"retry-after": "3"})],
        "m-free": [("status", 402, {"error": {"message": "Insufficient credits. Add more using https://openrouter.ai/credits", "code": 402}})],
        "m-gone": [("status", 404, {"error": {"message": "The model `m-gone` does not exist", "code": "model_not_found"}})],
        "m-5xx": [("status", 503, {"error": {"message": "upstream overloaded"}})],
        "m-slow": [("hang", 2.0)],
        "m-ok": [("reply", "the fix", 1000, 500)],
    })
    scripted_ladder(store, srv, [("m-busy", PRICED), ("m-free", FREE), ("m-gone", PRICED), ("m-5xx", PRICED), ("m-slow", PRICED), ("m-ok", PRICED)])
    sleeps: list[float] = []
    client = AIClient(store, ledger, events, db, sleep=sleeps.append)
    ledger.create("case:c", "case:c", 1.0)
    r = Request(model="", messages=[Message.user("repair this")], max_output_tokens=1000, stream=False, timeout_s=0.6)
    res = client.call("repair", r, budget="case:c", case_id="c", activity={"subject": "candidate r1"})
    seq = [(a["position"], a["model"], a["outcome"]) for a in res.attempts]
    assert seq == [(1, "m-busy", "rate_limit"), (1, "m-busy", "rate_limit"), (2, "m-free", "credits_exhausted"), (3, "m-gone", "model_unavailable"),
                   (4, "m-5xx", "unavailable"), (5, "m-slow", "timeout"), (6, "m-ok", "ok")]
    assert sleeps == [3.0]                                         # Retry-After honoured, one bounded re-send
    assert res.model == "m-ok" and [t["model"] for t in res.took_over_from] == ["m-busy", "m-free", "m-gone", "m-5xx", "m-slow"]
    assert srv.count("m-slow") == 1                                # ambiguous: never re-sent
    reasons = {a["model"]: a["reason"] for a in res.attempts}
    assert reasons["m-free"] == "Fake router has run out of credits for m-free"
    assert reasons["m-gone"] == "m-gone is not available on Fake router (model not found)"
    assert reasons["m-5xx"] == "Fake router returned a server error (HTTP 503)"
    assert "counted as spent and is not sent again" in reasons["m-slow"]
    # spend: everything released except the ambiguous call (assumed spent at its ceiling) and the winner (actual usage)
    slow_res = [x for x in ledger.reservations("case:c") if ":m-slow:" in x["request_key"]]
    assert len(slow_res) == 1 and slow_res[0]["state"] == "settled" and slow_res[0]["actual_usd"] == slow_res[0]["amount_usd"] > 0
    ok_cost = 1000 * 1e-6 + 500 * 2e-6
    b = ledger.get("case:c")
    assert b["spent_usd"] == pytest.approx(slow_res[0]["amount_usd"] + ok_cost) and b["reserved_usd"] == 0
    assert len(ledger.reservations("case:c", "released")) == 5
    texts = activity_texts(events, "c")
    assert texts[0] == "Repairing candidate r1 with local model m-busy"
    assert texts[1].startswith("m-busy on Fake router is rate-limited (HTTP 429); it asked to wait 3s; waiting 3s and sending again")
    assert texts[2] == "m-busy on Fake router is rate-limited (HTTP 429); it asked to wait 3s; trying m-free (runs on this PC)"
    # after the FREE model ran out of credits, every paid fallback says so (known price, within the budget)
    assert texts[3].startswith("Fake router has run out of credits for m-free; trying m-gone (runs on this PC) - a paid model, up to $")
    assert texts[4].startswith("m-gone is not available on Fake router (model not found); trying m-5xx (runs on this PC) - a paid model")
    assert texts[5].startswith("Fake router returned a server error (HTTP 503); trying m-slow (runs on this PC) - a paid model")
    assert texts[6].startswith("m-slow timed out after the request was sent") and "trying m-ok (runs on this PC) - a paid model" in texts[6]
    assert texts[7].startswith("m-ok answered (1000 tokens in, 500 out, $0.0020")
    rows = db.query("SELECT outcome FROM ai_calls WHERE case_id='c' ORDER BY rowid")
    assert [x["outcome"] for x in rows] == ["rate_limit", "rate_limit", "credits_exhausted", "model_unavailable", "unavailable", "timeout", "ok"]


def test_every_option_fails_preserves_spend_accounting_and_gives_a_precise_recovery(store, ledger, events, db, fake):
    srv = fake({"m-free": [("status", 402, {"error": {"message": "Insufficient credits", "code": 402}})],
                "m-gone": [("status", 404, {"error": {"message": "model \"m-gone\" not found, try pulling it first"}})],
                "m-slow": [("hang", 2.0)]})
    scripted_ladder(store, srv, [("m-free", FREE), ("m-gone", PRICED), ("m-slow", PRICED)])
    client = AIClient(store, ledger, events, db, sleep=lambda s: None)
    ledger.create("case:c", "case:c", 1.0)
    r = Request(model="", messages=[Message.user("repair this")], max_output_tokens=1000, stream=False, timeout_s=0.6)
    with pytest.raises(AllCandidatesFailed) as ei:
        client.call("repair", r, budget="case:c", case_id="c")
    e = ei.value
    assert [a["outcome"] for a in e.attempts] == ["credits_exhausted", "model_unavailable", "timeout"] and srv.count("m-slow") == 1
    assert e.recovery.startswith("To continue: add credits to Fake router") and "choose a current model instead of m-gone" in e.recovery
    assert "check whether Fake router processed the timed-out request" in e.recovery
    b = ledger.get("case:c")
    assert b["reserved_usd"] == 0 and b["spent_usd"] > 0          # only the ambiguous call counts as spent
    last = activity_texts(events, "c")[-1]
    assert "no other model is left to try" in last and "To continue:" in last


def test_per_model_capabilities_beat_a_connection_wide_probe_result(client, store):
    # a capability probe of a text model on the same Ollama marks the CONNECTION images=rejected; llava itself reports vision
    llava, ml = local(store, "Ollama", "llava:7b", script=["a red square"], capabilities={"vision": True, "tools": False, "completion": True})
    caps = dict(store.get(llava["connection_id"])["capabilities"], images="rejected")
    store.db.update("connections", "connection_id", llava["connection_id"], {"capabilities": caps})
    ladder(store, "visual_review", (llava, "llava:7b"))
    img = Request(model="", messages=[Message("user", [ImagePart(data="AAAA"), TextPart("what is this")])], max_output_tokens=50)
    assert client.call("visual_review", img).response.text == "a red square"
