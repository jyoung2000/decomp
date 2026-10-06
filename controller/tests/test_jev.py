"""JeV advisory router: env-only config, capped spend, cached decisions, deterministic fallback. Mock transports only."""
import json
import threading
import warnings

import httpx
import pytest

from rebuild_controller.budget import BudgetLedger
from rebuild_controller.providers import MockProvider, Request
from rebuild_controller.providers import jev as J
from rebuild_controller.providers.base import Message
from rebuild_controller.providers.connections import ConnectionStore
from rebuild_controller.providers.router import AIClient
from rebuild_controller.providers.secrets import SecretStore

from test_providers import Recorder, chat_chunks, json_response

JEV_KEY = "jev-NOT-REAL-key-0123456789abcdef"
NOW = 1_790_000_000.0  # 2026-09-21 in epoch seconds; fixed so monthly budget ids are deterministic
MONTH = "jev:monthly:2026-09"

CANDS = [{"index": 0, "connection_id": "c0", "provider": "anthropic", "model": "claude-sonnet-5-5", "price_known": True,
          "input_per_mtok": 2.0, "output_per_mtok": 10.0},
         {"index": 1, "connection_id": "c1", "provider": "anthropic", "model": "claude-haiku-4-5", "price_known": True,
          "input_per_mtok": 1.0, "output_per_mtok": 5.0}]
SUMMARY = {"task": "repair", "needs": ["tools"], "est_input_tokens": 500, "max_output_tokens": 100, "messages": 1}


def env(**over):
    e = {"JEV_API_KEY": JEV_KEY, "JEV_ENDPOINT": "https://jev.example.test/v1", "JEV_MODEL": "jev-advisor-1",
         "JEV_PRICE_INPUT_PER_MTOK": "0.5", "JEV_PRICE_OUTPUT_PER_MTOK": "1.5"}
    e.update(over)
    return {k: v for k, v in e.items() if v is not None}


def advice(order=(1, 0), confidence=0.9, reason="cheaper model suffices"):
    return chat_chunks(json.dumps({"order": list(order), "confidence": confidence, "reason": reason}))


@pytest.fixture
def ledger(db, events):
    return BudgetLedger(db, events)


def make(ledger, events, tmp_path, reply=None, environ=None, **kw):
    rec = Recorder(reply or (lambda r: advice()))
    r = J.JeVRouter(ledger, events, tmp_path, env=env() if environ is None else environ, transport=rec.transport, now=lambda: NOW, **kw)
    return r, rec


# =============================================================================================== config
def test_config_is_environment_only_and_flags_itself_unverified():
    cfg = J.JeVConfig.from_env(env())
    assert cfg.enabled and cfg.unverified is True and cfg.model == "jev-advisor-1" and cfg.endpoint == "https://jev.example.test/v1"
    assert JEV_KEY not in repr(cfg)
    assert cfg.price().known and cfg.price().input_per_mtok == 0.5 and "unverified" in cfg.price().source
    empty = J.JeVConfig.from_env({})
    assert not empty.enabled and empty.missing() == ["JEV_API_KEY", "JEV_ENDPOINT", "JEV_MODEL"]
    assert not J.JeVConfig.from_env(env(JEV_API_KEY=None)).enabled
    # prices absent / malformed => unknown (never zero)
    for bad in (dict(JEV_PRICE_INPUT_PER_MTOK=None), dict(JEV_PRICE_OUTPUT_PER_MTOK="abc"), dict(JEV_PRICE_INPUT_PER_MTOK="-1")):
        p = J.JeVConfig.from_env(env(**bad)).price()
        assert p.known is False and p.input_per_mtok > 0 and p.approval_required


