"""JSWeb backend on real @electron/asar-packed output (4.3.1) and tiny Vite/webpack/esbuild-style trees with source maps.

Fixtures under tests/data/managed/js (regenerate with tests/data/managed/make_js_samples.py):
  electron_app/   resources/app.asar (+ app.asar.unpacked/native/addon.node), runtime marker files
  web_app/        index.html, Vite-style bundle + .map with sourcesContent, inline data: map, dangling map ref, sw.js, manifest
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebuild_controller.adapters.contract import Availability
from rebuild_controller.backends.archive import discover_executable
from rebuild_controller.backends.jsweb import JSWebBackend, build_asar, sanitize_source_path, tree_digest
from rebuild_controller.config import Limits, Settings

DATA = Path(__file__).parent / "data" / "managed" / "js"
ELECTRON = DATA / "electron_app"
WEB = DATA / "web_app"

APP_SRC = "export function greet(name) {\n  return `hello ${name}`;\n}\nexport const VERSION = \"0.1.0\";\n"
UTIL_SRC = "export function add(a, b) {\n  return a + b;\n}\n"


@pytest.fixture
def backend(tmp_path) -> JSWebBackend:
    return JSWebBackend(Settings(data_dir=tmp_path / "data"))


@pytest.fixture
def studio(cases):
    return SimpleNamespace(cases=cases)


@pytest.fixture
def case(cases, src_out):
    src, out = src_out
    return cases.create_case(name="js", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe")


def files_under(p: Path) -> list[str]:
    return sorted(x.relative_to(p).as_posix() for x in p.rglob("*") if x.is_file())


def smap(sources: dict[str, str | None], file: str = "b.js") -> str:
    names = list(sources)
    return json.dumps({"version": 3, "file": file, "sources": names, "sourcesContent": [sources[n] for n in names], "names": [], "mappings": "AAAA"})


# ---------------------------------------------------------------------------------------------------------------------
# probe / smoke
# ---------------------------------------------------------------------------------------------------------------------
def test_probe_native_engine_installed_and_optional_tools_do_not_lower_availability(backend, tmp_path, monkeypatch):
    info = backend.probe()
    native, *optional = info.tools
    assert native.name == "jsweb-native" and native.availability == Availability.INSTALLED and not native.optional
    assert {t.name for t in optional} == {"node", "asar"} and all(t.optional for t in optional)
    asar = next(t for t in optional if t.name == "asar")
    if asar.availability == Availability.INSTALLED:
        assert asar.version == "4.3.1" and asar.pinned == "4.3.1" and asar.license == "MIT" and asar.integrity.startswith("sha512-")
        assert Path(asar.path).parts[-3:] == ("node_modules", ".bin", "asar")
    node = next(t for t in optional if t.name == "node")
    if node.availability == Availability.INSTALLED:
        assert node.version.startswith("22.")
    # a host without node/asar: still INSTALLED overall, with next_action strings on the optional tools
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no_localappdata"))   # the per-user tools dir is a discovery location too
    b2 = JSWebBackend(Settings(tools_dir=tmp_path / "none", data_dir=tmp_path / "d2"))
    i2 = b2.probe()
    assert i2.availability == Availability.INSTALLED
    miss = {t.name: t for t in i2.tools if t.optional}
    assert miss["asar"].availability == Availability.MISSING and "@electron/asar@4.3.1" in miss["asar"].next_action
    assert miss["node"].availability == Availability.MISSING and miss["node"].next_action


def test_smoke_inspects_and_extracts_builtin_sample(backend):
    t = backend.smoke()
    assert t.availability == Availability.USABLE and "built-in asar" in t.detail


# ---------------------------------------------------------------------------------------------------------------------
# inspect: Electron app
# ---------------------------------------------------------------------------------------------------------------------
def test_inspect_electron_app(backend, studio, case, cases):
    r = backend.inspect(ELECTRON, studio=studio, case_id=case["case_id"])
    d = r.data
    assert r.ok and not r.truncated and d["root_kind"] == "dir" and d["truncation"] == []
    # package.json found inside the asar, with the Electron main entry resolved and checked
    assert d["package_json"]["primary"] == "resources/app.asar!/package.json"
    pkg = d["package_json"]["all"]["items"][0]
    assert pkg["name"] == "tiny-electron" and pkg["version"] == "0.1.0" and pkg["main"] == "main.js"
    el = d["electron"]
    assert el["detected"] and el["confidence"] == "high" and el["electron_version_hint"] == "31.0.0"
    assert el["main"] == {"entry": "resources/app.asar!/main.js", "exists": True, "location": "asar", "browser_window": True,
                          "load_file": ["renderer/index.html"], "load_url": [], "preload": []}
    assert any("resources/app.asar present" in s for s in el["signals"]) and any("resources.pak" in s for s in el["signals"])
    # asar listing + integrity
    (a,) = d["asar"]
    assert a["ok"] and a["entries"] == 16 and a["declared_entries"] == 16 and not a["truncated"]
    assert a["unpacked_files"] == 1 and a["unpacked_dir_present"] and a["integrity"]["checked"] >= 10 and a["integrity"]["mismatched"] == 0
    # bundler fingerprints (heuristic, with their markers)
    det = d["bundlers"]["detected"]
    assert set(det) == {"webpack", "vite", "esbuild"} and all(v["strength"] == "strong" for v in det.values())
    assert "__webpack_require__" in det["webpack"]["markers"] and "import.meta.env" in det["vite"]["markers"] and "__commonJS" in det["esbuild"]["markers"]
    assert det["webpack"]["example_files"] == ["resources/app.asar!/renderer/bundle.js"] and "heuristic" in d["bundlers"]["note"]
    # source maps: two maps, four sources with embedded content, one known name without content
    sm = d["source_maps"]
    assert (sm["maps_found"], sm["maps_valid"], sm["maps_with_sources_content"]) == (2, 2, 2)
    assert sm["sources_with_content_total"] == 4 and sm["sources_without_content_total"] == 1
    assert all(ref["kind"] == "file" and ref["resolved"] for ref in sm["bundle_references"]["items"])
    assert sm["js_files_scanned_without_map_reference"] == 4
    # service worker + manifest + html entries
    sw = d["service_worker"]
    assert sw["detected"] and sw["files"]["items"][0]["path"] == "resources/app.asar!/renderer/sw.js" and sw["files"]["items"][0]["events"] == ["fetch", "install"]
    assert sw["registrations"]["items"] == [{"from": "resources/app.asar!/renderer/index.html", "script": "sw.js"}]
    mf = d["manifest"]
    assert mf["detected"] and mf["files"]["items"][0]["start_url"] == "./index.html" and mf["linked_from_html"] == ["manifest.webmanifest"]
    assert d["html_entries"]["items"] == [{"path": "resources/app.asar!/renderer/index.html", "scripts": ["bundle.js"]}]
    assert d["counts"]["files"] == 17 and d["counts"]["js_files"] == 6 and d["counts"]["symlinks_not_followed"] == 0
    # evidence keyed by engine version + tree digest
    ev = cases.get_evidence(r.evidence_ids[0])
    assert ev["kind"] == "js.inspection" and ev["producer"] == "jsweb"
    assert cases.evidence_body(ev["evidence_id"])["tree_sha256"] == d["tree_sha256"]
    assert backend.inspect(ELECTRON, studio=studio, case_id=case["case_id"]).evidence_ids == r.evidence_ids


def test_inspect_asar_listing_matches_real_asar_cli(backend):
    cli = discover_executable(Settings(), ["asar"], subdirs=["asar"])
    if cli is None:
        pytest.skip("@electron/asar CLI not installed")
    out = subprocess.run([str(cli), "list", str(ELECTRON / "resources" / "app.asar")], capture_output=True, text=True, check=True, timeout=60).stdout
    cli_files = {ln.strip().lstrip("/") for ln in out.splitlines() if ln.strip()}
    d = backend.inspect(ELECTRON / "resources" / "app.asar").data
    assert d["root_kind"] == "asar"
    inside = {f["path"].split("!/", 1)[1] for f in d["files"]["items"] if "!/" in f["path"]}
    dirs = {"assets", "native", "renderer", "vendor"}
    assert inside == cli_files - dirs


def test_inspect_web_app_inline_and_dangling_source_maps(backend):
    d = backend.inspect(WEB).data
    assert not d["electron"]["detected"] and d["electron"]["confidence"] == "none" and d["asar"] == []
    assert d["package_json"]["primary"] == "package.json"
    sm = d["source_maps"]
    refs = {r["bundle"]: r for r in sm["bundle_references"]["items"]}
    assert refs["assets/index-3f9a1c.js"]["resolved"] and refs["assets/index-3f9a1c.js"]["map"] == "assets/index-3f9a1c.js.map"
    assert refs["assets/inline.js"]["kind"] == "inline" and refs["assets/inline.js"]["resolved"] and refs["assets/inline.js"]["url"] == "data:..."
    assert refs["assets/no-map.js"]["kind"] == "file" and refs["assets/no-map.js"]["resolved"] is False       # referenced map is not on disk
    assert sm["maps_found"] == 2 and sm["maps_valid"] == 2 and sm["sources_with_content_total"] == 3
    assert {m["rel"] for m in sm["maps"]["items"]} == {"assets/index-3f9a1c.js.map", "assets/inline.js#inline"}
    assert set(d["bundlers"]["detected"]) == {"vite"} and d["service_worker"]["detected"] and d["manifest"]["files"]["items"][0]["name"] == "Tiny Web"
    assert d["service_worker"]["registrations"]["items"][0]["script"] == "/sw.js"


def test_service_worker_detected_by_behaviour_not_only_by_name(backend, tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "worker-abc.js").write_text("self.addEventListener('install', e => e.waitUntil(caches.open('v1')));\nself.addEventListener('fetch', e => {});\n")
    (root / "plain.js").write_text("self.addEventListener('message', e => {});\n")      # event but no worker-specific API
    (root / "manifest.json").write_text(json.dumps({"name": "npm-ish", "version": "1.0"}))       # not a web manifest
    d = backend.inspect(root).data
    assert [f["path"] for f in d["service_worker"]["files"]["items"]] == ["worker-abc.js"] and not d["service_worker"]["files"]["items"][0]["name_match"]
    assert d["manifest"]["detected"] is False


def test_bundler_markers_are_reported_as_heuristics_with_strength(backend, tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "weak.js").write_text("var x = __webpack_require__;\n")
    (root / "parcel.js").write_text("parcelRequire('a'); var $parcel$global = this;\n")
    (root / "clean.js").write_text("console.log('plain');\n")
    d = backend.inspect(root).data["bundlers"]["detected"]
    assert d["webpack"]["strength"] == "weak" and d["parcel"]["strength"] == "strong" and "esbuild" not in d


# ---------------------------------------------------------------------------------------------------------------------
# bounds, links, hostile input
# ---------------------------------------------------------------------------------------------------------------------
def test_inspect_inventory_limit_sets_truncated(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    for i in range(20):
        (root / f"f{i:02d}.js").write_text("x")
    b = JSWebBackend(Settings(limits=Limits(max_inventory_files=5), data_dir=tmp_path / "d"))
    r = b.inspect(root)
    assert r.ok and r.truncated and r.data["truncated"] and r.data["counts"]["files"] == 5
    assert any("inventory file limit 5" in t for t in r.data["truncation"])


def test_inspect_scan_limit_and_map_size_bound(backend, tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    for i in range(6):
        (root / f"b{i}.js").write_text("var __webpack_require__;\n")
    big = smap({"a.js": "x" * 5000})
    (root / "big.js.map").write_text(big)
    r = backend.inspect(root, max_scan_files=3, max_map_bytes=1000)
    d = r.data
    assert r.truncated and any("only the first 3 scanned" in t for t in d["truncation"])
    assert d["bundlers"]["detected"]["webpack"]["files"] == 3
    (m,) = d["source_maps"]["maps"]["items"]
    assert m["valid"] is False and "over the 1000 bound" in m["error"]


def test_symlinks_are_not_followed(backend, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.js").write_text("var __webpack_require__; // secret\n")
    root = tmp_path / "app"
    root.mkdir()
    (root / "a.js").write_text("console.log(1)")
    os.symlink(outside, root / "linked_dir")
    os.symlink(outside / "leak.js", root / "linked.js")
    d = backend.inspect(root).data
    assert d["counts"]["symlinks_not_followed"] == 2 and d["counts"]["files"] == 1 and d["bundlers"]["detected"] == {}
    out = tmp_path / "out"
    e = backend.extract(root, out).data["extraction_report"]
    assert files_under(out) == ["extraction_manifest.json", "tree/a.js"] and e["written"]["files"] == 2


def test_node_modules_are_counted_not_descended(backend, tmp_path):
    root = tmp_path / "app"
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "node_modules" / "left-pad" / "index.js").write_text("var __webpack_require__;")
    (root / "node_modules" / "@scope" / "pkg").mkdir(parents=True)
    (root / "main.js").write_text("1")
    d = backend.inspect(root).data
    assert d["node_modules"]["directories"] == 1 and d["node_modules"]["top_level_packages"]["items"] == ["@scope", "left-pad"]
    assert d["counts"]["files"] == 1 and d["bundlers"]["detected"] == {}
    out = tmp_path / "out"
    e = backend.extract(root, out).data["extraction_report"]
    assert files_under(out) == ["extraction_manifest.json", "tree/main.js"]
    assert json.loads((out / "extraction_manifest.json").read_text())["tree"]["node_modules_skipped"] is True and e["status"] == "ok"


def test_malformed_asar_is_reported_and_inspection_continues(backend, tmp_path):
    root = tmp_path / "app"
    (root / "resources").mkdir(parents=True)
    (root / "resources" / "app.asar").write_bytes(b"\x04\x00\x00\x00" + b"\xff" * 100)
    (root / "package.json").write_text(json.dumps({"name": "x", "main": "main.js", "devDependencies": {"electron": "1.0.0"}}))
    d = backend.inspect(root).data
    (a,) = d["asar"]
    assert a["ok"] is False and "malformed asar" in a["error"]
    assert d["electron"]["detected"] and d["electron"]["main"]["exists"] is False
    out = tmp_path / "out"
    r = backend.extract(root, out)
    assert r.ok and r.data["extraction_report"]["asar"][0]["ok"] is False and r.data["extraction_report"]["status"] == "partial"


def test_asar_inside_app_with_entry_limit_is_truncated_not_silent(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "app.asar").write_bytes(build_asar({f"d/f{i:02d}.js": b"var a;" for i in range(30)} | {"package.json": b"{}"}))
    b = JSWebBackend(Settings(limits=Limits(max_archive_entries=10), data_dir=tmp_path / "d"))
    r = b.inspect(root)
    assert r.truncated and r.data["asar"][0]["truncated"] and any("asar app.asar" in t for t in r.data["truncation"])
    e = b.extract(root, tmp_path / "out")
    assert e.truncated and e.data["extraction_report"]["truncated"] and e.data["extraction_report"]["status"] == "partial"


def test_inspect_missing_root_and_unsupported_file(backend, tmp_path):
    assert not backend.inspect(tmp_path / "missing").ok
    f = tmp_path / "plain.txt"
    f.write_text("hello")
    r = backend.inspect(f)
    assert r.ok and r.data["root_kind"] == "file" and r.data["electron"]["detected"] is False


# ---------------------------------------------------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------------------------------------------------
def test_extract_electron_app_recovers_asar_and_sources_content(backend, studio, case, cases, tmp_path):
    out = tmp_path / "out"
    before = {p.name for p in tmp_path.iterdir()}
    r = backend.extract(ELECTRON, out, studio=studio, case_id=case["case_id"])
    rep = r.data["extraction_report"]
    assert r.ok and not r.truncated and rep["errors"] == [] and rep["refused_total"] == 0
    assert {p.name for p in tmp_path.iterdir()} - before == {"out"}                    # nothing written outside out_dir
    # asar contents extracted incl. the unpacked sibling
    assert (out / "asar/resources__app/package.json").exists() and (out / "asar/resources__app/native/addon.node").read_bytes() == b"\x7fELF-fake-native-addon"
    assert json.loads((out / "asar/resources__app/package.json").read_text())["name"] == "tiny-electron"
    (aout,) = rep["asar"]
    assert aout["ok"] and aout["files"] == 12 and aout["declared_entries"] == 16 and aout["truncated"] is False
    # sourcesContent recovered verbatim with provenance, and the hostile `..` path neutralised
    s = rep["sources_from_source_maps"]
    assert s["recovered"] == 4 and s["known_names_without_content"] == 1 and s["provenance"] == "sourcemap.sourcesContent"
    items = {i["source"]: i for i in s["items"]["items"]}
    app = items["webpack://tiny/./src/app.js"]
    assert (out / app["path"]).read_text() == APP_SRC and app["sha256"] == hashlib.sha256(APP_SRC.encode()).hexdigest() and not app["neutralized_escape"]
    assert (out / items["webpack://tiny/./src/util.js"]["path"]).read_text() == UTIL_SRC
    esc = items["webpack://tiny/../../outside/escape.js"]
    assert esc["neutralized_escape"] and esc["path"].endswith("tiny/outside/escape.js") and (out / esc["path"]).is_file()
    assert items["../../src/main.ts"]["path"].endswith("src/main.ts") and all(i["provenance"] == "sourcemap.sourcesContent" for i in items.values())
    assert not any(p.name == "escape.js" for p in tmp_path.rglob("escape.js") if out not in p.parents)
    # manifest on disk matches the report
    man = json.loads((out / "extraction_manifest.json").read_text())
    assert man["sources_recovered_count"] == 4 and man["sources_known_without_content"]["items"][0]["source"].endswith("node_modules/dep/index.js")
    assert "provenance: sourcemap.sourcesContent" in man["layout"]["sources/<map>/"]
    assert rep["status"] == "partial"                      # one source name had no content -> reported as a gap
    ev = cases.get_evidence(r.evidence_ids[0])
    assert ev["kind"] == "js.extraction_report" and cases.evidence_body(ev["evidence_id"])["sources_from_source_maps"]["recovered"] == 4


def test_extract_web_app_copies_loose_files_verbatim_and_inline_map_sources(backend, tmp_path):
    out = tmp_path / "out"
    r = backend.extract(WEB, out)
    rep = r.data["extraction_report"]
    assert r.ok and rep["status"] == "ok" and rep["loose_files_copied"] == 9
    for rel in ("index.html", "assets/index-3f9a1c.js", "assets/index-3f9a1c.js.map", "sw.js", "manifest.webmanifest"):
        assert (out / "tree" / rel).read_bytes() == (WEB / rel).read_bytes()
    srcs = [i for i in rep["sources_from_source_maps"]["items"]["items"]]
    assert {i["source"] for i in srcs} == {"../../src/main.ts", "../../src/app.ts", "inline-src.js"}
    inline = next(i for i in srcs if i["source"] == "inline-src.js")
    assert inline["map"] == "assets/inline.js#inline" and (out / inline["path"]).read_text() == "export const inline = true;\n"


def test_extract_budget_limits_report_truncation(tmp_path):
    b = JSWebBackend(Settings(limits=Limits(max_archive_expansion_bytes=2500), data_dir=tmp_path / "d"))
    r = b.extract(ELECTRON, tmp_path / "out")
    rep = r.data["extraction_report"]
    assert r.truncated and rep["truncated"] and rep["status"] == "partial" and "expansion limit" in (rep["truncation_reason"] or "")
    assert rep["written"]["bytes"] <= 2500
    assert sum(p.stat().st_size for p in (tmp_path / "out").rglob("*") if p.is_file() and p.name != "extraction_manifest.json") <= 2500
    b2 = JSWebBackend(Settings(limits=Limits(max_archive_entries=6), data_dir=tmp_path / "d2"))
    r2 = b2.extract(WEB, tmp_path / "out2")
    assert r2.truncated and r2.data["extraction_report"]["truncated"] and r2.data["extraction_report"]["written"]["files"] <= 7


def test_extract_refuses_unsafe_outputs(backend, tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "a.js").write_text("1")
    r = backend.extract(root, root / "out")
    assert not r.ok and "path policy" in r.error and not (root / "out").exists()
    r = backend.extract(root, tmp_path)
    assert not r.ok and "path policy" in r.error
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "x").write_text("x")
    r = backend.extract(root, dirty)
    assert not r.ok and "not empty" in r.error
    assert not backend.extract(tmp_path / "missing", tmp_path / "o").ok
    r = backend.extract(root, root / "sub" / "out", source_root=root)
    assert not r.ok and "path policy" in r.error


def test_extract_hostile_asar_member_names_are_refused_and_nothing_escapes(backend, tmp_path):
    tree = {"files": {"..": {"files": {"evil.js": {"size": 4, "offset": "0"}}}, "ok.js": {"size": 4, "offset": "0"},
                      "package.json": {"size": 2, "offset": "4"}}}
    js = json.dumps(tree, separators=(",", ":")).encode()
    padded = js + b"\0" * ((-len(js)) % 4)
    payload = 4 + len(padded)
    root = tmp_path / "work" / "app"
    root.mkdir(parents=True)
    (root / "app.asar").write_bytes(struct.pack("<IIII", 4, payload + 4, payload, len(js)) + padded + b"var{}")
    out = tmp_path / "work" / "out"
    before = {p.name for p in (tmp_path / "work").iterdir()}
    r = backend.extract(root, out)
    rep = r.data["extraction_report"]
    assert r.ok and rep["refused_total"] >= 1 and any("escapes" in x["reason"] for x in rep["refused"]) and rep["status"] == "partial"
    assert {p.name for p in (tmp_path / "work").iterdir()} - before == {"out"}
    assert not list(tmp_path.rglob("evil.js")) and (out / "asar/app/ok.js").read_bytes() == b"var{"


def test_hostile_source_map_names_cannot_escape(backend, tmp_path):
    root = tmp_path / "work" / "app"
    root.mkdir(parents=True)
    names = {"../../../../etc/cron.d/evil": "boom\n", "/abs/path/abs.js": "abs\n", "C:\\Windows\\win.js": "win\n", "webpack:///./ok.js": "ok\n",
             "..": "dots\n", "": "empty\n", "a/../../b.js": "b\n", "nul\x00byte.js": "nul\n", "dup.js": "one\n"}
    (root / "b.js").write_text("//# sourceMappingURL=b.js.map\n")
    (root / "b.js.map").write_text(smap(names) + "")
    # duplicate sanitised names inside one map must not overwrite each other
    m = json.loads((root / "b.js.map").read_text())
    m["sources"] += ["dup.js", "./dup.js"]
    m["sourcesContent"] += ["two\n", "three\n"]
    (root / "b.js.map").write_text(json.dumps(m))
    out = tmp_path / "work" / "out"
    r = backend.extract(root, out)
    rep = r.data["extraction_report"]
    assert r.ok and rep["errors"] == [] and rep["refused_total"] == 0
    assert {p.name for p in (tmp_path / "work").iterdir()} == {"app", "out"}
    assert not (tmp_path / "etc").exists() and not Path("/abs").exists()
    for item in rep["sources_from_source_maps"]["items"]["items"]:
        assert (out / item["path"]).is_file() and ".." not in Path(item["path"]).parts
    dups = [i for i in rep["sources_from_source_maps"]["items"]["items"] if i["path"].rsplit("/", 1)[-1].startswith("dup")]
    assert len(dups) == 3 and len({i["path"] for i in dups}) == 3
    assert sorted((out / i["path"]).read_text() for i in dups) == ["one\n", "three\n", "two\n"]


def test_tree_fingerprint_changes_with_content_and_keys_the_cache(backend, studio, case, cases, tmp_path):
    root = tmp_path / "app"
    shutil.copytree(WEB, root)
    r1 = backend.inspect(root, studio=studio, case_id=case["case_id"])
    (root / "sw.js").write_text("self.addEventListener('install', () => self.skipWaiting()); // changed\n")
    r2 = backend.inspect(root, studio=studio, case_id=case["case_id"])
    assert r1.data["tree_sha256"] != r2.data["tree_sha256"] and r1.evidence_ids != r2.evidence_ids
    assert len(cases.list_evidence(case["case_id"], kind="js.inspection")) == 2
    d1, partial = tree_digest([("a", root / "sw.js")], budget=0)
    assert partial is True and d1 != tree_digest([("a", root / "sw.js")])[0]
    assert backend.inspect(root, module_sha256="f" * 64).data["tree_sha256"] == "f" * 64


def test_routes_through_call_interface(backend, tmp_path):
    r = backend.call("inspect", None, root=str(WEB))
    assert r.ok and r.data["bundlers"]["detected"]
    r = backend.call("extract", None, root=str(WEB), out_dir=str(tmp_path / "out"))
    assert r.ok and (tmp_path / "out" / "tree" / "index.html").exists()
    assert not backend.call("deobfuscate", None).ok


def test_sanitize_source_path_cases():
    cases_ = {"webpack://tiny/./src/app.js": ("tiny/src/app.js", False), "webpack://tiny/../../outside/escape.js": ("tiny/outside/escape.js", True),
              "/etc/passwd": ("etc/passwd", True), "C:\\Users\\x\\a.js": ("Users/x/a.js", True), "../../src/main.ts": ("src/main.ts", True),
              "a/b?x#y": ("a/b", False), "": ("unnamed_0.txt", True), "..": ("unnamed_0.txt", True), "a<b>.js": ("a_b_.js", True),
              "file:///home/u/x.ts": ("home/u/x.ts", False)}
    for src, want in cases_.items():
        assert sanitize_source_path(src) == want, src
    assert sanitize_source_path("x" * 300)[1] is True and len(sanitize_source_path("x" * 300)[0]) == 120
