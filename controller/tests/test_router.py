"""Connections, routes, probing and the AIClient call path. Mock adapters / MockTransport only: no network, no spend."""
import logging
import warnings

import httpx
import pytest

from rebuild_controller.budget import BudgetExhausted, BudgetLedger, DuplicateReservation
from rebuild_controller.providers import (AmbiguousCompletion, ApprovalRequired, AuthError, InvalidRequest, MockProvider, ProviderUnavailable,
                                          RaisingProvider, RateLimit, Request, Response, Timeout, Tool, Unreachable, Usage, UsageLimit)
from rebuild_controller.providers import subscription as sub
from rebuild_controller.providers.base import Message
from rebuild_controller.providers.connections import (ConnectionStore, ConnectionStoreError, ModelNotListed, PROBE_BUDGET_USD)
from rebuild_controller.providers.mock import AICallAttempted
from rebuild_controller.providers.pricing import PriceTable, UNKNOWN_INPUT_PER_MTOK, estimate_request_tokens
from rebuild_controller.providers.router import AIClient, AllCandidatesFailed, BudgetRequired, NoRoute
from rebuild_controller.providers.secrets import SecretStore, register_secret

from test_providers import Recorder, anthropic_stream, chat_chunks, json_response, probing_handler

KEY_A = "sk-ant-NOT-REAL-aaaaaaaaaaaaaaaaaaaa"
KEY_B = "sk-ant-NOT-REAL-bbbbbbbbbbbbbbbbbbbb"


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


def req(text="hello", **kw):
    kw.setdefault("max_output_tokens", 100)
    return Request(model="", messages=[Message.user(text)], **kw)


def two_routes(store, task="implementation", a_script=None, b_script=None):
    """Connection A (primary) and B (fallback), both with priced Anthropic models, backed by mock adapters."""
    a = store.create("anthropic", "A", api_key=KEY_A, models=["claude-sonnet-5-5"])
    b = store.create("anthropic", "B", api_key=KEY_B, models=["claude-haiku-4-5"])
    ma, mb = MockProvider(a_script), MockProvider(b_script)
    store.set_adapter_override(a["connection_id"], ma)
    store.set_adapter_override(b["connection_id"], mb)
    store.set_route(task, a["connection_id"], "claude-sonnet-5-5", [{"connection": b["connection_id"], "model": "claude-haiku-4-5"}])
    return a, b, ma, mb


@pytest.fixture
def client(store, ledger, events, db):
    sleeps: list[float] = []
    c = AIClient(store, ledger, events, db, sleep=sleeps.append)
    c.sleeps = sleeps  # type: ignore[attr-defined]
    return c


# =============================================================================================== connection store
def test_create_never_exposes_or_persists_the_key_in_the_db(store, db, secret_store):
    c = store.create("anthropic", "Main", api_key=KEY_A, models=["claude-sonnet-5-5"])
    assert c["has_secret"] is True and "api_key" not in c and "secret_ref" not in c and c["state"] == "unprobed"
    assert c["models"] == [{"id": "claude-sonnet-5-5", "source": "explicit"}]
    dump = repr(db.query("SELECT * FROM connections")) + repr(db.query("SELECT * FROM events"))
    assert KEY_A not in dump
    ref = db.query_one("SELECT secret_ref FROM connections")["secret_ref"]
    assert ref.startswith("secret:") and secret_store.get(ref) == KEY_A
    assert store.get(c["connection_id"]) == c and [x["connection_id"] for x in store.list()] == [c["connection_id"]]


@pytest.mark.parametrize("kw,msg", [
    (dict(provider="acme", label="x", api_key="k"), "unknown provider"),
    (dict(provider="openai", label="x"), "requires api_key"),
    (dict(provider="openai", label="x", auth_mode="weird", api_key="k"), "unknown auth_mode"),
    (dict(provider="local", label="x", auth_mode="none"), "needs an endpoint"),
    (dict(provider="openai", label=" ", api_key="k"), "label"),
    (dict(provider="openai", label="x", api_key="k", endpoint="http://example.com/v1"), "plain http"),
    (dict(provider="openai", label="x", api_key="k", endpoint="ftp://example.com"), "http"),
    (dict(provider="anthropic", label="x", api_key="k", dialect="chat"), "dialect"),
    (dict(provider="anthropic", label="x", api_key="k", models=[{"nope": 1}]), "invalid model"),
    (dict(provider="openai", label="x", auth_mode="subscription_handoff", api_key="k"), "never stores credentials"),
    (dict(provider="openrouter", label="x", auth_mode="subscription_handoff"), "no subscription handoff"),
])
def test_create_validation(store, kw, msg):
    with pytest.raises(ConnectionStoreError, match=msg):
        store.create(**kw)
    assert store.list() == []


def test_local_http_endpoint_is_allowed_for_loopback_and_local_provider(store):
    assert store.create("openai", "dev", api_key="k", endpoint="http://localhost:8080/v1")["endpoint"] == "http://localhost:8080/v1"
    assert store.create("local", "lm", endpoint="http://192.168.1.5:1234/v1/", auth_mode="none")["endpoint"] == "http://192.168.1.5:1234/v1"


