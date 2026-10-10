"""R9 granular AI control (docs/AI_LADDER.md section 9): task migration, per-rung failure rules with a scripted provider,
provider cooldowns, the dry-run route endpoint and the JeV / cooldown API. Mock adapters only: no network, no spend."""
import json
import types
import warnings

import pytest
from fastapi.testclient import TestClient

from rebuild_controller.budget import BudgetLedger
from rebuild_controller.providers import MockProvider, RateLimit, Request
from rebuild_controller.providers.base import CreditsExhausted, ContextWindowExceeded, Message, UsageLimit
from rebuild_controller.providers.connections import TASKS, ConnectionStore, ConnectionStoreError
from rebuild_controller.providers.ladder import ladder_view, put_ladder, validate_rules
from rebuild_controller.providers.router import AIClient, AllCandidatesFailed, NoRoute, StopRequested
from rebuild_controller.providers.secrets import SecretStore

KEY = "sk-ant-NOT-REAL-rrrrrrrrrrrrrrrrrrrr"
PRICED = {"input_per_mtok": 1.0, "output_per_mtok": 2.0}
TOKEN = "t" * 32
T0 = 1_790_000_000.0


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
    s = ConnectionStore(db, events, settings, secrets=secret_store, ledger=ledger)
    clock = {"t": T0}
    s.clock = lambda: clock["t"]
    s.tick = clock          # type: ignore[attr-defined]
    return s


@pytest.fixture
def client(store, ledger, events, db):
    sleeps: list[float] = []
    c = AIClient(store, ledger, events, db, sleep=sleeps.append)
    c.sleeps = sleeps       # type: ignore[attr-defined]
    ledger.create("job:j", "job:j", 5.0)
    return c


def req(text="hello"):
    return Request(model="", messages=[Message.user(text)], max_output_tokens=100)


def conn(store, label, model, script, price=PRICED, provider="anthropic"):
    c = store.create(provider, label, api_key=KEY, models=[{"id": model, "price": price}])
    m = MockProvider(script)
    store.set_adapter_override(c["connection_id"], m)
    return c, m


def ladder(store, task, *pairs, rules=None):
    entries = [{"connection_id": c["connection_id"], "model": m, **({"rules": (rules or {}).get(i, {})} if rules is not None else {})}
               for i, (c, m) in enumerate(pairs)]
    return put_ladder(store, task, entries)


def activity(events):
    return [json.loads(r["payload"])["text"] for r in events.db.query("SELECT payload FROM events WHERE kind='ai.activity' ORDER BY seq")]


# =============================================================================================== (a) tasks + migration
def test_tasks_match_what_the_app_does():
    assert TASKS == ("implementation", "repair", "naming", "visual_review", "verification_assist", "knowledge")


def test_legacy_interpretation_ladder_is_migrated_with_a_config_revision(db, events, settings, secret_store, ledger, cases, src_out):
    first = ConnectionStore(db, events, settings, secrets=secret_store, ledger=ledger)
    a = first.create("anthropic", "A", api_key=KEY, models=["claude-haiku-4-5"])
    # a pre-R9 database: the route row, a revision snapshot and a project override still say "interpretation"
    db.insert("task_routes", {"task": "interpretation", "primary_connection": a["connection_id"], "primary_model": "claude-haiku-4-5",
                              "fallbacks": [], "updated_at": "2026-01-01T00:00:00Z"})
    db.insert("ai_config_revisions", {"revision": 41, "created_at": "2026-01-01T00:00:00Z", "reason": "old",
                                      "snapshot": {"tasks": {"interpretation": {"entries": [], "rationale": "preset:local_first"}}}})
    db.insert("ai_rung_rules", {"task": "interpretation", "connection_id": a["connection_id"], "model": "claude-haiku-4-5",
                                "rules": {"rate_limit": {"action": "stop"}}, "updated_at": "x"})
    src, out = src_out
    case = cases.create_case(name="n", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe",
                             ai_policy={"mode": "custom", "ladder_overrides": {"interpretation": [{"connection_id": a["connection_id"],
                                                                                                    "model": "claude-haiku-4-5"}]}})
    db.execute("DELETE FROM meta WHERE key='ai_tasks_r9'")             # the case predates the migration
    store = ConnectionStore(db, events, settings, secrets=secret_store, ledger=ledger)
    assert store.get_route("implementation")["primary_model"] == "claude-haiku-4-5"
    assert db.query_one("SELECT task FROM task_routes WHERE task='interpretation'") is None
    snap = store.revision_snapshot()
    assert snap["revision"] == 42 and "renamed to 'implementation'" in snap["reason"]
    assert snap["tasks"]["implementation"]["rationale"] == "preset:local_first" and "interpretation" not in snap["tasks"]
    assert store.rules_for("implementation", a["connection_id"], "claude-haiku-4-5") == {"rate_limit": {"action": "stop"}}
    pol = json.loads(db.query_one("SELECT ai_policy FROM cases WHERE case_id=?", (case["case_id"],))["ai_policy"])
    assert set(pol["ladder_overrides"]) == {"implementation"}
    # idempotent: a restart changes nothing
    ConnectionStore(db, events, settings, secrets=secret_store, ledger=ledger)
    assert store.config_revision() == 42
    # the old task name is still accepted as an alias by every entry point
    assert store.route_entries("interpretation") == [(a["connection_id"], "claude-haiku-4-5")]
    assert put_ladder(store, "interpretation", [])["task"] == "implementation" and store.route_entries("implementation") == []


