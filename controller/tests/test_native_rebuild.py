"""R4: rebuild in the original language first (.NET -> C#, JVM -> Java).

Fast unit tests: target selection, unsupported combinations, forecast text, the dependency table, the lock pin, and every
deterministic repair rule (project file from metadata, ILSpy project quirks, reserved attributes, missing using/import, the
IL-verified 'case null' rule, CFR nested-class names), the jar writer and the launch specs.

e2e (marked, real tools): the dotnetapp and javacli fixtures through the full pipeline with AI off -> every scenario of the frozen
fixture oracle passes (9/9 and 11/11); and a run where the IL rule is disabled, so the recovered C# fails one scenario and a
(scripted, local) model repairs only that, starting from the recovered source.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

import pytest

from rebuild_controller import native_rebuild as nr
from rebuild_controller.builders import dotnet as bdotnet, java as bjava
from rebuild_controller.reconstruct import _unsupported_combo, choose_target

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "fixtures"


# ====================================================================================== target selection
@pytest.mark.parametrize("profile,want", [("dotnet", "csharp"), ("jvm", "java"), ("native_pe", "rust"), ("unity_mono", "rust"),
                                          ("android", "rust"), ("godot", "rust_bevy"), ("web", "web")])
def test_auto_picks_the_original_language_for_dotnet_and_jvm(profile, want):
    target, reasons = choose_target({"target_language": "auto"}, profile, {})
    assert target == want and reasons
    assert choose_target({"target_language": "rust"}, "dotnet", {})[0] == "rust"          # an explicit choice (port) is kept


def test_native_targets_refuse_inputs_they_cannot_rebuild():
    assert _unsupported_combo("dotnet", "csharp", "exe") is None and _unsupported_combo("jvm", "java", "portable") is None
    assert _unsupported_combo("unknown", "csharp", "exe") is None                         # before discovery: decided later
    assert "C# target rebuilds a .NET program" in _unsupported_combo("native_pe", "csharp", "exe")
    assert "Android SDK" in _unsupported_combo("android", "java", "exe")
    assert "HTML/CSS/JS" in _unsupported_combo("dotnet", "csharp", "web")
    assert nr.native_target_for("dotnet") == "csharp" and nr.native_target_for("jvm") == "java" and nr.native_target_for("native_pe") is None


# ====================================================================================== forecast
@pytest.fixture
def studio(settings):
    from rebuild_controller.services import StudioServices
    s = StudioServices(settings)
    s.ai.advisor = None
    yield s
    s.stop()


def test_forecast_native_rebuild_without_ai(studio):
    from rebuild_controller.implement import forecast
    fc = forecast(studio, target_language="csharp", output_type="exe", profile="dotnet", ai_policy={"mode": "no_ai"}, launch_profile={"baseline_file": "x"})
    assert fc["state"] == "native_rebuild" and fc["can_produce_implementation"] is True and fc["will_use_ai"] is False
    assert "Rebuild in the original language" in fc["summary"] and "likely path to a verified rebuild" in fc["summary"]
    assert fc["recommended_target"] == "csharp" and fc["effective_target"] == "csharp" and fc["toolchain"]["tool"] == "dotnet-sdk"
    assert any(o["target"] == "csharp" and o["likely_verified"] for o in fc["target_options"])
    assert any("No AI" in d for d in fc["details"]) and fc["verifiable"] is True
    auto = forecast(studio, target_language="auto", output_type="exe", profile="jvm", ai_policy={"mode": "no_ai"}, launch_profile={})
    assert auto["state"] == "native_rebuild" and auto["effective_target"] == "java" and auto["toolchain"]["tool"] == "temurin-jdk21"


def test_forecast_port_names_the_likely_path(studio):
    from rebuild_controller.implement import forecast
    fc = forecast(studio, target_language="rust", output_type="exe", profile="dotnet", ai_policy={"mode": "no_ai"}, launch_profile={})
    assert fc["state"] == "scaffold_only" and fc["recommended_target"] == "csharp"
    assert fc["likely_path"].startswith("For a verified rebuild, choose C#") and fc["details"][0] == fc["likely_path"]
    pe = forecast(studio, target_language="rust", output_type="exe", profile="native_pe", ai_policy={"mode": "no_ai"}, launch_profile={})
    assert pe["recommended_target"] == "rust" and "likely path" in pe["likely_path"]
    unknown = forecast(studio, target_language="auto", output_type="exe", profile=None, ai_policy={"mode": "no_ai"}, launch_profile={})
    assert ".NET programs are rebuilt in C#" in unknown["likely_path"]
    bad = forecast(studio, target_language="csharp", output_type="exe", profile="native_pe", ai_policy={"mode": "no_ai"}, launch_profile={})
    assert bad["state"] == "unsupported"


def test_forecast_native_with_ai_uses_it_only_on_failure(studio):
    from rebuild_controller.implement import forecast
    conn = studio.connections.create("local", "LM Studio", endpoint="http://127.0.0.1:1234/v1", auth_mode="local", models=[{"id": "m"}], dialect="chat")
    studio.connections.set_route("implementation", conn["connection_id"], "m")
    fc = forecast(studio, target_language="java", output_type="exe", profile="jvm", ai_policy={"mode": "assisted", "max_attempts": 2}, launch_profile={})
    assert fc["state"] == "native_rebuild" and fc["ai_on_failure"] is True and fc["max_attempts"] == 2
    assert any(d.startswith("Only if scenarios still fail") for d in fc["details"])


def test_api_accepts_the_new_targets(studio, src_out):
    from fastapi.testclient import TestClient
    from rebuild_controller.api.server import create_app
    with TestClient(create_app(studio, "tok")) as c:
        c.headers.update({"Authorization": "Bearer tok", "Origin": "http://localhost:5173"})
        caps = c.get("/capabilities").json()
        combos = {(x["target_language"], x["output_type"]): x["state"] for x in caps["output_combinations"]}
        assert combos[("csharp", "exe")] == "supported" and combos[("java", "portable")] == "supported" and combos[("java", "pwa")] == "unsupported"
        r = c.post("/implementation/forecast", json={"target_language": "csharp", "output_type": "exe", "profile": "dotnet"}).json()
        assert r["state"] == "native_rebuild"
        src, out = src_out
        made = c.post("/cases", json={"name": "n", "source_root": str(src), "output_root": str(out), "target_language": "java", "output_type": "exe",
                                      "ai_policy": {"mode": "no_ai"}})
        assert made.status_code == 200 and made.json()["target_language"] == "java"


# ====================================================================================== dependency table + pins
def test_needs_table_and_lock_pin_cover_the_native_builders(studio, tmp_path):
    from rebuild_controller.dependency_health import DependencyHealth, load_needs
    needs = load_needs()
    assert needs["targets"]["csharp"]["required"] == ["dotnet-sdk"] and needs["targets"]["java"]["required"] == ["temurin-jdk21"]
    assert needs["auto_target"]["csharp"] == ["dotnet"] and needs["auto_target"]["java"] == ["jvm"]
    lock = json.loads((REPO / "docs" / "dependency-lock.json").read_text("utf-8"))["tools"]
    sdk = lock["dotnet-sdk"]
    art = sdk["artifact"]
    assert art["url"] == f"https://builds.dotnet.microsoft.com/dotnet/Sdk/{sdk['version']}/{art['name']}" and art["name"] == f"dotnet-sdk-{sdk['version']}-win-x64.zip"
    assert len(art["sha256"]) == 64 and len(art["sha512_official"]) == 128 and art["verify_required"] is False and art["size_bytes"] > 100_000_000
    assert sdk["layout"]["entry"] == "dotnet.exe" and sdk["layout"]["version_args"] == ["--list-sdks"] and sdk["install_dir"] == "dotnet-sdk"
    assert sdk["layout"]["entry_sha256"] == lock["dotnet-runtime"]["layout"]["entry_sha256"]      # same release: the same host binary
    from rebuild_controller.tool_setup import FRIENDLY
    assert FRIENDLY["dotnet-sdk"][0] == bdotnet.TOOL_TITLE and FRIENDLY["temurin-jdk21"][0] == bjava.TOOL_TITLE
    for name in ("dependency-lock.json", "dependency-needs.json"):
        assert (REPO / "docs" / name).read_bytes() == (REPO / "controller" / "rebuild_controller" / "data" / name).read_bytes(), name
    src = tmp_path / "s"; src.mkdir()
    dh = DependencyHealth(studio, alt_probes={})
    for target, tool in (("csharp", "dotnet-sdk"), ("java", "temurin-jdk21")):
        case = studio.create_case(name=target, source_root=str(src), output_root=str(tmp_path / f"o-{target}"), target_language=target, output_type="exe")
        cn = dh.case_needs(case)
        assert tool in cn["required"] and cn["target"] == target


# ====================================================================================== C# rules
META = {"assembly": {"name": "app"}, "target_framework": ".NETCoreApp,Version=v8.0", "pe": {"is_dll": False, "entry_point_token": "0x06000013"}}
RC = {"runtimeOptions": {"tfm": "net8.0", "configProperties": {"System.Globalization.Invariant": True, "System.GC.Server": False,
                                                                "System.Runtime.Serialization.EnableUnsafeBinaryFormatterSerialization": False}}}


@pytest.mark.parametrize("tf,want", [(".NETCoreApp,Version=v8.0", "net8.0"), (".NETCoreApp,Version=v3.1", "netcoreapp3.1"),
                                     (".NETStandard,Version=v2.0", "netstandard2.0"), (".NETFramework,Version=v4.7.2", "net472"), (None, None)])
def test_tfm_from_metadata(tf, want):
    assert nr.tfm_from_metadata(tf) == want


def test_project_file_from_metadata_and_runtimeconfig():
    p = nr.csproj_from_metadata(META, RC)
    assert "<AssemblyName>app</AssemblyName>" in p and "<OutputType>Exe</OutputType>" in p and "<TargetFramework>net8.0</TargetFramework>" in p
    assert "<InvariantGlobalization>true</InvariantGlobalization>" in p and "<ServerGarbageCollection>false</ServerGarbageCollection>" in p
    assert "EnableUnsafeBinaryFormatter" not in p and "<GenerateAssemblyInfo>False</GenerateAssemblyInfo>" in p
    lib = nr.csproj_from_metadata({**META, "pe": {"is_dll": True, "entry_point_token": "0x00000000"}})
    assert "<OutputType>Library</OutputType>" in lib


def test_fix_csproj_rewrites_ilspy_moniker_and_is_idempotent():
    ilspy = ('<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup>\n    <AssemblyName>app</AssemblyName>\n    <TargetFramework>netcoreapp8.0</TargetFramework>\n'
             '  </PropertyGroup>\n</Project>')
    fixed, notes = nr.fix_csproj(ilspy, META, RC)
    assert "<TargetFramework>net8.0</TargetFramework>" in fixed and "<InvariantGlobalization>true</InvariantGlobalization>" in fixed
    assert "<Nullable>annotations</Nullable>" in fixed and fixed.rstrip().endswith("</Project>")
    assert any("netcoreapp8.0 -> net8.0" in n for n in notes) and any("runtimeconfig.json" in n for n in notes)
    again, notes2 = nr.fix_csproj(fixed, META, RC)
    assert again == fixed and notes2 == []
    old, _ = nr.fix_csproj(ilspy.replace("netcoreapp8.0", "netcoreapp3.1"), META, None)
    assert "netcoreapp3.1" in old                          # a real .NET Core 3.1 moniker is left alone


def test_reserved_attributes_are_stripped_only_at_assembly_or_module_level():
    src = ('using System.Reflection;\n[assembly: AssemblyTitle("x")]\n[module: RefSafetyRules(11)]\n'
           '[module: System.Runtime.CompilerServices.RefSafetyRulesAttribute(11)]\n[assembly: NullablePublicOnly(false)]\nclass C { [Obsolete] void M() {} }\n')
    out, found = nr.strip_reserved_attributes(src)
    assert found == ["RefSafetyRules", "RefSafetyRules", "NullablePublicOnly"]
    assert "RefSafety" not in out and "NullablePublicOnly" not in out and '[assembly: AssemblyTitle("x")]' in out and "[Obsolete]" in out


def _cs(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_repair_csharp_adds_usings_and_removes_reserved_attributes(tmp_path):
    src = _cs(tmp_path, {
        "App/Program.cs": "namespace App;\n\npublic static class Program\n{\n\tpublic static void Main()\n\t{\n\t\tvar l = new List<int>();\n"
                          "\t\tFile.Exists(\"x\");\n\t\tvar n = new string[0].Select(s => s).ToList();\n\t\tvar h = new Helper();\n\t}\n}\n",
        "Lib/Helper.cs": "namespace Lib;\n\npublic class Helper { }\n",
        "Properties/AssemblyInfo.cs": "using System.Reflection;\n[assembly: AssemblyTitle(\"a\")]\n[module: Embedded]\n",
    })
    p = str(src / "App" / "Program.cs")
    log = "\n".join([
        f"{p}(7,15): error CS0246: The type or namespace name 'List<>' could not be found (are you missing a using directive or an assembly reference?) [x.csproj]",
        f"{p}(8,3): error CS0103: The name 'File' does not exist in the current context [x.csproj]",
        f"{p}(9,30): error CS1061: 'string[]' does not contain a definition for 'Select' and no accessible extension method 'Select' accepting a first argument "
        f"of type 'string[]' could be found (are you missing a using directive or an assembly reference?) [x.csproj]",
        f"{p}(10,15): error CS0246: The type or namespace name 'Helper' could not be found (are you missing a using directive or an assembly reference?) [x.csproj]",
        f"{p}(10,15): error CS0246: The type or namespace name 'Helper' could not be found (are you missing a using directive or an assembly reference?) [x.csproj]",
        f"{src / 'Properties' / 'AssemblyInfo.cs'}(3,10): error CS8335: Do not use 'System.Runtime.CompilerServices.EmbeddedAttribute'. This is reserved for compiler usage. [x.csproj]",
        f"{src / 'Unknown.cs'}(1,1): error CS0246: The type or namespace name 'Nope' could not be found [x.csproj]",
    ])
    assert len(nr.parse_csharp_errors(log)) == 6                          # the duplicate line is reported once
    files, notes = nr.repair_csharp(src, log)
    prog = files["App/Program.cs"]
    for ns in ("System.Collections.Generic", "System.IO", "System.Linq", "Lib"):
        assert f"using {ns};" in prog and prog.count(f"using {ns};") == 1
    assert prog.index("using ") < prog.index("namespace App;")
    assert "[module: Embedded]" not in files["Properties/AssemblyInfo.cs"] and "AssemblyTitle" in files["Properties/AssemblyInfo.cs"]
    assert len(notes) == 5
    assert nr.repair_csharp(src, "Build FAILED.\nerror MSB1009: Project file does not exist.") == ({}, [])


IL = """
.method private hidebysig
	instance void RunMain () cil managed
{
	.locals init (
		[0] string
	)
	IL_0040: ldarg.0
	IL_0041: ldstr "> "
	IL_0046: call instance string App.Menus::Prompt(string)
	IL_004b: stloc.0
	IL_004c: ldloc.0
	IL_004d: brfalse.s IL_0069
	IL_004f: ldloc.0
	IL_0050: ldstr "0"
	IL_0055: call bool [System.Runtime]System.String::op_Equality(string, string)
	IL_005a: brtrue.s IL_0069
	IL_005c: ldloc.0
	IL_005d: ldstr "q"
	IL_0062: call bool [System.Runtime]System.String::op_Equality(string, string)
	IL_0067: brfalse.s IL_006a
	IL_0069: ret
	IL_006a: ldloc.0
	IL_006b: ldstr "1"
	IL_0070: call bool [System.Runtime]System.String::op_Equality(string, string)
	IL_0075: brfalse.s IL_007f
	IL_0077: ret
	IL_007f: ret
} // end of method Menus::RunMain

