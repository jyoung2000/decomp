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


# =============================================================================================== piece 3: references + call graph
XREFS = {"addr": "0x401000", "scope": "function", "function": "entry0", "internal_jumps": 4, "to_total": 3, "from_total": 4,
         "to": [{"from": 0x402010, "to": 0x401000, "type": "CALL", "kind": "call", "fcn_addr": 0x402000, "fcn_name": "main"},
                {"from": 0x403020, "to": 0x401000, "type": "CODE", "kind": "jump", "fcn_addr": 0x403000, "fcn_name": "thunk"},
                {"from": 0x405000, "to": 0x401000, "type": "DATA", "kind": "data", "fcn_addr": None, "fcn_name": None, "secret": 1}],
         "from": [{"from": 0x401004, "to": 0x402000, "type": "CALL", "kind": "call", "to_fcn_addr": 0x402000, "to_fcn_name": "main"},
                  {"from": 0x401008, "to": 0x406000, "type": "DATA", "kind": "string", "to_fcn_addr": None, "to_fcn_name": None,
                   "string": "<b>hello</b>"},
                  {"from": 0x40100c, "to": 0x407000, "type": "DATA", "kind": "data", "to_fcn_addr": None, "to_fcn_name": None,
                   "name": "KERNEL32.dll!ExitProcess"},
                  {"from": 0x401010, "to": 0x404000, "type": "CODE", "kind": "jump", "to_fcn_addr": 0x404000, "to_fcn_name": "tail"}]}
# entry0 -> main -> entry0 is a cycle; the cap cut a branch under main
GRAPH = {"root": "0x401000", "direction": "callees", "depth": 3, "node_limit_hit": True,
         "nodes": [{"addr": "0x401000", "name": "entry0", "depth": 0}, {"addr": "0x402000", "name": "main", "depth": 1},
                   {"addr": "0x404000", "name": "tail", "depth": 1}],
         "edges": [{"from": "0x401000", "to": "0x402000"}, {"from": "0x401000", "to": "0x404000"},
                   {"from": "0x402000", "to": "0x401000"}]}


class XrefBackend(DetailBackend):
    def __init__(self):
        super().__init__()
        self.xref_calls: list[tuple[str, str]] = []
        self.graph_calls: list[dict] = []

    def op_xrefs(self, ctx, *, case_id, module_id, addr, scope="address"):
        self.xref_calls.append((addr, scope))
        if addr != "0x401000":
            return OperationResult(ok=False, error=f"invalid target: no analysed function contains {addr}")
        return OperationResult(ok=True, data={**json.loads(json.dumps(XREFS)), "cached": False}, evidence_ids=["ev_x"])

    def op_call_graph(self, ctx, *, case_id, module_id, function, depth=2, direction="callees", max_nodes=200):
        self.graph_calls.append({"function": function, "depth": depth, "direction": direction, "max_nodes": max_nodes})
        return OperationResult(ok=True, data={**json.loads(json.dumps(GRAPH)), "direction": direction, "depth": depth,
                                              "cached": True}, evidence_ids=["ev_g"], truncated=True)


def test_shape_xrefs_hexifies_keeps_fixed_fields_and_caps():
    from rebuild_controller.re_workbench import shape_xrefs
    p = shape_xrefs(XREFS, "to", 2)
    assert p["total"] == 3 and p["more"] is True and len(p["refs"]) == 2 and p["direction"] == "to"
    assert p["refs"][0] == {"from": "0x402010", "to": "0x401000", "type": "CALL", "kind": "call", "fcn_addr": "0x402000",
                            "fcn_name": "main"}
    assert "secret" not in shape_xrefs(XREFS, "to", 10)["refs"][2]
    f = shape_xrefs(XREFS, "from", 10)["refs"]
    assert f[1]["string"] == "<b>hello</b>" and f[2]["name"] == "KERNEL32.dll!ExitProcess" and f[3]["to_fcn_addr"] == "0x404000"
    # the backend's own cap: total reports what it saw, more is set
    capped = shape_xrefs({**XREFS, "to_total": 2500}, "to", 1000)
    assert capped["total"] == 2500 and capped["more"] is True
    with pytest.raises(WorkbenchError):
        shape_xrefs(XREFS, "sideways", 1)
    with pytest.raises(WorkbenchError):
        shape_xrefs(XREFS, "to", 0)


def test_xrefs_and_call_graph_routes(studio, tmp_path, monkeypatch):
    case = _case(studio, tmp_path)
    mid = studio.cases.add_module(case["case_id"], "a.exe", "0" * 64, 66, "pe", "native_pe")
    fake = XrefBackend()
    monkeypatch.setattr(studio.re, "backend", lambda: fake)
    c = _client(studio)
    base = f"/cases/{case['case_id']}/modules/{mid}/functions"
    r = c.get(f"{base}/401000/xrefs")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_analyzed" and fake.xref_calls == []
    assert c.get(f"{base}/401000/callgraph").json()["error"]["code"] == "not_analyzed" and fake.graph_calls == []
    studio.cases.add_evidence(case["case_id"], "native.functions", "functions", body={"functions": []}, module_id=mid)

    r = c.get(f"{base}/401000/xrefs", params={"limit": 2})
    assert r.status_code == 200, r.text
    d = r.json()
    assert fake.xref_calls[-1] == ("0x401000", "function")
    assert d["function"] == "entry0" and d["addr"] == "0x401000" and d["internal_jumps"] == 4 and d["limit"] == 2
    assert d["to"]["total"] == 3 and d["to"]["more"] is True and [x["kind"] for x in d["to"]["refs"]] == ["call", "jump"]
    assert d["to"]["refs"][0]["fcn_name"] == "main" and d["to"]["refs"][0]["from"] == "0x402010"
    assert d["from"]["total"] == 4 and d["from"]["refs"][1]["string"] == "<b>hello</b>"         # plain text in JSON
    assert d["untrusted"] is True and d["note"] and d["evidence_ids"] == ["ev_x"] and d["session_id"].startswith("res_")
    assert c.get(f"{base}/401000/xrefs", params={"limit": 1001}).status_code == 400
    nf = c.get(f"{base}/0x10/xrefs")
    assert nf.status_code == 404 and nf.json()["error"]["code"] == "function_not_found" and nf.json()["error"]["next_action"]
    assert c.get(f"{base}/zz/xrefs").status_code == 400

    g = c.get(f"{base}/0x401000/callgraph", params={"direction": "both", "depth": 3, "max_nodes": 3})
    assert g.status_code == 200, g.text
    gd = g.json()
    assert fake.graph_calls[-1] == {"function": "0x401000", "depth": 3, "direction": "both", "max_nodes": 3}
    assert gd["node_limit_hit"] is True and gd["truncated"] is True and gd["cached"] is True and gd["max_nodes"] == 3
    assert {"from": "0x402000", "to": "0x401000"} in gd["edges"]                                 # the cycle edge is kept
    assert len({n["addr"] for n in gd["nodes"]}) == len(gd["nodes"]) == 3
    assert gd["untrusted"] is True and gd["root"] == "0x401000"
    assert c.get(f"{base}/0x401000/callgraph").json()["direction"] == "callees"                  # defaults
    assert fake.graph_calls[-1] == {"function": "0x401000", "depth": 2, "direction": "callees", "max_nodes": 200}
    for bad in ({"depth": 0}, {"depth": 6}, {"max_nodes": 0}, {"max_nodes": 501}, {"direction": "up"}):
        assert c.get(f"{base}/0x401000/callgraph", params=bad).status_code == 400, bad