# =============================================================================================== (b) per-rung failure rules
def test_rules_validation_is_plain_and_strict():
    assert validate_rules({"rate_limit": {"action": "next"}}) == {}
    assert validate_rules({"rate_limit": "stop"}) == {"rate_limit": {"action": "stop"}}
    assert validate_rules({"usage_limit": {"action": "wait", "wait_minutes": 10, "max_tries": 2}}) == \
        {"usage_limit": {"action": "wait", "wait_minutes": 10.0, "max_tries": 2}}
    for bad, msg in [({"nope": "stop"}, "unknown failure kind"), ({"rate_limit": "panic"}, "unknown action"),
                     ({"auth_failed": {"action": "wait"}}, "waiting does not help"),
                     ({"rate_limit": {"action": "wait", "wait_minutes": 0}}, "wait_minutes"),
                     ({"rate_limit": {"action": "wait", "max_tries": 99}}, "max_tries")]:
        with pytest.raises(ConnectionStoreError, match=msg):
            validate_rules(bad)


def test_rules_follow_the_rung_and_are_in_the_ladder_view_and_snapshot(store):
    a, _ = conn(store, "A", "m-a", [])
    b, _ = conn(store, "B", "m-b", [])
    ladder(store, "repair", (a, "m-a"), (b, "m-b"), rules={0: {"credits_exhausted": "stop"}})
    v = ladder_view(store)
    ents = v["tasks"]["repair"]["entries"]
    assert ents[0]["rules"] == {"credits_exhausted": {"action": "stop"}} and ents[1]["rules"] == {}
    assert v["tasks"]["repair"]["chain"].startswith("Use m-a (A); if it hits a limit or fails, use m-b (B)")
    assert {k["kind"] for k in v["rules_meta"]["failure_kinds"]} >= {"credits_exhausted", "usage_limit", "rate_limit", "context_exceeded"}
    assert store.revision_snapshot()["tasks"]["repair"]["entries"][0]["rules"] == {"credits_exhausted": {"action": "stop"}}
    # reorder without "rules" keys: rules stay with their rung
    put_ladder(store, "repair", [{"connection_id": b["connection_id"], "model": "m-b"}, {"connection_id": a["connection_id"], "model": "m-a"}])
    ents = ladder_view(store)["tasks"]["repair"]["entries"]
    assert ents[1]["model"] == "m-a" and ents[1]["rules"] == {"credits_exhausted": {"action": "stop"}}
    with pytest.raises(ConnectionStoreError):
        ladder(store, "repair", (a, "m-a"), rules={0: {"bogus": "stop"}})


def test_credits_exhausted_starts_a_cooldown_and_moves_to_the_next_rung_for_every_task(client, store, ledger, events):
    a, ma = conn(store, "Claude", "claude-x", [CreditsExhausted("402 no credits", status=402), "never"])
    b, mb = conn(store, "GPT", "gpt-x", ["B1", "B2", "B3"], provider="openai")
    ladder(store, "implementation", (a, "claude-x"), (b, "gpt-x"))
    ladder(store, "repair", (a, "claude-x"), (b, "gpt-x"))
    res = client.call("implementation", req(), budget="job:j")
    assert res.response.text == "B1" and res.attempts[0]["outcome"] == "credits_exhausted" and res.attempts[0]["cooldown_until"]
    cool = store.cooldown(a["connection_id"])
    assert cool["outcome"] == "credits_exhausted" and cool["until_ts"] == pytest.approx(T0 + 24 * 3600)
    # another task skips the paused provider without sending anything
    res2 = client.call("repair", req(), budget="job:j")
    assert res2.response.text == "B2" and len(ma.calls) == 1 and res2.attempts[0]["outcome"] == "cooldown"
    assert "every task skips it until" in res2.attempts[0]["reason"] and any("trying gpt-x" in t for t in activity(events))
    assert [c["connection_id"] for c in store.cooldowns()] == [a["connection_id"]]
    assert ladder_view(store)["tasks"]["repair"]["entries"][0]["cooldown"]["outcome"] == "credits_exhausted"
    # the user clears it: the provider is tried again
    assert store.clear_cooldown(a["connection_id"]) is True and store.cooldown(a["connection_id"]) is None
    assert client.call("repair", req(), budget="job:j").response.text == "never"


