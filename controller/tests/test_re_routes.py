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


# =============================================================================================== piece 2: one function
FN_INFO = {"offset": 0x401000, "addr": "0x401000", "name": "entry0", "size": 23718, "realsz": 40, "nbbs": 3, "cc": 2,
           "calltype": "ms", "signature": "void entry0(void);", "minbound": 0x401000, "maxbound": 0x406ca6, "n_callrefs": 2,
           "n_datarefs": 0, "callees": 1}
OPS = [{"offset": 0x401000 + i, "addr": hex(0x401000 + i), "size": 1, "bytes": "90", "disasm": "nop", "type": "nop"} for i in range(7)]


class DetailBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.decompile_result = OperationResult(ok=True, data={
            "function": "entry0", "addr": "0x401000", "decompiler": "rz-ghidra(pdg)", "is_real_decompiler": True,
            "decompiled": {"untrusted": True, "note": "n", "text": "void entry0(void)\n{\n    <script>alert(1)</script>;\n}\n",
                           "truncated": False, "total_bytes": 50}}, evidence_ids=["ev_d"])
        self.targets: list[str] = []

    def op_function(self, ctx, *, case_id, module_id, function):
        self.targets.append(function)
        if function != "0x401000":
            return OperationResult(ok=False, error=f"invalid target: no analysed function contains {function}")
        return OperationResult(ok=True, data={"function": dict(FN_INFO), "addr": "0x401000", "blocks": [], "variables": [],
                                              "annotations": {"function": None, "locals": {}, "comments": []}}, evidence_ids=["ev_f"])

    def op_decompile(self, ctx, *, case_id, module_id, function):
        return self.decompile_result

    def op_disasm(self, ctx, *, case_id, module_id, function):
        return OperationResult(ok=True, data={"untrusted": True, "note": "n", "function": "entry0", "addr": "0x401000",
                                              "disasm": {"name": "entry0", "addr": 0x401000, "size": 7, "ops": list(OPS)}},
                               evidence_ids=["ev_s"])


class _NoGhidra:
    class _Probe:
        class availability:
            value = "missing"
        detail = "GHIDRA_INSTALL_DIR is not set"
        version = None

    def tool_probe(self):
        return self._Probe()


def test_function_detail_routes_header_decompile_disassembly(studio, tmp_path, monkeypatch):
    case = _case(studio, tmp_path)
    mid = studio.cases.add_module(case["case_id"], "a.exe", "0" * 64, 66, "pe", "native_pe")
    fake = DetailBackend()
    monkeypatch.setattr(studio.re, "backend", lambda: fake)
    monkeypatch.setattr(studio.re, "ghidra_backend", lambda: _NoGhidra())
    c = _client(studio)
    base = f"/cases/{case['case_id']}/modules/{mid}/functions"

    r = c.get(f"{base}/401000")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_analyzed"          # same gate as the list
    studio.cases.add_evidence(case["case_id"], "native.functions", "functions", body={"functions": []}, module_id=mid)
    r = c.get(f"{base}/401000")
    assert r.status_code == 200, r.text
    d = r.json()
    assert fake.targets[-1] == "0x401000"                                                # address normalised to 0x-hex
    h = d["header"]
    assert h["name"] == "entry0" and h["size"] == 40 and h["span"] == 23718 and h["signature"] == "void entry0(void);"
    assert h["range"] == {"start": "0x401000", "end": "0x406ca6"} and h["cc"] == 2 and h["calltype"] == "ms"
    assert d["untrusted"] is True and d["evidence_ids"] == ["ev_f"] and d["session_id"].startswith("res_")
    dec = {x["id"]: x for x in d["decompilers"]}
    assert dec["ghidra"]["available"] is False and "GHIDRA_INSTALL_DIR" in dec["ghidra"]["reason"]
    assert dec["rizin"]["available"] is True

    dz = c.get(f"{base}/0x401000/decompiled").json()
    assert dz["engine"] == "rizin" and dz["is_real_decompiler"] is True and dz["decompiler"] == "rz-ghidra(pdg)"
    assert dz["decompiled"]["text"].startswith("void entry0") and "<script>" in dz["decompiled"]["text"]   # plain text in JSON
    assert dz["untrusted"] is True and dz["requested"] == "rizin" and dz["evidence_ids"] == ["ev_d"]
    fake.decompile_result = OperationResult(ok=True, data={"function": "entry0", "addr": "0x401000", "decompiler": "pdc(pseudo)",
                                                           "is_real_decompiler": False, "decompiled": {"text": "x", "truncated": True}},
                                            truncated=True)
    dz = c.get(f"{base}/0x401000/decompiled").json()
    assert dz["is_real_decompiler"] is False and dz["truncated"] is True

    g = c.get(f"{base}/0x401000/decompiled", params={"decompiler": "ghidra"})
    assert g.status_code == 409
    e = g.json()["error"]
    assert e["code"] == "decompiler_unavailable" and "Ghidra is not installed" in e["message"] and "rizin" in e["next_action"]
    assert c.get(f"{base}/0x401000/decompiled", params={"decompiler": "ida"}).status_code == 400

    s = c.get(f"{base}/0x401000/disassembly", params={"limit": 3, "offset": 2}).json()
    assert [o["addr"] for o in s["ops"]] == ["0x401002", "0x401003", "0x401004"]
    assert s["total"] == 7 and s["returned"] == 3 and s["more"] is True and s["untrusted"] is True and s["function"] == "entry0"
    assert c.get(f"{base}/0x401000/disassembly", params={"limit": 5000}).status_code == 400

    nf = c.get(f"{base}/0x10")
    assert nf.status_code == 404 and nf.json()["error"]["code"] == "function_not_found" and nf.json()["error"]["next_action"]
    assert c.get(f"{base}/not-hex").status_code == 400
    assert c.get(f"{base}/12345678901234567").status_code == 400                         # more than 16 hex digits