def test_subscription_connection_records_handoff_limits_and_has_no_adapter(store):
    c = store.create("openai", "Codex plan", auth_mode="subscription_handoff")
    h = c["limits"]["handoff"]
    assert h["key"] == "openai_siwc" and h["checked_on"] and h["limits"] and c["has_secret"] is False
    with pytest.raises(Exception, match="no API adapter"):
        store.adapter(c["connection_id"])
    claude = store.create("anthropic", "Claude plan", auth_mode="subscription_handoff")
    assert claude["limits"]["handoff"]["supported"] is False


def test_update_rotates_key_and_resets_state(store, secret_store, db):
    c = store.create("anthropic", "A", api_key=KEY_A)
    old = db.query_one("SELECT secret_ref FROM connections")["secret_ref"]
    store.set_state(c["connection_id"], "auth_failed")
    u = store.update(c["connection_id"], api_key=KEY_B, label="A2")
    new = db.query_one("SELECT secret_ref FROM connections")["secret_ref"]
    assert u["label"] == "A2" and u["state"] == "unprobed" and new != old
    assert secret_store.get(old) is None and secret_store.get(new) == KEY_B


def test_delete_removes_secret_and_cleans_routes(store, secret_store, db):
    a, b, *_ = two_routes(store)
    ref = db.query_one("SELECT secret_ref FROM connections WHERE connection_id=?", (b["connection_id"],))["secret_ref"]
    assert store.delete(b["connection_id"]) is True and secret_store.get(ref) is None
    assert store.get_route("implementation")["fallbacks"] == []
    assert store.delete(a["connection_id"]) is True
    r = store.get_route("implementation")
    assert r["primary_connection"] is None and r["primary_model"] is None
    assert store.delete("conn_missing") is False


# =============================================================================================== routes / resolve
def test_routes_require_an_explicit_model_and_known_task(store):
    c = store.create("anthropic", "A", api_key=KEY_A)
    with pytest.raises(ConnectionStoreError, match="explicit model"):
        store.set_route("repair", c["connection_id"], "")
    with pytest.raises(ConnectionStoreError, match="unknown task"):
        store.set_route("write_poetry", c["connection_id"], "m")
    with pytest.raises(ConnectionStoreError, match="unknown connection"):
        store.set_route("repair", "conn_nope", "m")
    with pytest.raises(ConnectionStoreError, match="duplicate"):
        store.set_route("repair", c["connection_id"], "m", [{"connection": c["connection_id"], "model": "m"}])
    assert store.get_routes() == []  # failed writes leave nothing behind
    store.set_route("repair", c["connection_id"], "claude-x")
    assert {"id": "claude-x", "source": "explicit"} in store.get("connection_id".replace("connection_id", c["connection_id"]))["models"]


def test_resolve_is_deterministic_primary_then_fallbacks(store):
    a, b, *_ = two_routes(store)
    r = store.resolve("implementation")
    assert [(c["label"], m) for c, m in r] == [("A", "claude-sonnet-5-5"), ("B", "claude-haiku-4-5")]
    assert store.resolve("implementation") == r
    assert store.resolve("repair") == []  # nothing configured => nothing invented


def test_resolve_skips_unusable_connections_and_explains(store):
    a, b, *_ = two_routes(store)
    store.set_state(a["connection_id"], "auth_failed")
    usable, skipped = store.resolve_detailed("implementation")
    assert [m for _, m in usable] == ["claude-haiku-4-5"] and "auth failed" in skipped[0]["reason"]
    h = store.create("openai", "plan", auth_mode="subscription_handoff")
    store.set_route("repair", h["connection_id"], "whatever")
    usable, skipped = store.resolve_detailed("repair")
    assert usable == [] and "no API path" in skipped[0]["reason"]


def test_resolve_honours_probed_capabilities_only_when_rejected(store, db):
    a, b, *_ = two_routes(store)
    db.update("connections", "connection_id", a["connection_id"], {"capabilities": {"tools": "rejected", "images": "untested"}})
    db.update("connections", "connection_id", b["connection_id"], {"capabilities": {"tools": "accepted_no_call"}})
    assert [c["label"] for c, _ in store.resolve("implementation", {"tools"})] == ["B"]   # inconclusive != unsupported
    assert [c["label"] for c, _ in store.resolve("implementation", {"images"})] == ["A", "B"]  # untested is not excluded


# =============================================================================================== probing / discovery
def local_store_conn(store, transport, **kw):
    store.transport = transport
    return store.create("local", "LM Studio", endpoint="http://localhost:1234/v1", auth_mode="none", **kw)


def local_handler(models=("m1", "m2"), **probe_kw):
    probe = probing_handler(**probe_kw)

    def reply(r: httpx.Request):
        if r.method == "GET" and r.url.path.endswith("/models"):
            return json_response({"data": [{"id": m} for m in models]})
        if r.url.path.endswith("/responses"):  # a chat-completions-only server (typical LM Studio / Ollama build)
            return json_response({"error": {"message": "Unknown endpoint"}}, status=404)
        return probe(r)
    return reply


