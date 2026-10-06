"""Plain-language statement, before a case starts, of whether an implementation can be produced (API /capabilities, /implementation/forecast,
case + plan payloads, StudioServices.create_case/start_rebuild) plus the pure helpers of the implement loop (policy, response parsing)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rebuild_controller.api.server import create_app
from rebuild_controller.implement import LoopPolicy, forecast, parse_file_map
from rebuild_controller.services import StudioServices

TOKEN = "t0k3n"


@pytest.fixture
def studio(settings):
    s = StudioServices(settings)
    s.ai.advisor = None
    yield s
    s.stop()


@pytest.fixture
def client(studio):
    app = create_app(studio, TOKEN)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}", "Origin": "http://localhost:5173"})
        yield c, studio


def _route(studio, *, price=True, provider="openai", model="gpt-x"):
    entry = {"id": model, **({"price": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}} if price else {})}
    if provider == "local":
        conn = studio.connections.create("local", "LM Studio", endpoint="http://127.0.0.1:1234/v1", auth_mode="local", models=[entry], dialect="chat")
    else:
        conn = studio.connections.create("openai", "My OpenAI", endpoint="http://127.0.0.1:9/v1", auth_mode="api_key", api_key="sk-test-0123456789abcdef", models=[entry], dialect="chat")
    studio.connections.set_route("interpretation", conn["connection_id"], model)
    return conn


NATIVE = dict(target_language="rust", output_type="exe", profile="native_pe")


def test_no_ai_says_scaffold_does_not_implement_the_program(studio):
    fc = forecast(studio, ai_policy={"mode": "no_ai"}, launch_profile={}, **NATIVE)
    assert fc["state"] == "scaffold_only" and fc["can_produce_implementation"] is False and fc["will_use_ai"] is False
    assert fc["summary"] == "No AI connected: you will get recovered evidence and a Rust scaffold that does not implement the program yet."
    assert any("optional" in d or "not required" in d for d in fc["details"])           # external MCP clients are optional


def test_ai_connected_states_route_attempts_and_budget(studio):
    _route(studio)
    fc = forecast(studio, ai_policy={"mode": "assisted", "budget_usd": 2.5, "max_attempts": 4}, launch_profile={"baseline_file": "x.json"}, **NATIVE)
    assert fc["state"] == "ai_ready" and fc["can_produce_implementation"] is True and fc["will_use_ai"] is True and fc["verifiable"] is True
    assert fc["summary"].startswith("AI connected (route My OpenAI: gpt-x): the app will try up to 4 implementation attempts within your $2.50 budget.")
    assert fc["max_attempts"] == 4 and fc["budget_usd"] == 2.5 and fc["route"][0]["model"] == "gpt-x"
    assert any("verifier" in d for d in fc["details"])
    default = forecast(studio, ai_policy={"mode": "assisted", "budget_usd": 1}, launch_profile={}, **NATIVE)
    assert default["max_attempts"] == 3 and "up to 3" in default["summary"]
    assert default["verifiable"] is False and any("not verified" in d for d in default["details"])


def test_ai_mode_without_a_route_falls_back_to_scaffold_and_says_so(studio):
    fc = forecast(studio, ai_policy={"mode": "assisted", "budget_usd": 2}, launch_profile={}, **NATIVE)
    assert fc["state"] == "ai_blocked" and fc["can_produce_implementation"] is False
    assert "no implementation will be attempted" in fc["summary"] and "scaffold" in fc["summary"] and "Connections" in fc["blockers"][0]


def test_unknown_price_blocks_until_priced_or_capped(studio):
    _route(studio, price=False)
    fc = forecast(studio, ai_policy={"mode": "assisted", "budget_usd": 2}, launch_profile={}, **NATIVE)
    assert fc["state"] == "ai_blocked" and "never treated as free" in fc["blockers"][0] and "max_output_tokens" in fc["blockers"][0]
    capped = forecast(studio, ai_policy={"mode": "assisted", "budget_usd": 2, "max_output_tokens": 4000}, launch_profile={}, **NATIVE)
    assert capped["state"] == "ai_ready" and capped["pricing"] == "unknown_capped" and any("conservative ceiling" in d for d in capped["details"])


def test_metered_route_needs_a_budget_but_a_local_server_does_not(studio):
    _route(studio)
    fc = forecast(studio, ai_policy={"mode": "assisted"}, launch_profile={}, **NATIVE)
    assert fc["state"] == "ai_blocked" and "budget" in fc["blockers"][0]
    for c in studio.connections.list():
        studio.connections.delete(c["connection_id"])
    _route(studio, provider="local", price=False)
    loc = forecast(studio, ai_policy={"mode": "assisted"}, launch_profile={}, **NATIVE)
    assert loc["state"] == "ai_ready" and loc["pricing"] == "free" and "local endpoint" in loc["summary"] and "$" not in loc["summary"]


def test_web_target_and_unsupported_combinations(studio):
    web = forecast(studio, target_language="web", output_type="web", profile="web", ai_policy={"mode": "no_ai"}, launch_profile={})
    assert web["state"] == "deterministic_port" and web["can_produce_implementation"] is True and "no AI is needed" in web["summary"]
    bad = forecast(studio, target_language="web", output_type="web", profile="native_pe", ai_policy={"mode": "assisted", "budget_usd": 1}, launch_profile={})
    assert bad["state"] == "unsupported" and bad["can_produce_implementation"] is False
    bevy = forecast(studio, target_language="rust_bevy", output_type="exe", profile="godot", ai_policy={"mode": "no_ai"}, launch_profile={})
    assert "Rust (Bevy) scaffold" in bevy["summary"]


def test_forecast_is_in_capabilities_case_plan_and_a_pre_case_endpoint(client, src_out, tmp_path):
    c, st = client
    caps = c.get("/capabilities").json()
    assert caps["implementation"]["ai_connected"] is False and caps["implementation"]["summary"].startswith("No AI connected")
    _route(st)
    caps = c.get("/capabilities").json()
    assert caps["implementation"]["ai_connected"] is True and "AI connected (route My OpenAI: gpt-x)" in caps["implementation"]["summary"]
    # before creating anything
    r = c.post("/implementation/forecast", json={"target_language": "rust", "output_type": "exe", "ai_policy": {"mode": "assisted", "budget_usd": 3}, "profile": "native_pe"}).json()
    assert r["state"] == "ai_ready" and "up to 3 implementation attempts within your $3.00 budget" in r["summary"]
    src, out = src_out
    created = c.post("/cases", json={"name": "t", "source_root": str(src), "output_root": str(out), "target_language": "rust", "output_type": "exe",
                                     "ai_policy": {"mode": "no_ai"}}).json()
    assert created["implementation_forecast"]["state"] == "scaffold_only"
    cid = created["case_id"]
    assert c.get(f"/cases/{cid}").json()["implementation_forecast"]["summary"].startswith("No AI connected")
    assert c.get(f"/cases/{cid}/plan").json()["implementation_forecast"]["state"] == "scaffold_only"
    started = c.post(f"/cases/{cid}/start").json()
    assert started["implementation_forecast"]["can_produce_implementation"] is False
    bad = c.post("/implementation/forecast", json={"target_language": "cobol", "output_type": "exe"})
    assert bad.status_code == 422 or bad.status_code == 400


# ---------------------------------------------------------------------------------------------- helpers
def test_policy_defaults_caps_and_legacy_key():
    p = LoopPolicy.from_case({"ai_policy": {"mode": "assisted", "budget_usd": 2}})
    assert (p.max_attempts, p.max_output_tokens, p.has_token_cap, p.unknown_price_ok, p.ai_enabled) == (3, 16000, False, False, True)
    assert LoopPolicy.from_case({"ai_policy": {"mode": "assisted", "max_repairs": 4}}).max_attempts == 5       # legacy: repairs after the first attempt
    assert LoopPolicy.from_case({"ai_policy": {"mode": "assisted", "max_attempts": 99}}).max_attempts == 10     # hard cap
    assert LoopPolicy.from_case({"ai_policy": {"mode": "assisted", "max_attempts": 0}}).max_attempts == 1
    assert LoopPolicy.from_case({"ai_policy": {"mode": "assisted", "max_output_tokens": 3000}}).unknown_price_ok is True
    assert LoopPolicy.from_case({"ai_policy": {"mode": "no_ai"}}).ai_enabled is False
    assert LoopPolicy.from_case({"ai_policy": {"mode": "assisted", "budget_usd": "nan"}}).budget_usd == 0.0


@pytest.mark.parametrize("text,ok", [
    ('{"Cargo.toml": "[package]", "src/main.rs": "fn main(){}"}', True),
    ('Sure!\n```json\n{"src/main.rs": "fn main(){}"}\n```\nDone.', True),
    ('{"files": {"src/main.rs": "x"}}', True),
    ('{"files": [{"path": "src/main.rs", "content": "x"}]}', True),
    ("I verified it, all 8 scenarios pass.", False),
    ("", False),
    ('{"../evil.rs": "x", "/abs.rs": "x", "C:/x.rs": "x", ".cargo/config.toml": "x", "target/x": "x"}', False),
    ("[1, 2, 3]", False),
])
def test_parse_file_map(text, ok):
    files, problem = parse_file_map(text)
    assert bool(files) is ok and (ok or problem)


def test_parse_file_map_drops_unsafe_entries_but_keeps_good_ones():
    files, problem = parse_file_map('{"src/main.rs": "x", "../../etc/passwd": "y", ".cargo/config.toml": "z", "n": 5}')
    assert list(files) == ["src/main.rs"] and "ignored" in problem
