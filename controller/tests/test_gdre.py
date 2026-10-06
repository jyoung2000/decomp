"""GDRE backend against the real GDRE tools 2.7.0 (headless) on tiny Godot 4.3-format PCKs built by this file's own packer.

The packer below is deliberately independent of gdre.build_probe_pck (which only feeds smoke()). Layout verified by actually
running `gdre_tools --headless --recover` on its output:
  "GDPC" | u32 pack_version=2 | u32 major | u32 minor | u32 patch | u32 flags | u64 file_base | 16*u32 reserved | u32 file_count
  entries: u32 path_len, path (NUL padded to 4), u64 offset (relative to file_base), u64 size, 16B md5, u32 flags
  data region at file_base (files 16-byte aligned).  Embedded: [exe][pck][u64 pck_size]["GDPC"] with flags bit1 (rel filebase).
GDScript bytecode (.gdc) is produced by the real tool (`--compile ... --bytecode=4.3.0`), not hand-made.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebuild_controller.adapters.contract import Availability
from rebuild_controller.backends.gdre import (GDREBackend, build_probe_pck, clean_lines, parse_gdre_output, parse_project_godot,
                                              read_pck_header, summarize_entries)
from rebuild_controller.config import Limits, Settings

MAIN_GD = 'extends Node2D\n\nfunc _ready():\n\tprint("hello from tiny game")\n'
UTIL_GD = "extends Node\nclass_name Util\n\nstatic func add(a: int, b: int) -> int:\n\treturn a + b\n"
MAIN_TSCN = '[gd_scene load_steps=2 format=3]\n\n[ext_resource type="Script" path="res://main.gd" id="1"]\n\n[node name="Main" type="Node2D"]\nscript = ExtResource("1")\n'
PINNED_ZIP_SHA = "abb4c197fe517d6a46b67faf41fc76d9890c882661a55ca2d49ba605955dfacc"


# ---------------------------------------------------------------------------------------------------------------------
# independent test packer
# ---------------------------------------------------------------------------------------------------------------------
def pad4(b: bytes) -> bytes:
    return b + b"\0" * ((-len(b)) % 4)


def v_str(s: str) -> bytes:
    b = s.encode()
    return struct.pack("<II", 4, len(b)) + pad4(b)


def v_int(i: int) -> bytes:
    return struct.pack("<Ii", 2, i)


def project_binary(name: str = "TinyGame", main_scene: str = "res://main.tscn") -> bytes:
    items = [("config_version", v_int(5)), ("application/config/name", v_str(name)), ("application/run/main_scene", v_str(main_scene))]
    out = b"ECFG" + struct.pack("<I", len(items))
    for k, v in items:
        kb = k.encode()
        out += struct.pack("<I", len(kb)) + kb + struct.pack("<I", len(v)) + v
    return out


def remap(target: str) -> bytes:
    return f'[remap]\n\npath="{target}"\n'.encode()


def pack_pck(files: list[tuple[str, bytes]], *, version=(4, 3, 0), flags=0, pack_version=2) -> bytes:
    entries = b""
    data = b""
    for path, content in files:
        pb = pad4(path.encode() + b"\0")
        entry = struct.pack("<I", len(pb)) + pb + struct.pack("<QQ", len(data), len(content)) + hashlib.md5(content).digest()
        if pack_version >= 2:
            entry += struct.pack("<I", 0)
        entries += entry
        data += content + b"\0" * ((-len(content)) % 16)
    if pack_version >= 2:
        header_len = 4 + 4 + 12 + 4 + 8 + 64 + 4
        base = -(-(header_len + len(entries)) // 16) * 16
        head = b"GDPC" + struct.pack("<IIIIIQ", pack_version, *version, flags, base) + b"\0" * 64 + struct.pack("<I", len(files))
        pre = head + entries
        return pre + b"\0" * (base - len(pre)) + data
    # pack versions 0/1: no flags/file_base; offsets are absolute from the start of the file
    header_len = 4 + 4 + 12 + 64 + 4
    base = header_len + len(entries)
    abs_entries = b""
    off = 0
    for path, content in files:
        pb = pad4(path.encode() + b"\0")
        abs_entries += struct.pack("<I", len(pb)) + pb + struct.pack("<QQ", base + off, len(content)) + hashlib.md5(content).digest()
        off += len(content) + ((-len(content)) % 16)
    head = b"GDPC" + struct.pack("<IIII", pack_version, *version) + b"\0" * 64 + struct.pack("<I", len(files))
    return head + abs_entries + data


def embed_trailer(exe: bytes, pck: bytes) -> bytes:
    exe += b"\0" * ((-len(exe)) % 8)
    pck = bytearray(pck)
    struct.pack_into("<I", pck, 20, 2)               # PACK_REL_FILEBASE: file_base is relative to the embedded pck
    return exe + bytes(pck) + struct.pack("<Q", len(pck)) + b"GDPC"


def elf_with_pck_section(pck: bytes) -> bytes:
    pck = bytearray(pck)
    struct.pack_into("<I", pck, 20, 2)
    pck = bytes(pck)
    names = [".text", "pck", ".shstrtab"]
    shstr, idx = b"\0", {}
    for n in names:
        idx[n] = len(shstr)
        shstr += n.encode() + b"\0"
    off_text = 64
    off_pck = (off_text + 16 + 15) // 16 * 16
    off_str = off_pck + len(pck)
    off_sh = (off_str + len(shstr) + 7) // 8 * 8

    def sh(name, typ, flags, addr, off, size, align=1):
        return struct.pack("<IIQQQQIIQQ", name, typ, flags, addr, off, size, 0, 0, align, 0)

    shdrs = b"\0" * 64 + sh(idx[".text"], 1, 6, 0x401000, off_text, 16, 16) + sh(idx["pck"], 1, 0, 0, off_pck, len(pck), 16) \
        + sh(idx[".shstrtab"], 3, 0, 0, off_str, len(shstr))
    ehdr = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\0" * 8 + struct.pack("<HHIQQQIHHHHHH", 2, 0x3e, 1, 0x401000, 0, off_sh, 0, 64, 0, 0, 64, 4, 3)
    body = ehdr + b"\xc3" * 16 + b"\0" * (off_pck - off_text - 16) + pck + shstr
    return body + b"\0" * (off_sh - len(body)) + shdrs


# ---------------------------------------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def backend(tmp_path_factory) -> GDREBackend:
    s = Settings(data_dir=tmp_path_factory.mktemp("gdre-data"))
    b = GDREBackend(s)
    tool = b.probe().tools[0]
    if tool.availability not in (Availability.INSTALLED, Availability.USABLE):
        pytest.skip(f"gdre_tools not usable on this host: {tool.detail}")
    return b


@pytest.fixture(scope="module")
def gdc(backend, tmp_path_factory) -> dict[str, bytes]:
    """Real GDScript 4.3 bytecode compiled by the real tool."""
    d = tmp_path_factory.mktemp("gdc")
    (d / "main.gd").write_text(MAIN_GD)
    (d / "util.gd").write_text(UTIL_GD)
    env = dict(os.environ, HOME=str(d / "home"))
    (d / "home").mkdir()
    subprocess.run([backend.find_tool(), "--headless", f"--compile={d / 'main.gd'}", f"--compile={d / 'util.gd'}", "--bytecode=4.3.0",
                    f"--output={d}"], env=env, capture_output=True, timeout=120)
    out = {n: (d / f"{n}.gdc").read_bytes() for n in ("main", "util")}
    assert all(v[:4] == b"GDSC" for v in out.values())
    return out


@pytest.fixture(scope="module")
def good_files(gdc) -> list[tuple[str, bytes]]:
    return [("res://project.binary", project_binary()), ("res://main.gd.remap", remap("res://main.gdc")), ("res://main.gdc", gdc["main"]),
            ("res://util.gd.remap", remap("res://util.gdc")), ("res://util.gdc", gdc["util"]), ("res://main.tscn", MAIN_TSCN.encode())]


@pytest.fixture
def studio(cases):
    return SimpleNamespace(cases=cases)


@pytest.fixture
def case(cases, src_out):
    src, out = src_out
    return cases.create_case(name="gdre", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe")


@pytest.fixture
def no_tools(tmp_path, monkeypatch):
    empty = tmp_path / "empty_path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no_localappdata"))   # the per-user tools dir is a discovery location too
    return GDREBackend(Settings(tools_dir=tmp_path / "no_tools", data_dir=tmp_path / "data"))


# ---------------------------------------------------------------------------------------------------------------------
# probe / smoke / missing tool
# ---------------------------------------------------------------------------------------------------------------------
def test_probe_reports_pinned_tool_with_verified_integrity(backend):
    info = backend.probe()
    t = info.tools[0]
    assert t.availability == Availability.INSTALLED and t.version == "2.7.0" and t.pinned == "2.7.0"
    assert t.license == "MIT" and "GDRETools/gdsdecomp" in t.source and Path(t.path).name == ("gdre_tools.exe" if os.name == "nt" else "gdre_tools.x86_64")
    if (Path(Settings().tools_dir) / "GDRE_tools-v2.7.0-linux.zip").exists():
        assert t.integrity == PINNED_ZIP_SHA and "matches the pinned sha256" in t.detail
    assert info.profiles == ["godot"] and {o.name for o in info.operations} == {"detect", "recover"}


def test_smoke_recovers_a_builtin_pck(backend):
    t = backend.smoke()
    assert t.availability == Availability.USABLE and "one-file PCK" in t.detail


def test_missing_tool_probe_has_next_action_and_nothing_raises(no_tools, tmp_path):
    info = no_tools.probe()
    t = info.tools[0]
    assert t.availability == Availability.MISSING and t.path is None
    assert "releases/tag/v2.7.0" in t.next_action and PINNED_ZIP_SHA in t.next_action
    assert info.availability == Availability.MISSING and info.resources["next_action"] == t.next_action
    assert no_tools.smoke().availability == Availability.MISSING
    pck = tmp_path / "x.pck"
    pck.write_bytes(pack_pck([("res://a.txt", b"a")]))
    r = no_tools.recover(pck, tmp_path / "out")
    assert not r.ok and "next_action" in r.error and r.data["next_action"]
    assert not (tmp_path / "out").exists()
    assert no_tools.detect(pck).ok                       # native detection needs no tool


# ---------------------------------------------------------------------------------------------------------------------
# detect (native)
# ---------------------------------------------------------------------------------------------------------------------
def test_detect_standalone_pck_lists_entries_natively(backend, good_files, tmp_path):
    p = tmp_path / "game.pck"
    p.write_bytes(pack_pck(good_files))
    r = backend.detect(p)
    d = r.data
    assert r.ok and d["kind"] == "pck" and d["pack_version"] == 2 and d["engine_version"] == "4.3.0"
    assert d["flags"] == {"dir_encrypted": False, "rel_filebase": False} and d["file_count"] == 6 and d["native_listing"]
    assert [e["path"] for e in d["entries"]["items"]] == [f for f, _ in good_files]
    assert all(e["in_bounds"] for e in d["entries"]["items"]) and d["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    s = d["summary"]
    assert s["scripts_bytecode"] == 2 and s["scripts_text_gd"] == 0 and s["has_project_binary"] and not s["gdextension_files"]


def test_detect_trailer_embedded_and_elf_section(backend, good_files, tmp_path):
    pck = pack_pck(good_files)
    exe = tmp_path / "game.exe"
    exe.write_bytes(embed_trailer(b"MZ" + b"\0" * 5000, pck))
    d = backend.detect(exe).data
    assert d["kind"] == "embedded_pck" and d["pck_size"] == len(pck) and d["pck_start"] > 5000 and d["flags"]["rel_filebase"]
    assert d["file_count"] == 6 and d["engine_version"] == "4.3.0"
    elf = tmp_path / "game.x86_64"
    elf.write_bytes(elf_with_pck_section(pck))
    d = backend.detect(elf).data
    assert d["kind"] == "embedded_pck" and d["file_count"] == 6 and any("'pck' executable section" in n for n in d["parse_notes"])


def test_detect_non_pck_and_malformed_inputs(backend, tmp_path):
    txt = tmp_path / "t.exe"
    txt.write_bytes(b"MZ" + b"\0" * 400)
    r = backend.detect(txt)
    assert not r.ok and r.data["kind"] == "none" and any("no GDPC" in n for n in r.data["parse_notes"])
    good = pack_pck([("res://a.txt", b"a")])
    cut = tmp_path / "cut.pck"
    cut.write_bytes(good[:60])
    r = backend.detect(cut)
    assert not r.ok and "malformed PCK" in r.error and r.data["kind"] == "malformed"
    huge = bytearray(good)
    struct.pack_into("<I", huge, 96, 4_000_000)           # file_count far beyond what the file can hold
    p = tmp_path / "huge.pck"
    p.write_bytes(bytes(huge))
    r = backend.detect(p)
    assert not r.ok and "more than the file can hold" in r.error
    badver = bytearray(good)
    struct.pack_into("<I", badver, 4, 9)
    p = tmp_path / "badver.pck"
    p.write_bytes(bytes(badver))
    assert not backend.detect(p).ok and "unknown PCK format version" in backend.detect(p).error
    bad_path = bytearray(good)
    struct.pack_into("<I", bad_path, 100, 0x7FFFFFFF)     # absurd path length in the first directory entry
    p = tmp_path / "badpath.pck"
    p.write_bytes(bytes(bad_path))
    assert not backend.detect(p).ok
    assert not backend.detect(tmp_path / "missing.pck").ok


def test_detect_flags_encrypted_directory_and_v1_v3_headers(backend, tmp_path):
    enc = pack_pck([("res://a.txt", b"a")], flags=1)
    p = tmp_path / "enc.pck"
    p.write_bytes(enc)
    d = backend.detect(p).data
    assert d["flags"]["dir_encrypted"] and d["native_listing"] is False and any("key" in n for n in d["parse_notes"])
    v1 = pack_pck([("res://a.gd", b"x"), ("res://b.gdc", b"y")], version=(3, 5, 0), pack_version=1)
    p = tmp_path / "v1.pck"
    p.write_bytes(v1)
    d = backend.detect(p).data
    assert d["pack_version"] == 1 and d["engine_version"] == "3.5.0" and [e["path"] for e in d["entries"]["items"]] == ["res://a.gd", "res://b.gdc"]
    assert d["summary"]["scripts_text_gd"] == 1 and d["summary"]["scripts_bytecode"] == 1
    v3 = bytearray(pack_pck([("res://a.txt", b"a")]))
    struct.pack_into("<I", v3, 4, 3)
    v3[32:40] = struct.pack("<Q", 0)                       # v3 inserts dir_offset after file_base; layout differs, listing is not attempted
    p = tmp_path / "v3.pck"
    p.write_bytes(bytes(v3))
    d = backend.detect(p).data
    assert d["pack_version"] == 3 and d["native_listing"] is False and any("pack version 3" in n for n in d["parse_notes"])


def test_detect_entry_limit_sets_truncated(tmp_path):
    b = GDREBackend(Settings(limits=Limits(max_archive_entries=5), data_dir=tmp_path / "d"))
    p = tmp_path / "many.pck"
    p.write_bytes(pack_pck([(f"res://f{i}.txt", b"x") for i in range(20)]))
    r = b.detect(p)
    assert r.ok and r.truncated and r.data["entries_truncated"] and r.data["file_count"] == 20
    assert len(r.data["entries"]["items"]) == 5


def test_detect_directory_input(backend, tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    assert backend.detect(d).data["kind"] == "none"
    (d / "project.godot").write_text("config_version=5\n")
    assert backend.detect(d).data["kind"] == "project_dir"


def test_read_pck_header_summarize_and_project_godot_parsing():
    entries = [{"path": p} for p in ("res://a.gd", "res://b.gdc", "res://c.gde", "res://x.gdextension", "res://libx.so", "res://Game.cs", "res://project.binary", "res://i.png.import")]
    s = summarize_entries(entries)
    assert (s["scripts_text_gd"], s["scripts_bytecode"], s["scripts_encrypted_bytecode"], s["csharp_files"]) == (1, 2, 1, 1)
    assert s["has_project_binary"] and s["has_import_files"] and s["gdextension_files"] and s["native_libraries"] == ["res://libx.so"]
    pg = parse_project_godot('; c\nconfig_version=5\n\n[application]\nconfig/name="X"\nrun/main_scene="res://m.tscn"\nconfig/features=PackedStringArray("4.3", "Forward Plus")\n\n[autoload]\nG="*res://g.gd"\n\n[dotnet]\nproject/assembly_name="X"\n')
    assert pg["name"] == "X" and pg["main_scene"] == "res://m.tscn" and pg["feature_engine_version"] == "4.3" and pg["autoloads"] == ["G"] and pg["has_dotnet_section"]
    assert clean_lines("Exporting resources... [===       ] 50%   \rreal line\r\n\r\n") == ["real line"]


# ---------------------------------------------------------------------------------------------------------------------
# recover (real GDRE)
# ---------------------------------------------------------------------------------------------------------------------
def test_recover_full_project_with_decompiled_scripts(backend, good_files, studio, case, cases, tmp_path):
    p = tmp_path / "game.pck"
    p.write_bytes(pack_pck(good_files))
    out = tmp_path / "out"
    r = backend.recover(p, out, studio=studio, case_id=case["case_id"])
    assert r.ok and not r.truncated, r.error
    rep = r.data["recovery_report"]
    assert rep["status"] == "ok" and not rep["gaps_found"] and rep["equivalence_claimed"] is False
    assert rep["engine"]["version"] == "4.3.0" and rep["engine"]["from_gdre"] == "4.3.0" and rep["engine"]["from_pck_header"] == "4.3.0"
    assert rep["engine"]["bytecode_revision"].startswith("4.3.0-stable")
    assert rep["project"]["name"] == "TinyGame" and rep["project"]["main_scene"] == "res://main.tscn"
    # scripts: bytecode in the PCK, decompiled by GDRE, content verified on disk
    sc = rep["scripts"]
    assert (sc["bytecode_decompiled"], sc["bytecode_not_decompiled"], sc["text_gd"]) == (2, 0, 0) and sc["originals_kept_under_.autoconverted"] == 2
    assert {i["path"]: i["status"] for i in sc["items"]["items"]} == {"res://main.gdc": "decompiled", "res://util.gdc": "decompiled"}
    assert (out / "main.gd").read_text().strip() == MAIN_GD.strip() and "return a + b" in (out / "util.gd").read_text()
    assert (out / "main.tscn").exists() and (out / "project.godot").exists() and (out / "gdre_export.log").exists()
    assert rep["gdre_totals"]["scripts_decompiled"] == 2 and rep["gdre_totals"]["failed_conversions"] == 0
    assert rep["recovered_resources"]["pck_entries_listed_natively"] == 6 and rep["recovered_resources"]["gdre_verified_files"] == 6
    g = rep["gaps"]
    assert g["scripts_not_decompiled_count"] == 0 and g["failed_conversions_count"] == 0 and g["native_extensions"] == [] and g["errors"] == []
    assert g["encryption"] == {"needs_key": False, "key_supplied": False, "encrypted_files": 0}
    assert rep["tool"]["version"] == "2.7.0" and rep["module"]["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    ev = cases.get_evidence(r.evidence_ids[0])
    assert ev["kind"] == "godot.recovery_report" and ev["producer"] == "gdre"
    assert cases.evidence_body(ev["evidence_id"])["engine"]["version"] == "4.3.0"


def test_recover_evidence_is_keyed_by_tool_version_and_module_hash(backend, good_files, gdc, studio, case, cases, tmp_path):
    p = tmp_path / "game.pck"
    p.write_bytes(pack_pck(good_files))
    r1 = backend.recover(p, tmp_path / "o1", studio=studio, case_id=case["case_id"])
    r2 = backend.recover(p, tmp_path / "o2", studio=studio, case_id=case["case_id"])
    assert r1.evidence_ids == r2.evidence_ids                      # identical inputs and report -> deduplicated
    q = tmp_path / "game2.pck"
    q.write_bytes(pack_pck(good_files + [("res://extra.txt", b"more")]))
    r3 = backend.recover(q, tmp_path / "o3", studio=studio, case_id=case["case_id"])
    assert r3.evidence_ids != r1.evidence_ids
    ev = cases.list_evidence(case["case_id"], kind="godot.recovery_report")
    assert len(ev) == 2
    row = cases.get_evidence(r1.evidence_ids[0])
    from rebuild_controller.ids import stable_json_hash
    assert row["input_hash"] == stable_json_hash({"op": "recover", "backend": "gdre", "schema": 1, "tool": "gdre_tools", "tool_version": "2.7.0",
                                                  "module_sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "scripts_only": False,
                                                  "ignore_checksum_errors": False, "key_fingerprint": None})


def test_recover_reports_undecompilable_script_as_explicit_gap(backend, good_files, gdc, tmp_path):
    bad = bytearray(gdc["util"])
    bad[-20:] = b"\xff" * 20
    files = [(n, bytes(bad) if n == "res://util.gdc" else d) for n, d in good_files]
    p = tmp_path / "bad.pck"
    p.write_bytes(pack_pck(files))
    out = tmp_path / "out"
    r = backend.recover(p, out)
    rep = r.data["recovery_report"]
    assert r.ok and rep["status"] == "partial" and rep["gaps_found"]
    g = rep["gaps"]
    assert g["scripts_not_decompiled"] == ["res://util.gdc"] and g["scripts_not_decompiled_count"] == 1
    assert g["bytecode_left_undecompiled_on_disk"] == ["util.gdc"] and (out / "util.gdc").exists() and not (out / "util.gd").exists()
    assert g["failed_conversions"] and g["failed_conversions"][0]["resource"] == "res://util.gd"
    assert any("Error decompiling code res://util.gdc" in e["message"] for e in g["errors"]) and g["error_line_total"] > 0
    sc = rep["scripts"]
    assert (sc["bytecode_decompiled"], sc["bytecode_not_decompiled"]) == (1, 1)
    assert {i["path"]: i["status"] for i in sc["items"]["items"]} == {"res://main.gdc": "decompiled", "res://util.gdc": "not_decompiled"}
    assert (out / "main.gd").exists()                                 # the good script was still recovered


def test_recover_reports_native_extension_gaps(backend, good_files, tmp_path):
    ext = b'[configuration]\nentry_symbol="x_init"\ncompatibility_minimum="4.3"\n\n[libraries]\nlinux.x86_64="res://addons/native/libnative.so"\nwindows.x86_64="res://addons/native/native.dll"\n'
    files = good_files + [("res://addons/native/native.gdextension", ext), ("res://addons/native/libnative.so", b"\x7fELF")]
    p = tmp_path / "ext.pck"
    p.write_bytes(pack_pck(files))
    r = backend.recover(p, tmp_path / "out")
    g = r.data["recovery_report"]["gaps"]
    assert r.data["recovery_report"]["status"] == "partial"
    (e,) = g["native_extensions"]
    assert e["config"] == "addons/native/native.gdextension" and e["libraries"]["linux.x86_64"] == "res://addons/native/libnative.so"
    assert e["missing_libraries"] == [{"platform": "windows.x86_64", "library": "res://addons/native/native.dll"}]
    assert g["native_extensions_missing_libraries"] == 1 and g["gdextension_library_errors"]
    assert r.data["recovery_report"]["gdre_totals"]["scripts_decompiled"] == 2          # unrelated recovery still worked


def test_recover_from_trailer_embedded_exe_and_elf_pck_section(backend, good_files, tmp_path):
    pck = pack_pck(good_files)
    exe = tmp_path / "game.bin"
    exe.write_bytes(embed_trailer(b"\x7fELF" + b"\0" * 3000, pck))
    r = backend.recover(exe, tmp_path / "o_trailer")
    assert r.ok and r.data["recovery_report"]["module"]["kind"] == "embedded_pck" and r.data["recovery_report"]["scripts"]["bytecode_decompiled"] == 2
    elf = tmp_path / "game.x86_64"
    elf.write_bytes(elf_with_pck_section(pck))
    r = backend.recover(elf, tmp_path / "o_elf")
    assert r.ok and r.data["recovery_report"]["module"]["kind"] == "embedded_pck" and (tmp_path / "o_elf" / "main.gd").read_text().strip() == MAIN_GD.strip()


def test_recover_text_scripts_are_extracted_as_is_not_claimed_decompiled(backend, tmp_path):
    files = [("res://project.binary", project_binary()), ("res://main.gd", MAIN_GD.encode()), ("res://main.tscn", MAIN_TSCN.encode()),
             ("res://data/blob.bin.import", b'[remap]\n\nimporter="keep"\ntype="Resource"\npath="res://.godot/imported/blob.bin-aaaa.bin"\n\n[deps]\n\nsource_file="res://data/blob.bin"\ndest_files=["res://.godot/imported/blob.bin-aaaa.bin"]\n'),
             ("res://.godot/imported/blob.bin-aaaa.bin", b"\x01\x02\x03")]
    p = tmp_path / "txt.pck"
    p.write_bytes(pack_pck(files))
    r = backend.recover(p, tmp_path / "out")
    rep = r.data["recovery_report"]
    assert r.ok
    assert (rep["scripts"]["text_gd"], rep["scripts"]["bytecode_decompiled"]) == (1, 0)
    assert rep["scripts"]["items"]["items"] == [{"path": "res://main.gd", "source": "pck_text", "status": "extracted_as_is"}]
    # the 'keep' importer is something GDRE cannot convert: surfaced as a gap, not silently dropped
    assert rep["gaps"]["not_converted_count"] == 1 and any("blob.bin" in f for f in rep["gaps"]["not_converted_files"])
    assert rep["gaps"]["unsupported_resource_types"] and rep["status"] == "partial"


def test_recover_garbage_input_is_a_failed_result_not_an_exception(backend, tmp_path):
    p = tmp_path / "garbage.pck"
    p.write_bytes(os.urandom(4096))
    out = tmp_path / "out"
    r = backend.recover(p, out)
    assert not r.ok and r.error
    assert r.data["recovery_report"]["status"] == "failed" and r.data["recovery_report"]["recovered_resources"]["files_user_visible"] == 0
    assert any("no PCK structure detected natively" in n for n in r.data["recovery_report"]["notes"])


def test_recover_checksum_corruption_is_surfaced(backend, good_files, tmp_path):
    raw = bytearray(pack_pck(good_files))
    raw[-40] ^= 0xFF                                         # flip a byte inside file data
    p = tmp_path / "corrupt.pck"
    p.write_bytes(bytes(raw))
    r = backend.recover(p, tmp_path / "out")
    assert not r.ok and r.data["recovery_report"]["status"] == "failed"
    assert r.data["recovery_report"]["gaps"]["fatal"] and any("MD5" in f for f in r.data["recovery_report"]["gaps"]["fatal"])
    # explicit opt-in recovers what is readable, and the checksum failure stays visible as a gap
    r2 = backend.recover(p, tmp_path / "out2", ignore_checksum_errors=True)
    rep2 = r2.data["recovery_report"]
    assert r2.ok and rep2["status"] == "partial" and rep2["gaps"]["checksum_errors"] >= 1 and rep2["gaps"]["fatal"] == []
    assert rep2["tool"]["command"].endswith("--ignore-checksum-errors") and (tmp_path / "out2" / "main.gd").exists()


def test_recover_key_is_validated_and_never_stored(backend, good_files, studio, case, cases, tmp_path):
    p = tmp_path / "game.pck"
    p.write_bytes(pack_pck(good_files))
    assert "64-character" in backend.recover(p, tmp_path / "o0", key="not-hex").error
    key = "00112233445566778899AABBCCDDEEFF00112233445566778899AABBCCDDEEFF"
    r = backend.recover(p, tmp_path / "o1", key=key, studio=studio, case_id=case["case_id"])
    rep = r.data["recovery_report"]
    assert rep["tool"]["command"].count("<redacted>") == 1 and key not in json.dumps(rep)
    blob = cases.blobs.path_for(cases.get_evidence(r.evidence_ids[0])["blob_sha"]).read_text()
    assert key not in blob and key.lower() not in blob.lower()
    ev = cases.get_evidence(r.evidence_ids[0])
    assert ev["input_hash"]                                    # cache key includes a key fingerprint, not the key
    assert rep["gaps"]["encryption"]["key_supplied"] is True


def test_recover_refuses_unsafe_outputs(backend, good_files, tmp_path):
    p = tmp_path / "game.pck"
    p.write_bytes(pack_pck(good_files))
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "keep.txt").write_text("x")
    r = backend.recover(p, dirty)
    assert not r.ok and "not empty" in r.error and (dirty / "keep.txt").read_text() == "x"
    r = backend.recover(p, tmp_path)
    assert not r.ok and "path policy" in r.error
    src = tmp_path / "srcroot"
    src.mkdir()
    r = backend.recover(p, src / "out", source_root=src)
    assert not r.ok and "path policy" in r.error and not (src / "out").exists()
    assert not backend.recover(tmp_path / "nope.pck", tmp_path / "o").ok


def test_recover_routes_through_stage_context_and_times_out_visibly(backend, good_files, tmp_path):
    p = tmp_path / "game.pck"
    p.write_bytes(pack_pck(good_files))
    calls = []

    class Ctx:
        services = {}

        def run(self, command, *, cwd=None, env=None, timeout=None, stdin=None, check=False):
            calls.append((command, env["HOME"]))
            q = subprocess.run(command, capture_output=True, env=env, cwd=cwd, timeout=timeout)
            return SimpleNamespace(returncode=q.returncode, stdout=q.stdout, stderr=q.stderr, truncated=False, timed_out=False, duration_s=0.0)

    r = backend.call("recover", Ctx(), pck=str(p), out_dir=str(tmp_path / "out"))
    assert r.ok and len(calls) == 1 and calls[0][0][1] == "--headless" and "tool-home" in calls[0][1]
    r = backend.call("detect", Ctx(), path=str(p))
    assert r.ok and r.data["kind"] == "pck"
    t = backend.recover(p, tmp_path / "out_t", timeout=0.01)
    assert not t.ok and "timeout" in t.error


def test_parse_gdre_output_on_real_log_shapes():
    log = ("GDRE Tools v2.7.0\nDetected Engine Version: 4.3.0\nDetected Bytecode Revision: 4.3.0-stable (77af6ca)\nSuccessfully loaded PCK!\n"
           "Verified 8 files, no errors detected!\nExtracted 8 files, no errors detected!\n"
           "Reading folder x structure... [                              ] 100%\n"
           "********************************EXPORT REPORT********************************\n\nTotals:\n"
           "Decompiled scripts:                     1\nScripts not decompiled:                 1\nFailed conversions:                     2\n-------------\n\n------\n"
           "The following scripts were not decompiled:\nres://util.gdc\n------\n\nFailed conversions:\n* res://util.gd\n  * Error decompiling code: boom\n  * Errors:\n"
           "    ERROR: inner detail\n------------------------------------------------------------------------------------\n"
           "ERROR: Failed to find gdextension libraries for plugin res://a.gdextension\n   at: x (y.cpp:1)\nERROR: Failed to find gdextension libraries for plugin res://a.gdextension\n"
           "Recovery finished in 00m00s\n")
    p = parse_gdre_output(log)
    assert p["engine_version"] == "4.3.0" and p["tool_version"] == "2.7.0" and p["verified_files"] == 8 and p["recovery_finished"] and p["pck_loaded"]
    assert p["totals"] == {"scripts_decompiled": 1, "scripts_not_decompiled": 1, "failed_conversions": 2}
    assert p["scripts_not_decompiled"] == ["res://util.gdc"]
    assert p["failed_conversions"] == [{"resource": "res://util.gd", "errors": ["Error decompiling code: boom", "Errors:"]}]
    assert p["errors"] == [{"message": "Failed to find gdextension libraries for plugin res://a.gdextension", "count": 2}]
    assert p["gdextension_library_errors"] and not p["fatal"]
    assert parse_gdre_output("ERROR: FATAL ERROR: Can't open project config!\nError: Failed to open [\"x.pck\"]:\n")["fatal"]