def test_cooldown_ends_by_itself_and_respects_the_setting(client, store):
    a, ma = conn(store, "Claude", "claude-x", [UsageLimit("daily limit"), "later"])
    b, mb = conn(store, "GPT", "gpt-x", ["B1"], provider="openai")
    ladder(store, "repair", (a, "claude-x"), (b, "gpt-x"))
    client.call("repair", req(), budget="job:j")
    assert store.cooldown(a["connection_id"])["until_ts"] == pytest.approx(T0 + 60 * 60)        # usage limit: 60 min by default
    store.tick["t"] += 3601
    assert store.cooldown(a["connection_id"]) is None and client.call("repair", req(), budget="job:j").response.text == "later"
    store.put_setting("cooldown_minutes", {"usage_limit": 0})                                  # 0 = never pause
    ma.script = [UsageLimit("again")]
    mb.script = ["B2"]
    client.call("repair", req(), budget="job:j")
    assert store.cooldown(a["connection_id"]) is None


def test_free_tier_cooldown_does_not_pause_paid_models_on_the_same_connection(client, store):
    c = store.create("openrouter", "OpenRouter", api_key=KEY, models=[{"id": "llama:free", "price": {"input_per_mtok": 0, "output_per_mtok": 0}},
                                                                       {"id": "paid", "price": PRICED}])
    m = MockProvider([CreditsExhausted("free tier used up", status=402), "paid answer"])
    store.set_adapter_override(c["connection_id"], m)
    ladder(store, "repair", (c, "llama:free"), (c, "paid"))
    assert client.call("repair", req(), budget="job:j").response.text == "paid answer"
    cool = store.cooldown(c["connection_id"])
    assert cool["scope"] == "free_models" and store.cooldown(c["connection_id"], "paid") is None
    assert store.cooldown(c["connection_id"], "llama:free") is not None


def test_rate_limit_rule_waits_and_retries_the_same_rung(client, store, ledger):
    a, ma = conn(store, "Claude", "claude-x", [RateLimit("429"), RateLimit("429"), "A after waiting"])
    b, mb = conn(store, "GPT", "gpt-x", ["never"], provider="openai")
    ladder(store, "repair", (a, "claude-x"), (b, "gpt-x"), rules={0: {"rate_limit": {"action": "wait", "wait_minutes": 2, "max_tries": 2}}})
    res = client.call("repair", req(), budget="job:j")
    assert res.response.text == "A after waiting" and client.sleeps == [120.0, 120.0] and mb.calls == []
    assert [x["outcome"] for x in res.attempts] == ["rate_limit", "rate_limit", "ok"]
    assert ledger.get("job:j")["reserved_usd"] == 0
    # tries used up -> the next rung (no extra default re-send)
    client.sleeps.clear()
    ma.script = [RateLimit("429")] * 3
    mb.script = ["B"]
    res2 = client.call("repair", req(), budget="job:j")
    assert res2.response.text == "B" and client.sleeps == [120.0, 120.0] and len(ma.calls) == 3 + 3


def test_stop_rule_stops_with_a_plain_message_and_never_tries_the_next_rung(client, store, events):
    a, ma = conn(store, "Claude", "claude-x", [CreditsExhausted("402", status=402)])
    b, mb = conn(store, "GPT", "gpt-x", ["never"], provider="openai")
    ladder(store, "repair", (a, "claude-x"), (b, "gpt-x"), rules={0: {"credits_exhausted": "stop"}})
    with pytest.raises(StopRequested) as ei:
        client.call("repair", req(), budget="job:j")
    e = ei.value
    assert isinstance(e, AllCandidatesFailed) and mb.calls == [] and e.kind == "credits_exhausted"
    assert e.message.startswith("Stopped and waiting for you: Claude has run out of credits") and "says to stop" in e.message
    assert "add credits to Claude" in e.message and activity(events)[-1] == e.message
    assert store.cooldown(a["connection_id"]) is not None