.method private hidebysig
	instance void Other () cil managed
{
	IL_0000: ldarg.0
	IL_0001: call instance string App.Menus::Read()
	IL_0006: stloc.0
	IL_0007: ldloc.0
	IL_0008: brfalse.s IL_0020
	IL_000a: ldloc.0
	IL_000b: ldstr "x"
	IL_0010: call bool [System.Runtime]System.String::op_Equality(string, string)
	IL_0015: brtrue.s IL_0030
	IL_0020: ret
	IL_0030: ret
} // end of method Menus::Other
"""

CS = """namespace App;

public sealed class Menus
{
	public void RunMain()
	{
		while (true)
		{
			string text = Prompt("> ");
			switch (text)
			{
			case "0":
				return;
			case "q":
				return;
			case "1":
				break;
			default:
				break;
			}
		}
	}

	private void Other()
	{
		switch (Read())
		{
		case "x":
			return;
		}
	}
}
"""


def test_il_rule_restores_the_null_case_ilspy_drops_and_only_there():
    shared = nr.il_null_shared_cases(IL)
    assert shared == {"RunMain": {"0", "q"}}                # Other(): null goes elsewhere than "x" -> no rule
    out, n = nr.add_null_cases(CS, "RunMain", shared["RunMain"])
    assert n == 1 and "\t\t\tcase null:\n\t\t\tcase \"0\":" in out
    again, n2 = nr.add_null_cases(out, "RunMain", {"0", "q"})
    assert n2 == 0 and again == out                         # an existing 'case null:' is never duplicated
    assert nr.add_null_cases(CS, "Missing", {"0"}) == (CS, 0)


def test_il_rule_applies_through_the_backend_listing(tmp_path):
    class FakeIlspy:
        def il_listing(self, dll, type_name, ctx=None):
            return IL if type_name == "App.Menus" else None
    (tmp_path / "App").mkdir()
    (tmp_path / "App" / "Menus.cs").write_text(CS, encoding="utf-8")
    notes = nr.apply_il_switch_rule(FakeIlspy(), tmp_path / "x.dll", tmp_path, [{"name": "App.Menus", "namespace": "App", "nested": False},
                                                                                 {"name": "App.Gone", "namespace": "App", "nested": False}])
    assert len(notes) == 1 and "restored 1 'case null:'" in notes[0]
    assert "case null:" in (tmp_path / "App" / "Menus.cs").read_text("utf-8")


# ====================================================================================== Java rules
def test_javac_errors_and_import_insertion(tmp_path):
    root = tmp_path / "src"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "util").mkdir(parents=True)
    (root / "a" / "b" / "Main.java").write_text("package a.b;\n\nimport java.io.IOException;\n\npublic class Main {\n  List<String> x;\n  Helper h;\n"
                                                "  Main$Inner i;\n  static class Inner {}\n}\n", encoding="utf-8")
    (root / "a" / "util" / "Helper.java").write_text("package a.util;\npublic class Helper {}\n", encoding="utf-8")
    log = ("src/a/b/Main.java:6: error: cannot find symbol\n  List<String> x;\n  ^\n  symbol:   class List\n  location: class Main\n"
           "src/a/b/Main.java:7: error: cannot find symbol\n  Helper h;\n  ^\n  symbol:   class Helper\n  location: class Main\n"
           "src/a/b/Main.java:8: error: cannot find symbol\n  Main$Inner i;\n  ^\n  symbol:   class Main$Inner\n  location: class Main\n"
           "src/a/b/Main.java:9: error: ';' expected\n3 errors\n")
    errs = nr.parse_javac_errors(log)
    assert [e.get("symbol") for e in errs] == ["List", "Helper", "Main$Inner", None]
    files, notes = nr.repair_java(tmp_path, log)
    text = files["src/a/b/Main.java"]
    assert "import java.util.List;" in text and "import a.util.Helper;" in text and "Main.Inner i;" in text
    assert text.index("import java.io.IOException;") < text.index("import java.util.List;") < text.index("public class Main")
    assert len(notes) == 3
    assert nr.add_java_import("package p;\nimport java.util.*;\n", "java.util.List") is None
    assert nr.add_java_import("class A {}\n", "java.util.List").startswith("import java.util.List;")


@pytest.mark.parametrize("major,rel", [(50, 8), (52, 8), (55, 11), (61, 17), (65, 21), (66, None)])
def test_release_for_class_major(major, rel):
    assert bjava.release_for_class_major(major) == rel


def test_jar_writer_is_deterministic_with_manifest_first(tmp_path):
    classes = tmp_path / "classes" / "p"
    classes.mkdir(parents=True)
    (classes / "Main.class").write_bytes(b"\xca\xfe\xba\xbe")
    res = tmp_path / "res"
    (res / "META-INF").mkdir(parents=True)
    (res / "META-INF" / "MANIFEST.MF").write_text("stale", encoding="utf-8")
    (res / "data.txt").write_text("hi", encoding="utf-8")
    proj = {"main_class": "p.Main", "manifest": {"Implementation-Title": "t", "Bad Key": "x"}}
    a, b = tmp_path / "a.jar", tmp_path / "b.jar"
    assert bjava.write_jar(a, tmp_path / "classes", res, proj) == 3
    bjava.write_jar(b, tmp_path / "classes", res, proj)
    assert a.read_bytes() == b.read_bytes()
    with zipfile.ZipFile(a) as z:
        assert z.namelist() == ["META-INF/MANIFEST.MF", "data.txt", "p/Main.class"]
        man = z.read("META-INF/MANIFEST.MF").decode()
        assert "Main-Class: p.Main\r\n" in man and "Implementation-Title: t" in man and "Bad Key" not in man and "stale" not in man


def test_launch_specs_for_csharp_and_java(tmp_path):
    from rebuild_controller.comparators.cli import host_launcher
    host = tmp_path / ("dotnet.exe" if os.name == "nt" else "dotnet")
    host.write_bytes(b"")
    argv, env, runner = host_launcher({"type": "dotnet", "path": "app.dll", "dotnet": str(host), "env": {"DOTNET_ROOT": str(tmp_path)}}, tmp_path)
    assert argv == [str(host), str(tmp_path / "app.dll")] and env["DOTNET_ROOT"] == str(tmp_path) and runner == "dotnet"
    java = tmp_path / ("java.exe" if os.name == "nt" else "java")
    java.write_bytes(b"")
    argv, _env, runner = host_launcher({"type": "java", "jar": "app.jar", "java": str(java)}, tmp_path)
    assert argv == [str(java), "-jar", str(tmp_path / "app.jar")] and runner == "java"


def test_builders_find_toolchains_and_say_what_is_missing(tmp_path, monkeypatch):
    assert bdotnet.private_sdk(tmp_path) is None and bjava.private_jdk(tmp_path) is None
    sdk = tmp_path / "dotnet-sdk"
    (sdk / "sdk" / "8.0.425").mkdir(parents=True)
    (sdk / ("dotnet.exe" if os.name == "nt" else "dotnet")).write_bytes(b"")
    assert bdotnet.private_sdk(tmp_path)["sdk"] == "8.0.425"
    (sdk / "sdk" / "8.0.425").rename(sdk / "sdk" / "7.0.100")
    assert bdotnet.private_sdk(tmp_path) is None                   # a .NET 7 SDK cannot build net8.0 projects
    assert "install" in bdotnet.BLOCKER and "install" in bjava.BLOCKER
    from rebuild_controller.implement import toolchain_note
    from types import SimpleNamespace
    monkeypatch.setattr(bdotnet, "system_sdk", lambda: None)
    note = toolchain_note(SimpleNamespace(settings=SimpleNamespace(tools_dir=tmp_path)), "csharp")
    assert note["available"] is False and note["title"] == ".NET SDK (private)" and "Open Tools" in note["message"]


# ====================================================================================== e2e: the real pipeline
def _have_dotnet_chain() -> bool:
    from support.merged_tools import find_tool_dir
    il = find_tool_dir("ilspycmd")
    return bool(il and (il / "ilspycmd.dll").is_file() and (find_tool_dir("dotnet") or shutil.which("dotnet"))
                and (find_tool_dir("dotnet-sdk") or bdotnet.system_sdk()))


def _have_java_chain() -> bool:
    from support.merged_tools import find_tool_dir
    cfr = find_tool_dir("cfr")
    return bool(cfr and (cfr / "cfr-0.152.jar").is_file() and (find_tool_dir("jre") or find_tool_dir("jdk21") or shutil.which("java"))
                and (find_tool_dir("jdk21") or bjava.system_jdk()))


@pytest.fixture
def pipeline(settings, tmp_path):
    from rebuild_controller.services import StudioServices
    from support.merged_tools import merged_tools, remove
    tools, links = merged_tools(tmp_path / "tools")
    settings.tools_dir = tools
    settings.limits.max_stage_seconds = 900
    settings.limits.lease_timeout_seconds = 300
    s = StudioServices(settings)
    s.ai.advisor = None
    yield s
    s.stop()
    remove(links)


def _drain(st, rounds=400):
    from rebuild_controller.jobs import JobState
    for _ in range(rounds):
        n = st.runner.run_pending()
        if n == 0 and not st.jobs.list(None, [JobState.QUEUED, JobState.RUNNING]):
            return
    raise AssertionError("pipeline did not settle")


def _run_fixture(st, tmp_path, fixture: str, target: str, ai_policy: dict | None = None) -> str:
    case = st.create_case(name=fixture, source_root=str(FIX / fixture / "original"), output_root=str(tmp_path / "out"), target_language=target,
                          output_type="exe", ai_policy=ai_policy or {"mode": "no_ai"},
                          launch_profile={"baseline_file": str(FIX / fixture / "expected" / "scenarios.json")})
    st.start_rebuild(case["case_id"])
    _drain(st)
    return case["case_id"]


def _jobs(st, cid):
    return {j.stage: j for j in st.jobs.list(cid)}


@pytest.mark.e2e
@pytest.mark.skipif(not _have_dotnet_chain(), reason="needs ILSpy + the .NET runtime and a .NET 8 SDK (private or on PATH)")
def test_dotnetapp_rebuilt_in_csharp_passes_every_scenario_without_ai(pipeline, tmp_path):
    from rebuild_controller.jobs import JobState
    from rebuild_controller.outcome import case_outcome
    st = pipeline
    cid = _run_fixture(st, tmp_path, "dotnetapp", "auto")            # Auto picks C# for a .NET program
    jobs = _jobs(st, cid)
    assert jobs["native_rebuild"].state == JobState.COMPLETED, jobs["native_rebuild"].error
    assert "implement_loop" not in jobs and st.cases.get_case(cid)["target_language"] == "csharp"
    r = jobs["native_rebuild"].result
    assert r["built"] and r["verified"] and (r["passed"], r["scenarios"]) == (9, 9) and r["ai_used"] is False
    assert any("netcoreapp8.0 -> net8.0" in n for n in r["deterministic_repairs"])
    assert any("RefSafetyRules" in n for n in r["deterministic_repairs"])
    assert any("case null:" in n for n in r["deterministic_repairs"])          # ILSpy dropped them; the IL proves them
    oc = case_outcome(st, cid)
    assert oc["state"] == "fully_matched" and oc["verification"]["passed"] == 9 and not oc["scaffold_only"]
    cand = st.candidates.get(r["final_candidate"])
    assert cand["target_language"] == "csharp" and cand["meta"]["origin"] == "native_recovered" and cand["meta"]["build"]["launch"]["type"] == "dotnet"
    out = tmp_path / "out"
    assert jobs["deliver"].state == JobState.COMPLETED, jobs["deliver"].error
    assert (out / "source" / "dotnetapp.csproj").is_file() and (out / "dist" / "dotnetapp.dll").is_file()
    assert not (out / "source" / "obj").exists() and not (out / "source" / "bin").exists()
    md = (out / "reports" / "parity-report.md").read_text("utf-8")
    assert "**Full parity:** YES" in md and "Native-language rebuild" in md and "no AI calls were made" in md
    assert not st.cases.list_evidence(cid, kind="ai_attempt")


@pytest.mark.e2e
@pytest.mark.skipif(not _have_java_chain(), reason="needs CFR + a Java runtime and a javac (private JDK 21 or on PATH)")
def test_javacli_rebuilt_in_java_passes_every_scenario_without_ai(pipeline, tmp_path):
    from rebuild_controller.jobs import JobState
    st = pipeline
    cid = _run_fixture(st, tmp_path, "javacli", "java")
    jobs = _jobs(st, cid)
    assert jobs["native_rebuild"].state == JobState.COMPLETED, jobs["native_rebuild"].error
    r = jobs["native_rebuild"].result
    assert r["built"] and r["verified"] and (r["passed"], r["scenarios"]) == (11, 11) and "implement_loop" not in jobs
    cand = st.candidates.get(r["final_candidate"])
    assert cand["meta"]["build"]["launch"]["type"] == "java" and cand["meta"]["build"]["release"] == 17
    jar = tmp_path / "out" / "dist" / "javacli.jar"
    with zipfile.ZipFile(jar) as z:
        assert z.namelist()[0] == "META-INF/MANIFEST.MF" and "Main-Class: dev.rebuild.ledger.Main" in z.read("META-INF/MANIFEST.MF").decode()
    proj = json.loads((tmp_path / "out" / "source" / "rebuild-java.json").read_text("utf-8"))
    assert proj["main_class"] == "dev.rebuild.ledger.Main" and proj["class_file_major"] == 61


@pytest.mark.e2e
@pytest.mark.skipif(not _have_dotnet_chain(), reason="needs ILSpy + the .NET runtime and a .NET 8 SDK (private or on PATH)")
def test_dotnetapp_one_local_repair_fixes_only_what_fails(pipeline, tmp_path, monkeypatch):
    """Without the IL rule the recovered C# fails the EOF scenario (8/9). AI is then scheduled from the recovered source; the (scripted,
    local) model returns only Menus.cs with the null cases back, and the verifier passes 9/9 after that one repair."""
    from rebuild_controller.jobs import JobState
    from test_implement_loop import FakeOpenAI, Hook, Reply, connect
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(nr, "apply_il_switch_rule", lambda *a, **k: [])
    st = pipeline
    seen: dict = {}

    def answer(body):
        user = next(m["content"] for m in body["messages"] if m["role"] == "user")
        system = next((m["content"] for m in body["messages"] if m["role"] == "system"), "")
        seen["user"], seen["system"] = user, system
        cur = json.loads(user.split("CURRENT FILES (scaffold or your previous attempt):\n", 1)[1].split("\n\n", 1)[0])
        menus = cur["NotesApp/Menus.cs"]
        fixed = re.sub(r'(switch \(text\)\s*\{[ \t]*\r?\n)([ \t]*)case "0":', r'\1\2case null:\n\2case "0":', menus)
        assert fixed.count("case null:") == 3
        return Reply(json.dumps({"NotesApp/Menus.cs": fixed}))
    fake = FakeOpenAI([Hook(answer)])
    try:
        connect(st, fake, provider="local", price=None)
        cid = _run_fixture(st, tmp_path, "dotnetapp", "csharp", {"mode": "assisted", "max_attempts": 2, "retry_backoff_s": 0.01})
    finally:
        fake.close()
    jobs = _jobs(st, cid)
    r = jobs["native_rebuild"].result
    assert r["built"] and not r["verified"] and (r["passed"], r["scenarios"]) == (8, 9) and r["ai_scheduled"]
    loop = jobs["implement_loop"]
    assert loop.state == JobState.COMPLETED, loop.error
    assert loop.result["verified"] is True and len(loop.result["attempts"]) == 1 and loop.result["stop_reason"] == "verified"
    assert "ORIGINAL language (C#)" in seen["system"] and "stdin_eof_exits_cleanly" in seen["user"]
    assert len(fake.requests) == 1
    final = st.candidates.get(loop.result["final_candidate"])
    assert final["verification"] == "verified" and final["meta"].get("author") == "model"
    assert jobs["deliver"].state == JobState.COMPLETED, jobs["deliver"].error