def test_caps_are_fixed_by_code_and_not_raisable_from_env(ledger, events, tmp_path):
    r, _ = make(ledger, events, tmp_path, environ=env(JEV_SETUP_CAP="50", JEV_MONTHLY_CAP="500", JEV_MONTHLY_CAP_USD="500"))
    st = r.status()
    assert (J.SETUP_CAP_USD, J.MONTHLY_CAP_USD) == (0.05, 1.00)
    assert st["setup"]["limit_usd"] == 0.05 and st["monthly"]["limit_usd"] == 1.0 and st["monthly"]["budget_id"] == MONTH
    assert st["unverified"] is True and st["enabled"] is True
    assert {b["budget_id"] for b in ledger.list("jev:")} == {"jev:setup", MONTH}


# =============================================================================================== fallback
def test_absent_jev_is_a_deterministic_fallback_with_no_network(ledger, events, tmp_path):
    r, rec = make(ledger, events, tmp_path, environ={})
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order is None and d.source == "fallback" and d.reason.startswith("jev_not_configured") and rec.requests == []
    assert r.status()["enabled"] is False and r.setup()["reason"] == "not_configured"


def test_router_keeps_deterministic_order_when_jev_is_absent(db, events, settings, ledger, tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        store = ConnectionStore(db, events, settings, secrets=SecretStore(tmp_path / "s"), ledger=ledger)
    a = store.create("anthropic", "A", api_key="sk-ant-NOT-REAL-aaaaaaaaaaaaaaaaaaaa", models=["claude-sonnet-5-5"])
    b = store.create("anthropic", "B", api_key="sk-ant-NOT-REAL-bbbbbbbbbbbbbbbbbbbb", models=["claude-haiku-4-5"])
    ma, mb = MockProvider(["A"]), MockProvider(["B"])
    store.set_adapter_override(a["connection_id"], ma)
    store.set_adapter_override(b["connection_id"], mb)
    store.set_route("repair", a["connection_id"], "claude-sonnet-5-5", [{"connection": b["connection_id"], "model": "claude-haiku-4-5"}])
    ledger.create("job:j", "job:j", 1.0)
    jev, rec = make(ledger, events, tmp_path, environ={})
    res = AIClient(store, ledger, events, db, advisor=jev).call("repair", Request(model="", messages=[Message.user("secret case text")], max_output_tokens=50),
                                                                 budget="job:j")
    assert res.response.text == "A" and res.advisor["source"] == "fallback" and rec.requests == [] and mb.calls == []


def test_single_candidate_never_calls_jev(ledger, events, tmp_path):
    r, rec = make(ledger, events, tmp_path)
    assert r.advise("repair", CANDS[:1], SUMMARY).reason == "single_candidate" and rec.requests == []


# =============================================================================================== advice, spend, caching
def test_advice_reorders_spends_from_monthly_budget_and_logs(ledger, events, db, tmp_path):
    r, rec = make(ledger, events, tmp_path)
    d = r.advise("repair", CANDS, SUMMARY, case_id="c1")
    assert d.order == [1, 0] and d.source == "jev" and d.confidence == 0.9 and d.unverified and not d.cached
    m = ledger.get(MONTH)
    assert 0 < m["spent_usd"] < 0.001 and m["reserved_usd"] == 0 and ledger.get("jev:setup")["spent_usd"] == 0
    assert d.spent_usd == pytest.approx(m["spent_usd"])
    row = db.query_one("SELECT * FROM ai_calls")
    assert (row["provider"], row["task"], row["outcome"], row["case_id"]) == ("jev", "jev_advice", "ok", "c1")
    http = rec.requests[0]
    assert str(http.url) == "https://jev.example.test/v1/chat/completions" and http.headers["authorization"] == f"Bearer {JEV_KEY}"
    body = json.loads(http.content)
    assert body["model"] == "jev-advisor-1" and body["response_format"]["json_schema"]["name"] == "jev_order"


def test_only_routing_metadata_is_sent_never_case_content(ledger, events, tmp_path):
    r, rec = make(ledger, events, tmp_path)
    r.advise("repair", CANDS, SUMMARY)
    sent = rec.requests[0].content.decode()
    assert "claude-haiku-4-5" in sent and "repair" in sent and "connection_id" not in sent and "c0" not in sent


def test_key_is_never_persisted_anywhere(ledger, events, db, tmp_path):
    r, _ = make(ledger, events, tmp_path)
    r.advise("repair", CANDS, SUMMARY)
    r.setup()
    blobs = [repr(db.query(f"SELECT * FROM {t}")) for t in ("budgets", "reservations", "ai_calls", "events", "meta")]
    blobs += [p.read_text(errors="replace") for p in tmp_path.rglob("*") if p.is_file()]
    assert all(JEV_KEY not in b for b in blobs)


def test_decisions_are_cached_by_request_hash(ledger, events, tmp_path):
    r, rec = make(ledger, events, tmp_path)
    first = r.advise("repair", CANDS, SUMMARY)
    second = r.advise("repair", CANDS, SUMMARY)
    assert len(rec.requests) == 1 and second.source == "cache" and second.cached and second.order == first.order == [1, 0] and second.spent_usd == 0
    spent = ledger.get(MONTH)["spent_usd"]
    r.advise("repair", CANDS, {**SUMMARY, "est_input_tokens": 5000})      # different request => new decision
    assert len(rec.requests) == 2 and ledger.get(MONTH)["spent_usd"] > spent
    # the cache survives a restart and even works with JeV unconfigured (no key needed to reuse a decision)
    r2, rec2 = make(ledger, events, tmp_path, environ=env(JEV_API_KEY=None))
    d = r2.advise("repair", CANDS, SUMMARY)
    assert d.source == "cache" and d.order == [1, 0] and rec2.requests == []


def test_low_confidence_falls_back_and_is_cached_to_avoid_respending(ledger, events, tmp_path):
    r, rec = make(ledger, events, tmp_path, reply=lambda q: advice(confidence=0.3))
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order is None and d.source == "fallback" and d.reason == "low_confidence" and d.confidence == 0.3
    d2 = r.advise("repair", CANDS, SUMMARY)
    assert d2.order is None and d2.reason == "cached_low_confidence" and len(rec.requests) == 1


@pytest.mark.parametrize("payload", [
    {"order": [0], "confidence": 0.9, "reason": "x"}, {"order": [0, 0], "confidence": 0.9, "reason": "x"},
    {"order": [0, 7], "confidence": 0.9, "reason": "x"}, {"order": "01", "confidence": 0.9, "reason": "x"},
    {"order": [1, 0], "confidence": 1.5, "reason": "x"}, {"order": [1, 0], "confidence": "high", "reason": "x"},
    {"order": [True, False], "confidence": 0.9, "reason": "x"},
])
def test_invalid_advice_is_rejected_and_never_reorders(ledger, events, tmp_path, payload):
    r, _ = make(ledger, events, tmp_path, reply=lambda q: chat_chunks(json.dumps(payload)))
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order is None and d.source == "fallback" and d.reason == "invalid_advice"


def test_unparseable_response_falls_back(ledger, events, tmp_path):
    r, _ = make(ledger, events, tmp_path, reply=lambda q: chat_chunks("not json at all"))
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order is None and d.reason == "unparseable_response"


@pytest.mark.parametrize("reply,settled", [
    (lambda q: json_response({"error": {"message": "bad key"}}, status=401), False),
    (lambda q: json_response({"error": {"message": "slow"}}, status=429), False),
    (lambda q: httpx.Response(503, text="down"), False),
    (lambda q: httpx.ConnectError("refused", request=q), False),
    (lambda q: httpx.ReadTimeout("slow", request=q), True),
])
def test_any_jev_failure_falls_back_and_accounts_conservatively(ledger, events, tmp_path, reply, settled):
    r, _ = make(ledger, events, tmp_path, reply=reply)
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order is None and d.source == "fallback" and d.reason.startswith("provider_error:")
    m = ledger.get(MONTH)
    assert m["reserved_usd"] == 0
    assert (m["spent_usd"] > 0) is settled   # a read timeout may have been billed; clean rejections cost nothing


def test_jev_failures_are_not_cached(ledger, events, tmp_path):
    state = {"n": 0}

    def reply(q):
        state["n"] += 1
        return httpx.Response(503, text="down") if state["n"] == 1 else advice()
    r, rec = make(ledger, events, tmp_path, reply=reply)
    assert r.advise("repair", CANDS, SUMMARY).order is None
    assert r.advise("repair", CANDS, SUMMARY).order == [1, 0] and len(rec.requests) == 2


# =============================================================================================== caps
def test_monthly_cap_is_enforced_and_blocks_the_request(ledger, events, tmp_path):
    r, rec = make(ledger, events, tmp_path)
    r._ensure_budgets()
    rsv = ledger.reserve(MONTH, 0.9999, "pre-spend")
    ledger.settle(rsv["reservation_id"], 0.9999)
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order is None and d.reason == "budget_exhausted" and rec.requests == []
    assert ledger.get(MONTH)["spent_usd"] == pytest.approx(0.9999)


def test_monthly_budget_rolls_over_with_the_calendar_month(ledger, events, tmp_path):
    clock = {"t": NOW}
    rec = Recorder(lambda q: advice())
    r = J.JeVRouter(ledger, events, tmp_path, env=env(), transport=rec.transport, now=lambda: clock["t"])
    r._ensure_budgets()
    rsv = ledger.reserve(MONTH, 1.0, "all-of-it")
    ledger.settle(rsv["reservation_id"], 1.0)
    assert r.advise("repair", CANDS, SUMMARY).reason == "budget_exhausted"
    clock["t"] = NOW + 31 * 86400
    d = r.advise("repair", CANDS, {**SUMMARY, "messages": 2})
    assert d.source == "jev" and ledger.get("jev:monthly:2026-10")["spent_usd"] > 0 and ledger.get(MONTH)["spent_usd"] == pytest.approx(1.0)


def test_unknown_jev_price_reserves_a_conservative_ceiling_never_zero(ledger, events, db, tmp_path):
    r, rec = make(ledger, events, tmp_path, environ=env(JEV_PRICE_INPUT_PER_MTOK=None, JEV_PRICE_OUTPUT_PER_MTOK=None))
    assert r.setup()["ok"] is True
    rsv = ledger.reservations("jev:setup")[0]
    assert 0.03 < rsv["amount_usd"] <= J.SETUP_CAP_USD                           # ceiling at the conservative limit, nowhere near 0
    assert rsv["actual_usd"] == pytest.approx((20 * 30.0 + 5 * 150.0) / 1e6)     # actual usage priced at the conservative limit
    assert db.query_one("SELECT cost_known FROM ai_calls")["cost_known"] == 0


def test_setup_refused_when_the_conservative_ceiling_cannot_fit_the_cap(ledger, events, tmp_path):
    r, rec = make(ledger, events, tmp_path)
    r._ensure_budgets()
    rsv = ledger.reserve("jev:setup", 0.04, "pre")
    ledger.settle(rsv["reservation_id"], 0.04)
    r2, rec2 = make(ledger, events, tmp_path, environ=env(JEV_PRICE_INPUT_PER_MTOK=None, JEV_PRICE_OUTPUT_PER_MTOK=None))
    out = r2.setup()
    assert out["ok"] is False and out["reason"] == "budget_exhausted" and rec2.requests == []


def test_setup_is_funded_by_the_setup_budget_only(ledger, events, tmp_path):
    r, rec = make(ledger, events, tmp_path, reply=lambda q: advice())
    out = r.setup()
    assert out["ok"] is True and out["unverified"] is True and len(rec.requests) == 1
    s = ledger.get("jev:setup")
    assert 0 < s["spent_usd"] <= J.SETUP_CAP_USD and ledger.get(MONTH)["spent_usd"] == 0
    assert (r.dir / "setup.json").exists()
    bad, _ = make(ledger, events, tmp_path, reply=lambda q: chat_chunks("nonsense"))
    assert bad.setup()["ok"] is False


def test_setup_cannot_exceed_its_cap_over_repeated_runs(ledger, events, tmp_path):
    big = Recorder(lambda q: chat_chunks(json.dumps({"order": [0, 1], "confidence": 0.9, "reason": "r"}), usage=True))
    # price chosen so one setup call really costs ~$0.03 (20 in / 5 out tokens): the second must be refused by the ledger
    e = env(JEV_PRICE_INPUT_PER_MTOK="1000", JEV_PRICE_OUTPUT_PER_MTOK="1000")
    r = J.JeVRouter(ledger, events, tmp_path, env=e, transport=big.transport, now=lambda: NOW)
    r.setup()
    r.setup()
    r.setup()
    assert ledger.get("jev:setup")["spent_usd"] <= J.SETUP_CAP_USD + 1e-9


# =============================================================================================== router integration
def test_jev_reorders_the_real_router_but_cannot_add_candidates(db, events, settings, ledger, tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        store = ConnectionStore(db, events, settings, secrets=SecretStore(tmp_path / "s"), ledger=ledger)
    a = store.create("anthropic", "A", api_key="sk-ant-NOT-REAL-aaaaaaaaaaaaaaaaaaaa", models=["claude-sonnet-5-5"])
    b = store.create("anthropic", "B", api_key="sk-ant-NOT-REAL-bbbbbbbbbbbbbbbbbbbb", models=["claude-haiku-4-5"])
    ma, mb = MockProvider(["A"]), MockProvider(["B"])
    store.set_adapter_override(a["connection_id"], ma)
    store.set_adapter_override(b["connection_id"], mb)
    store.set_route("repair", a["connection_id"], "claude-sonnet-5-5", [{"connection": b["connection_id"], "model": "claude-haiku-4-5"}])
    ledger.create("job:j", "job:j", 1.0)
    jev, rec = make(ledger, events, tmp_path)
    client = AIClient(store, ledger, events, db, advisor=jev)
    res = client.call("repair", Request(model="", messages=[Message.user("secret case text")], max_output_tokens=50), budget="job:j", case_id="c1")
    assert res.response.text == "B" and ma.calls == [] and res.advisor["source"] == "jev"
    assert "secret case text" not in rec.requests[0].content.decode()
    tasks = [r["task"] for r in db.query("SELECT task FROM ai_calls ORDER BY created_at, call_id")]
    assert sorted(tasks) == ["jev_advice", "repair"]
    # an outage of JeV never blocks the call: same request, JeV down, deterministic order
    down, _ = make(ledger, events, tmp_path / "other", reply=lambda q: httpx.Response(500, text="x"))
    ma.script, mb.script = ["A2"], ["B2"]
    res2 = AIClient(store, ledger, events, db, advisor=down).call("repair", Request(model="", messages=[Message.user("again")], max_output_tokens=50),
                                                                   budget="job:j")
    assert res2.response.text == "A2"


# =============================================================================================== decision ledger file
def test_decision_ledger_atomic_writes_and_corruption_tolerance(tmp_path):
    led = J.DecisionLedger(tmp_path / "j" / "decisions.json")
    led.put("k1", {"order": [0, 1], "confidence": 1, "ts": 1})
    assert led.get("k1")["order"] == [0, 1] and led.get("nope") is None
    assert [p.name for p in (tmp_path / "j").iterdir()] == ["decisions.json"]     # no temp files left behind
    (tmp_path / "j" / "decisions.json").write_text("{truncated")
    assert led.get("k1") is None                                                  # corrupt file is quarantined, not fatal
    assert (tmp_path / "j" / "decisions.corrupt").exists()
    led.put("k2", {"ts": 2})
    assert json.loads((tmp_path / "j" / "decisions.json").read_text()) == {"k2": {"ts": 2}}


def test_decision_ledger_is_bounded_and_thread_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(J, "MAX_CACHE_ENTRIES", 20)
    led = J.DecisionLedger(tmp_path / "d.json")
    ts = [threading.Thread(target=lambda i=i: [led.put(f"k{i}-{j}", {"ts": i * 100 + j}) for j in range(10)]) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    data = json.loads((tmp_path / "d.json").read_text())      # always valid JSON
    assert len(data) == 20 and sorted(p.name for p in tmp_path.iterdir()) == ["d.json"]
