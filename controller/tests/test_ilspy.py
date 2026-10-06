"""ILSpy backend against the real ilspycmd 9.1.0.7988 and tiny assemblies built with `dotnet new classlib`-style projects.

Fixtures under tests/data/managed (all built on this host from known C# source):
  RebuildSample.dll            net8.0 library: interface, enum, struct, delegate, class with event/iterator, static class
  RebuildSample.badil.dll      same, one method body patched to an invalid opcode  -> ILSpy emits /*Error near IL_...*/
  RebuildSample.badtoken.dll   same, one method body patched with a bad metadata token -> ILSpy throws for the whole project
  unity_mono/                  Assembly-CSharp.dll (MonoBehaviour subclass) + a UnityEngine.dll stub
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebuild_controller.adapters.contract import Availability
from rebuild_controller.backends.ilspy import (ILSpyBackend, MetadataError, count_markers, detect_unity, read_clr_metadata)
from rebuild_controller.config import Limits, Settings

DATA = Path(__file__).parent / "data" / "managed"
SAMPLE = DATA / "RebuildSample.dll"
BADIL = DATA / "RebuildSample.badil.dll"
BADTOKEN = DATA / "RebuildSample.badtoken.dll"
UNITY = DATA / "unity_mono"
PECLI = Path(__file__).resolve().parents[2] / "fixtures" / "pecli" / "original"

PINNED = "9.1.0.7988"


@pytest.fixture(scope="module")
def real_backend() -> ILSpyBackend:
    b = ILSpyBackend(Settings())
    tool = b.probe().tools[0]
    if tool.availability not in (Availability.INSTALLED, Availability.USABLE):
        pytest.skip(f"ilspycmd not usable on this host: {tool.detail}")
    return b


@pytest.fixture
def studio(cases):
    return SimpleNamespace(cases=cases)


@pytest.fixture
def case(cases, src_out):
    src, out = src_out
    return cases.create_case(name="ilspy", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe")


@pytest.fixture
def no_tools(tmp_path, monkeypatch):
    """A host with no ilspycmd: empty tools dir, empty PATH, HOME without ~/.dotnet/tools."""
    home = tmp_path / "home"
    home.mkdir()
    empty = tmp_path / "empty_path"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(empty))
    return ILSpyBackend(Settings(tools_dir=tmp_path / "no_tools"))


# ---------------------------------------------------------------------------------------------------------------------
# probe / smoke / missing tool
# ---------------------------------------------------------------------------------------------------------------------
def test_probe_reports_installed_pinned_tool(real_backend):
    info = real_backend.probe()
    t = info.tools[0]
    assert t.availability == Availability.INSTALLED and t.version == PINNED and t.pinned == PINNED
    assert t.license == "MIT" and t.source.startswith("https://github.com/icsharpcode/ILSpy")
    assert ".NET 8 runtime" in t.prerequisites and Path(t.path).name.startswith("ilspycmd")
    assert len(t.integrity) == 64 and int(t.integrity, 16)          # sha256 of the installed ilspycmd.dll
    assert info.backend_id == "ilspy" and not info.experimental
    assert {o.name for o in info.operations} == {"metadata", "decompile", "detect"}
    assert info.to_dict()["tools"][0]["next_action"] == ""


def test_smoke_runs_a_real_decompile_and_upgrades_to_usable(real_backend):
    t = real_backend.smoke()
    assert t.availability == Availability.USABLE and "Probe.Answer" in t.detail


def test_missing_tool_is_reported_with_next_action_never_raised(no_tools):
    info = no_tools.probe()                                 # must not raise
    t = info.tools[0]
    assert t.availability == Availability.MISSING and t.path is None
    assert "dotnet tool install ilspycmd" in t.next_action and PINNED in t.next_action
    assert info.availability == Availability.MISSING and info.resources["next_action"] == t.next_action
    assert no_tools.smoke().availability == Availability.MISSING


def test_decompile_without_tool_fails_cleanly_but_metadata_still_works_natively(no_tools, tmp_path):
    r = no_tools.decompile(SAMPLE, tmp_path / "out")
    assert not r.ok and "next_action" in r.error and r.data["next_action"]
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())
    m = no_tools.metadata(SAMPLE)
    assert m.ok and m.data["assembly"]["name"] == "RebuildSample" and m.data["tool"]["version"] is None
    assert any("ilspycmd not available" in w for w in m.data["warnings"])
    assert m.data["type_listing_check"]["tool_listing_available"] is False


# ---------------------------------------------------------------------------------------------------------------------
# native metadata reader
# ---------------------------------------------------------------------------------------------------------------------
def test_native_metadata_matches_known_source():
    md = read_clr_metadata(SAMPLE)
    assert md["assembly"]["name"] == "RebuildSample" and md["assembly"]["version"] == "1.2.3.0"
    assert md["target_framework"] == ".NETCoreApp,Version=v8.0"
    assert md["runtime_version"].startswith("v4.0")
    assert {r["name"] for r in md["references"]} == {"System.Runtime", "System.Collections", "System.Threading"}
    assert all(r["version"] == "8.0.0.0" for r in md["references"])
    kinds = {t["name"]: t["kind"] for t in md["types"]}
    assert kinds == {"<Module>": "class", "Rebuild.Sample.IScorer": "interface", "Rebuild.Sample.Mode": "enum", "Rebuild.Sample.Point": "struct",
                     "Rebuild.Sample.Notify": "delegate", "Rebuild.Sample.Calculator": "class", "Rebuild.Sample.Greeter": "class",
                     "Rebuild.Sample.Calculator.<History>d__11": "class"}
    nested = [t["name"] for t in md["types"] if t["nested"]]
    assert nested == ["Rebuild.Sample.Calculator.<History>d__11"]
    assert md["type_count"] == 8 and md["method_count"] > 10
    assert md["pe"]["is_dll"] and md["pe"]["il_only"] and md["pe"]["entry_point_token"] is None


def test_native_metadata_unity_stub_and_netstandard():
    md = read_clr_metadata(UNITY / "Assembly-CSharp.dll")
    assert md["assembly"]["name"] == "Assembly-CSharp"
    assert md["target_framework"] == ".NETStandard,Version=v2.1"
    assert {r["name"] for r in md["references"]} >= {"UnityEngine", "netstandard"}


def test_native_metadata_rejects_non_managed_and_garbage(tmp_path):
    junk = tmp_path / "junk.dll"
    junk.write_bytes(b"MZ" + b"\0" * 500)
    with pytest.raises(MetadataError):
        read_clr_metadata(junk)
    elf = tmp_path / "elf"
    elf.write_bytes(b"\x7fELF" + b"\0" * 100)
    with pytest.raises(MetadataError):
        read_clr_metadata(elf)
    native_pe = next((p for p in PECLI.glob("*") if p.suffix.lower() == ".exe"), None) if PECLI.is_dir() else None
    if native_pe is not None:
        with pytest.raises(MetadataError, match="no CLR header"):
            read_clr_metadata(native_pe)
    truncated = tmp_path / "trunc.dll"
    truncated.write_bytes(SAMPLE.read_bytes()[:1500])
    with pytest.raises(MetadataError):
        read_clr_metadata(truncated)


def test_metadata_op_cross_checks_listing_and_records_cache_keyed_evidence(real_backend, studio, case, cases, tmp_path):
    sha = hashlib.sha256(SAMPLE.read_bytes()).hexdigest()
    r = real_backend.metadata(SAMPLE, studio=studio, case_id=case["case_id"], module_id=None)
    assert r.ok and not r.truncated
    d = r.data
    assert d["assembly"]["version"] == "1.2.3.0" and d["target_framework"] == ".NETCoreApp,Version=v8.0"
    assert d["type_count"] == 8 and d["user_type_count"] == 7 and d["top_level_type_count"] == 6
    assert d["type_listing_check"] == {"source": "ilspycmd -l cisde", "tool_listing_available": True, "tool_type_count": 8,
                                       "matches_native_metadata": True}
    assert d["module"]["sha256"] == sha and d["tool"] == {"name": "ilspycmd", "version": PINNED} and d["profile"]["profile"] == "dotnet"
    ev = cases.get_evidence(r.evidence_ids[0])
    assert ev["kind"] == "dotnet.metadata" and ev["producer"] == "ilspy"
    body = cases.evidence_body(ev["evidence_id"])
    assert body["module"]["sha256"] == sha
    # same inputs -> same evidence row; a different module -> a new row (inputs carry tool version + module sha256)
    r2 = real_backend.metadata(SAMPLE, studio=studio, case_id=case["case_id"])
    assert r2.evidence_ids == r.evidence_ids
    other = tmp_path / "Other.dll"
    other.write_bytes(BADIL.read_bytes())
    r3 = real_backend.metadata(other, studio=studio, case_id=case["case_id"])
    assert r3.evidence_ids != r.evidence_ids
    assert len(cases.list_evidence(case["case_id"], kind="dotnet.metadata")) == 2


def test_metadata_of_non_managed_module_is_a_failed_result_not_an_exception(real_backend, tmp_path):
    p = tmp_path / "native.dll"
    p.write_bytes(b"MZ" + b"\0" * 300)
    r = real_backend.metadata(p)
    assert not r.ok and "PE" in r.error
    r = real_backend.metadata(tmp_path / "missing.dll")
    assert not r.ok and "not found" in r.error


# ---------------------------------------------------------------------------------------------------------------------
# decompile
# ---------------------------------------------------------------------------------------------------------------------
def test_decompile_project_mode_clean(real_backend, studio, case, cases, tmp_path):
    out = tmp_path / "out"
    r = real_backend.decompile(SAMPLE, out, studio=studio, case_id=case["case_id"])
    assert r.ok and not r.truncated, r.error
    rep = r.data["recovery_report"]
    assert rep["mode"] == "project" and rep["status"] == "ok" and rep["profile"] == "dotnet"
    assert (rep["types_total"], rep["types_decompiled"], rep["types_with_errors"], rep["types_failed"], rep["types_unmapped"]) == (6, 6, 0, 0, 0)
    assert rep["error_markers"] == 0 and rep["equivalence_claimed"] is False and "not asserted" in rep["claims"]
    assert rep["project_file"] == "RebuildSample.csproj" and (out / "RebuildSample.csproj").is_file()
    calc = (out / "Rebuild.Sample" / "Calculator.cs").read_text()
    assert "public int Fibonacci(int n)" in calc and "Hello, " in (out / "Rebuild.Sample" / "Greeter.cs").read_text()
    assert "enum Mode" in (out / "Rebuild.Sample" / "Mode.cs").read_text()
    names = {t["name"]: t for t in rep["type_listing"]["items"]}
    assert names["Rebuild.Sample.Point"]["kind"] == "struct" and names["Rebuild.Sample.Point"]["file"] == "Rebuild.Sample/Point.cs"
    assert all(t["status"] == "decompiled" for t in names.values())
    # the per-file digests in the report describe what is on disk
    f = next(x for x in rep["files"]["items"] if x["path"] == "Rebuild.Sample/Calculator.cs")
    assert f["sha256"] == hashlib.sha256((out / f["path"]).read_bytes()).hexdigest() and f["error_markers"] == 0
    ev = cases.get_evidence(r.evidence_ids[0])
    assert ev["kind"] == "dotnet.recovery_report"
    assert cases.evidence_body(ev["evidence_id"])["module"]["sha256"] == hashlib.sha256(SAMPLE.read_bytes()).hexdigest()


def test_decompile_output_is_deterministic(real_backend, tmp_path):
    a = real_backend.decompile(SAMPLE, tmp_path / "a").data["recovery_report"]
    b = real_backend.decompile(SAMPLE, tmp_path / "b").data["recovery_report"]
    assert a["output_tree_sha256"] == b["output_tree_sha256"] and a["output_file_count"] == b["output_file_count"] == 8


def test_decompile_counts_ilspy_error_markers_per_type(real_backend, tmp_path):
    out = tmp_path / "out"
    r = real_backend.decompile(BADIL, out)
    assert r.ok
    rep = r.data["recovery_report"]
    assert rep["mode"] == "project"
    assert rep["il_error_markers"] == 1 and rep["error_markers"] == 1
    assert (rep["types_total"], rep["types_decompiled"], rep["types_with_errors"], rep["types_failed"]) == (6, 5, 1, 0)
    point = next(t for t in rep["type_listing"]["items"] if t["name"] == "Rebuild.Sample.Point")
    assert point["status"] == "decompiled_with_errors" and point["error_markers"] == 1
    assert "/*Error near IL_000c: Unknown opcode: 0xFEFD*/" in (out / "Rebuild.Sample" / "Point.cs").read_text()
    assert rep["equivalence_claimed"] is False


def test_decompile_falls_back_per_type_when_ilspy_aborts_the_project(real_backend, tmp_path):
    out = tmp_path / "out"
    r = real_backend.decompile(BADTOKEN, out)
    rep = r.data["recovery_report"]
    assert r.ok and rep["mode"] == "per_type_fallback" and rep["status"] == "partial" and rep["project_file"] is None
    assert (rep["types_total"], rep["types_decompiled"], rep["types_failed"]) == (6, 5, 1)
    by = {t["name"]: t for t in rep["type_listing"]["items"]}
    assert by["Rebuild.Sample.Point"]["status"] == "failed" and "Error decompiling" in by["Rebuild.Sample.Point"]["message"]
    assert by["Rebuild.Sample.Calculator"]["status"] == "decompiled" and by["Rebuild.Sample.Calculator"]["file"] == "types/Rebuild.Sample/Calculator.cs"
    assert [f["member"] for f in rep["failures"]] == ["Rebuild.Sample.Point.Sum"] and rep["failures"][0]["token"] == "0x06000002"
    assert (out / "types/Rebuild.Sample/Calculator.cs").is_file() and not (out / "types/Rebuild.Sample/Point.cs").exists()
    assert "Invalid token" in rep["project_run_stderr"] or "Error decompiling" in rep["project_run_stderr"]


def test_fallback_budget_marks_unattempted_types_and_truncated(real_backend, tmp_path):
    r = real_backend.decompile(BADTOKEN, tmp_path / "out", fallback_max_types=2)
    rep = r.data["recovery_report"]
    assert rep["types_not_attempted"] == 4 and r.truncated and rep["status"] == "partial"
    assert rep["types_decompiled"] + rep["types_failed"] == 2


def test_decompile_refuses_unsafe_or_dirty_output(real_backend, tmp_path):
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "keep.txt").write_text("x")
    r = real_backend.decompile(SAMPLE, dirty)
    assert not r.ok and "not empty" in r.error and (dirty / "keep.txt").read_text() == "x"
    src = tmp_path / "srcroot"
    src.mkdir()
    shutil.copy(SAMPLE, src / SAMPLE.name)
    r = real_backend.decompile(src / SAMPLE.name, src / "out", source_root=src)
    assert not r.ok and "path policy" in r.error and not (src / "out").exists()
    r = real_backend.decompile(src / SAMPLE.name, src, source_root=None)
    assert not r.ok and "path policy" in r.error


def test_decompile_routes_through_stage_context_run(real_backend, tmp_path):
    calls = []

    class Ctx:
        services = {}

        def run(self, command, *, cwd=None, env=None, timeout=None, stdin=None, check=False):
            calls.append(command)
            p = subprocess.run(command, capture_output=True, env=env, timeout=timeout)
            return SimpleNamespace(returncode=p.returncode, stdout=p.stdout, stderr=p.stderr, truncated=False, timed_out=False, duration_s=0.0)

    r = real_backend.call("decompile", Ctx(), module_path=str(SAMPLE), out_dir=str(tmp_path / "out"))
    assert r.ok and any("-p" in c for c in calls) and all("--disable-updatecheck" in c for c in calls)
    r = real_backend.call("metadata", Ctx(), module_path=str(SAMPLE))
    assert r.ok and r.data["assembly"]["name"] == "RebuildSample"
    assert not real_backend.call("nope", Ctx()).ok


def test_timeout_is_a_visible_failure(real_backend, tmp_path):
    r = real_backend.decompile(SAMPLE, tmp_path / "out", timeout=0.001)
    assert not r.ok and "timeout" in r.error


# ---------------------------------------------------------------------------------------------------------------------
# Unity
# ---------------------------------------------------------------------------------------------------------------------
def make_unity_mono(root: Path) -> Path:
    managed = root / "Game_Data" / "Managed"
    managed.mkdir(parents=True)
    for f in UNITY.glob("*.dll"):
        shutil.copy(f, managed / f.name)
    return managed / "Assembly-CSharp.dll"


def make_il2cpp(root: Path) -> Path:
    (root / "Game_Data" / "il2cpp_data" / "Metadata").mkdir(parents=True)
    (root / "Game_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat").write_bytes(b"\xaf\x1b\xb1\xfa" + b"\0" * 64)
    ga = root / "GameAssembly.dll"
    ga.write_bytes(b"MZ" + b"\0" * 600)
    (root / "Game.exe").write_bytes(b"MZ" + b"\0" * 100)
    return ga


def test_unity_mono_is_detected_and_decompiled(real_backend, tmp_path):
    asm = make_unity_mono(tmp_path / "game")
    prof = detect_unity(tmp_path / "game")
    assert prof["profile"] == "unity_mono" and prof["supported"] and not prof["experimental"]
    assert real_backend.detect(asm).data["profile"]["profile"] == "unity_mono"
    m = real_backend.metadata(asm)
    assert m.ok and m.data["profile"]["profile"] == "unity_mono"
    r = real_backend.decompile(asm, tmp_path / "out")
    rep = r.data["recovery_report"]
    assert r.ok and rep["profile"] == "unity_mono" and rep["types_total"] == 1 and rep["types_decompiled"] == 1
    cs = (tmp_path / "out" / "PlayerController.cs").read_text()
    assert "class PlayerController : MonoBehaviour" in cs and "Debug.Log" in cs
    assert rep["unresolved_references"] == []          # UnityEngine.dll sits next to the module


def test_unity_mono_detected_from_references_even_without_layout(real_backend, tmp_path):
    lone = tmp_path / "Weird.dll"
    shutil.copy(UNITY / "Assembly-CSharp.dll", lone)
    m = real_backend.metadata(lone)
    assert m.data["profile"]["profile"] == "unity_mono"
    r = real_backend.decompile(lone, tmp_path / "out")
    assert r.ok and r.data["recovery_report"]["profile"] == "unity_mono"
    assert r.data["recovery_report"]["unresolved_references"] == ["UnityEngine"]     # reported, not hidden


def test_il2cpp_is_reported_unsupported_and_nothing_is_faked(real_backend, studio, case, cases, tmp_path):
    root = tmp_path / "il2cpp_game"
    ga = make_il2cpp(root)
    prof = detect_unity(root)
    assert prof["profile"] == "unity_il2cpp" and prof["supported"] is False and prof["experimental"] is True
    assert any(e.endswith("global-metadata.dat") for e in prof["evidence"]) and prof["next_action"]
    # native GameAssembly: not a managed module -> explicit unsupported, no output tree
    out = tmp_path / "out"
    r = real_backend.decompile(ga, out, studio=studio, case_id=case["case_id"])
    assert not r.ok and "is not a managed assembly" in r.error and "IL2CPP" in r.error
    assert r.data["recovered"] is False and r.data["profile"] == "unity_il2cpp" and r.data["equivalence_claimed"] is False
    assert not out.exists() or not any(out.iterdir())
    ev = cases.get_evidence(r.evidence_ids[0])
    assert ev["kind"] == "dotnet.profile" and cases.evidence_body(ev["evidence_id"])["status"] == "unsupported"
    # a managed stub dropped into an IL2CPP layout is refused too (no method bodies to recover)
    stub = root / "Game_Data" / "Managed"
    stub.mkdir()
    shutil.copy(SAMPLE, stub / "Assembly-CSharp.dll")
    out2 = tmp_path / "out2"
    r2 = real_backend.decompile(stub / "Assembly-CSharp.dll", out2)
    assert not r2.ok and "unity_il2cpp" in r2.data["profile"] and "stubs" in r2.data["reason"]
    assert not out2.exists()


# ---------------------------------------------------------------------------------------------------------------------
# marker counting (real ILSpy texts)
# ---------------------------------------------------------------------------------------------------------------------
def test_count_markers_on_real_ilspy_phrases():
    text = ("public int A() {\n\t/*Error near IL_000c: Unknown opcode: 0xFEFD*/;\n}\n"
            "// Error decompiling something\n"
            "\t//IL_0000: Unknown result type (might be due to invalid IL or missing references)\n"
            "\t/*Error near IL_0007: Handle with invalid row number.*/;\n")
    assert count_markers(text) == {"il_error_markers": 2, "comment_error_markers": 1, "error_markers": 3, "unknown_result_warnings": 1}
    assert count_markers("clean code only")["error_markers"] == 0
    assert count_markers("// Errors are handled elsewhere")["error_markers"] == 0     # word boundary, not a marker