def test_context_exceeded_is_its_own_rule_kind(client, store):
    a, ma = conn(store, "Local", "small", [ContextWindowExceeded("prompt too long")])
    b, mb = conn(store, "GPT", "gpt-x", ["never"], provider="openai")
    ladder(store, "repair", (a, "small"), (b, "gpt-x"), rules={0: {"context_exceeded": "stop"}})
    with pytest.raises(StopRequested) as ei:
        client.call("repair", req(), budget="job:j")
    assert ei.value.kind == "context_exceeded" and mb.calls == []


def test_stop_rule_becomes_a_plain_case_blocker_in_the_implement_loop(settings, tmp_path):
    from rebuild_controller import implement as I
    from rebuild_controller.services import StudioServices
    st = StudioServices(settings)
    st.ai.advisor = None
    try:
        a, ma = conn(st.connections, "Claude", "claude-x", [CreditsExhausted("402", status=402)])
        b, mb = conn(st.connections, "GPT", "gpt-x", ["never"], provider="openai")
        ladder(st.connections, "implementation", (a, "claude-x"), (b, "gpt-x"), rules={0: {"credits_exhausted": "stop"}})
        src = tmp_path / "src"; src.mkdir(); (src / "a.txt").write_text("x")
        case = st.cases.create_case(name="demo", source_root=str(src), output_root=str(tmp_path / "out"), target_language="rust",
                                    output_type="exe", ai_policy={"mode": "assisted", "budget_usd": 1.0})
        pol = I.LoopPolicy.from_case(case)
        ctx = types.SimpleNamespace(job=types.SimpleNamespace(job_id="job_x"), heartbeat=lambda force=False: None)
        with pytest.raises(I.ImplementStop) as ei:
            I.ask_model(st, ctx, case, pol, task="implementation", system="s", prompt="p", key="loop:a1")
        assert ei.value.code == "stopped_by_rule" and ei.value.message.startswith("Stopped and waiting for you:") and mb.calls == []
    finally:
        st.stop()


# =============================================================================================== (c) dry run
def test_dry_run_explains_which_rung_answers_now_without_sending(client, store, ledger, db):
    a, ma = conn(store, "Claude", "claude-x", [])
    b, mb = conn(store, "GPT", "gpt-x", [], provider="openai")
    loc = store.create("local", "Ollama (this PC)", endpoint="http://127.0.0.1:11434/v1", auth_mode="local",
                       models=[{"id": "qwen2.5-coder:14b"}])
    ladder(store, "implementation", (a, "claude-x"), (b, "gpt-x"), (loc, "qwen2.5-coder:14b"))
    store.set_cooldown(a["connection_id"], "usage_limit", reason="Claude hit its usage limit")
    out = client.dry_run("implementation", budget="job:j")
    assert out["sent"] is False and out["tokens_spent"] == 0 and ma.calls == mb.calls == []
    assert [r["status"] for r in out["rungs"]] == ["skipped", "would_answer", "standby"] and out["answer"] == 2
    assert out["rungs"][0]["outcome"] == "cooldown" and "every task skips it" in out["rungs"][0]["reason"]
    assert "Right now position 2 (gpt-x, GPT) would answer" in out["summary"]
    assert out["chain"] == "Use claude-x (Claude); if it hits a limit or fails, use gpt-x (GPT); then local qwen2.5-coder:14b."
    assert db.query("SELECT * FROM ai_calls") == [] and ledger.reservations("job:j") == []
    # a budget too small for the cloud rungs: only the free local model would answer
    ledger.create("job:tiny", "job:tiny", 0.000001)
    out2 = client.dry_run("implementation", budget="job:tiny")
    assert [r["outcome"] for r in out2["rungs"]] == ["cooldown", "budget_exhausted", "ok"] and out2["answer"] == 3
    # project policy "no AI": nothing would be asked
    assert client.dry_run("implementation", policy={"mode": "no_ai"})["answer"] is None


# =============================================================================================== API
@pytest.fixture
def api(settings, tmp_path):
    from rebuild_controller.api.server import create_app
    from rebuild_controller.providers import jev as J
    from rebuild_controller.services import StudioServices
    import httpx
    st = StudioServices(settings)
    replies = []

    def handler(q):
        replies.append(json.loads(q.content))
        return httpx.Response(200, json={"model": "jev-1.13.0", "usage": {"input_tokens": 50, "output_tokens": 2},
                                         "answers": {"check": {"type": "choice", "choice": "OK", "probabilities": {"OK": 1.0, "NOT_OK": 0.0}}}})
    st.ai.advisor = J.JeVRouter(st.budgets, st.events, settings.data_dir, transport=httpx.MockTransport(handler), env={
        "JEV_SECRET_FILE": str(tmp_path / "jev" / "typesafe.key")})
    st.jev_requests = replies       # type: ignore[attr-defined]
    with TestClient(create_app(st, TOKEN)) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c, st
    st.stop()