def test_probe_discovers_models_and_records_capabilities(store):
    c = local_store_conn(store, Recorder(local_handler()).transport, models=["typed-by-user"])
    p = store.probe(c["connection_id"], model="m1")
    assert p["state"] == "ok" and p["last_probe"]
    assert [(m["id"], m["source"]) for m in p["models"]] == [("typed-by-user", "explicit"), ("m1", "discovered"), ("m2", "discovered")]
    caps = p["capabilities"]
    assert caps["discovery"] == "supported" and caps["tools"] == "supported" and caps["json_schema"] == "supported" and caps["images"] == "supported"
    assert caps["model_probed"] == "m1" and "discovered 2" in caps["probe_detail"]
    assert p["limits"]["dialect"] == "chat"  # 'auto' resolved: the responses dialect was not answered by this server


def test_probe_records_partial_support_for_a_not_fully_compatible_endpoint(store):
    c = local_store_conn(store, Recorder(local_handler(tools=False, schema=False)).transport)
    caps = store.probe(c["connection_id"], model="m1")["capabilities"]
    assert caps["tools"] == "rejected" and caps["json_schema"] == "rejected" and caps["streaming"] == "supported"
    # the router now refuses to send tool requests to this endpoint
    store.set_route("repair", c["connection_id"], "m1")
    assert store.resolve("repair", {"tools"}) == [] and len(store.resolve("repair", set())) == 1


def test_model_discovery_unsupported_requires_explicit_model_id_and_invents_nothing(store):
    def reply(r):
        if r.url.path.endswith("/models"):
            return json_response({"error": "nope"}, status=404)
        return probing_handler()(r)
    c = local_store_conn(store, Recorder(reply).transport)
    p = store.probe(c["connection_id"])
    assert p["capabilities"]["discovery"] == "unsupported" and p["models"] == []
    assert "no model id" in p["capabilities"]["probe_detail"] and p["capabilities"]["tools"] == "untested"
    assert p["state"] == "unprobed"  # auth was not proven by anything
    with pytest.raises(ConnectionStoreError, match="explicit model"):
        store.set_route("repair", c["connection_id"], " ")
    p = store.probe(c["connection_id"], model="what-i-typed")
    assert p["capabilities"]["model_probed"] == "what-i-typed" and p["state"] == "ok" and p["capabilities"]["streaming"] == "supported"


def test_model_names_are_not_derived_from_provider_or_label(store):
    c = store.create("openai", "gpt-5 production", api_key="k")
    assert c["models"] == []
    with pytest.raises(ConnectionStoreError):
        store.set_route("repair", c["connection_id"], "")


def test_route_must_use_a_model_the_endpoint_reports_when_discovery_worked(store):
    c = local_store_conn(store, Recorder(local_handler()).transport)
    store.probe(c["connection_id"], capabilities=False)
    with pytest.raises(ModelNotListed):
        store.set_route("repair", c["connection_id"], "m3")
    store.set_route("repair", c["connection_id"], "m3", allow_unlisted=True)
    store.set_route("repair", c["connection_id"], "m2")


def test_probe_state_machine(store):
    def probe_with(reply):
        store.transport = Recorder(reply).transport
        c = store.create("local", f"c{len(store.list())}", endpoint="http://localhost:1/v1", auth_mode="none")
        return store.probe(c["connection_id"], capabilities=False)["state"]
    assert probe_with(lambda r: json_response({"error": {"message": "bad key"}}, status=401)) == "auth_failed"
    assert probe_with(lambda r: httpx.ConnectError("refused", request=r)) == "unreachable"
    assert probe_with(lambda r: httpx.ReadTimeout("t", request=r)) == "unreachable"
    assert probe_with(lambda r: json_response({"error": {"message": "x", "code": "insufficient_quota"}}, status=429)) == "limited"
    assert probe_with(lambda r: json_response({"data": [{"id": "a"}]})) == "ok"


def test_probe_without_stored_key_is_auth_failed(store, db):
    c = store.create("anthropic", "A", api_key=KEY_A)
    db.update("connections", "connection_id", c["connection_id"], {"secret_ref": None})
    assert store.probe(c["connection_id"])["state"] == "auth_failed"


def test_probe_never_spends_on_unpriced_model_without_approval(store, ledger):
    calls = []

    def reply(r):
        calls.append(r.url.path)
        if r.url.path.endswith("/models"):
            return json_response({"data": [{"id": "gpt-unpriced"}]})
        return probing_handler()(r)
    store.transport = Recorder(reply).transport
    c = store.create("openai", "O", api_key="sk-openai-NOT-REAL-12345678", dialect="chat")
    p = store.probe(c["connection_id"], model="gpt-unpriced")
    assert p["state"] == "ok" and p["capabilities"]["probe_skipped"] == "pricing_unknown" and p["capabilities"]["tools"] == "untested"
    assert calls == ["/v1/models"] and not ledger.exists(f"probe:{c['connection_id']}")
    p = store.probe(c["connection_id"], model="gpt-unpriced", approve_unknown_pricing=True)
    assert p["capabilities"]["tools"] == "supported" and "probe_skipped" not in p["capabilities"]
    b = ledger.get(f"probe:{c['connection_id']}")
    assert b["limit_usd"] == PROBE_BUDGET_USD and b["reserved_usd"] == 0 and 0 < b["spent_usd"] < PROBE_BUDGET_USD