def test_xrefs_routes_refuse_non_native(studio, tmp_path):
    case = _case(studio, tmp_path)
    mid = studio.cases.add_module(case["case_id"], "App.dll", "1" * 64, 10, "pe", "dotnet")
    c = _client(studio)
    for tail in ("xrefs", "callgraph"):
        r = c.get(f"/cases/{case['case_id']}/modules/{mid}/functions/0x401000/{tail}")
        assert r.status_code == 409 and r.json()["error"]["code"] == "not_native"


@pytest.mark.skipif(not _rizin() or not PECLI.is_file(), reason="needs rizin (REBUILD_STUDIO_TOOLS) and the pecli fixture")
def test_xrefs_and_call_graph_routes_on_pecli_match_the_mcp_tools(studio, tmp_path):
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
    assert c.get(f"{base}/0x140001190/xrefs").json()["error"]["code"] == "not_analyzed"
    sid = c.get(base, params={"analyze": "true", "limit": 1}).json()["session_id"]

    async def mcp(tool, args):
        async with Client(build_server(studio, ServerConfig(toolset="re"))) as cl:
            res = await cl.call_tool(tool, {"session_id": sid, **args})
        body = json.loads(res.content[0].text)
        assert body["ok"], (tool, args, body)
        return body

    # 0x140001190 is called twice from one function; an address inside it resolves to the same function
    x = c.get(f"{base}/0x140001190/xrefs")
    assert x.status_code == 200, x.text
    x = x.json()
    assert x["addr"] == "0x140001190" and x["session_id"] == sid and x["untrusted"] is True
    callers = [r for r in x["to"]["refs"] if r["kind"] == "call"]
    assert {r["from"] for r in callers} == {"0x1400013f5", "0x140001425"}
    assert all(r["fcn_addr"] and r["fcn_name"] and int(r["fcn_addr"], 16) <= int(r["from"], 16) for r in callers)
    kinds = {r["kind"] for r in x["from"]["refs"]}
    assert "call" in kinds and kinds <= {"call", "jump", "data", "string"}
    assert x["internal_jumps"] > 0                                     # jumps inside the function are left to the disassembly
    calls = [r for r in x["from"]["refs"] if r["kind"] == "call"]
    assert any(r["to_fcn_name"] for r in calls)
    inner = c.get(f"{base}/0x1400011b4/xrefs").json()
    assert inner["addr"] == "0x140001190" and inner["to"] == x["to"] and inner["from"] == x["from"]

    for fn in ("0x140001190", "0x140001440", "0x140002b70"):
        h = c.get(f"{base}/{fn}/xrefs", params={"limit": 1000}).json()
        for direction, tool in (("to", "xrefs_to"), ("from", "xrefs_from")):
            m = _unwrap(anyio.run(mcp, tool, {"address": fn, "scope": "function", "limit": 1000})["data"])
            assert m["refs"] == h[direction]["refs"], (fn, direction)
            assert m["total"] == h[direction]["total"] and m["addr"] == h["addr"] and m["scope"] == "function", (fn, direction)
        small = c.get(f"{base}/{fn}/xrefs", params={"limit": 1}).json()
        assert small["from"]["refs"] == h["from"]["refs"][:1] and small["from"]["more"] == (h["from"]["total"] > 1)
    # the MCP default (scope=address) is unchanged: references of the one address only
    one = _unwrap(anyio.run(mcp, "xrefs_from", {"address": "0x140001190"})["data"])
    assert one["scope"] == "address" and len(one["refs"]) < x["from"]["total"]

    for args in ({}, {"direction": "callers", "depth": 1}, {"direction": "both", "depth": 3, "max_nodes": 40},
                 {"direction": "callees", "depth": 5, "max_nodes": 5}):
        fn = "0x140001190"
        h = c.get(f"{base}/{fn}/callgraph", params=args)
        assert h.status_code == 200, h.text
        h = h.json()
        m = _unwrap(anyio.run(mcp, "call_graph", {"address": fn, **args})["data"])
        for k in ("root", "direction", "depth", "nodes", "edges", "node_limit_hit"):
            assert m[k] == h[k], (args, k)
        addrs = [n["addr"] for n in h["nodes"]]
        assert len(addrs) == len(set(addrs)) <= h["max_nodes"] and h["root"] == fn == addrs[0]
        assert all(e["from"] in addrs and e["to"] in addrs for e in h["edges"])
        assert all(0 <= n["depth"] <= h["depth"] for n in h["nodes"])
    capped = c.get(f"{base}/0x140001190/callgraph", params={"depth": 5, "max_nodes": 2}).json()
    assert capped["node_limit_hit"] is True and capped["truncated"] is True and len(capped["nodes"]) == 2
    # both directions from a callee: its caller appears and the edge back to the root is not duplicated
    both = c.get(f"{base}/0x140001440/callgraph", params={"direction": "both", "depth": 2}).json()
    pairs = [(e["from"], e["to"]) for e in both["edges"]]
    assert len(pairs) == len(set(pairs)) and any(t == "0x140001440" for _, t in pairs)


# =============================================================================================== piece 4: strings, imports, exports, sections, used by
STRS = {"untrusted": True, "seen": 5, "count_truncated": False,
        "strings": [{"vaddr": 0x406000, "paddr": 0x5000, "length": 12, "section": ".rdata", "type": "ascii", "string": "<b>hello</b>"},
                    {"vaddr": 0x406010, "paddr": 0x5010, "length": 3, "section": ".rdata", "type": "ascii", "string": "abc"},
                    {"vaddr": 0x406020, "paddr": 0x5020, "length": 9, "section": ".data", "type": "utf16le", "string": "Hello%d\n!"},
                    {"vaddr": 0x406030, "paddr": 0x5030, "length": 1500, "section": ".rdata", "type": "ascii", "string": "x" * 1000},
                    "junk"]}
IMPS = {"imports": [{"ordinal": 1, "type": "FUNC", "name": "ExitProcess", "libname": "KERNEL32.dll", "plt": 0x407000},
                    {"ordinal": 2, "type": "FUNC", "name": "printf", "libname": "msvcrt.dll", "plt": 0x407008},
                    {"ordinal": 3, "type": "FUNC", "name": "GetLastError", "libname": "KERNEL32.dll", "plt": 0x407010}], "count": 3}
EXPS = {"exports": [{"name": "Open", "realname": "ItemStore_Open", "vaddr": 0x401000, "type": "FUNC"}], "count": 1}
SECS = {"sections": [{"name": ".text", "size": 512, "vsize": 4096, "perm": "-r-x", "paddr": 0x400, "vaddr": 0x401000},
                     {"name": ".rsrc", "size": 8192, "vsize": 8192, "perm": "-r--", "paddr": 0x600, "vaddr": 0x408000}],
        "segments": []}
USED = {"addr": "0x406000", "scope": "address", "to_total": 3, "from_total": 0, "from": [],
        "to": [{"from": 0x401010, "to": 0x406000, "type": "DATA", "kind": "data", "fcn_addr": 0x401000, "fcn_name": "entry0"},
               {"from": 0x401020, "to": 0x406000, "type": "DATA", "kind": "data", "fcn_addr": 0x401000, "fcn_name": "entry0"},
               {"from": 0x409000, "to": 0x406000, "type": "DATA", "kind": "data", "fcn_addr": None, "fcn_name": None}]}


class ListBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.ops: list[tuple] = []

    def _r(self, body):
        return OperationResult(ok=True, data={**json.loads(json.dumps(body)), "cached": False}, evidence_ids=["ev_l"])

    def op_strings(self, ctx, *, case_id, module_id, limit=2000):
        self.ops.append(("strings", limit))
        return self._r(STRS)

    def op_imports(self, ctx, *, case_id, module_id):
        self.ops.append(("imports",))
        return self._r(IMPS)

    def op_exports(self, ctx, *, case_id, module_id):
        self.ops.append(("exports",))
        return self._r(EXPS)

    def op_sections(self, ctx, *, case_id, module_id):
        return self._r(SECS)

    def op_entrypoints(self, ctx, *, case_id, module_id):
        return self._r({"entrypoints": [{"vaddr": 0x401000, "type": "program", "addr": "0x401000"}]})

    def op_packer_report(self, ctx, *, case_id, module_id):
        return self._r({"report": {"sections": [{"name": ".text", "entropy": 6.1}, {"name": ".rsrc", "entropy": 7.9}]}})

    def op_xrefs(self, ctx, *, case_id, module_id, addr, scope="address"):
        self.ops.append(("xrefs", addr, scope))
        return self._r({**USED, "addr": addr})


def test_shared_list_shapers():
    from rebuild_controller.re_workbench import (ReWorkbench, add_section_entropy, shape_sections, shape_strings, shape_table,
                                                 string_sections)
    d = json.loads(json.dumps(STRS))
    p = shape_strings(d, min_length=4)
    assert [x["addr"] for x in p["strings"]] == ["0x406000", "0x406020", "0x406030"] and p["total"] == 3 and p["scanned"] == 5
    assert d["strings"][0].get("addr") is None                                   # the cached evidence is not mutated
    assert [x["string"] for x in shape_strings(d, contains="HELLO")["strings"]] == ["<b>hello</b>", "Hello%d\n!"]
    assert [x["section"] for x in shape_strings(d, section=".data")["strings"]] == [".data"]
    rx = ReWorkbench.compile_regex(r"^H\w+%d")
    assert [x["addr"] for x in shape_strings(d, regex=rx)["strings"]] == ["0x406020"]
    assert shape_strings(d, min_length=1, offset=1, limit=2)["more"] is True
    for bad in ("(a+)+", r"(\w)\1", "x" * 201, "("):
        with pytest.raises(WorkbenchError):
            ReWorkbench.compile_regex(bad)
    assert string_sections(d) == [{"section": ".data", "count": 1}, {"section": ".rdata", "count": 3}]
    t = shape_table(json.loads(json.dumps(IMPS)), "imports", "kernel32", 0, 1)
    assert t["imports"][0]["addr"] == "0x407000" and t["total"] == 2 and t["more"] is True
    secs = shape_sections(json.loads(json.dumps(SECS)))["sections"]
    assert secs[0]["vaddr"] == "0x401000" and secs[0]["paddr"] == "0x400"
    assert add_section_entropy(secs, {"sections": [{"name": ".text", "entropy": 6.0}, {"name": ".rsrc", "entropy": 7.5}]})
    assert (secs[0]["high_entropy"], secs[1]["high_entropy"]) == (False, True)
    assert add_section_entropy(secs, None) is False


def test_module_list_routes_and_used_by(studio, tmp_path, monkeypatch):
    case = _case(studio, tmp_path)
    mid = studio.cases.add_module(case["case_id"], "a.exe", "0" * 64, 66, "pe", "native_pe")
    fake = ListBackend()
    monkeypatch.setattr(studio.re, "backend", lambda: fake)
    c = _client(studio)
    base = f"/cases/{case['case_id']}/modules/{mid}"

    # lists need no analysis
    r = c.get(f"{base}/strings", params={"min_length": 4, "limit": 2})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 3 and d["returned"] == 2 and d["more"] is True and d["untrusted"] is True and d["note"]
    assert d["strings"][0]["string"] == "<b>hello</b>" and d["strings"][0]["type"] == "ascii"   # verbatim; the app renders text
    assert {s["section"] for s in d["sections"]} == {".rdata", ".data"}
    from rebuild_controller.backends.rizin_worker import MAX_STRINGS
    assert fake.ops[-1] == ("strings", MAX_STRINGS)                     # the same (cached) call as the MCP strings tool
    assert c.get(f"{base}/strings", params={"regex": "hel+o", "section": ".rdata"}).json()["total"] == 1
    e = c.get(f"{base}/strings", params={"regex": "(a+)+"})
    assert e.status_code == 400 and e.json()["error"]["code"] == "invalid_regex" and e.json()["error"]["next_action"]
    assert c.get(f"{base}/strings", params={"regex": "("}).json()["error"]["code"] == "invalid_regex"
    assert c.get(f"{base}/strings", params={"contains": "a\x01"}).json()["error"]["code"] == "validation"
    for bad in ({"section": "a b"}, {"limit": 501}, {"min_length": 0}, {"regex": "x" * 201}):
        assert c.get(f"{base}/strings", params=bad).status_code == 400, bad

    i = c.get(f"{base}/imports", params={"contains": "KERNEL32", "limit": 1}).json()
    assert i["imports"][0]["name"] == "ExitProcess" and i["imports"][0]["addr"] == "0x407000" and i["total"] == 2 and i["more"]
    assert c.get(f"{base}/imports", params={"limit": 1001}).status_code == 400
    x = c.get(f"{base}/exports").json()
    assert x["exports"][0]["addr"] == "0x401000" and x["total"] == 1 and x["untrusted"] is True
    s = c.get(f"{base}/sections").json()
    assert [(z["name"], z["vaddr"], z["entropy"], z["high_entropy"]) for z in s["sections"]] == [
        (".text", "0x401000", 6.1, False), (".rsrc", "0x408000", 7.9, True)]
    assert s["entrypoints"][0]["addr"] == "0x401000" and s["entropy_source"] == "packer report"

    # used by: references need the analysis, which runs only when asked
    u = c.get(f"{base}/used-by/0x406000")
    assert u.status_code == 409 and u.json()["error"]["code"] == "not_analyzed" and not any(o[0] == "xrefs" for o in fake.ops)
    u = c.get(f"{base}/used-by/406000", params={"analyze": "true", "limit": 2})
    assert u.status_code == 200, u.text
    u = u.json()
    assert fake.ops[-1] == ("xrefs", "0x406000", "address") and fake.calls == 1      # op_functions recorded the analysis
    assert [r["from"] for r in u["refs"]] == ["0x401010", "0x401020"] and u["total"] == 3 and u["more"] is True
    assert u["functions"] == [{"addr": "0x401000", "name": "entry0"}] and u["untrusted"] is True
    studio.cases.add_evidence(case["case_id"], "native.functions", "functions", body={"functions": []}, module_id=mid)
    assert c.get(f"{base}/used-by/0x406000").status_code == 200                       # analysed: no prompt again
    assert c.get(f"{base}/used-by/zz").status_code == 400
    assert c.get(f"{base}/used-by/0x406000", params={"limit": 1001}).status_code == 400

    monkeypatch.setattr(studio.re, "backend", lambda: FakeBackend(tool=None))
    m = c.get(f"{base}/strings").json()["error"]
    assert m["code"] == "engine_missing" and "strings" in m["message"] and "Tools" in m["next_action"]


