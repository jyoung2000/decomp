"""R0 benchmark: corpus/truth schema, scoring math on synthetic data, and a guard that a weaker analyzer scores lower.

The guard and the full run use the real rizin backend (skipped when rizin is absent). The full corpus run is marked e2e so the
default suite (-m "not e2e and not live") stays fast; the guard analyses one 16 KB binary twice (a few seconds).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "fixtures" / "bench"


def _load_benchmark():
    spec = importlib.util.spec_from_file_location("rs_benchmark", REPO / "scripts" / "benchmark.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rs_benchmark"] = mod
    spec.loader.exec_module(mod)
    return mod


bm = _load_benchmark()


def _rizin_available() -> bool:
    from rebuild_controller.backends.rizin_worker import find_rizin
    from rebuild_controller.config import Settings
    t = find_rizin(Settings())
    return t is not None and bool(t.version)


def _manifest() -> dict:
    return json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------ corpus + truth schema
def test_manifest_rows_have_committed_binaries_within_size_limits():
    man = _manifest()
    assert man["schema"] == "rebuild-studio.bench-manifest/1"
    assert len(man["rows"]) >= 8
    total = 0
    for name, row in man["rows"].items():
        p = BENCH / row["binary"]
        assert p.is_file(), name
        data = p.read_bytes()
        assert hashlib.sha256(data).hexdigest() == row["sha256"], f"{name}: committed binary differs from manifest"
        assert len(data) == row["size"] < 5 * 1024 * 1024
        total += len(data)
    assert total == man["total_binary_bytes"] < 25 * 1024 * 1024
    for name, why in man["not_built"].items():   # every skipped row says why; none is silently dropped
        assert why.startswith("not run"), (name, why)


@pytest.mark.parametrize("row", sorted(_manifest()["rows"]))
def test_truth_schema(row):
    t = bm.load_truth(BENCH / row / "truth" / "truth.json")
    assert t["row"] == row and t["binary"]["path"].startswith("bin/")
    assert hashlib.sha256((BENCH / row / t["binary"]["path"]).read_bytes()).hexdigest() == t["binary"]["sha256"]
    assert t["strings"] and all(isinstance(s, str) and s for s in t["strings"])
    assert (BENCH / row / "build.json").is_file() and (BENCH / row / "README.md").is_file()
    build = json.loads((BENCH / row / "build.json").read_text(encoding="utf-8"))
    assert build["commands"] and build["toolchain"] and build["binary_sha256"] == t["binary"]["sha256"]
    if t["kind"] == "dotnet":
        assert t["dotnet"]["types"] and t["dotnet"]["methods"]
        assert all({"type", "name", "special_name", "compiler_generated"} <= set(m) for m in t["dotnet"]["methods"])
        return
    ranges = [(int(a, 16), int(b, 16)) for a, b in t["code_ranges"]]
    assert ranges and t["functions"]
    starts = [int(f["start"], 16) for f in t["functions"]]
    assert starts == sorted(set(starts)), "functions are unique by start and sorted"
    for f in t["functions"]:
        assert f["name"] and isinstance(f["own"], bool)
        assert f["size"] is None or f["size"] > 0
        assert any(a <= int(f["start"], 16) < b for a, b in ranges)
    assert sum(f["own"] for f in t["functions"]) == t["counts"]["own_functions"] > 0
    assert all({"lib", "name"} <= set(i) for i in t["imports"])
    assert t["packed"] == (row.startswith("upx"))


def test_upx_pin_is_mirrored_byte_identically():
    docs = (REPO / "docs" / "dependency-lock.json").read_bytes()
    data = (REPO / "controller" / "rebuild_controller" / "data" / "dependency-lock.json").read_bytes()
    assert docs == data
    upx = json.loads(docs)["fixture_build_tools"]["upx"]
    assert upx["version"] == "5.2.1" and len(upx["artifact"]["sha256"]) == 64
    assert upx["artifact"]["url"].startswith("https://github.com/upx/upx/releases/download/v5.2.1/")
    sys.path.insert(0, str(BENCH))
    import build_bench
    assert build_bench.UPX_EXE_SHA256 == upx["layout"]["entry_sha256"]


# ------------------------------------------------------------------------------------------------ scoring math (synthetic)
TRUTH_FUNCS = [
    {"name": "main", "aliases": [], "start": "0x1000", "size": 32, "own": True},
    {"name": "parse_args", "aliases": ["_parse_args"], "start": "0x1020", "size": 16, "own": True},
    {"name": "helper", "aliases": [], "start": "0x1030", "size": 16, "own": False},
    {"name": "__security_check_cookie", "aliases": [], "start": "0x1040", "size": None, "own": False},
]


def test_score_functions_recall_precision_named():
    found = [
        {"offset": 0x1000, "name": "main", "size": 32},             # match, named
        {"offset": 0x1020, "name": "sym._parse_args", "size": 15},   # match, named via alias/prefix, size differs
        {"offset": 0x1030, "name": "fcn.00001030", "size": 16},      # match, auto name -> not named
        {"offset": 0x1050, "name": "fcn.00001050", "size": 4},       # false positive inside code
        {"offset": 0x9000, "name": "sym.imp.KERNEL32.dll_Sleep"},    # outside code ranges: not counted for precision
    ]
    s = bm.score_functions(TRUTH_FUNCS, found, [(0x1000, 0x2000)])
    assert s["truth_functions"] == 4 and s["found_functions"] == 5 and s["found_in_code"] == 4
    assert s["matched"] == 3 and s["recall"] == 0.75 and s["precision"] == 0.75
    assert s["own_truth"] == 2 and s["own_recall"] == 1.0
    assert s["named"] == 2 and s["named_recall"] == 0.5 and s["own_named_recall"] == 1.0
    assert s["size_exact"] == round(2 / 3, 4)


def test_score_functions_empty_and_no_ranges():
    s = bm.score_functions(TRUTH_FUNCS, [], [])
    assert s["recall"] == 0.0 and s["precision"] is None and s["named_recall"] == 0.0
    assert bm.score_functions([], [{"offset": 1}], [])["recall"] is None


def test_analyzer_name_normalisation():
    assert bm.analyzer_name("fcn.140001000") is None and bm.analyzer_name("entry0") is None
    assert bm.analyzer_name("case.0x1400.3") is None
    assert bm.analyzer_name("sym.go.main.__Inventory_.Add") == "main.__Inventory_.Add"
    assert bm.canonical(bm.analyzer_name("sym.go.main.__Inventory_.Add")) == bm.canonical("main.(*Inventory).Add")
    assert bm.analyzer_name("flirt.scrt_initialize_crt") == "scrt_initialize_crt"
    assert bm.canonical("__scrt_initialize_crt") == bm.canonical("scrt_initialize_crt")


def test_score_imports_and_strings():
    truth_imp = [{"lib": "kernel32.dll", "name": "Sleep"}, {"lib": "kernel32.dll", "name": "ExitProcess"}, {"lib": "a.dll", "name": "Foo"}]
    s = bm.score_imports(truth_imp, [{"name": "Sleep"}, {"name": "KERNEL32.dll_ExitProcess"}, {"name": "Other"}])
    assert s["matched"] == 2 and s["recall"] == round(2 / 3, 4) and s["missing_sample"] == ["Foo"]
    st = bm.score_strings(["usage: x", "error %d", "absent"], ["xx usage: x yy", "error %d"])
    assert st["matched"] == 2 and st["recall"] == round(2 / 3, 4) and st["missing"] == ["absent"]
    assert bm.score_strings([], ["a"])["recall"] is None


def test_score_dotnet_type_and_method_recall():
    truth = {"types": [{"name": "Ns.Widget", "kind": "class", "nested": False}, {"name": "Ns.Widget.Part", "kind": "class", "nested": True},
                       {"name": "Ns.<>c", "kind": "class", "nested": True}, {"name": "Ns.Mode", "kind": "enum", "nested": False}],
             "methods": [{"type": "Ns.Widget", "name": "Render", "special_name": False, "compiler_generated": False},
                         {"type": "Ns.Widget", "name": "Hidden", "special_name": False, "compiler_generated": False},
                         {"type": "Ns.Widget", "name": "get_Size", "special_name": True, "compiler_generated": False},
                         {"type": "Ns.<>c", "name": "<Render>b__0", "special_name": False, "compiler_generated": True}]}
    cs = "namespace Ns { public class Widget { public class Part {} public void Render() { } } public enum Mode { A } }"
    s = bm.score_dotnet(truth, cs)
    assert s["truth_types"] == 3 and s["types_matched"] == 3 and s["type_recall"] == 1.0
    assert s["truth_methods"] == 2 and s["methods_matched"] == 1 and s["method_recall"] == 0.5
    renamed = "namespace Ab { public class Qx { public void Zz() { } } }"
    r = bm.score_dotnet(truth, renamed)
    assert r["type_recall"] == 0.0 and r["method_recall"] == 0.0


def test_config_merge_and_markdown_reports_not_run_rows():
    cfg = bm.merge_config(bm.DEFAULT_CONFIG, {"analysis": {"command": "aa"}, "name": "x"})
    assert cfg["analysis"] == {"command": "aa", "analysis.timeout": 300, "passes": ["sigpacks", "pdata", "relocptrs", "thunks"]}
    assert cfg["decompile"]["max_functions"] == 200 and cfg["packer"] == {"check": True, "unpack": True}
    assert bm.DEFAULT_CONFIG["analysis"]["command"] == "aaa"   # defaults not mutated
    rep = {"generated_utc": "t", "config": cfg, "environment": {}, "rows": [
        {"row": "unity_il2cpp", "kind": "?", "origin": "bench", "status": "not run: needs Unity"}]}
    md = bm.render_markdown(rep)
    assert "| unity_il2cpp | not run: needs Unity |" in md


def test_discover_rows_lists_every_fixture():
    rows = {r["row"]: r for r in bm.discover_rows(None, True)}
    for name in ("pecli", "dotnetapp", "javacli", "godotgame", "webapp", *_manifest()["rows"]):
        assert name in rows
    assert rows["javacli"]["not_run"].startswith("not scored")


# ------------------------------------------------------------------------------------------------ real analyzer
needs_rizin = pytest.mark.skipif(not _rizin_available(), reason="rizin not installed on this host")


@needs_rizin
def test_weaker_config_scores_lower_on_native_row():
    rows = bm.discover_rows(["c_msvc_x64_o2"], False)
    fast = {"strings": False, "imports": False, "dotnet": {"enabled": False}}
    good = bm.run_benchmark(rows, bm.merge_config(bm.DEFAULT_CONFIG, {**fast, "decompile": {"max_functions": 50}}))["rows"][0]
    weak = bm.run_benchmark(rows, bm.merge_config(bm.merge_config(bm.DEFAULT_CONFIG, fast), bm.WEAK_CONFIG))["rows"][0]
    assert good["status"] == weak["status"] == "ok", (good.get("status"), weak.get("status"))
    assert weak["analysis"]["settings"]["command"] == "aa"
    assert weak["functions"]["recall"] < good["functions"]["recall"]
    assert weak["decompile"]["coverage"] < good["decompile"]["coverage"]
    assert good["functions"]["recall"] >= 0.7 and good["functions"]["precision"] >= 0.9


@pytest.mark.e2e
@needs_rizin
def test_full_benchmark_run_writes_reports(tmp_path):
    rc = bm.main(["--out-dir", str(tmp_path)])
    assert rc == 0
    rep = json.loads((tmp_path / "benchmark.json").read_text(encoding="utf-8"))
    md = (tmp_path / "benchmark.md").read_text(encoding="utf-8")
    by = {r["row"]: r for r in rep["rows"]}
    for name, row in _manifest()["rows"].items():
        r = by[name]
        assert r["status"] == "ok" or r["status"].startswith("not run"), (name, r["status"])
        assert f"| {name} |" in md or f"| {name} " in md
        if row["kind"].startswith("native") and r["status"] == "ok":
            assert r["functions"]["recall"] is not None and "analysis" in r["seconds"]
    if by["upx_c_msvc_x64"]["status"] == "ok" and by["c_msvc_x64_o2"]["status"] == "ok":
        upx, plain = by["upx_c_msvc_x64"], by["c_msvc_x64_o2"]
        assert upx["packed"]["truth"] and upx["packed"]["detected"] and upx["packed"]["packer"] == "UPX"   # reported as packed
        assert plain["packed"]["detected"] is False
        if upx["packed"]["unpacked"]:     # pinned UPX installed: recovered after unpack, same analysis as the unpacked build
            assert upx["functions"]["recall"] == plain["functions"]["recall"]
        else:
            assert upx["functions"]["recall"] < plain["functions"]["recall"]
    if by["dotnet_plain"]["status"] == "ok" and by["dotnet_renamed"]["status"] == "ok":
        assert by["dotnet_renamed"]["dotnet"]["type_recall"] < by["dotnet_plain"]["dotnet"]["type_recall"]