def test_probe_spend_is_metered_through_the_ledger_for_priced_models(store, ledger):
    def reply(r):
        if r.url.path.endswith("/models"):
            return json_response({"data": [{"id": "claude-sonnet-5-5"}]})
        return anthropic_stream()
    store.transport = Recorder(reply).transport
    c = store.create("anthropic", "A", api_key=KEY_A)
    store.probe(c["connection_id"], model="claude-sonnet-5-5")
    b = ledger.get(f"probe:{c['connection_id']}")
    assert b["spent_usd"] > 0 and b["reserved_usd"] == 0 and len(ledger.reservations(b["budget_id"], "settled")) == 1


def test_probe_budget_exhaustion_skips_probe_rather_than_overspending(store, ledger):
    store.transport = Recorder(local_handler()).transport
    c = store.create("anthropic", "A", api_key=KEY_A)
    bid = f"probe:{c['connection_id']}"
    ledger.create(bid, bid, 0.0000001)
    store.transport = Recorder(lambda r: json_response({"data": [{"id": "claude-sonnet-5-5"}]}) if r.url.path.endswith("/models") else pytest.fail("no spend")).transport
    p = store.probe(c["connection_id"], model="claude-sonnet-5-5")
    assert p["capabilities"]["probe_skipped"] == "probe_budget_exhausted"


def test_probe_dialect_auto_prefers_responses_when_the_server_speaks_it(store):
    from test_providers import responses_stream

    def reply(r):
        if r.url.path.endswith("/models"):
            return json_response({"data": [{"id": "m"}]})
        if r.url.path.endswith("/responses"):
            return responses_stream()
        return json_response({"error": {"message": "unexpected chat call"}}, status=400)
    store.transport = Recorder(reply).transport
    c = store.create("local", "L", endpoint="http://localhost:1/v1", auth_mode="none")
    p = store.probe(c["connection_id"], model="m", capabilities=True)
    assert p["limits"]["dialect"] == "responses" and p["capabilities"]["streaming"] == "supported"


def test_probe_of_subscription_handoff_checks_cli_only(store, monkeypatch):
    c = store.create("gemini", "Gemini plan", auth_mode="subscription_handoff")
    monkeypatch.setattr(sub, "cli_available", lambda mode, which=None: False)
    p = store.probe(c["connection_id"])
    assert p["state"] == "unreachable" and "not found" in p["capabilities"]["probe_detail"]
    monkeypatch.setattr(sub, "cli_available", lambda mode, which=None: True)
    p = store.probe(c["connection_id"])
    assert p["state"] == "ok" and "not your login" in p["capabilities"]["probe_detail"]


# =============================================================================================== pricing
def test_unknown_pricing_is_never_zero_and_needs_approval():
    t = PriceTable()
    p = t.lookup("openai", "gpt-never-heard-of-it")
    assert p.known is False and p.approval_required and p.input_per_mtok == UNKNOWN_INPUT_PER_MTOK > 0 and p.output_per_mtok > 0
    assert p.ceiling(1000, 1000) > 0
    k = t.lookup("anthropic", "claude-sonnet-5-5")
    assert k.known and not k.approval_required and (k.input_per_mtok, k.output_per_mtok) == (2.0, 10.0)
    assert k.source.startswith("https://platform.claude.com") and k.checked_on == "2026-10-06"
    assert t.lookup("anthropic", "claude-haiku-4-5-20251001").known  # dated snapshot of a listed model
    assert not t.lookup("anthropic", "claude-invented-9").known
    assert not t.lookup("gemini", "gemini-anything").known and not t.lookup("openrouter", "x/y").known


def test_price_cost_is_cache_aware():
    p = PriceTable().lookup("anthropic", "claude-sonnet-5-5")
    u = Usage(input_tokens=135, cached_tokens=100, cache_write_tokens=10, output_tokens=42)
    assert p.cost(u) == pytest.approx((25 * 2 + 100 * 0.2 + 10 * 2.5 + 42 * 10) / 1e6)
    assert p.cost(Usage(input_tokens=10, cache_write_tokens=10, cache_write_ttl="1h")) == pytest.approx(10 * 4 / 1e6)


def test_connection_model_price_overrides_and_local_is_free(store):
    c = store.create("openai", "O", api_key="k", models=[{"id": "gpt-x", "price": {"input_per_mtok": 1.0, "output_per_mtok": 3.0}}])
    p = PriceTable().lookup("openai", "gpt-x", connection=c)
    assert p.known and (p.input_per_mtok, p.output_per_mtok) == (1.0, 3.0) and "user-supplied" in p.source
    bad = store.create("openai", "O2", api_key="k", models=[{"id": "gpt-y", "price": {"input_per_mtok": -1, "output_per_mtok": 3.0}}])
    assert not PriceTable().lookup("openai", "gpt-y", connection=bad).known
    loc = store.create("local", "L", endpoint="http://localhost:1/v1", auth_mode="none")
    assert PriceTable().lookup("local", "any", connection=loc).known and PriceTable().lookup("local", "any", connection=loc).input_per_mtok == 0