def test_module_list_routes_refuse_non_native(studio, tmp_path):
    case = _case(studio, tmp_path)
    mid = studio.cases.add_module(case["case_id"], "App.dll", "1" * 64, 10, "pe", "dotnet")
    c = _client(studio)
    for tail in ("strings", "imports", "exports", "sections", "used-by/0x401000"):
        r = c.get(f"/cases/{case['case_id']}/modules/{mid}/{tail}")
        assert r.status_code == 409 and r.json()["error"]["code"] == "not_native", tail
    assert c.get(f"/cases/{case['case_id']}/modules/mod_nope/strings").status_code == 404


@pytest.mark.skipif(not _rizin() or not PECLI.is_file(), reason="needs rizin (REBUILD_STUDIO_TOOLS) and the pecli fixture")
def test_module_lists_on_pecli_match_the_mcp_tools(studio, tmp_path):
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
    base = f"/cases/{case['case_id']}/modules/{mid}"
    first = c.get(f"{base}/strings", params={"limit": 5})               # strings need no analysis
    assert first.status_code == 200, first.text
    sid = first.json()["session_id"]

    async def mcp(tool, args):
        async with Client(build_server(studio, ServerConfig(toolset="re"))) as cl:
            res = await cl.call_tool(tool, {"session_id": sid, **args})
        body = json.loads(res.content[0].text)
        assert body["ok"], (tool, args, body)
        return body

    for args in ({}, {"contains": "error", "limit": 10}, {"regex": r"^error: \w+", "min_length": 6},
                 {"section": ".rdata", "offset": 20, "limit": 15}, {"min_length": 20, "limit": 500}):
        h = c.get(f"{base}/strings", params=args).json()
        m = _unwrap(anyio.run(mcp, "strings", args)["data"])
        assert m["strings"] == h["strings"], args
        assert (m["total"], m["offset"], m["returned"], m["scanned"]) == (h["total"], h["offset"], h["returned"], h["scanned"]), args
        assert h["total"] > 0, args
    assert all(x["section"] == ".rdata" for x in c.get(f"{base}/strings", params={"section": ".rdata"}).json()["strings"])
    for tool in ("imports", "exports"):
        for args in ({}, {"contains": "kernel32", "offset": 2, "limit": 5}):
            h = c.get(f"{base}/{tool}", params=args).json()
            m = _unwrap(anyio.run(mcp, tool, args)["data"])
            assert m[tool] == h[tool] and m["total"] == h["total"], (tool, args)
    imps = c.get(f"{base}/imports", params={"limit": 1000}).json()
    assert {i["libname"] for i in imps["imports"]} >= {"KERNEL32.dll"} and imps["total"] == 55
    s = c.get(f"{base}/sections").json()
    ms = _unwrap(anyio.run(mcp, "sections", {})["data"])
    strip = lambda rows: [{k: v for k, v in r.items() if k not in ("entropy", "high_entropy")} for r in rows]  # noqa: E731
    assert strip(s["sections"]) == ms["sections"] and s["segments"] == ms["segments"]
    assert s["entropy_source"] == "packer report" and isinstance(s["sections"][0]["entropy"], float)
    assert s["entrypoints"] == _unwrap(anyio.run(mcp, "entry_points", {})["data"])["entrypoints"]
    assert s["entrypoints"][0]["addr"] == "0x140001410"

    # used by: a string used twice from one function, and an import called from three functions
    assert c.get(f"{base}/used-by/0x14000a378").json()["error"]["code"] == "not_analyzed"
    u = c.get(f"{base}/used-by/0x14000a378", params={"analyze": "true"})
    assert u.status_code == 200, u.text
    u = u.json()
    assert {(r["from"], r["fcn_name"]) for r in u["refs"]} == {("0x140002078", "fcn.140001d63"), ("0x140002143", "fcn.140001d63")}
    assert u["functions"] == [{"addr": "0x140001d63", "name": "fcn.140001d63"}]
    imp = next(i for i in imps["imports"] if i["name"] == "EnterCriticalSection")
    v = c.get(f"{base}/used-by/{imp['addr']}").json()
    assert ("0x140002d5b", "fcn.140002d40") in {(r["from"], r["fcn_name"]) for r in v["refs"]} and len(v["functions"]) >= 3
    for addr in ("0x14000a378", imp["addr"]):
        h = c.get(f"{base}/used-by/{addr}", params={"limit": 1000}).json()
        m = _unwrap(anyio.run(mcp, "xrefs_to", {"address": addr, "limit": 1000})["data"])
        assert m["refs"] == h["refs"] and m["total"] == h["total"] and m["addr"] == h["addr"], addr


# =============================================================================================== piece 5: edits (a person's annotations)
class EditBackend(FakeBackend):
    """Records every edit operation; ``refuse`` makes the next one fail the way the rizin backend reports a refusal."""

    def __init__(self):
        super().__init__()
        self.edits: list[tuple[str, dict]] = []
        self.refuse: str | None = None
        self.rev = 0

    def _edit(self, op, kw):
        self.edits.append((op, kw))
        if self.refuse:
            err, self.refuse = self.refuse, None
            return OperationResult(ok=False, error=err)
        self.rev += 1
        return OperationResult(ok=True, data={"change": {"action": op, "author": kw.get("author")}, "revision": self.rev,
                                              "applied_to_analysis": True, "note": "Persisted"}, evidence_ids=[f"ev_{self.rev}"])

    def op_rename(self, ctx, *, case_id, module_id, **kw):
        return self._edit("rename", kw)

    def op_set_type(self, ctx, *, case_id, module_id, **kw):
        return self._edit("set_type", kw)

    def op_add_comment(self, ctx, *, case_id, module_id, **kw):
        return self._edit("add_comment", kw)

    def op_apply_struct(self, ctx, *, case_id, module_id, **kw):
        return self._edit("apply_struct", kw)


def _analysed(studio, cid, mid):
    """What a real analysis leaves behind (the fake backend stores no evidence)."""
    from rebuild_controller.backends import native
    native.store_evidence(studio.cases, cid, mid, "native.functions", "Functions", {"functions": []}, {"op": "functions"}, untrusted=True)


def _edit_setup(studio, tmp_path, monkeypatch):
    case = _case(studio, tmp_path)
    cid = case["case_id"]
    mid = studio.cases.add_module(cid, "a.exe", "0" * 64, 66, "pe", "native_pe")
    fake = EditBackend()
    monkeypatch.setattr(studio.re, "backend", lambda: fake)
    c = _client(studio)
    return c, cid, mid, fake, f"/cases/{cid}/modules/{mid}/annotations"


