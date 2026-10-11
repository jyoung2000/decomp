"""R6 piece 1: the app's Functions list (GET /cases/{c}/modules/{m}/functions).

The shaping helper is shared with the MCP ``list_functions`` tool; the real-rizin part checks the route and the MCP tool
return the same rows for the same arguments on the pecli fixture. Engine/route refusals are checked with a fake backend so
they run on every host; the real round trip skips when rizin is not installed."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from support.data_dirs import tool_settings  # noqa: E402  (R11: never the real data folder)
from rebuild_controller.adapters.contract import OperationResult
from rebuild_controller.re_workbench import WorkbenchError, shape_functions

REPO = Path(__file__).resolve().parents[2]
PECLI = REPO / "fixtures" / "pecli" / "original" / "pecli.exe"
TOKEN = "f" * 32


@pytest.fixture
def studio(settings):
    from rebuild_controller.services import StudioServices
    s = StudioServices(settings)
    s.ai.advisor = None
    yield s
    s.stop()


def _client(studio):
    from rebuild_controller.api.server import create_app
    c = TestClient(create_app(studio, TOKEN), raise_server_exceptions=False)
    c.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return c


def _case(studio, tmp_path, src: Path | None = None):
    src = src or (tmp_path / "src")
    src.mkdir(parents=True, exist_ok=True)
    return studio.cases.create_case(name="t", source_root=str(src), output_root=str(tmp_path / "out"),
                                    target_language="auto", output_type="exe")


# =============================================================================================== shared helper
ROWS = [{"offset": 0x401000, "addr": "0x401000", "name": "entry0", "size": 40, "realsz": 40, "nbbs": 3, "signature": "void entry0(void);"},
        {"offset": 0x401100, "addr": "0x401100", "name": "fcn.00401100", "size": 23718, "realsz": 18, "nbbs": 2, "cc": 1},
        {"offset": 0x400f00, "addr": "0x400f00", "name": "sym.Main", "size": 900, "realsz": 900, "nbbs": 40},
        "not a dict"]


def test_shape_functions_sorts_by_code_bytes_keeps_span_and_pages():
    p = shape_functions(list(ROWS), None, "size", 0, 2)
    assert [f["name"] for f in p["functions"]] == ["sym.Main", "entry0"]
    assert p["total"] == 3 and p["returned"] == 2 and p["more"] is True and p["offset"] == 0
    spanned = shape_functions(list(ROWS), "FCN", "addr", 0, 10)["functions"]
    assert spanned == [{"addr": "0x401100", "name": "fcn.00401100", "nbbs": 2, "cc": 1, "size": 18, "span": 23718}]
    assert "span" not in shape_functions(list(ROWS), "entry", "addr", 0, 10)["functions"][0]
    assert [f["name"] for f in shape_functions(list(ROWS), None, "addr", 0, 10)["functions"]] == ["sym.Main", "entry0", "fcn.00401100"]
    assert [f["name"] for f in shape_functions(list(ROWS), None, "name", 0, 10)["functions"]] == ["entry0", "fcn.00401100", "sym.Main"]
    last = shape_functions(list(ROWS), None, "addr", 2, 2)
    assert last["returned"] == 1 and last["more"] is False
    with pytest.raises(WorkbenchError):
        shape_functions(list(ROWS), None, "bogus", 0, 1)


# =============================================================================================== refusals (fake backend)
class _Tool:
    version = "0.9.1"


class FakeBackend:
    def __init__(self, tool=_Tool(), result=None):
        self._tool, self.calls = tool, 0
        self.result = result or OperationResult(ok=True, data={"functions": ROWS[:3], "count": 3, "cached": False}, evidence_ids=["ev_x"])

    def tool(self):
        return self._tool

    def op_functions(self, ctx, *, case_id, module_id):
        self.calls += 1
        return self.result


def test_route_refuses_non_native_and_unknown_modules(studio, tmp_path):
    case = _case(studio, tmp_path)
    mid = studio.cases.add_module(case["case_id"], "App.dll", "1" * 64, 10, "pe", "dotnet")
    c = _client(studio)
    r = c.get(f"/cases/{case['case_id']}/modules/{mid}/functions")
    assert r.status_code == 409
    e = r.json()["error"]
    assert e["code"] == "not_native" and "dotnet" in e["message"] and e["next_action"]
    assert c.get(f"/cases/{case['case_id']}/modules/mod_nope/functions").status_code == 404
    other = _case(studio, tmp_path / "o")
    assert c.get(f"/cases/{other['case_id']}/modules/{mid}/functions").status_code == 404
    assert c.get(f"/cases/{case['case_id']}/modules/{mid}/functions", params={"sort": "weird"}).status_code == 400
    assert c.get(f"/cases/{case['case_id']}/modules/{mid}/functions", params={"limit": 0}).status_code == 400


def test_route_reports_missing_engine_and_not_analysed_then_analyses_on_request(studio, tmp_path, monkeypatch):
    case = _case(studio, tmp_path)
    mid = studio.cases.add_module(case["case_id"], "a.exe", "0" * 64, 66, "pe", "native_pe")
    c = _client(studio)
    url = f"/cases/{case['case_id']}/modules/{mid}/functions"
    monkeypatch.setattr(studio.re, "backend", lambda: FakeBackend(tool=None))
    r = c.get(url)
    assert r.status_code == 503 and r.json()["error"]["code"] == "engine_missing" and "Tools" in r.json()["error"]["next_action"]

    fake = FakeBackend()
    monkeypatch.setattr(studio.re, "backend", lambda: fake)
    r = c.get(url)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_analyzed" and "Analyse now" in r.json()["error"]["next_action"]
    assert fake.calls == 0                                         # no analysis is started without asking
    r = c.get(url, params={"analyze": 1, "sort": "size", "limit": 1})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 3 and d["returned"] == 1 and d["more"] is True and d["functions"][0]["name"] == "sym.Main"
    assert d["session_id"].startswith("res_") and d["evidence_ids"] == ["ev_x"] and d["sort"] == "size"

    fake.result = OperationResult(ok=False, error="timeout: aaa exceeded 300s")
    r = c.get(url, params={"analyze": 1})
    assert r.status_code == 504 and r.json()["error"]["code"] == "timeout" and r.json()["error"]["next_action"]
    fake.result = OperationResult(ok=False, error="RizinError: module a.exe changed on disk since inventory")
    assert c.get(url, params={"analyze": 1}).json()["error"]["code"] == "module_changed"


# =============================================================================================== real rizin on pecli
def _rizin() -> bool:
    from rebuild_controller.backends.rizin_worker import find_rizin
    return find_rizin(tool_settings()) is not None


@pytest.mark.skipif(not _rizin() or not PECLI.is_file(), reason="needs rizin (REBUILD_STUDIO_TOOLS) and the pecli fixture")
def test_functions_route_on_pecli_matches_the_mcp_tool(studio, tmp_path):
    import anyio
    pytest.importorskip("mcp")
    from mcp import Client
    from rebuild_controller.ids import sha256_file
    from rebuild_controller.mcp.server import ServerConfig, build_server

    src = tmp_path / "src"
    src.mkdir()
    shutil.copy2(PECLI, src / "pecli.exe")
    case = _case(studio, tmp_path, src)
    mid = studio.cases.add_module(case["case_id"], "pecli.exe", sha256_file(src / "pecli.exe"), (src / "pecli.exe").stat().st_size,
                                  "pe", "native_pe")
    c = _client(studio)
    url = f"/cases/{case['case_id']}/modules/{mid}/functions"
    assert c.get(url).json()["error"]["code"] == "not_analyzed"
    r = c.get(url, params={"analyze": "true", "limit": 500})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] > 50 and d["evidence_ids"] and any(f["addr"] == "0x140001190" for f in d["functions"])
    sid = d["session_id"]
    # analysed now: no analyze flag needed, and the same workbench session is reused
    big = c.get(url, params={"sort": "size", "limit": 3}).json()
    assert big["session_id"] == sid and big["more"] is True
    sizes = [f["size"] for f in big["functions"]]
    assert sizes == sorted(sizes, reverse=True) and all(f["addr"] != "0x140002b70" for f in big["functions"])
    row = c.get(url, params={"contains": "140002b70"}).json()["functions"][0]
    assert row["size"] < 100 and row["span"] > 20000

    async def mcp(args):
        async with Client(build_server(studio, ServerConfig(toolset="re"))) as cl:
            res = await cl.call_tool("list_functions", {"session_id": sid, **args})
        body = json.loads(res.content[0].text)
        assert body["ok"], body
        return body["data"]

    def unwrap(v):
        if isinstance(v, dict) and v.get("untrusted") is True and "text" in v:
            return v["text"]
        if isinstance(v, dict):
            return {k: unwrap(x) for k, x in v.items()}
        if isinstance(v, list):
            return [unwrap(x) for x in v]
        return v

    for args in ({}, {"sort": "size", "limit": 7}, {"sort": "name", "offset": 5, "limit": 11}, {"contains": "fcn", "sort": "size", "limit": 20},
                 {"contains": "entry"}, {"offset": 10_000}):
        via_mcp = anyio.run(mcp, args)
        via_http = c.get(url, params=args).json()
        assert unwrap(via_mcp["functions"]) == via_http["functions"], args
        assert (via_mcp["total"], via_mcp["offset"], via_mcp["returned"]) == (via_http["total"], via_http["offset"], via_http["returned"]), args