# =============================================================================================== AIClient
def test_successful_call_reserves_sends_settles_logs_and_emits(client, store, ledger, db, events):
    a, b, ma, mb = two_routes(store, a_script=["the answer"])
    ledger.create("job:j1", "job:j1", 0.01)
    res = client.call("implementation", req(), job_id="j1", case_id="c1", budget="job:j1")
    assert res.response.text == "the answer" and res.model == "claude-sonnet-5-5" and res.connection_id == a["connection_id"]
    assert ma.calls[0].model == "claude-sonnet-5-5" and mb.calls == []
    assert res.cost_usd == pytest.approx((10 * 2 + 5 * 10) / 1e6) and res.cost_known
    bud = ledger.get("job:j1")
    assert bud["spent_usd"] == pytest.approx(res.cost_usd) and bud["reserved_usd"] == 0
    row = db.query_one("SELECT * FROM ai_calls")
    assert (row["provider"], row["model"], row["task"], row["outcome"], row["case_id"], row["job_id"]) == \
        ("anthropic", "claude-sonnet-5-5", "implementation", "ok", "c1", "j1")
    assert (row["input_tokens"], row["output_tokens"], row["cost_known"]) == (10, 5, 1) and row["latency_ms"] is not None
    ev = [e for e in events.events_since(0) if e["kind"] == "ai.call"]
    assert len(ev) == 1 and ev[0]["case_id"] == "c1" and ev[0]["job_id"] == "j1"
    assert ev[0]["payload"]["outcome"] == "ok" and ev[0]["payload"]["cost_usd"] == pytest.approx(res.cost_usd)


def test_exhausted_budget_blocks_the_send(client, store, ledger, db):
    a, b, ma, mb = two_routes(store)
    ledger.create("job:j", "job:j", 0.0000001)
    with pytest.raises(BudgetExhausted):
        client.call("implementation", req(), budget="job:j")
    assert ma.calls == [] and mb.calls == []          # nothing was sent anywhere
    assert ledger.get("job:j")["spent_usd"] == 0 and ledger.reservations("job:j") == []
    assert [r["outcome"] for r in db.query("SELECT outcome FROM ai_calls")] == ["budget_exhausted", "budget_exhausted"]


def test_cheaper_fallback_can_fit_when_primary_does_not(client, store, ledger):
    a, b, ma, mb = two_routes(store, b_script=["from B"])
    # ceiling(A, sonnet 5.5 @ $10/M out) ~ 0.0011; ceiling(B, haiku @ $5/M out) ~ 0.0006
    ledger.create("job:j", "job:j", 0.0008)
    res = client.call("implementation", req(), budget="job:j")
    assert res.response.text == "from B" and ma.calls == [] and len(mb.calls) == 1
    assert [x["outcome"] for x in res.attempts] == ["budget_exhausted", "ok"]


def test_duplicate_request_key_is_rejected_before_sending(client, store, ledger):
    a, b, ma, mb = two_routes(store)
    ledger.create("job:j", "job:j", 1.0)
    client.call("implementation", req(), budget="job:j", request_key="job:j:step-3")
    assert len(ma.calls) == 1
    with pytest.raises(DuplicateReservation):
        client.call("implementation", req(), budget="job:j", request_key="job:j:step-3")
    assert len(ma.calls) == 1 and mb.calls == []


def test_unknown_pricing_requires_approval_then_reserves_a_conservative_ceiling(client, store, ledger, db):
    c = store.create("openai", "O", api_key="sk-openai-NOT-REAL-12345678", models=["gpt-mystery"])
    m = MockProvider(["ok"])
    store.set_adapter_override(c["connection_id"], m)
    store.set_route("repair", c["connection_id"], "gpt-mystery")
    ledger.create("job:j", "job:j", 1.0)
    with pytest.raises(ApprovalRequired) as ei:
        client.call("repair", req(), budget="job:j")
    assert ei.value.models == [("openai", "gpt-mystery")] and m.calls == [] and ledger.get("job:j")["spent_usd"] == 0
    assert db.query_one("SELECT outcome FROM ai_calls")["outcome"] == "approval_required"
    res = client.call("repair", req(), budget="job:j", approve_unknown_pricing=True)
    assert res.cost_known is False and res.cost_usd > 0               # priced at the conservative limit, never zero
    assert res.cost_usd == pytest.approx((10 * UNKNOWN_INPUT_PER_MTOK + 5 * 150.0) / 1e6)
    assert db.query("SELECT cost_known FROM ai_calls WHERE outcome='ok'")[0]["cost_known"] == 0
    # a budget too small for the conservative ceiling still blocks an approved call
    ledger.create("job:tiny", "job:tiny", 0.0001)
    with pytest.raises(BudgetExhausted):
        client.call("repair", req(), budget="job:tiny", approve_unknown_pricing=True)


def test_user_supplied_price_makes_a_model_known(client, store, ledger):
    c = store.create("openai", "O", api_key="k", models=[{"id": "gpt-x", "price": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}])
    store.set_adapter_override(c["connection_id"], MockProvider(["ok"]))
    store.set_route("repair", c["connection_id"], "gpt-x")
    ledger.create("job:j", "job:j", 1.0)
    res = client.call("repair", req(), budget="job:j")
    assert res.cost_known and res.cost_usd == pytest.approx((10 * 1.0 + 5 * 2.0) / 1e6)