def test_edit_routes_refuse_bad_values_with_the_mcp_message_and_an_example(studio, tmp_path, monkeypatch):
    from rebuild_controller.re_workbench import EditRefused
    c, cid, mid, fake, base = _edit_setup(studio, tmp_path, monkeypatch)
    r = c.post(f"{base}/rename", json={"kind": "function", "address": "0x401000", "new_name": "ok_name"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_analyzed" and not fake.edits    # nothing is analysed implicitly
    _analysed(studio, cid, mid)
    sid = studio.re.open(case_id=cid, module_id=mid)["session_id"]

    cases = [
        ("rename", {"kind": "function", "address": "401000", "new_name": "2nd try"}, "name", "starts with a digit", "parse_args",
         dict(kind="function", target="0x401000", new_name="2nd try")),
        ("rename", {"kind": "local", "address": "0x401000", "variable": "var_8h", "new_name": "buf-len"}, "name", "'-'", "parse_args",
         dict(kind="local", target="0x401000", variable="var_8h", new_name="buf-len")),
        ("set-type", {"kind": "function", "address": "0x401000", "prototype": "int f(int a"}, "prototype", "exactly one '('",
         "int parse_args(int argc, char **argv)", dict(kind="function", target="0x401000", prototype="int f(int a")),
        ("set-type", {"kind": "local", "address": "0x401000", "variable": "var_8h", "type": "int[4]"}, "type", "no array sizes",
         "struct point *", dict(kind="local", target="0x401000", variable="var_8h", ctype="int[4]")),
        ("comment", {"address": "0x401000", "text": "two\nlines"}, "comment", "single line", "Opens the settings file",
         dict(addr="0x401000", text="two\nlines")),
        ("declare", {"declaration": "#include <windows.h>\nstruct a { int x; };"}, "declaration", "'#'",
         "struct point { int x; int y; };", dict(declaration="#include <windows.h>\nstruct a { int x; };")),
        ("declare", {"declaration": "struct a { int x;"}, "declaration", "unbalanced braces", "struct point", dict(declaration="struct a { int x;")),
    ]
    action = {"rename": "rename", "set-type": "set_type", "comment": "comment", "declare": "declare"}
    for route, body, field, why, example, wb_args in cases:
        r = c.post(f"{base}/{route}", json=body)
        assert r.status_code == 400, (route, body, r.text)
        e = r.json()["error"]
        assert e["code"] == f"invalid_{field}" and why in e["message"] and example in e["next_action"], (route, e)
        # the MCP path (author=model) refuses the same value with the same message, before rizin is touched
        with pytest.raises(EditRefused) as ex:
            studio.re.edit(sid, action[route], author="model", **wb_args)
        assert ex.value.message == e["message"] and ex.value.next_action == e["next_action"] and ex.value.field == field
    assert not fake.edits, "nothing reaches the backend for a value the grammar refuses"
    # validate-as-you-type: the same grammar without storing anything
    ok = c.post(f"{base}/check", json={"what": "prototype", "value": "int  parse(char *s,  int n)"}).json()
    assert ok == {"ok": True, "what": "prototype", "normalized": "int parse(char *s, int n)", "example": "int parse_args(int argc, char **argv)"}
    bad = c.post(f"{base}/check", json={"what": "name", "value": "a b"}).json()
    assert bad["ok"] is False and "space" in bad["message"] and bad["example"] == "parse_args"
    assert c.post(f"{base}/check", json={"what": "declaration", "value": "typedef struct { int x; } pt_t;"}).json()["normalized"] == "typedef pt_t"
    assert c.post(f"{base}/check", json={"what": "shell", "value": "x"}).status_code == 400
    assert not fake.edits


def test_edit_routes_record_a_person_and_map_backend_refusals(studio, tmp_path, monkeypatch):
    c, cid, mid, fake, base = _edit_setup(studio, tmp_path, monkeypatch)
    _analysed(studio, cid, mid)
    r = c.post(f"{base}/rename", json={"kind": "function", "address": "401000", "new_name": "init_runtime"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["revision"] == 1 and d["who"] == "person" and d["author"] == "user" and d["evidence_ids"] == ["ev_1"] and d["untrusted"] is True
    assert fake.edits[-1] == ("rename", {"kind": "function", "target": "0x401000", "new_name": "init_runtime", "author": "user"})
    c.post(f"{base}/rename", json={"kind": "local", "name": "init_runtime", "variable": "var_38h", "new_name": "start"})
    assert fake.edits[-1] == ("rename", {"kind": "local", "target": "var_38h", "new_name": "start", "function": "init_runtime", "author": "user"})
    c.post(f"{base}/rename", json={"kind": "global", "address": "0x402000", "new_name": "g_lock"})
    assert fake.edits[-1][1]["target"] == "0x402000"
    c.post(f"{base}/set-type", json={"kind": "local", "address": "0x401000", "variable": "start", "type": "char *"})
    assert fake.edits[-1] == ("set_type", {"kind": "local", "target": "0x401000", "prototype": None, "variable": "start", "ctype": "char *",
                                           "author": "user"})
    c.post(f"{base}/set-type", json={"kind": "function", "address": "0x401000", "prototype": "int init_runtime(void)"})
    assert fake.edits[-1][1]["prototype"] == "int init_runtime(void)"
    r = c.post(f"{base}/comment", json={"address": "0x401004", "text": ""})                 # empty text removes
    assert r.status_code == 200 and fake.edits[-1] == ("add_comment", {"addr": "0x401004", "text": "", "author": "user"})
    r = c.post(f"{base}/declare", json={"declaration": "struct rs_point { int x; int y; };"})
    assert r.status_code == 200 and fake.edits[-1] == ("apply_struct", {"declaration": "struct rs_point { int x; int y; };", "author": "user"})
    assert all(kw["author"] == "user" for _, kw in fake.edits)
    n = len(fake.edits)
    # argument rules (shared with MCP), checked before the backend
    for route, body, code in (("rename", {"kind": "global", "name": "x", "new_name": "g"}, "validation"),
                              ("rename", {"kind": "function", "new_name": "g"}, "validation"),
                              ("rename", {"kind": "function", "address": "0x1", "name": "x", "new_name": "g"}, "validation"),
                              ("rename", {"kind": "local", "address": "0x401000", "new_name": "g"}, "rejected"),
                              ("rename", {"kind": "function", "address": "0x401000", "variable": "v", "new_name": "g"}, "rejected"),
                              ("set-type", {"kind": "local", "address": "0x401000", "type": "int"}, "rejected"),
                              ("set-type", {"kind": "function", "address": "0x401000", "prototype": "int f(void)", "type": "int"}, "rejected")):
        r = c.post(f"{base}/{route}", json=body)
        assert r.status_code == 400 and r.json()["error"]["code"] == code, (route, body, r.text)
    assert c.post(f"{base}/rename", json={"kind": "function", "address": "0x401000", "new_name": "g", "cmd": "q"}).status_code == 400
    assert c.post(f"{base}/comment", json={"address": "zz", "text": "x"}).status_code == 400
    assert len(fake.edits) == n
    # refusals that need the program are mapped to plain messages with a next step
    for err, code, status in (("invalid target: no variable 'var_9h' in main (variables: argc, argv)", "variable_not_found", 400),
                              ("invalid target: rizin did not accept type 'struct nope *' for 'v' (now 'int'); declare structs first "
                               "with apply_struct", "type_not_accepted", 400),
                              ("invalid target: no function at 0x10", "function_not_found", 404),
                              ("timeout: rizin took too long", "timeout", 504)):
        fake.refuse = err
        r = c.post(f"{base}/set-type", json={"kind": "local", "address": "0x401000", "variable": "v", "type": "int"})
        e = r.json()["error"]
        assert r.status_code == status and e["code"] == code and e["next_action"], (err, e)
        if code == "variable_not_found":
            assert "argc, argv" in e["message"]
        if code == "type_not_accepted":
            assert "Types" in e["next_action"] and "struct point" in e["next_action"]
    other = studio.cases.add_module(cid, "App.dll", "1" * 64, 10, "pe", "dotnet")
    r = c.post(f"/cases/{cid}/modules/{other}/annotations/rename", json={"kind": "function", "address": "0x1", "new_name": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_native"


@pytest.mark.skipif(not _rizin() or not PECLI.is_file(), reason="needs rizin (REBUILD_STUDIO_TOOLS) and the pecli fixture")
def test_app_edits_on_pecli_match_mcp_edits_persist_and_undo(settings, tmp_path):
    """A person's edits in the app and a model's edits over MCP go through one path and store the same annotations (only the
    provenance differs); they show in decompile/disassembly/the list, survive a rizin restart and a new controller, and undo /
    roll back restore earlier states."""
    import anyio
    pytest.importorskip("mcp")
    from mcp import Client
    from rebuild_controller.backends import re_annotations as ann
    from rebuild_controller.ids import sha256_file
    from rebuild_controller.mcp.server import ServerConfig, build_server
    from rebuild_controller.services import StudioServices

    FN, GLOBAL = "0x140001190", "0x140052270"
    studio = StudioServices(settings)
    studio.ai.advisor = None
    try:
        src = tmp_path / "src"
        src.mkdir()
        shutil.copy2(PECLI, src / "pecli.exe")
        case = _case(studio, tmp_path, src)
        cid = case["case_id"]
        sha, size = sha256_file(src / "pecli.exe"), (src / "pecli.exe").stat().st_size
        app_mid = studio.cases.add_module(cid, "pecli.exe", sha, size, "pe", "native_pe")
        mcp_mid = studio.cases.add_module(cid, "copy/pecli.exe", sha, size, "pe", "native_pe")   # same file, its own annotations
        (src / "copy").mkdir()
        shutil.copy2(PECLI, src / "copy" / "pecli.exe")
        c = _client(studio)
        mod = f"/cases/{cid}/modules/{app_mid}"
        assert c.get(f"{mod}/functions", params={"analyze": "true", "limit": 1}).status_code == 200
        original = c.get(f"{mod}/functions/{FN}").json()["header"]["name"]

        decl = "struct rs_point { int x; int y; char tag[8]; };"
        steps = [("declare", {"declaration": decl}, "apply_struct", {"declaration": decl}),
                 ("rename", {"kind": "function", "address": FN, "new_name": "init_runtime"},
                  "rename", {"kind": "function", "address": FN, "new_name": "init_runtime"}),
                 ("rename", {"kind": "local", "address": FN, "variable": "var_38h", "new_name": "start_ptr"},
                  "rename", {"kind": "local", "address": FN, "variable": "var_38h", "new_name": "start_ptr"}),
                 ("set-type", {"kind": "local", "address": FN, "variable": "start_ptr", "type": "struct rs_point *"},
                  "set_type", {"kind": "local", "address": FN, "variable": "start_ptr", "type": "struct rs_point *"}),
                 ("set-type", {"kind": "function", "name": "init_runtime", "prototype": "int init_runtime(int argc, char **argv)"},
                  "set_type", {"kind": "function", "name": "init_runtime", "prototype": "int init_runtime(int argc, char **argv)"}),
                 ("comment", {"address": FN, "text": "CRT start-up: runs initialisers"},
                  "add_comment", {"address": FN, "text": "CRT start-up: runs initialisers"}),
                 ("rename", {"kind": "global", "address": GLOBAL, "new_name": "g_init_lock"},
                  "rename", {"kind": "global", "address": GLOBAL, "new_name": "g_init_lock"})]
        revs = []
        for route, body, _, _ in steps:
            r = c.post(f"{mod}/annotations/{route}", json=body)
            assert r.status_code == 200, (route, r.text)
            assert r.json()["who"] == "person"
            revs.append(r.json()["revision"])
        assert revs == sorted(revs) and len(set(revs)) == len(revs)

        msid = studio.re.open(case_id=cid, module_id=mcp_mid)["session_id"]

        async def mcp(tool, args, ok=True):
            async with Client(build_server(studio, ServerConfig(toolset="re"))) as cl:
                res = await cl.call_tool(tool, {"session_id": msid, **args})
            try:
                body = json.loads(res.content[0].text)
            except ValueError:
                body = {"ok": False, "error": {"code": "validation", "message": res.content[0].text}}
            assert body["ok"] is ok, (tool, args, body)
            return body

        for _, _, tool, args in steps:
            anyio.run(mcp, tool, args)
        # equivalent annotations: same entries, only who made them differs
        def strip(state):
            s = json.loads(json.dumps(state))
            for table in ("functions", "globals", "comments"):
                for e in s[table].values():
                    e.pop("prov", None)
            for vars_ in s["locals"].values():
                for e in vars_.values():
                    e.pop("prov", None)
            return s
        _, app_state, _ = ann.load(studio.cases, cid, app_mid)
        _, mcp_state, _ = ann.load(studio.cases, cid, mcp_mid)
        assert strip(app_state) == strip(mcp_state)
        assert app_state["functions"][FN]["prov"]["origin"] == "user" and mcp_state["functions"][FN]["prov"]["status"] == "proposed"
        app_hist = ann.history(studio.cases, cid, app_mid)
        mcp_hist = ann.history(studio.cases, cid, mcp_mid)
        assert [h["who"] for h in app_hist] == ["person"] * 7 and [h["who"] for h in mcp_hist] == ["model-proposed"] * 7
        drop = ("at", "author", "label")
        assert [{k: v for k, v in h["change"].items() if k not in drop} for h in app_hist] == \
               [{k: v for k, v in h["change"].items() if k not in drop} for h in mcp_hist]
        view = c.get(f"{mod}/annotations?history=20").json()
        assert view["open_proposals"] == 0 and [h["who"] for h in view["history"]] == ["person"] * 7
        # the same invalid value is refused with the same message by both
        bad = c.post(f"{mod}/annotations/set-type", json={"kind": "function", "address": FN, "prototype": "int f(int a"}).json()["error"]
        via_mcp = anyio.run(lambda: mcp("set_type", {"kind": "function", "address": FN, "prototype": "int f(int a"}, ok=False))["error"]
        assert bad["message"] == via_mcp["message"] and bad["next_action"] == via_mcp["next_action"]
        r = c.post(f"{mod}/annotations/set-type", json={"kind": "local", "address": FN, "variable": "nope_var", "type": "int"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "variable_not_found" and "start_ptr" in r.json()["error"]["message"]

        def check(tag, present=True):
            dec = c.get(f"{mod}/functions/{FN}/decompiled").json()
            code = dec["decompiled"]["text"]
            assert ("init_runtime" in code) is present, (tag, code[:400])
            assert ("CRT start-up: runs initialisers" in code) is present, tag
            if present and dec["is_real_decompiler"]:
                assert "start_ptr" in code and "rs_point" in code, (tag, code[:800])
            det = c.get(f"{mod}/functions/{FN}").json()
            vars_ = {v["name"]: v["type"] for v in det["variables"]}
            if present:
                assert det["header"]["name"] == "init_runtime" and vars_.get("start_ptr") == "struct rs_point *", (tag, vars_)
                assert "init_runtime(" in det["header"]["signature"], tag
            else:
                assert det["header"]["name"] == original and "start_ptr" not in vars_, (tag, det["header"])
            ops = c.get(f"{mod}/functions/{FN}/disassembly", params={"limit": 1}).json()["ops"]
            assert (ops[0].get("user_comment") == "CRT start-up: runs initialisers") is present, tag
            rows = c.get(f"{mod}/functions", params={"contains": "init_runtime"}).json()["functions"]
            assert ([f["addr"] for f in rows] == [FN]) is present, (tag, rows)

        check("live")
        for s in studio.re.backend().pool.sessions():
            s.kill()
        check("after a rizin restart")
        studio.stop()
        studio = StudioServices(settings)                       # a brand-new controller on the same data folder
        studio.ai.advisor = None
        c = _client(studio)
        check("new controller")

        # undo the last change (the global): roll back to the previous revision through the R5 route
        r = c.post(f"{mod}/annotations/rollback", json={"revision": revs[-2]})
        assert r.status_code == 200 and r.json()["change"]["to_revision"] == revs[-2]
        _, st, _ = ann.load(studio.cases, cid, app_mid)
        assert GLOBAL not in st["globals"] and st["functions"][FN]["name"] == "init_runtime"
        check("after undo")
        # roll back to before the comment: the comment is gone, the names stay
        c.post(f"{mod}/annotations/rollback", json={"revision": revs[-3]})
        _, st, _ = ann.load(studio.cases, cid, app_mid)
        assert not st["comments"] and st["functions"][FN]["name"] == "init_runtime"
        # roll back to revision 0 (before any edit): the analysis names come back
        r = c.post(f"{mod}/annotations/rollback", json={"revision": 0})
        assert r.status_code == 200, r.text
        _, st, _ = ann.load(studio.cases, cid, app_mid)
        assert st == ann.empty()
        check("rolled back to the start", present=False)
        hist = c.get(f"{mod}/annotations?history=20").json()["history"]
        assert [h["change"]["action"] for h in hist[-3:]] == ["rollback"] * 3 and all(h["who"] == "person" for h in hist)
        # undoing a roll back is itself a roll back (nothing is ever deleted)
        c.post(f"{mod}/annotations/rollback", json={"revision": revs[-1]})
        check("redo by rolling forward")
    finally:
        studio.stop()


# =============================================================================================== piece 6: search (byte pattern / string regex)
FILE = bytes(range(256)) * 4                    # the fake module file: offset k holds byte k % 256
HITS = [{"paddr": "0x10", "addr": "0x401010", "section": ".text"}, {"paddr": "0x4000", "addr": "0x405000", "section": ".text"},
        {"paddr": "0x200", "addr": None, "section": None}, {"paddr": "0x300", "addr": "0x500000", "section": ".data"}]
SEARCH_STRS = {"strings": [{"vaddr": 0x406000, "paddr": 0x40, "length": 5, "section": ".rdata", "type": "ascii", "string": "Usage"},
                           {"vaddr": 0x406010, "paddr": 0x50, "length": 3, "section": ".rdata", "type": "utf16le", "string": "use"},
                           {"vaddr": 0x406020, "paddr": 0x60, "length": 4, "section": ".rdata", "type": "ascii", "string": "nope"}],
               "seen": 3}


class SearchBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.ops: list[tuple] = []

    def op_search_bytes(self, ctx, *, case_id, module_id, pattern, limit=100):
        from rebuild_controller.backends.rizin_worker import byte_context, parse_byte_pattern
        self.ops.append(("search_bytes", pattern, limit))
        n = len(parse_byte_pattern(pattern))
        rows = [{**h, **byte_context(FILE, int(h["paddr"], 16), n)} for h in HITS]
        return OperationResult(ok=True, data={"pattern": pattern, "matches": rows[:limit], "returned": min(limit, 4), "limit": limit,
                                              "more": limit < 4, "cached": False}, evidence_ids=["ev_sb"], truncated=limit < 4)

    def op_strings(self, ctx, *, case_id, module_id, limit=2000):
        self.ops.append(("strings", limit))
        return OperationResult(ok=True, data=json.loads(json.dumps(SEARCH_STRS)), evidence_ids=["ev_str"])

    def bytes_context(self, ctx, *, case_id, module_id, spans):
        from rebuild_controller.backends.rizin_worker import byte_context
        self.ops.append(("context", list(spans)))
        return [byte_context(FILE, off, n) for off, n in spans]


def test_search_patterns_are_explained_and_paged():
    from rebuild_controller.backends.rizin_worker import byte_context
    from rebuild_controller.re_workbench import check_byte_pattern, check_text_pattern, page_search
    assert check_byte_pattern("48 8B ??24") == "48 8b ?? 24" and check_byte_pattern(" 4d5a ") == "4d 5a"
    for bad, why in (("", "empty"), ("0x48 0x8b", "0x prefixes"), ("48 zz", "'z' is not a hex digit"), ("48 8", "odd number"),
                     ("4? 00", "'4?' mixes"), ("?? ??", "only wildcards"), ("00" * 257, "at most 256 bytes"), ("4\x00", "U+0000")):
        with pytest.raises(WorkbenchError) as e:
            check_byte_pattern(bad)
        assert e.value.code == "invalid_pattern" and why in e.value.message and "48 8b ?? 24" in e.value.next_action, (bad, e.value.message)
    assert check_text_pattern("[Uu]sage").search("usage")
    for bad, why in (("", "empty"), ("(a+)+", "nested quantifiers"), (r"(\w)\1", "backreferences"), ("a\nb", "control"), ("(", "invalid regex")):
        with pytest.raises(WorkbenchError) as e:
            check_text_pattern(bad)
        assert e.value.code == "invalid_pattern" and why in e.value.message and "[Uu]sage" in e.value.next_action, (bad, e.value.message)
    ctx = byte_context(FILE, 4, 2)
    assert ctx == {"before": "00 01 02 03", "bytes": "04 05", "after": "06 07 08 09 0a 0b 0c 0d"}
    long = byte_context(FILE, 100, 40)
    assert long["bytes_clipped"] == 40 and len(long["bytes"].split()) == 16 and long["after"] == ""
    p = page_search({"matches": list(range(5)), "total": 9, "total_exact": True, "more": True}, 2, 2)
    assert p == {"matches": [2, 3], "listed": 5, "offset": 2, "returned": 2, "more": True, "found": 9, "found_exact": True, "capped": True}


def test_search_route_refusals_shaping_containing_function_and_mcp_equivalence(studio, tmp_path, monkeypatch):
    import anyio
    case = _case(studio, tmp_path)
    cid = case["case_id"]
    mid = studio.cases.add_module(cid, "a.exe", "0" * 64, 66, "pe", "native_pe")
    fake = SearchBackend()
    monkeypatch.setattr(studio.re, "backend", lambda: fake)
    c = _client(studio)
    url = f"/cases/{cid}/modules/{mid}/pattern-search"
    sid = studio.re.open(case_id=cid, module_id=mid)["session_id"]

    # a pattern that cannot be used: plain message + a valid example, nothing runs; the MCP path refuses with the same words
    for kind, pattern, why in (("hex", "0x48 0x8b", "0x prefixes"), ("hex", "48 8", "odd number"), ("hex", "", "empty"),
                               ("hex", "4? 00", "mixes"), ("text", "(a+)+", "nested"), ("text", "", "empty")):
        r = c.get(url, params={"kind": kind, "pattern": pattern})
        assert r.status_code == 400, (kind, pattern, r.text)
        e = r.json()["error"]
        assert e["code"] == "invalid_pattern" and why in e["message"] and ("48 8b ?? 24" if kind == "hex" else "[Uu]sage") in e["next_action"]
        if pattern:
            with pytest.raises(WorkbenchError) as ex:
                studio.re.search(sid, **({"hex": pattern} if kind == "hex" else {"string_regex": pattern}))
            assert (ex.value.code, ex.value.message, ex.value.next_action) == (e["code"], e["message"], e["next_action"])
    assert c.get(url, params={"kind": "regex", "pattern": "x"}).status_code == 400
    assert c.get(url, params={"pattern": "4d", "limit": 201}).status_code == 400
    with pytest.raises(WorkbenchError):
        studio.re.search(sid, hex="4d", string_regex="x")
    assert not fake.ops
    monkeypatch.setattr(studio.re, "backend", lambda: FakeBackend(tool=None))
    r = c.get(url, params={"pattern": "4d 5a"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "engine_missing"
    monkeypatch.setattr(studio.re, "backend", lambda: fake)

    # byte search before any analysis: no containing functions, and no analysis is started for them
    r = c.get(url, params={"pattern": "10 11 ?? 13", "limit": 3})
    assert r.status_code == 200, r.text
    d = r.json()
    assert fake.ops == [("search_bytes", "10 11 ?? 13", 1000)] and fake.calls == 0
    assert d["functions_known"] is False and d["listed"] == 4 and d["returned"] == 3 and d["more"] is True and d["capped"] is False
    assert d["kind"] == "hex" and d["pattern"] == "10 11 ?? 13" and d["untrusted"] is True and d["max_hits"] == 1000
    m0 = d["matches"][0]
    assert m0 == {"addr": "0x401010", "paddr": "0x10", "section": ".text", "before": "08 09 0a 0b 0c 0d 0e 0f", "bytes": "10 11 12 13",
                  "after": "14 15 16 17 18 19 1a 1b", "fcn_addr": None, "fcn_name": None}
    assert c.get(url, params={"pattern": "10 11 ?? 13", "offset": 3, "limit": 3}).json()["matches"][0]["paddr"] == "0x300"
    # analysed: the containing function of each hit (nearest entry owner, as in References), none outside functions / sections
    c.get(url, params={"pattern": "1011??13", "analyze": "true"})
    assert fake.calls == 1                                     # 'Analyse now' records the function list first
    _analysed(studio, cid, mid)
    r = c.get(url, params={"pattern": "1011??13"}).json()
    assert r["functions_known"] is True
    assert [(m["addr"], m["fcn_name"]) for m in r["matches"]] == [("0x401010", "entry0"), ("0x405000", "fcn.00401100"), (None, None),
                                                                ("0x500000", None)]
    assert r["matches"][1]["fcn_addr"] == "0x401100"

    # text search: regex over extracted strings, with the bytes at each string as hex (utf-16 width counted)
    t = c.get(url, params={"kind": "text", "pattern": "[Uu]s"}).json()
    assert t["kind"] == "text" and t["found"] == 2 and t["found_exact"] is True and t["listed"] == 2
    assert [(m["addr"], m["string"], m["encoding"]) for m in t["matches"]] == [("0x406000", "Usage", "ascii"), ("0x406010", "use", "utf16le")]
    assert t["matches"][0]["bytes"] == "40 41 42 43 44" and t["matches"][1]["bytes"] == "50 51 52 53 54 55"
    assert ("context", [(0x40, 5), (0x50, 6)]) in fake.ops

    # one code path: the MCP search_bytes tool returns the same rows as the route
    pytest.importorskip("mcp")
    from mcp import Client
    from rebuild_controller.mcp.server import ServerConfig, build_server

    async def mcp(args):
        async with Client(build_server(studio, ServerConfig(toolset="re"))) as cl:
            res = await cl.call_tool("search_bytes", {"session_id": sid, **args})
        return json.loads(res.content[0].text)

    for args, params in (({"hex": "10 11 ?? 13", "limit": 4}, {"pattern": "10 11 ?? 13", "limit": 4}),
                         ({"hex": "1011??13", "limit": 2}, {"pattern": "1011??13", "limit": 2}),
                         ({"string_regex": "[Uu]s"}, {"kind": "text", "pattern": "[Uu]s"})):
        m = anyio.run(mcp, args)
        assert m["ok"], m
        h = c.get(url, params=params).json()
        assert _unwrap(m["data"]["matches"]) == h["matches"], args
        assert m["data"]["functions_known"] is h["functions_known"] is True
    bad = anyio.run(mcp, {"hex": "0x48"})
    assert bad["ok"] is False and bad["error"]["code"] == "invalid_pattern"
    assert c.get(url, params={"pattern": "0x48"}).json()["error"]["message"] in json.dumps(bad)


@pytest.mark.skipif(not _rizin() or not PECLI.is_file(), reason="needs rizin (REBUILD_STUDIO_TOOLS) and the pecli fixture")
def test_search_route_on_pecli_matches_the_mcp_tool(studio, tmp_path):
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
    base = f"/cases/{case['case_id']}/modules/{mid}"
    mz = c.get(f"{base}/pattern-search", params={"pattern": "4d 5a ?? 00"})          # no analysis needed
    assert mz.status_code == 200, mz.text
    mz = mz.json()
    assert mz["matches"][0]["paddr"] == "0x0" and mz["matches"][0]["bytes"] == "4d 5a 90 00" and mz["functions_known"] is False
    sid = mz["session_id"]
    # the first bytes of a known function, found at its entry, inside it, once the module is analysed
    assert c.get(f"{base}/functions", params={"analyze": "true", "limit": 1}).status_code == 200
    ops = c.get(f"{base}/functions/0x140001190/disassembly", params={"limit": 3}).json()["ops"]
    # every row carries its address as 0x-hex (the app's Disassembly rows are keyed and labelled by it; found by the R6 e2e)
    assert ops[0]["addr"] == "0x140001190" and all(o["addr"] == "0x%x" % o["offset"] for o in ops)
    pattern = " ".join(o["bytes"] for o in ops)
    hit =c.get(f"{base}/pattern-search", params={"pattern": pattern}).json()
    assert hit["functions_known"] is True
    row = next(m for m in hit["matches"] if m["addr"] == "0x140001190")
    flat = pattern.replace(" ", "")
    assert row["fcn_addr"] == "0x140001190" and row["section"] == ".text" and row["bytes"].replace(" ", "") == flat[:32]
    # the same bytes with the second one as a wildcard still find that place
    wild = flat[:2] + " ?? " + " ".join(flat[i:i + 2] for i in range(4, min(len(flat), 16), 2))
    assert any(m["addr"] == "0x140001190" for m in c.get(f"{base}/pattern-search", params={"pattern": wild}).json()["matches"])
    txt = c.get(f"{base}/pattern-search", params={"kind": "text", "pattern": r"^error: key"}).json()
    assert txt["found"] >= 1 and txt["matches"][0]["string"].startswith("error: key") and txt["matches"][0]["bytes"].startswith("65 72 72")

    async def mcp(args):
        async with Client(build_server(studio, ServerConfig(toolset="re"))) as cl:
            res = await cl.call_tool("search_bytes", {"session_id": sid, **args})
        body = json.loads(res.content[0].text)
        assert body["ok"], (args, body)
        return body["data"]

    for args, params in (({"hex": "4d 5a ?? 00"}, {"pattern": "4d 5a ?? 00"}), ({"hex": pattern, "limit": 5}, {"pattern": pattern, "limit": 5}),
                         ({"hex": "48 8b ?? 24", "limit": 50}, {"pattern": "48 8b ?? 24", "limit": 50}),
                         ({"string_regex": r"^error: \w+"}, {"kind": "text", "pattern": r"^error: \w+"}),
                         ({"string_regex": "[Uu]sage", "limit": 3}, {"kind": "text", "pattern": "[Uu]sage", "limit": 3})):
        m = _unwrap(anyio.run(mcp, args))
        h = c.get(f"{base}/pattern-search", params=params).json()
        assert m["matches"] == h["matches"], args
        assert m["functions_known"] == h["functions_known"], args
    many = c.get(f"{base}/pattern-search", params={"pattern": "e8 ?? ?? ?? ??", "limit": 200}).json()
    code = [x for x in many["matches"] if x["section"] == ".text"]
    assert many["listed"] >= 200 and many["returned"] == 200 and many["more"] is True
    assert sum(1 for x in code if x["fcn_addr"]) >= 0.8 * len(code)