def test_function_detail_routes_refuse_non_native(studio, tmp_path):
    case = _case(studio, tmp_path)
    mid = studio.cases.add_module(case["case_id"], "App.dll", "1" * 64, 10, "pe", "dotnet")
    r = _client(studio).get(f"/cases/{case['case_id']}/modules/{mid}/functions/0x401000/decompiled")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_native"


def _unwrap(v):
    """Undo the MCP untrusted wrapping ({"untrusted": true, "text": ...} and untrusted_entries for program-supplied keys)."""
    if isinstance(v, dict) and v.get("untrusted") is True and set(v) == {"untrusted", "text"}:
        return v["text"]
    if isinstance(v, dict) and set(v) == {"untrusted_entries"}:
        return {_unwrap(e["key"]): _unwrap(e["value"]) for e in v["untrusted_entries"]}
    if isinstance(v, dict):
        return {k: _unwrap(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_unwrap(x) for x in v]
    return v


@pytest.mark.skipif(not _rizin() or not PECLI.is_file(), reason="needs rizin (REBUILD_STUDIO_TOOLS) and the pecli fixture")
def test_function_detail_routes_on_pecli_match_the_mcp_tools(studio, tmp_path):
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
    base = f"/cases/{case['case_id']}/modules/{mid}/functions"
    assert c.get(f"{base}/0x140001190").json()["error"]["code"] == "not_analyzed"
    sid = c.get(base, params={"analyze": "true", "limit": 1}).json()["session_id"]

    async def mcp(tool, args):
        async with Client(build_server(studio, ServerConfig(toolset="re"))) as cl:
            res = await cl.call_tool(tool, {"session_id": sid, **args})
        body = json.loads(res.content[0].text)
        assert body["ok"], (tool, args, body)
        return body

    for fn in ("0x140001190", "0x140002b70", "140001000"):
        d = c.get(f"{base}/{fn}")
        assert d.status_code == 200, d.text
        d = d.json()
        assert d["session_id"] == sid
        md = _unwrap(anyio.run(mcp, "get_function", {"address": fn})["data"])
        for k in ("function", "addr", "blocks", "variables", "annotations"):
            assert md[k] == d[k], (fn, k)
        info = md["function"]
        assert d["header"]["addr"] == info["addr"] and d["header"]["size"] == (info.get("realsz") or info["size"])
        assert ("span" in d["header"]) == (info["size"] != d["header"]["size"])

        z = c.get(f"{base}/{fn}/decompiled").json()
        mz = _unwrap(anyio.run(mcp, "decompile", {"address": fn})["data"])
        for k in ("function", "addr", "decompiler", "is_real_decompiler"):
            assert mz[k] == z[k], (fn, k)
        assert mz["decompiled"]["text"] == z["decompiled"]["text"] and z["decompiled"]["text"].strip()
        assert z["engine"] == "rizin" and z["untrusted"] is True

        s = c.get(f"{base}/{fn}/disassembly", params={"limit": 2000}).json()
        ms = anyio.run(mcp, "disassemble", {"address": fn})
        assert not ms["truncated"], fn
        msd = _unwrap(ms["data"])
        assert s["ops"] == msd["disasm"]["ops"] and s["total"] == len(s["ops"]) > 0, fn
        assert s["function"] == msd["function"] and s["addr"] == msd["addr"]
        if s["total"] > 3:                                                   # paging returns the same rows in slices
            p = c.get(f"{base}/{fn}/disassembly", params={"offset": 1, "limit": 2}).json()
            assert p["ops"] == s["ops"][1:3] and p["more"] is True

    r = c.get(f"{base}/0x10/decompiled")
    assert r.status_code == 404 and r.json()["error"]["code"] == "function_not_found"