def test_paid_call_without_a_budget_is_refused_but_local_is_free(client, store):
    a, b, ma, mb = two_routes(store)
    with pytest.raises(BudgetRequired):
        client.call("implementation", req(), budget=None)
    assert ma.calls == []
    loc = store.create("local", "LM", endpoint="http://localhost:1/v1", auth_mode="none", models=["m"])
    store.set_adapter_override(loc["connection_id"], MockProvider(["local ok"]))
    store.set_route("knowledge", loc["connection_id"], "m")
    res = client.call("knowledge", req(), budget=None)
    assert res.response.text == "local ok" and res.cost_usd == 0 and res.cost_known


def test_missing_usage_settles_at_the_reservation_not_zero(client, store, ledger):
    a, b, ma, mb = two_routes(store, a_script=[Response(provider="mock", model="m", text="t", tool_calls=[], stop_reason="end_turn",
                                                       usage=Usage(known=False))])
    ledger.create("job:j", "job:j", 1.0)
    res = client.call("implementation", req(), budget="job:j")
    assert res.cost_known is False and res.cost_usd > 0
    assert ledger.get("job:j")["spent_usd"] == pytest.approx(res.cost_usd)


def test_provider_reported_cost_wins(client, store, ledger):
    a, b, ma, mb = two_routes(store, a_script=[Response(provider="mock", model="m", text="t", tool_calls=[], stop_reason="end_turn",
                                                       usage=Usage(input_tokens=1, output_tokens=1, reported_cost_usd=0.0042))])
    ledger.create("job:j", "job:j", 1.0)
    assert client.call("implementation", req(), budget="job:j").cost_usd == pytest.approx(0.0042)


def test_no_route_is_a_typed_error_with_reasons(client, store):
    with pytest.raises(NoRoute, match="no route configured"):
        client.call("repair", req(), budget="x")
    a, b, *_ = two_routes(store)
    store.db.update("connections", "connection_id", a["connection_id"], {"capabilities": {"tools": "rejected"}})
    store.db.update("connections", "connection_id", b["connection_id"], {"capabilities": {"tools": "rejected"}})
    with pytest.raises(NoRoute, match="rejected tools"):
        client.call("implementation", req(tools=[Tool("t", "d", {"type": "object"})]), budget="x")


# -------------------------------------------------------------------------------------------- bounded retries
def test_rate_limit_is_retried_at_most_once_with_bounded_wait(client, store, ledger):
    a, b, ma, mb = two_routes(store, a_script=[RateLimit("slow", retry_after=2.0), "after wait"])
    ledger.create("job:j", "job:j", 1.0)
    res = client.call("implementation", req(), budget="job:j")
    assert res.response.text == "after wait" and len(ma.calls) == 2 and mb.calls == [] and client.sleeps == [2.0]
    assert [x["outcome"] for x in res.attempts] == ["rate_limit", "ok"]
    assert len(ledger.reservations("job:j", "released")) == 1 and len(ledger.reservations("job:j", "settled")) == 1
    assert ledger.get("job:j")["reserved_usd"] == 0


def test_retry_wait_is_capped(client, store, ledger):
    a, b, ma, mb = two_routes(store, a_script=[RateLimit("slow", retry_after=9999), "ok"])
    ledger.create("job:j", "job:j", 1.0)
    client.call("implementation", req(), budget="job:j")
    assert client.sleeps == [client.max_retry_wait_s]


def test_second_rate_limit_is_not_retried_again_and_falls_back(client, store, ledger):
    a, b, ma, mb = two_routes(store, a_script=[RateLimit("1"), RateLimit("2"), "never"], b_script=["fallback"])
    ledger.create("job:j", "job:j", 1.0)
    res = client.call("implementation", req(), budget="job:j")
    assert len(ma.calls) == 2 and ma.script == ["never"] and res.response.text == "fallback" and len(mb.calls) == 1
    with pytest.raises(AllCandidatesFailed) as ei:
        two = AIClient(store, ledger, client.events, client.db, sleep=lambda s: None)
        store.set_adapter_override(b["connection_id"], MockProvider([RateLimit("x"), RateLimit("y")]))
        store.set_adapter_override(a["connection_id"], MockProvider([RateLimit("x"), RateLimit("y")]))
        two.call("implementation", req(), budget="job:j")
    assert len(ei.value.attempts) == 4  # 2 candidates x (1 try + 1 resend): bounded


def test_timeout_is_never_resent_and_is_charged_in_full(client, store, ledger):
    a, b, ma, mb = two_routes(store, a_script=[Timeout("read timeout", retry_safe=False)], b_script=["via fallback"])
    ledger.create("job:j", "job:j", 1.0)
    res = client.call("implementation", req(), budget="job:j")
    assert len(ma.calls) == 1 and client.sleeps == []                           # no re-send to the same endpoint
    rsv = ledger.reservations("job:j", "settled")
    assert len(rsv) == 2                                                         # the timed-out request may have been billed
    timed_out = [r for r in rsv if r["usage"].get("assumed_spent")]
    assert len(timed_out) == 1 and timed_out[0]["actual_usd"] == pytest.approx(timed_out[0]["amount_usd"])
    assert [x["outcome"] for x in res.attempts] == ["timeout", "ok"]


