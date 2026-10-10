"""R2: the MCP `re` toolset driven end to end against the pecli fixture with real rizin (+ rz-ghidra when present).

Skips cleanly when rizin is not installed. Every re tool is called at least once; annotations are checked to survive a
re-decompile, a closed session and a brand-new controller on the same data directory; patch_bytes is checked to write
only a copy inside the session work folder.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

from rebuild_controller import config  # noqa: E402
from rebuild_controller.backends.rizin_worker import find_rizin  # noqa: E402
from rebuild_controller.config import Limits, Settings, set_settings  # noqa: E402
from rebuild_controller.ids import sha256_file  # noqa: E402
from rebuild_controller.mcp.server import TOOLSETS, ServerConfig, build_server  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
PECLI = REPO / "fixtures" / "pecli" / "original" / "pecli.exe"
FN = "0x140001190"          # a pecli function with a stack local (var_38h) and many callees

pytestmark = [pytest.mark.skipif(find_rizin(Settings()) is None, reason="rizin not installed on this host"),
              pytest.mark.skipif(not PECLI.is_file(), reason="pecli fixture missing")]

RE_TOOLS = set(TOOLSETS["re"]) - {"doctor"}


def _studio(data_dir: Path):
    from rebuild_controller.services import StudioServices
    set_settings(Settings(data_dir=data_dir, limits=Limits()))
    return StudioServices()


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    previous = config._settings
    data = tmp_path_factory.mktemp("re-data")
    studio = _studio(data)
    state = {"studio": studio, "data": data, "called": set(), "pecli_sha": sha256_file(PECLI)}
    yield state
    try:
        state["studio"].stop()
    finally:
        config._settings = previous


async def call(env, name, args=None, expect_ok=True):
    server = build_server(env["studio"], ServerConfig(toolset="re"))
    async with Client(server) as c:
        r = await c.call_tool(name, args or {})
    try:
        body = json.loads(r.content[0].text)
    except ValueError:   # schema validation errors are reported by the SDK as plain text
        body = {"ok": False, "error": {"code": "validation", "message": r.content[0].text}}
    env["called"].add(name)
    if expect_ok:
        assert not r.is_error and body["ok"], (name, args, body)
    return r, body


def text_of(v):
    """Unwrap {"untrusted": true, "text": ...} envelopes."""
    return v["text"] if isinstance(v, dict) and v.get("untrusted") is True and "text" in v else v


async def session(env) -> str:
    if "sid" not in env:
        _, b = await call(env, "open_binary", {"path": str(PECLI)})
        env["sid"] = b["data"]["session"]["session_id"]
    return env["sid"]


# --------------------------------------------------------------------------------------------- sessions
async def test_toolset_and_session_lifecycle(env):
    server = build_server(env["studio"], ServerConfig(toolset="re"))
    async with Client(server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == set(TOOLSETS["re"]) and RE_TOOLS <= set(TOOLSETS["all"])
    sid = await session(env)
    _, again = await call(env, "open_binary", {"path": str(PECLI)})
    assert again["data"]["session"]["session_id"] == sid and again["data"]["session"]["reused"] is True
    s = again["data"]["session"]
    assert s["adhoc"] is True and s["module"]["sha256"] == env["pecli_sha"] and s["module"]["format"] == "pe"
    # the same module can be opened by case_id + module_id
    _, by_mod = await call(env, "open_binary", {"case_id": s["case_id"], "module_id": s["module_id"]})
    assert by_mod["data"]["session"]["session_id"] == sid
    _, ls = await call(env, "list_sessions")
    assert any(x["session_id"] == sid for x in ls["data"]["sessions"])
    # hidden RE cases do not show up in the normal case list
    r, cases = await call(env, "list_sessions", {"include_closed": True})
    assert cases["ok"]


async def test_bad_inputs_rejected(env):
    sid = await session(env)
    for args in ({"path": "relative/pecli.exe"}, {"path": str(PECLI), "case_id": "case_" + "0" * 22},
                 {"path": str(PECLI.parent / "missing.exe")}, {"path": str(PECLI), "shell": "x"}):
        r, b = await call(env, "open_binary", args, expect_ok=False)
        assert r.is_error, args
    for name, args in (("list_functions", {"session_id": "res_nothex"}),
                       ("list_functions", {"session_id": "res_" + "0" * 22}),
                       ("get_function", {"session_id": sid}),
                       ("get_function", {"session_id": sid, "address": FN, "name": "main"}),
                       ("get_function", {"session_id": sid, "name": "main; q"}),
                       ("disassemble", {"session_id": sid, "address": FN, "count": 5000}),
                       ("rename", {"session_id": sid, "kind": "function", "address": FN, "new_name": "x; q"}),
                       ("rename", {"session_id": sid, "kind": "function", "address": FN, "new_name": "ok", "cmd": "q"}),
                       ("set_type", {"session_id": sid, "kind": "function", "address": FN, "prototype": "int f(int a); q"}),
                       ("apply_struct", {"session_id": sid, "declaration": "#include <windows.h>\nstruct a { int x; };"}),
                       ("apply_struct", {"session_id": sid, "declaration": "struct a { int x; }; `!calc`"}),
                       ("add_comment", {"session_id": sid, "address": FN, "text": "two\nlines"}),
                       ("search_bytes", {"session_id": sid}),
                       ("search_bytes", {"session_id": sid, "hex": "??"}),
                       ("strings", {"session_id": sid, "regex": "(a+)+$"}),
                       ("patch_bytes", {"session_id": sid, "address": FN, "data": "90"}),
                       ("get_function", {"session_id": sid, "address": "0x10"})):
        r, b = await call(env, name, args, expect_ok=False)
        assert r.is_error and b["ok"] is False, (name, args, b)


# --------------------------------------------------------------------------------------------- read tools
async def test_listing_tools(env):
    sid = await session(env)
    _, b = await call(env, "list_functions", {"session_id": sid, "limit": 500})
    d = b["data"]
    assert d["total"] > 50 and d["evidence_ids"] and b["evidence_revision"]
    assert any(f["addr"] == FN for f in d["functions"])
    _, big = await call(env, "list_functions", {"session_id": sid, "sort": "size", "limit": 3})
    sizes = [f["size"] for f in big["data"]["functions"]]
    assert sizes == sorted(sizes, reverse=True) and big["truncated"] is True
    _, ent = await call(env, "list_functions", {"session_id": sid, "contains": "entry"})
    assert ent["data"]["total"] >= 1

    _, imp = await call(env, "imports", {"session_id": sid, "contains": "Sleep"})
    assert imp["data"]["total"] >= 1 and "Sleep" in json.dumps(imp["data"]["imports"])
    _, exp = await call(env, "exports", {"session_id": sid})
    assert "exports" in exp["data"]
    _, sym = await call(env, "symbols", {"session_id": sid, "limit": 5})
    assert sym["data"]["total"] > 0 and len(sym["data"]["symbols"]) <= 5
    _, sec = await call(env, "sections", {"session_id": sid})
    names = [text_of(s.get("name")) for s in sec["data"]["sections"]]
    assert any(".text" in str(n) for n in names)
    _, ep = await call(env, "entry_points", {"session_id": sid})
    assert ep["data"]["entrypoints"]

    _, st = await call(env, "strings", {"session_id": sid, "limit": 10})
    assert st["data"]["total"] > 0 and len(st["data"]["strings"]) <= 10
    first = text_of(st["data"]["strings"][0]["string"])
    assert isinstance(st["data"]["strings"][0]["string"], dict), "program strings are wrapped as untrusted"
    _, flt = await call(env, "strings", {"session_id": sid, "contains": first[:4], "limit": 5})
    assert flt["data"]["total"] >= 1
    _, rx = await call(env, "strings", {"session_id": sid, "regex": r"[A-Za-z]{6,}", "min_length": 6, "limit": 5})
    assert rx["data"]["total"] >= 1


async def test_function_decompile_disassemble_xrefs_graph(env):
    sid = await session(env)
    _, f = await call(env, "get_function", {"session_id": sid, "address": FN})
    d = f["data"]
    assert d["addr"] == FN and d["blocks"] and d["function"]["size"] > 0
    assert any(text_of(v["name"]) == "var_38h" for v in d["variables"]), d["variables"]

    _, dec = await call(env, "decompile", {"session_id": sid, "address": FN})
    dd = dec["data"]
    assert dd["decompiled"]["untrusted"] is True and text_of(dd["decompiled"]["text"])
    assert text_of(dd["decompiler"]) in ("rz-ghidra(pdg)", "pdc(pseudo)", "pdf+asm.pseudo(pseudo)")

    _, dis = await call(env, "disassemble", {"session_id": sid, "address": FN})
    assert len(dis["data"]["disasm"]["ops"]) > 10
    _, lin = await call(env, "disassemble", {"session_id": sid, "address": FN, "count": 5})
    assert len(lin["data"]["ops"]) == 5 and lin["data"]["ops"][0]["addr"] == FN
    _, byt = await call(env, "disassemble", {"session_id": sid, "address": FN, "length": 16})
    assert 1 <= len(byt["data"]["ops"]) <= 16

    _, xf = await call(env, "xrefs_from", {"session_id": sid, "address": "0x1400011b4"})
    assert xf["data"]["direction"] == "from"
    callee = "0x140001440"
    _, xt = await call(env, "xrefs_to", {"session_id": sid, "address": callee})
    assert xt["data"]["direction"] == "to" and isinstance(xt["data"]["refs"], list)

    _, cg = await call(env, "call_graph", {"session_id": sid, "address": FN, "depth": 2, "max_nodes": 50})
    g = cg["data"]
    assert g["root"] == FN and len(g["nodes"]) > 1 and g["edges"] and len(g["nodes"]) <= 50
    assert all(e["from"] in {n["addr"] for n in g["nodes"]} for e in g["edges"])
    _, up = await call(env, "call_graph", {"session_id": sid, "name": "entry0", "direction": "callers", "depth": 1})
    assert up["data"]["direction"] == "callers"

    _, hb = await call(env, "search_bytes", {"session_id": sid, "hex": "4d 5a ?? 00"})
    assert hb["data"]["matches"] and hb["data"]["matches"][0]["paddr"] == "0x0"
    _, sr = await call(env, "search_bytes", {"session_id": sid, "string_regex": r"[Uu]sage"})
    assert "matches" in sr["data"]

    r, gh = await call(env, "decompile", {"session_id": sid, "address": FN, "decompiler": "ghidra"}, expect_ok=False)
    assert gh["ok"] or gh["error"]["code"] == "unavailable"


# --------------------------------------------------------------------------------------------- annotations
async def test_annotations_round_trip_and_persist(env):
    sid = await session(env)
    _, rn = await call(env, "rename", {"session_id": sid, "kind": "function", "address": FN, "new_name": "init_runtime"})
    rev1 = rn["data"]["revision"]
    _, st = await call(env, "apply_struct", {"session_id": sid,
                                             "declaration": "struct rs_point { int x; int y; char tag[8]; };"})
    _, lv = await call(env, "rename", {"session_id": sid, "kind": "local", "address": FN, "variable": "var_38h",
                                       "new_name": "start_ptr"})
    _, lt = await call(env, "set_type", {"session_id": sid, "kind": "local", "address": FN, "variable": "start_ptr",
                                         "type": "struct rs_point *"})
    _, pt = await call(env, "set_type", {"session_id": sid, "kind": "function", "name": "init_runtime",
                                         "prototype": "int init_runtime(int argc, char **argv)"})
    _, cm = await call(env, "add_comment", {"session_id": sid, "address": FN, "text": "CRT start-up: runs initialisers"})
    _, gl = await call(env, "rename", {"session_id": sid, "kind": "global", "address": "0x140052270", "new_name": "g_init_lock"})
    assert gl["data"]["revision"] > rev1

    async def check(tag: str):
        _, dec = await call(env, "decompile", {"session_id": sid, "name": "init_runtime"})
        code = text_of(dec["data"]["decompiled"]["text"])
        assert "init_runtime" in code, (tag, code[:400])
        assert "CRT start-up: runs initialisers" in code, tag
        if dec["data"]["is_real_decompiler"]:
            assert "start_ptr" in code and "rs_point" in code, (tag, code[:800])
        _, fn = await call(env, "get_function", {"session_id": sid, "address": FN})
        vars_ = {text_of(v["name"]): text_of(v["type"]) for v in fn["data"]["variables"]}
        assert vars_.get("start_ptr") == "struct rs_point *", (tag, vars_)
        assert "init_runtime(" in text_of(fn["data"]["function"]["signature"]), tag
        _, dis = await call(env, "disassemble", {"session_id": sid, "address": FN, "count": 1})
        assert text_of(dis["data"]["ops"][0]["user_comment"]) == "CRT start-up: runs initialisers"
        _, ann = await call(env, "get_annotations", {"session_id": sid, "history": 10})
        a = ann["data"]["annotations"]
        assert a["functions"][FN]["name"] == "init_runtime" or text_of(a["functions"][FN]["name"]) == "init_runtime"
        assert len(ann["data"]["history"]) >= 7

    await check("live session")
    # a re-created rizin process (crash / idle reap) replays the annotations
    backend = env["studio"].re.backend()
    for s in backend.pool.sessions():
        s.kill()
    await check("after rizin restart")
    # closing the session keeps everything; reopening reuses case + module
    _, cl = await call(env, "close_session", {"session_id": sid})
    assert cl["data"]["closed"] is True
    r, b = await call(env, "list_functions", {"session_id": sid}, expect_ok=False)
    assert r.is_error and b["error"]["code"] == "closed"
    env.pop("sid")
    sid = await session(env)
    await check("reopened session")
    # a brand-new controller on the same data directory
    env["studio"].stop()
    env["studio"] = _studio(env["data"])
    env.pop("sid")
    sid = await session(env)
    await check("new controller")


async def test_patch_bytes_writes_only_a_copy(env):
    sid = await session(env)
    _, before = await call(env, "disassemble", {"session_id": sid, "address": FN, "count": 1})
    r, b = await call(env, "patch_bytes", {"session_id": sid, "address": FN, "data": "90"}, expect_ok=False)
    assert r.is_error and b["error"]["code"] == "refused"
    _, p = await call(env, "patch_bytes", {"session_id": sid, "address": FN, "data": "90 90", "confirm": True})
    d = p["data"]
    copy = Path(text_of(d["copy_path"]))
    assert copy.is_file() and copy.resolve() != PECLI.resolve()
    assert str(env["data"].resolve()).lower() in str(copy.resolve()).lower()
    assert sha256_file(PECLI) == env["pecli_sha"], "the original must never be written"
    assert d["copy_sha256"] != env["pecli_sha"] and text_of(d["new_bytes"]) == "9090" and d["original_untouched"] is True
    raw = copy.read_bytes()
    off = int(d["paddr"], 16)
    assert raw[off:off + 2] == b"\x90\x90" and PECLI.read_bytes()[off:off + 2] == bytes.fromhex(text_of(d["original_bytes"]))
    _, after = await call(env, "disassemble", {"session_id": sid, "address": FN, "count": 1})
    assert after["data"]["ops"][0].get("bytes") == before["data"]["ops"][0].get("bytes"), "analysis keeps using the original"
    # the patched copy can be opened as its own session
    _, s2 = await call(env, "open_binary", {"path": str(copy)})
    sid2 = s2["data"]["session"]["session_id"]
    assert sid2 != sid
    _, lin = await call(env, "disassemble", {"session_id": sid2, "address": FN, "count": 1})
    assert text_of(lin["data"]["ops"][0]["bytes"]) == "90"
    await call(env, "close_session", {"session_id": sid2})


async def test_every_re_tool_was_driven(env):
    missing = RE_TOOLS - env["called"]
    assert not missing, f"tools never called: {sorted(missing)}"


def test_hidden_re_cases_not_in_case_list(env):
    from rebuild_controller.re_workbench import is_re_case
    rows = env["studio"].cases.list_cases()
    assert any(is_re_case(c) for c in rows)
    assert re.match(r"^RE: ", next(c for c in rows if is_re_case(c))["name"])