def test_jev_card_api_key_import_settings_and_test(api, tmp_path):
    c, st = api
    s0 = c.get("/ai/jev").json()
    assert s0["has_key"] is False and s0["offline_reason"] == "no_key" and s0["model"] == "jev-1.13.0" and s0["monthly_cap_usd"] == 1.0
    assert s0["jev_install"]["key_file_found"] is False
    assert c.post("/ai/jev/key/import").status_code == 404
    kf = tmp_path / "jev" / "typesafe.key"
    kf.parent.mkdir(parents=True)
    kf.write_text("apikey_NOT_REAL_from_install_0123\n")
    r = c.post("/ai/jev/key/import")
    assert r.status_code == 200 and r.json()["key_source"] == "jev_install" and "apikey_NOT_REAL" not in r.text
    r = c.put("/ai/jev/key", json={"key": "apikey_NOT_REAL_entered_456789"})
    assert r.json()["has_key"] is True and r.json()["key_source"] == "entered" and "apikey_NOT_REAL" not in r.text
    # the shared credential store holds it (one store for the app)
    assert st.connections.secrets.get("secret:jev-advisor") == "apikey_NOT_REAL_entered_456789"
    assert c.put("/ai/jev", json={"monthly_cap_usd": 99}).status_code == 400
    s = c.put("/ai/jev", json={"monthly_cap_usd": 2, "enabled": False}).json()
    assert s["monthly_cap_usd"] == 2 and s["enabled"] is False and s["offline_reason"] == "off"
    t = c.post("/ai/jev/test").json()
    assert t["ok"] is True and st.jev_requests[-1]["model"] == "jev-1.13.0"
    assert c.put("/ai/jev/key", json={"key": None}).json()["has_key"] is False
    dump = repr(st.db.query("SELECT * FROM events")) + repr(st.db.query("SELECT * FROM ai_calls"))
    assert "apikey_NOT_REAL" not in dump


def test_cooldown_rules_and_route_test_api(api):
    c, st = api
    a = st.connections.create("anthropic", "Claude", api_key=KEY, models=[{"id": "claude-x", "price": PRICED}])
    b = st.connections.create("openai", "GPT", api_key="sk-NOT-REAL-oooooooooooooooooooo", models=[{"id": "gpt-x", "price": PRICED}])
    r = c.put("/ai/ladder/implementation", json={"entries": [
        {"connection_id": a["connection_id"], "model": "claude-x", "rules": {"usage_limit": {"action": "wait", "wait_minutes": 5, "max_tries": 2}}},
        {"connection_id": b["connection_id"], "model": "gpt-x", "rules": {}}]})
    assert r.status_code == 200 and r.json()["entries"][0]["rules"]["usage_limit"]["action"] == "wait"
    bad = c.put("/ai/ladder/implementation", json={"entries": [{"connection_id": a["connection_id"], "model": "claude-x", "rules": {"x": "stop"}}]})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "ladder"
    assert c.get("/ai/rules").json()["actions"] == ["next", "wait", "stop"]
    st.connections.set_cooldown(a["connection_id"], "credits_exhausted", reason="no credits")
    cd = c.get("/ai/cooldowns").json()
    assert cd["cooldowns"][0]["connection_id"] == a["connection_id"] and cd["settings"]["cooldown_minutes"]["credits_exhausted"] == 1440
    t = c.post("/ai/route/test", json={"task": "implementation"}).json()
    assert t["answer"] == 2 and t["rungs"][0]["status"] == "skipped" and t["tokens_spent"] == 0
    assert c.delete(f"/ai/cooldowns/{a['connection_id']}").json()["cleared"] is True
    assert c.post("/ai/route/test", json={"task": "interpretation"}).json()["answer"] == 1        # legacy alias accepted
    assert c.post("/ai/route/test", json={"task": "nope"}).status_code == 400
    s = c.put("/ai/cooldowns/settings", json={"cooldown_minutes": {"usage_limit": 15}}).json()
    assert s["settings"]["cooldown_minutes"]["usage_limit"] == 15
    assert c.put("/ai/cooldowns/settings", json={"cooldown_minutes": {"bogus": 1}}).status_code == 400
    lad = c.get("/ai/ladder").json()
    assert lad["task_ids"] == list(TASKS) and "naming" in lad["tasks"] and lad["tasks"]["implementation"]["chain"]