def test_connect_timeout_confirms_not_sent_so_one_resend_is_allowed(client, store, ledger):
    a, b, ma, mb = two_routes(store, a_script=[Timeout("connect timeout", retry_safe=True, request_sent=False), "ok"])
    ledger.create("job:j", "job:j", 1.0)
    res = client.call("implementation", req(), budget="job:j")
    assert len(ma.calls) == 2 and res.response.text == "ok" and ledger.get("job:j")["spent_usd"] == pytest.approx(res.cost_usd)


def test_ambiguous_completion_not_retried_and_settled_at_ceiling_with_partial_usage_noted(client, store, ledger, db):
    a, b, ma, mb = two_routes(store, a_script=[AmbiguousCompletion("cut off", partial_usage=Usage(input_tokens=50, output_tokens=2))], b_script=["ok"])
    ledger.create("job:j", "job:j", 1.0)
    client.call("implementation", req(), budget="job:j")
    assert len(ma.calls) == 1
    amb = [r for r in ledger.reservations("job:j", "settled") if r["usage"].get("outcome") == "ambiguous"][0]
    assert amb["actual_usd"] == pytest.approx(amb["amount_usd"]) and amb["usage"]["partial_usage"]["input_tokens"] == 50
    row = db.query_one("SELECT * FROM ai_calls WHERE outcome='ambiguous'")
    assert row["cost_known"] == 0 and row["input_tokens"] == 50


def test_ambiguous_stops_at_once_when_fallback_after_ambiguity_is_disabled(client, store, ledger):
    a, b, ma, mb = two_routes(store, a_script=[AmbiguousCompletion("cut off")], b_script=["would double spend"])
    client.fallback_on_ambiguous = False
    ledger.create("job:j", "job:j", 1.0)
    with pytest.raises(AllCandidatesFailed):
        client.call("implementation", req(), budget="job:j")
    assert mb.calls == []


def test_auth_failure_releases_marks_state_and_falls_back(client, store, ledger):
    a, b, ma, mb = two_routes(store, a_script=[AuthError("bad key")], b_script=["B answers"])
    ledger.create("job:j", "job:j", 1.0)
    res = client.call("implementation", req(), budget="job:j")
    assert res.response.text == "B answers" and len(ma.calls) == 1
    assert store.get(a["connection_id"])["state"] == "auth_failed" and store.get(b["connection_id"])["state"] == "ok"
    assert [x["connection_id"] for x in [{"connection_id": c["connection_id"]} for c, _ in store.resolve("implementation")]] == [b["connection_id"]]
    assert len(ledger.reservations("job:j", "released")) == 1
    assert ledger.get("job:j")["spent_usd"] == pytest.approx(res.cost_usd)  # the failed auth cost nothing


def test_usage_limit_marks_connection_limited_without_resend(client, store, ledger):
    a, b, ma, mb = two_routes(store, a_script=[UsageLimit("quota exhausted")], b_script=["ok"])
    ledger.create("job:j", "job:j", 1.0)
    client.call("implementation", req(), budget="job:j")
    assert len(ma.calls) == 1 and client.sleeps == [] and store.get(a["connection_id"])["state"] == "limited"


@pytest.mark.parametrize("err,outcome", [(InvalidRequest("bad"), "invalid_request"), (ProviderUnavailable("503"), "unavailable"),
                                         (Unreachable("down"), "unreachable")])
def test_other_failures_release_the_reservation(client, store, ledger, err, outcome):
    a, b, ma, mb = two_routes(store, a_script=[err, err], b_script=["ok"])
    ledger.create("job:j", "job:j", 1.0)
    res = client.call("implementation", req(), budget="job:j")
    assert res.attempts[0]["outcome"] == outcome and res.response.text == "ok"
    assert all(r["actual_usd"] == 0 for r in ledger.reservations("job:j", "released"))


def test_all_candidates_failing_raises_with_every_attempt(client, store, ledger):
    a, b, ma, mb = two_routes(store, a_script=[AuthError("x")], b_script=[ProviderUnavailable("y")])
    ledger.create("job:j", "job:j", 1.0)
    with pytest.raises(AllCandidatesFailed) as ei:
        client.call("implementation", req(), budget="job:j")
    assert [x["outcome"] for x in ei.value.attempts] == ["auth_failed", "unavailable"] and isinstance(ei.value.last, ProviderUnavailable)
    assert ledger.get("job:j")["spent_usd"] == 0 and ledger.get("job:j")["reserved_usd"] == 0


# -------------------------------------------------------------------------------------------- redaction
def test_secrets_never_reach_events_logs_or_ai_calls(client, store, ledger, db, events, caplog):
    a, b, ma, mb = two_routes(store, a_script=[], b_script=["ok"])
    err = AuthError("placeholder")
    err.args = (f"upstream rejected key {KEY_A} (header x-api-key: {KEY_A})",)  # a message that leaked a key past the adapter
    ma.script = [err]
    ledger.create("job:j", "job:j", 1.0)
    with caplog.at_level(logging.DEBUG):
        res = client.call("implementation", req(), budget="job:j")
    blob = repr(events.events_since(0)) + repr(db.query("SELECT * FROM ai_calls")) + repr(res.attempts) + caplog.text + repr(db.query("SELECT * FROM reservations"))
    assert KEY_A not in blob and KEY_B not in blob
    assert "[REDACTED]" in repr(events.events_since(0))


