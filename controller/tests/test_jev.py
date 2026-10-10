"""JeV advisor over the TypeSafe contract (docs/AI_LADDER.md section 9). A fake TypeSafe server (httpx.MockTransport) only:
no network, no real key. The opt-in live check is tests/test_jev_live.py (REBUILD_LIVE_JEV=1)."""
import json
import math
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

JEV_KEY = "apikey_NOT_REAL_jev_0123456789abcdef"
NOW = 1_790_000_000.0  # 2026-09-21 in epoch seconds; fixed so monthly budget ids are deterministic
MONTH = "jev:monthly:2026-09"
URL = "https://api.typesafe.ai/v1/systemone"

CANDS = [{"index": 0, "connection_id": "conn_secret_id_0", "provider": "anthropic", "model": "claude-sonnet-5-5", "locality": "cloud",
          "price_known": True, "input_per_mtok": 2.0, "output_per_mtok": 10.0},
         {"index": 1, "connection_id": "conn_secret_id_1", "provider": "local", "model": "qwen2.5-coder:14b", "locality": "local",
          "price_known": True, "input_per_mtok": 0.0, "output_per_mtok": 0.0}]
SUMMARY = {"task": "repair", "needs": ["tools"], "est_input_tokens": 500, "max_output_tokens": 100, "messages": 1}


class FakeTypeSafe:
    """Records requests; replies from a list (consumed) or a callable."""

    def __init__(self, reply=None):
        self.reply = reply if reply is not None else (lambda q, body: answer("R2", {"R1": 0.2, "R2": 0.8}))
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        r = self.reply.pop(0) if isinstance(self.reply, list) else self.reply(request, body)
        if callable(r):
            r = r(request, body)
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def transport(self):
        return httpx.MockTransport(self)

    def body(self, i=-1):
        return json.loads(self.requests[i].content)


def answer(choice, probs, confidence=None, qid=None, tokens=120, model="jev-1.13.0", typ="choice"):
    def make(request, body):
        a = {"type": typ, "choice": choice, "probabilities": probs}
        if confidence is not None:
            a["confidence"] = confidence
        q = qid or next(iter(body["questions"]))
        text = json.dumps({"model": model, "answers": {q: a}, "usage": {"input_tokens": tokens, "output_tokens": 3}}, allow_nan=True)
        return httpx.Response(200, content=text.encode(), headers={"content-type": "application/json"})
    return make


@pytest.fixture
def ledger(db, events):
    return BudgetLedger(db, events)