def test_real_adapter_error_bodies_that_echo_the_key_are_redacted_end_to_end(store, ledger, events, db, caplog):
    body = {"type": "error", "error": {"type": "authentication_error", "message": f"invalid x-api-key {KEY_A}"}}
    store.transport = Recorder(lambda r: json_response(body, status=401)).transport
    c = store.create("anthropic", "A", api_key=KEY_A, models=["claude-sonnet-5-5"])
    store.set_route("repair", c["connection_id"], "claude-sonnet-5-5")
    ledger.create("job:j", "job:j", 1.0)
    client = AIClient(store, ledger, events, db, sleep=lambda s: None)
    with caplog.at_level(logging.DEBUG), pytest.raises(AllCandidatesFailed) as ei:
        client.call("repair", req(), budget="job:j")
    assert KEY_A not in repr(events.events_since(0)) + caplog.text + str(ei.value) + repr(ei.value.attempts) + str(ei.value.last)
    assert store.get(c["connection_id"])["state"] == "auth_failed"


# -------------------------------------------------------------------------------------------- advisor
class FixedAdvisor:
    def __init__(self, order, raises=False):
        self.order, self.raises, self.seen = order, raises, []

    def advise(self, task, candidates, summary, **kw):
        self.seen.append((candidates, summary))
        if self.raises:
            raise RuntimeError("advisor down")
        from rebuild_controller.providers.jev import AdvisorDecision
        return AdvisorDecision(order=self.order, source="jev", confidence=0.9)


def test_advisor_reorders_only_among_resolved_candidates(client, store, ledger):
    a, b, ma, mb = two_routes(store, a_script=["A"], b_script=["B"])
    adv = FixedAdvisor([1, 0])
    client.advisor = adv
    ledger.create("job:j", "job:j", 1.0)
    res = client.call("implementation", req("secret case text"), budget="job:j")
    assert res.response.text == "B" and ma.calls == [] and res.advisor["source"] == "jev"
    cands, summary = adv.seen[0]
    assert [c["index"] for c in cands] == [0, 1] and "secret case text" not in repr(adv.seen)   # only metadata is shared


@pytest.mark.parametrize("advisor", [FixedAdvisor(None), FixedAdvisor([5, 6]), FixedAdvisor([0, 0]), FixedAdvisor(None, raises=True)])
def test_advisor_failure_or_nonsense_keeps_the_deterministic_order(client, store, ledger, advisor):
    a, b, ma, mb = two_routes(store, a_script=["A"], b_script=["B"])
    client.advisor = advisor
    ledger.create("job:j", "job:j", 1.0)
    assert client.call("implementation", req(), budget="job:j").response.text == "A"


# -------------------------------------------------------------------------------------------- reuse demo & real adapter
def test_raising_provider_proves_zero_ai_calls_for_a_reuse_path(client, store, ledger, db):
    c = store.create("anthropic", "A", api_key=KEY_A, models=["claude-sonnet-5-5"])
    raising = RaisingProvider()
    store.set_adapter_override(c["connection_id"], raising)
    store.set_route("knowledge", c["connection_id"], "claude-sonnet-5-5")
    ledger.create("job:reuse", "job:reuse", 1.0)
    # a pipeline that reuses promoted knowledge never touches the client, so the raising adapter stays unused ...
    assert raising.attempts == 0 and db.query("SELECT * FROM ai_calls") == []
    # ... and if any code path did call, the adapter would blow up (and the unproven spend is accounted conservatively)
    with pytest.raises(AICallAttempted):
        client.call("knowledge", req(), budget="job:reuse")
    assert raising.attempts == 1
    assert ledger.get("job:reuse")["reserved_usd"] == 0 and db.query_one("SELECT outcome FROM ai_calls")["outcome"] == "internal_error"


def test_end_to_end_with_real_anthropic_adapter_over_mock_transport(store, ledger, events, db):
    rec = Recorder(lambda r: anthropic_stream())
    store.transport = rec.transport
    c = store.create("anthropic", "A", api_key=KEY_A, models=["claude-sonnet-5-5"])
    store.set_route("implementation", c["connection_id"], "claude-sonnet-5-5")
    ledger.create("case:c1", "case:c1", 0.05)
    res = AIClient(store, ledger, events, db).call("implementation", req("analyse this"), case_id="c1", budget="case:c1")
    assert res.response.text == "Hi there"
    assert res.cost_usd == pytest.approx((25 * 2 + 100 * 0.2 + 10 * 2.5 + 42 * 10) / 1e6) and res.cost_known
    assert rec.requests[0].headers["x-api-key"] == KEY_A
    assert ledger.get("case:c1")["spent_usd"] == pytest.approx(res.cost_usd)
    assert store.get(c["connection_id"])["state"] == "ok"


def test_estimate_is_generous_and_counts_images(store):
    from rebuild_controller.providers.base import ImagePart, TextPart
    r1 = req("x" * 3000)
    assert estimate_request_tokens(r1) >= 1000
    r2 = Request(model="", messages=[Message("user", [ImagePart.from_bytes(b"x"), TextPart("hi")])])
    assert estimate_request_tokens(r2) >= 2000