@pytest.fixture
def secrets(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return SecretStore(tmp_path / "secret-data")


def make(ledger, events, tmp_path, secrets, reply=None, key=True, **kw):
    srv = FakeTypeSafe(reply)
    sleeps: list[float] = []
    clock = kw.pop("clock", None) or {"t": NOW}
    r = J.JeVRouter(ledger, events, tmp_path / "data", secrets=secrets, transport=srv.transport, now=lambda: clock["t"],
                    sleep=sleeps.append, env=kw.pop("env", {}), **kw)
    if key:
        r.set_key(JEV_KEY)
    r.sleeps, r.clock = sleeps, clock  # type: ignore[attr-defined]
    return r, srv


# =============================================================================================== configuration
def test_no_key_is_a_deterministic_fallback_with_no_network(ledger, events, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets, key=False)
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order is None and d.source == "fallback" and d.reason == "jev_not_configured" and srv.requests == []
    st = r.status()
    assert st["has_key"] is False and st["offline_reason"] == "no_key" and st["model"] == "jev-1.13.0" and st["endpoint"] == URL
    assert st["monthly_cap_usd"] == 1.0 and st["month"]["limit_usd"] == 1.0 and st["month"]["budget_id"] == MONTH
    assert r.setup()["reason"] == "no_key" and srv.requests == []


def test_off_means_off_even_for_cached_decisions(ledger, events, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets)
    assert r.advise("repair", CANDS, SUMMARY).order == [1, 0]
    r.update_settings(enabled=False)
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order is None and d.reason == "jev_off" and len(srv.requests) == 1
    assert r.status()["offline_reason"] == "off"


def test_key_is_stored_in_the_credential_store_and_never_returned_or_written_elsewhere(ledger, events, db, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets)
    st = r.status()
    assert st["has_key"] is True and st["key_source"] == "entered" and JEV_KEY not in json.dumps(st)
    r.advise("repair", CANDS, SUMMARY, case_id="c1")
    r.setup()
    assert srv.requests[0].headers["authorization"] == f"Bearer {JEV_KEY}"
    blobs = [repr(db.query(f"SELECT * FROM {t}")) for t in ("budgets", "reservations", "ai_calls", "events", "meta")]
    blobs += [p.read_text(errors="replace") for p in (tmp_path / "data").rglob("*") if p.is_file()]
    assert all(JEV_KEY not in b for b in blobs)
    assert secrets.get(J.SECRET_REF) == JEV_KEY
    r.set_key(None)
    assert r.has_key() is False and r.status()["key_source"] is None
    with pytest.raises(ValueError):
        r.set_key("short")


# =============================================================================================== the contract
def test_request_follows_the_typesafe_contract_and_sends_only_routing_metadata(ledger, events, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets)
    r.advise("repair", CANDS, SUMMARY)
    q = srv.requests[0]
    assert q.method == "POST" and str(q.url) == URL and q.headers["content-type"] == "application/json"
    body = srv.body()
    assert set(body) == {"model", "state", "questions"} and body["model"] == "jev-1.13.0"
    (qid, question), = body["questions"].items()
    assert question["type"] == "choice" and set(question["criteria"]) == {"R1", "R2"} and question["instructions"]
    assert "qwen2.5-coder:14b" in question["criteria"]["R2"] and "free" in question["criteria"]["R2"]
    sent = q.content.decode()
    assert "repair" in body["state"] and "500" in body["state"]
    assert "conn_secret_id" not in sent and "connection_id" not in sent


def test_probabilities_rank_the_rungs_and_spend_is_priced_at_the_input_list_price(ledger, events, db, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets, reply=[answer("R2", {"R1": 0.1, "R2": 0.9}, confidence=0.82, tokens=1000)])
    d = r.advise("repair", CANDS, SUMMARY, case_id="c1")
    assert d.order == [1, 0] and d.source == "jev" and d.confidence == 0.82 and d.model == "jev-1.13.0"
    assert d.spent_usd == pytest.approx(1000 * 0.042 / 1e6)
    m = ledger.get(MONTH)
    assert m["spent_usd"] == pytest.approx(d.spent_usd) and m["reserved_usd"] == 0 and ledger.get("jev:setup")["spent_usd"] == 0
    row = db.query_one("SELECT * FROM ai_calls")
    assert (row["provider"], row["task"], row["outcome"], row["case_id"], row["input_tokens"]) == ("jev", "jev_advice", "ok", "c1", 1000)
    feed = [json.loads(e["payload"]) for e in db.query("SELECT payload FROM events WHERE kind='ai.activity'")]
    assert feed and "JeV suggests trying qwen2.5-coder:14b first (confidence 0.82)" in feed[-1]["text"] and feed[-1]["confidence"] == 0.82
    assert r.status()["last_decisions"][0]["choice"] == "R2" and r.status()["last_decisions"][0]["confidence"] == 0.82


def test_confidence_defaults_to_the_top_probability(ledger, events, tmp_path, secrets):
    r, _ = make(ledger, events, tmp_path, secrets, reply=[answer("R1", {"R1": 0.7, "R2": 0.3})])
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order == [0, 1] and d.confidence == 0.7


@pytest.mark.parametrize("bad", [
    answer("R1", {"R1": 0.5, "R2": 0.2}),                       # does not sum to 1
    answer("R3", {"R1": 0.5, "R2": 0.5}),                       # unknown choice
    answer("R1", {"R1": 1.4, "R2": -0.4}),                      # out of range
    answer("R1", {"R1": float("nan"), "R2": 0.5}),              # non-finite
    answer("R1", {"R1": 0.6}),                                  # missing probability for an allowed choice
    answer("R1", {"R1": 0.6, "R2": 0.4}, confidence=1.5),       # bad confidence
    answer("R1", {"R1": 0.6, "R2": 0.4}, typ="score"),          # type mismatch
    lambda q, b: httpx.Response(200, json={"model": "jev-1.13.0", "usage": {"input_tokens": 10}}),          # no answers map
    lambda q, b: httpx.Response(200, text="not json"),
])
def test_invalid_answers_are_rejected_and_never_reorder(ledger, events, tmp_path, secrets, bad):
    r, srv = make(ledger, events, tmp_path, secrets, reply=[bad])
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order is None and d.source == "fallback" and d.reason == "jev_error:invalid_response" and len(srv.requests) == 1
    assert ledger.get(MONTH)["reserved_usd"] == 0 and ledger.get(MONTH)["spent_usd"] > 0     # a 2xx is billed even when rejected


def test_validate_choice_matches_the_bridge_rules():
    ok = {"type": "choice", "choice": "A", "probabilities": {"A": 0.98, "B": 0.03}}
    assert J.validate_choice(ok, ["A", "B"]) == (True, "")                  # +-0.05 tolerance
    assert J.validate_choice({**ok, "probabilities": {"A": True, "B": 0.0}}, ["A", "B"])[1] == "INVALID_PROBABILITY"
    assert J.validate_choice({**ok, "confidence": math.inf}, ["A", "B"])[1] == "INVALID_CONFIDENCE"
    assert J.validate_choice(None, ["A"])[1] == "MALFORMED_ANSWER"


def test_low_confidence_falls_back_and_is_cached_to_avoid_respending(ledger, events, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets, reply=[answer("R2", {"R1": 0.45, "R2": 0.55})])
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order is None and d.reason == "low_confidence" and d.confidence == 0.55
    assert r.advise("repair", CANDS, SUMMARY).reason == "cached_low_confidence" and len(srv.requests) == 1


def test_decisions_are_cached_by_request_and_survive_a_restart(ledger, events, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets)
    first = r.advise("repair", CANDS, SUMMARY)
    second = r.advise("repair", CANDS, SUMMARY)
    assert len(srv.requests) == 1 and second.source == "cache" and second.order == first.order == [1, 0]
    r.advise("repair", CANDS, {**SUMMARY, "est_input_tokens": 5000})
    assert len(srv.requests) == 2
    r2, srv2 = make(ledger, events, tmp_path, secrets, key=False)      # same data dir: the cache is reused without a key
    assert r2.advise("repair", CANDS, SUMMARY).source == "cache" and srv2.requests == []


# =============================================================================================== errors, back-off, breaker
def test_429_backs_off_with_retry_after_then_succeeds(ledger, events, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets, reply=[httpx.Response(429, headers={"retry-after": "2"}), answer("R2", {"R1": 0.1, "R2": 0.9})])
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order == [1, 0] and r.sleeps == [2.0] and len(srv.requests) == 2


def test_529_overloaded_backs_off_once_then_falls_back_without_spend(ledger, events, tmp_path, secrets, db):
    r, srv = make(ledger, events, tmp_path, secrets, reply=[httpx.Response(529), httpx.Response(529)])
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order is None and d.reason == "jev_error:overloaded" and len(srv.requests) == 2 and r.sleeps == [0.4]
    m = ledger.get(MONTH)
    assert m["spent_usd"] == 0 and m["reserved_usd"] == 0
    assert db.query_one("SELECT outcome FROM ai_calls")["outcome"] == "rate_limit"


def test_a_long_retry_after_is_not_waited_for(ledger, events, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets, reply=[httpx.Response(429, headers={"retry-after": "600"})])
    assert r.advise("repair", CANDS, SUMMARY).reason == "jev_error:rate_limited" and r.sleeps == [] and len(srv.requests) == 1


@pytest.mark.parametrize("reply,code,spent", [
    (httpx.Response(401, json={"error": "bad key"}), "unauthenticated", False),
    (httpx.Response(403), "unauthenticated", False),
    (httpx.Response(422, json={"detail": "bad body"}), "invalid_request", False),
    (httpx.Response(500, text="boom"), "provider_error", False),
    (lambda q, b: httpx.ReadTimeout("slow", request=q), "timeout", True),
])
def test_typed_errors_fall_back_and_account_conservatively(ledger, events, tmp_path, secrets, reply, code, spent):
    r, srv = make(ledger, events, tmp_path, secrets, reply=[reply, reply])
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order is None and d.reason == f"jev_error:{code}" and len(srv.requests) == 1
    m = ledger.get(MONTH)
    assert m["reserved_usd"] == 0 and (m["spent_usd"] > 0) is spent       # a read timeout may have been billed


def test_network_failure_is_retried_once_then_falls_back(ledger, events, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets, reply=lambda q, b: httpx.ConnectError("refused", request=q))
    assert r.advise("repair", CANDS, SUMMARY).reason == "jev_error:unavailable" and len(srv.requests) == 2


def test_failures_are_not_cached(ledger, events, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets, reply=[httpx.Response(500), answer("R2", {"R1": 0.1, "R2": 0.9})])
    assert r.advise("repair", CANDS, SUMMARY).order is None
    assert r.advise("repair", CANDS, SUMMARY).order == [1, 0] and len(srv.requests) == 2


def test_circuit_breaker_opens_after_repeated_failures_and_half_opens_after_cooldown(ledger, events, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets, reply=lambda q, b: httpx.Response(500))
    for i in range(J.BREAKER_THRESHOLD):
        r.advise("repair", CANDS, {**SUMMARY, "messages": i})
    assert len(srv.requests) == J.BREAKER_THRESHOLD and r.breaker_state() == "open"
    d = r.advise("repair", CANDS, {**SUMMARY, "messages": 99})
    assert d.reason == "breaker_open" and len(srv.requests) == J.BREAKER_THRESHOLD
    assert r.status()["offline_reason"] == "breaker_open" and r.status()["breaker"]["open_until"]
    r.clock["t"] += J.BREAKER_COOLDOWN_S + 1
    assert r.breaker_state() == "half_open"
    r.advise("repair", CANDS, {**SUMMARY, "messages": 100})                 # one probe, fails, re-opens at once
    assert len(srv.requests) == J.BREAKER_THRESHOLD + 1 and r.breaker_state() == "open"
    srv.reply = lambda q, b: answer("R2", {"R1": 0.1, "R2": 0.9})(q, b)
    r.clock["t"] += J.BREAKER_COOLDOWN_S + 1
    assert r.advise("repair", CANDS, {**SUMMARY, "messages": 101}).order == [1, 0] and r.breaker_state() == "closed"


# =============================================================================================== budget cap
def test_monthly_cap_is_user_settable_and_blocks_the_request_when_spent(ledger, events, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets)
    r.update_settings(monthly_cap_usd=0.0)
    d = r.advise("repair", CANDS, SUMMARY)
    assert d.order is None and d.reason == "budget_exhausted" and srv.requests == []
    r.update_settings(monthly_cap_usd=2.5)
    assert ledger.get(MONTH)["limit_usd"] == 2.5 and r.advise("repair", CANDS, SUMMARY).order == [1, 0]
    with pytest.raises(ValueError):
        r.update_settings(monthly_cap_usd=1000)
    with pytest.raises(ValueError):
        r.update_settings(monthly_cap_usd=-1)


def test_monthly_budget_rolls_over_with_the_calendar_month(ledger, events, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets)
    r._ensure_budgets()
    rsv = ledger.reserve(MONTH, 1.0, "all-of-it")
    ledger.settle(rsv["reservation_id"], 1.0)
    assert r.advise("repair", CANDS, SUMMARY).reason == "budget_exhausted"
    r.clock["t"] = NOW + 31 * 86400
    assert r.advise("repair", CANDS, {**SUMMARY, "messages": 2}).source == "jev" and ledger.get("jev:monthly:2026-10")["spent_usd"] > 0


def test_setup_check_is_funded_by_the_setup_budget_only(ledger, events, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets, reply=[answer("OK", {"OK": 1.0, "NOT_OK": 0.0})])
    out = r.setup()
    assert out["ok"] is True and out["model"] == "jev-1.13.0" and 0 < ledger.get("jev:setup")["spent_usd"] <= J.SETUP_CAP_USD
    assert ledger.get(MONTH)["spent_usd"] == 0 and srv.body()["questions"]["check"]["type"] == "choice"
    bad, _ = make(ledger, events, tmp_path, secrets, reply=[httpx.Response(401)])
    assert bad.setup()["reason"] == "unauthenticated"


# =============================================================================================== between repair attempts
def test_reassess_advises_switch_or_stop_from_counts_only(ledger, events, tmp_path, secrets):
    r, srv = make(ledger, events, tmp_path, secrets, reply=[answer("SWITCH", {"RETRY": 0.1, "SWITCH": 0.8, "STOP": 0.1})])
    out = r.reassess("repair", attempt=2, max_attempts=4, build="failed", scenarios=8, passed=3,
                     current={"provider": "local", "model": "qwen2.5-coder:14b", "locality": "local"}, other_rungs=1)
    assert out["advice"] == "switch" and out["source"] == "jev" and out["confidence"] == 0.8
    body = srv.body()
    assert set(body["questions"]["next"]["criteria"]) == {"RETRY", "SWITCH", "STOP"} and "3 of 8" in body["state"]
    # no other rung: SWITCH is not offered; an answer outside the allowlist is rejected
    r2, srv2 = make(ledger, events, tmp_path / "b", secrets, reply=[answer("SWITCH", {"RETRY": 0.1, "SWITCH": 0.8, "STOP": 0.1})])
    out2 = r2.reassess("repair", attempt=2, max_attempts=4, build="built", other_rungs=0)
    assert out2["advice"] is None and out2["reason"] == "invalid_response" and set(srv2.body()["questions"]["next"]["criteria"]) == {"RETRY", "STOP"}


# =============================================================================================== router integration
def _store(db, events, settings, ledger, tmp_path):
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
    return store, ma, mb


def test_jev_reorders_the_real_router_but_cannot_add_candidates(db, events, settings, ledger, tmp_path, secrets):
    store, ma, mb = _store(db, events, settings, ledger, tmp_path)
    # JeV "prefers" a third route that does not exist: an unknown choice is rejected, nothing is added
    r, srv = make(ledger, events, tmp_path, secrets, reply=[answer("R3", {"R1": 0.0, "R2": 0.0, "R3": 1.0}),
                                                            answer("R2", {"R1": 0.2, "R2": 0.8})])
    client = AIClient(store, ledger, events, db, advisor=r)
    res = client.call("repair", Request(model="", messages=[Message.user("secret case text")], max_output_tokens=50), budget="job:j")
    assert res.response.text == "A" and res.advisor["source"] == "fallback" and mb.calls == []
    ma.script = ["A2"]
    res2 = client.call("repair", Request(model="", messages=[Message.user("again")], max_output_tokens=60), budget="job:j", case_id="c1")
    assert res2.response.text == "B" and res2.advisor["source"] == "jev" and len(ma.calls) == 1
    assert all("secret case text" not in q.content.decode() for q in srv.requests)


def test_jev_outage_never_blocks_a_call(db, events, settings, ledger, tmp_path, secrets):
    store, ma, mb = _store(db, events, settings, ledger, tmp_path)
    r, _ = make(ledger, events, tmp_path, secrets, reply=lambda q, b: httpx.Response(500))
    res = AIClient(store, ledger, events, db, advisor=r).call("repair", Request(model="", messages=[Message.user("x")], max_output_tokens=50),
                                                              budget="job:j")
    assert res.response.text == "A" and res.advisor["source"] == "fallback"


# =============================================================================================== "use the key from my JeV install"
def test_import_install_key_reads_only_the_documented_file_and_never_returns_the_value(ledger, events, tmp_path, secrets):
    keyfile = tmp_path / "jevhome" / "secrets" / "typesafe.key"
    keyfile.parent.mkdir(parents=True)
    keyfile.write_text("\n" + JEV_KEY + "\n", encoding="utf-8")
    r, srv = make(ledger, events, tmp_path, secrets, key=False, env={"JEV_RUNTIME_DIR": str(tmp_path / "jevhome")})
    assert r.status()["jev_install"] == {"key_file_found": True, "path": str(keyfile)}
    out = r.import_install_key()
    assert out["imported"] is True and out["key_source"] == "jev_install" and out["has_key"] is True
    assert JEV_KEY not in json.dumps(out) and secrets.get(J.SECRET_REF) == JEV_KEY
    # JEV_SECRET_FILE wins; placeholders and missing files are refused with a plain message
    ph = tmp_path / "ph.key"
    ph.write_text("# REPLACE with your key\n")
    r2, _ = make(ledger, events, tmp_path / "x", secrets, key=False, env={"JEV_SECRET_FILE": str(ph)})
    with pytest.raises(J.JeVError) as ei:
        r2.import_install_key()
    assert ei.value.code == "invalid" and "placeholder" in str(ei.value)
    r3, _ = make(ledger, events, tmp_path / "y", secrets, key=False, env={"JEV_SECRET_FILE": str(tmp_path / "missing.key")})
    with pytest.raises(J.JeVError) as ei:
        r3.import_install_key()
    assert ei.value.code == "not_found"
    big = tmp_path / "big.key"
    big.write_bytes(b"x" * 5000)
    r4, _ = make(ledger, events, tmp_path / "z", secrets, key=False, env={"JEV_SECRET_FILE": str(big)})
    with pytest.raises(J.JeVError):
        r4.import_install_key()


def test_default_install_path_is_the_bridge_documented_one(tmp_path):
    assert J.jev_install_key_path({"JEV_SECRET_FILE": "C:/k/typesafe.key"}).as_posix() == "C:/k/typesafe.key"
    assert J.jev_install_key_path({"JEV_RUNTIME_DIR": str(tmp_path)}) == tmp_path / "secrets" / "typesafe.key"
    p = J.jev_install_key_path({})
    assert p.parts[-3:] == (".jev", "secrets", "typesafe.key")


# =============================================================================================== decision files
def test_decision_ledger_atomic_writes_and_corruption_tolerance(tmp_path):
    led = J.DecisionLedger(tmp_path / "j" / "decisions.json")
    led.put("k1", {"order": [0, 1], "confidence": 1, "ts": 1})
    assert led.get("k1")["order"] == [0, 1] and led.get("nope") is None
    assert [p.name for p in (tmp_path / "j").iterdir()] == ["decisions.json"]
    (tmp_path / "j" / "decisions.json").write_text("{truncated")
    assert led.get("k1") is None and (tmp_path / "j" / "decisions.corrupt").exists()
    led.put("k2", {"ts": 2})
    assert json.loads((tmp_path / "j" / "decisions.json").read_text()) == {"k2": {"ts": 2}}


def test_decision_ledger_is_bounded_and_thread_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(J, "MAX_CACHE_ENTRIES", 20)
    led = J.DecisionLedger(tmp_path / "d.json")
    ts = [threading.Thread(target=lambda i=i: [led.put(f"k{i}-{j}", {"ts": i * 100 + j}) for j in range(10)]) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    data = json.loads((tmp_path / "d.json").read_text())
    assert len(data) == 20 and sorted(p.name for p in tmp_path.iterdir()) == ["d.json"]
