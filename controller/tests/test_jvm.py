"""JVM/Android backend against the real CFR 0.152 (and jadx 1.5.6 for the APK path), on the javacli fixture and a minimal APK.

Tool availability
  The tests look for java + cfr (+ jadx) through the normal discovery order (Settings.tools_dir = $REBUILD_STUDIO_TOOLS, PATH, JAVA_HOME).
  * CFR missing  -> the pinned jar (2 MB, sha256 verified) is downloaded into <tmp>/rebuild-studio-test-tools/cfr unless
                    REBUILD_TEST_NO_DOWNLOAD=1; with no network the real-tool tests skip with the reason.
  * jadx missing -> opt-in only (72 MB): set REBUILD_TEST_DOWNLOAD_JADX=1. Otherwise the jadx tests skip with that reason.
  * no Java at all -> every real-tool test skips ("no Java runtime"); the tool-free tests still run.
Tool-free tests (inspection, AXML/dex parsers, lock entries, installer single-file support, blocked states) always run.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebuild_controller.adapters.contract import Availability
from rebuild_controller.backends import android_info, jvm
from rebuild_controller.backends.jvm import JVMBackend
from rebuild_controller.config import Limits, Settings

REPO = Path(__file__).resolve().parents[2]
FX = REPO / "fixtures" / "javacli"
JAR = FX / "original" / "javacli.jar"
MINAPP = Path(__file__).parent / "data" / "jvm" / "minapp.apk"
LOCK = REPO / "docs" / "dependency-lock.json"
CACHE = Path(tempfile.gettempdir()) / "rebuild-studio-test-tools"
EXE = ".exe" if os.name == "nt" else ""


def _download(url: str, sha: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:       # noqa: S310 - pinned https URL from the lock
        shutil.copyfileobj(r, f)
    if hashlib.sha256(tmp.read_bytes()).hexdigest() != sha:
        tmp.unlink()
        raise RuntimeError(f"sha256 mismatch for {url}")
    tmp.replace(dest)


def _settings_with(*, cfr_dir: Path | None = None) -> Settings:
    base = Settings()
    if cfr_dir is not None:
        base.tools_dir = cfr_dir
    return base


@pytest.fixture(scope="module")
def real(tmp_path_factory) -> JVMBackend:
    """A backend with a working java + CFR (downloading the pinned CFR jar when absent)."""
    b = JVMBackend(Settings())
    java = next(t for t in b.probe().tools if t.name == "java")
    if java.availability not in (Availability.INSTALLED, Availability.USABLE):
        pytest.skip(f"no Java runtime on this host: {java.detail}")
    cfr = next(t for t in b.probe().tools if t.name == "cfr")
    if cfr.availability not in (Availability.INSTALLED, Availability.USABLE):
        if os.environ.get("REBUILD_TEST_NO_DOWNLOAD") == "1":
            pytest.skip("cfr-0.152.jar not found under <tools>/cfr and REBUILD_TEST_NO_DOWNLOAD=1")
        try:
            _download(jvm.CFR_URL, jvm.CFR_SHA256, CACHE / "cfr" / "cfr-0.152.jar")
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"cfr-0.152.jar not installed and download failed ({type(e).__name__}: {e}); set REBUILD_STUDIO_TOOLS to a tools dir with cfr/cfr-0.152.jar")
        s = Settings()
        s.tools_dir = CACHE
        b = JVMBackend(s)
        cfr = next(t for t in b.probe().tools if t.name == "cfr")
        if cfr.availability not in (Availability.INSTALLED, Availability.USABLE):
            pytest.skip(f"downloaded CFR is not usable: {cfr.detail}")
    return b


@pytest.fixture(scope="module")
def with_jadx(real):
    t = next(t for t in real.probe().tools if t.name == "jadx")
    if t.availability in (Availability.INSTALLED, Availability.USABLE):
        yield real
        return
    if os.environ.get("REBUILD_TEST_DOWNLOAD_JADX") != "1":
        pytest.skip("jadx 1.5.6 not installed under <tools>/jadx (72 MB; opt in to a temp download with REBUILD_TEST_DOWNLOAD_JADX=1)")
    jar = CACHE / "jadx" / "lib" / "jadx-1.5.6-all.jar"
    if not jar.is_file():
        zpath = CACHE / "dl" / "jadx-1.5.6.zip"
        try:
            _download("https://github.com/skylot/jadx/releases/download/v1.5.6/jadx-1.5.6.zip", jvm.JADX_SHA256, zpath)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"jadx download failed ({type(e).__name__}: {e})")
        with zipfile.ZipFile(zpath) as z:
            z.extractall(CACHE / "jadx")
    os.environ["REBUILD_JADX_JAR"] = str(jar)
    try:
        t = next(t for t in real.probe().tools if t.name == "jadx")
        if t.availability not in (Availability.INSTALLED, Availability.USABLE):
            pytest.skip(f"downloaded jadx is not usable: {t.detail}")
        yield real
    finally:
        os.environ.pop("REBUILD_JADX_JAR", None)


@pytest.fixture
def studio(cases):
    return SimpleNamespace(cases=cases)


@pytest.fixture
def case(cases, src_out):
    src, out = src_out
    return cases.create_case(name="jvm", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe")


def _scenario_check(launcher: str) -> str:
    p = subprocess.run([sys.executable, str(REPO / "fixtures" / "tools" / "scenario_runner.py"), "check", "--fixture", str(FX), "--launcher", launcher],
                       capture_output=True, text=True, timeout=600)
    last = [ln for ln in p.stdout.splitlines() if ln.startswith("summary:")]
    assert last, p.stdout[-800:] + p.stderr[-800:]
    return last[0] + ("\n" + p.stdout if p.returncode else "")


# ---------------------------------------------------------------------------------------------------------------------
# tool-free: lock, installer, parsers, inspection, blocked states
# ---------------------------------------------------------------------------------------------------------------------
def test_lock_entries_pin_exact_artifacts_with_provenance():
    lock = json.loads(LOCK.read_text(encoding="utf-8"))["tools"]
    packaged = REPO / "controller" / "rebuild_controller" / "data" / "dependency-lock.json"
    assert json.loads(packaged.read_text(encoding="utf-8")) == json.loads(LOCK.read_text(encoding="utf-8"))
    for name in ("temurin-jre", "cfr", "jadx"):
        t = lock[name]
        a = t["artifact"]
        assert t["optional"] is True and re.fullmatch(r"[0-9a-f]{64}", a["sha256"]) and a["url"].startswith("https://") and a["size_bytes"] > 0
        assert a["verify_required"] is False and "2026-10-06" in a["hash_provenance"] and t["license"] and t["install_dir"] == name.replace("temurin-", "")
        assert re.fullmatch(r"[0-9a-f]{64}", t["layout"]["entry_sha256"])
        assert bool(t["layout"].get("requires")) == (name != "temurin-jre")
    assert lock["cfr"]["version"] == jvm.CFR_VERSION and lock["cfr"]["artifact"]["sha256"] == jvm.CFR_SHA256
    assert lock["cfr"]["license"] == "MIT" and lock["cfr"]["layout"]["requires"] == ["temurin-jre"] and lock["cfr"]["artifact"]["format"] == "file"
    assert lock["jadx"]["version"] == jvm.JADX_VERSION and lock["jadx"]["artifact"]["sha256"] == jvm.JADX_SHA256 and lock["jadx"]["license"] == "Apache-2.0"
    assert lock["jadx"]["layout"]["extra_files"]["lib/jadx-1.5.6-all.jar"] == jvm.JADX_JAR_SHA256
    jre = lock["temurin-jre"]
    assert jre["artifact"]["sha256"] == jre["artifact"]["sha256_official"] and jre["artifact"]["name"].endswith("windows_hotspot_17.0.20.1_1.zip")
    assert jre["layout"]["archive_root"] == "jdk-17.0.20.1+1-jre" and jre["install_dir"] == "jre" and jre["version"].startswith(jvm.JRE_VERSION)
    assert "side by side" in jre["role"]


def test_installer_copies_single_file_artifacts_without_unzipping(tmp_path):
    from rebuild_controller.tool_setup import ToolSetup
    blob = b"PK\x03\x04 pretend jar bytes " * 20
    src = tmp_path / "fake-1.0.jar"
    src.write_bytes(blob)
    lock = {"schema_version": 1, "tools": {"fakejar": {
        "version": "1.0", "role": "r", "license": "MIT", "optional": True,
        "artifact": {"name": "fake-1.0.jar", "url": "https://example.invalid/fake-1.0.jar", "size_bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest(),
                     "verify_required": False, "format": "file"},
        "layout": {"archive_root": "", "entry": "fake-1.0.jar", "entry_sha256": hashlib.sha256(blob).hexdigest()}, "install_dir": "fakejar"}}}
    lp = tmp_path / "lock.json"
    lp.write_text(json.dumps(lock))
    ts = ToolSetup(Settings(data_dir=tmp_path / "d", tools_dir=tmp_path / "tools", limits=Limits()), None, lp)
    ts.install_from_file("fakejar", src)
    assert ts.join(30)
    assert ts.status("fakejar")["status"] == "installed", ts.status("fakejar")
    assert (tmp_path / "tools" / "fakejar" / "fake-1.0.jar").read_bytes() == blob


def test_class_header_parser_and_axml_dex_reject_garbage():
    cls = (b"\xca\xfe\xba\xbe" + struct.pack(">HHH", 0, 52, 5) + bytes([1]) + struct.pack(">H", 4) + b"Main" + bytes([7]) + struct.pack(">H", 1)
           + bytes([1]) + struct.pack(">H", 16) + b"java/lang/Object" + bytes([7]) + struct.pack(">H", 3) + struct.pack(">HHH", 0x21, 2, 4))
    h = jvm.read_class_header(cls)
    assert h["class_name"] == "Main" and h["super_class"] == "java.lang.Object" and h["java_version"] == 8
    with pytest.raises(ValueError):
        jvm.read_class_header(b"nope")
    with pytest.raises(android_info.AxmlError):
        android_info.parse_axml(b"<?xml version='1.0'?><manifest/>")
    with pytest.raises(ValueError):
        android_info.dex_header(b"dex\n035\0" + b"\0" * 8)


def test_inspect_jar_without_any_tool():
    info = jvm.inspect_jar(JAR)
    assert info["main_class"] == "dev.rebuild.ledger.Main" and info["class_file_versions"] == {"61": 5} and info["java_versions"] == [17]
    assert info["class_count"] == 5 and info["top_level_class_count"] == 4 and info["flags"]["likely_obfuscated"] is False
    assert [c["name"] for c in info["classes"]["items"]] == ["dev.rebuild.ledger.Entry", "dev.rebuild.ledger.Ledger", "dev.rebuild.ledger.Main",
                                                             "dev.rebuild.ledger.Money"]


def test_inspect_jar_flags_obfuscation_spring_boot_and_nested_jars(tmp_path):
    p = tmp_path / "obf.jar"
    cls = b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 52) + b"\0" * 20
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\nMain-Class: a.a\r\nMulti-Release: true\r\n\r\n")
        for i in range(30):
            z.writestr(f"a/{chr(97 + i % 26)}{i // 26 or ''}.class", cls)
        z.writestr("BOOT-INF/classes/x/App.class", cls)
        z.writestr("BOOT-INF/lib/dep.jar", b"PK")
    info = jvm.inspect_jar(p)
    assert info["flags"]["likely_obfuscated"] is True and info["flags"]["spring_boot"] is True and info["flags"]["multi_release"] is True
    assert info["flags"]["nested_jars"] == 1


def test_inspect_apk_manifest_and_dex_without_tools():
    r = jvm.JVMBackend(Settings()).inspect(MINAPP)
    assert r.ok
    ins = r.data["inspect"]
    m = ins["manifest"]
    assert (m["package"], m["version_code"], m["version_name"], m["min_sdk"], m["target_sdk"]) == ("com.example.hello", 7, "1.2.3", 24, 34)
    assert m["permissions"] == ["android.permission.INTERNET", "android.permission.VIBRATE"] and m["debuggable"] is True and m["allow_backup"] is False
    assert m["launcher_activities"] == ["com.example.hello.MainActivity"] and m["component_counts"] == {"activity": 1, "service": 1, "receiver": 0, "provider": 0}
    assert ins["dex_count"] == 1 and ins["class_count"] == 2 and ins["classes"]["items"] == ["com.example.hello.MainActivity", "com.example.hello.SyncService"]
    assert ins["dex_files"][0]["version"] == "037" and ins["has_resources_arsc"] is True
    assert r.data["support"]["status"] == "partial" and "jadx" in r.data["support"]["blocker"]


def test_inspect_records_evidence_with_module_sha(studio, case, cases):
    r = JVMBackend(Settings()).inspect(JAR, studio=studio, case_id=case["case_id"])
    ev = cases.get_evidence(r.evidence_ids[0])
    body = cases.evidence_body(ev["evidence_id"])
    assert ev["kind"] == "jvm.inspect" and body["module"]["sha256"] == hashlib.sha256(JAR.read_bytes()).hexdigest()
    assert body["support"]["status"] == "supported" and not r.truncated


def test_inspect_class_cap_truncation_is_disclosed(tmp_path, monkeypatch):
    p = tmp_path / "big.jar"
    cls = b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 52) + b"\0" * 20
    with zipfile.ZipFile(p, "w") as z:
        for i in range(jvm.LIST_CAP + 5):
            z.writestr(f"pkg/Cls{i}.class", cls)
    r = JVMBackend(Settings()).inspect(p)
    assert r.truncated and r.data["inspect"]["classes"]["truncated"] and len(r.data["inspect"]["classes"]["items"]) == jvm.LIST_CAP


def test_missing_cfr_blocks_jar_recovery_but_inspect_still_works(tmp_path, monkeypatch):
    monkeypatch.delenv("REBUILD_CFR_JAR", raising=False)
    s = Settings()
    s.tools_dir = tmp_path / "empty-tools"
    b = JVMBackend(s)
    b.find_cfr = lambda: None                      # type: ignore[method-assign]
    info = b.probe()
    cfr = next(t for t in info.tools if t.name == "cfr")
    assert cfr.availability == Availability.MISSING and "cfr" in cfr.next_action.lower() and cfr.pinned == "0.152"
    r = b.decompile(JAR, tmp_path / "out")
    assert not r.ok and "code recovery needs CFR" in r.error and r.data["status"] == "blocked" and r.data["recovered"] is False
    assert b.inspect(JAR).ok                        # evidence needs no tool
    assert not (tmp_path / "out" / "dev").exists()


def test_apk_recovery_without_jadx_states_the_blocker(tmp_path):
    s = Settings()
    s.tools_dir = tmp_path / "t"
    b = JVMBackend(s)
    b.find_jadx = lambda: None                     # type: ignore[method-assign]
    r = b.decompile(MINAPP, tmp_path / "out")
    assert not r.ok and "code recovery needs jadx" in r.error and r.data["status"] == "blocked" and r.data["inspect_available"] is True
    assert r.data["engine"] == "jadx"


def test_decompile_refuses_bad_paths_and_non_jvm_input(tmp_path):
    b = JVMBackend(Settings())
    assert not b.decompile(tmp_path / "nope.jar", tmp_path / "o").ok
    txt = tmp_path / "x.jar"
    txt.write_text("hello")
    assert "not a recoverable" in b.decompile(txt, tmp_path / "o2").error
    nonempty = tmp_path / "full"
    nonempty.mkdir()
    (nonempty / "f").write_text("x")
    assert "not empty" in b.decompile(JAR, nonempty).error
    assert "path policy" in b.decompile(JAR, JAR.parent).error          # output directory must not contain the module


def test_archive_limits_refuse_oversized_jars(tmp_path):
    s = Settings()
    s.limits = Limits(max_archive_entries=3)
    r = JVMBackend(s).decompile(JAR, tmp_path / "o")
    assert not r.ok and "limits" in r.error


# ---------------------------------------------------------------------------------------------------------------------
# real CFR
# ---------------------------------------------------------------------------------------------------------------------
def test_probe_installed_pinned_tools_and_smoke_usable(real):
    info = real.probe()
    t = {x.name: x for x in info.tools}
    assert t["java"].availability == Availability.INSTALLED and re.match(r"\d+", t["java"].version)
    assert t["cfr"].availability == Availability.INSTALLED and t["cfr"].version == "0.152" and t["cfr"].pinned == "0.152"
    assert t["cfr"].license == "MIT" and t["cfr"].integrity == jvm.CFR_SHA256 and t["cfr"].detail == ""
    assert t["jadx"].optional is True and info.backend_id == "jvm" and not info.experimental
    assert {"jvm", "android"} == set(info.profiles) and {o.name for o in info.operations} == {"detect", "inspect", "decompile"}
    sm = real.smoke()
    assert sm.name == "cfr" and sm.availability == Availability.USABLE and "SmokeProbe" in sm.detail


def test_cfr_decompiles_javacli_with_per_class_report_and_evidence(real, studio, case, cases, tmp_path):
    out = tmp_path / "recovered"
    r = real.decompile(JAR, out, studio=studio, case_id=case["case_id"])
    assert r.ok and not r.truncated, r.error
    rep = r.data["recovery_report"]
    assert rep["status"] == "ok" and rep["engine"] == "cfr" and rep["tool"]["version"] == "0.152" and rep["tool"]["path_sha256"] == jvm.CFR_SHA256
    assert (rep["classes_total"], rep["classes_decompiled"], rep["classes_failed"], rep["classes_with_failed_methods"]) == (4, 4, 0, 0)
    assert rep["java_files"] == 4 and rep["failed_markers"] == 0 and rep["equivalence_claimed"] is False and "not asserted to compile" in rep["claims"]
    assert rep["module"]["main_class"] == "dev.rebuild.ledger.Main" and rep["module"]["class_file_versions"] == {"61": 5}
    assert [c["file"] for c in rep["per_class"]["items"]] == [f"dev/rebuild/ledger/{n}.java" for n in ("Entry", "Ledger", "Main", "Money")]
    assert "summary.txt" not in {f["path"] for f in rep["files"]["items"]} and len(rep["output_tree_sha256"]) == 64
    main = (out / "dev" / "rebuild" / "ledger" / "Main.java").read_text(encoding="utf-8")
    assert "package dev.rebuild.ledger;" in main and "usage: javacli <ledger.txt> add" in main and "refusing to touch" in main
    assert 'case "stats"' in main or '"stats"' in main
    assert "record Entry" in (out / "dev/rebuild/ledger/Entry.java").read_text(encoding="utf-8")
    ev = cases.get_evidence(r.evidence_ids[0])
    body = cases.evidence_body(ev["evidence_id"])
    assert ev["kind"] == "jvm.recovery_report" and body["module"]["sha256"] == hashlib.sha256(JAR.read_bytes()).hexdigest() and body["tool"]["version"] == "0.152"


def test_cfr_output_recompiles_and_replays_the_frozen_oracle(real, tmp_path):
    javac = shutil.which("javac") or (str(Path(os.environ["JAVA_HOME"]) / "bin" / f"javac{EXE}") if os.environ.get("JAVA_HOME") else None)
    if not javac or not Path(javac).exists():
        pytest.skip("no javac on PATH/JAVA_HOME: cannot recompile the recovered sources")
    out = tmp_path / "recovered"
    assert real.decompile(JAR, out).ok
    classes = tmp_path / "classes"
    classes.mkdir()
    srcs = [str(p) for p in sorted(out.rglob("*.java"))]
    cp = subprocess.run([javac, "--release", "17", "-encoding", "UTF-8", "-d", str(classes), *srcs], capture_output=True, text=True, timeout=300)
    assert cp.returncode == 0, cp.stderr[-1500:]
    java = next(t for t in real.probe().tools if t.name == "java").path
    summary = _scenario_check(f'"{Path(java).as_posix()}" -cp "{classes.as_posix()}" dev.rebuild.ledger.Main')
    assert summary.startswith("summary: 11 passed, 0 failed, 11 total"), summary


def test_original_jar_replays_its_own_oracle(real):
    java = next(t for t in real.probe().tools if t.name == "java").path
    summary = _scenario_check(f'"{Path(java).as_posix()}" -jar "{JAR.as_posix()}"')
    assert summary.startswith("summary: 11 passed, 0 failed, 11 total"), summary


def test_wrong_recovery_is_rejected_by_the_same_oracle(real, tmp_path):
    """Negative control: a mutated recovered tree (amount parsing broken) must fail scenarios, proving the oracle has teeth."""
    javac = shutil.which("javac")
    if not javac:
        pytest.skip("no javac on PATH")
    out = tmp_path / "recovered"
    assert real.decompile(JAR, out).ok
    money = out / "dev" / "rebuild" / "ledger" / "Money.java"
    text = money.read_text(encoding="utf-8")
    assert "> 2" in text
    money.write_text(text.replace("> 2", "> 3"), encoding="utf-8")        # accept 3 decimals: 1.234 would no longer be rejected
    classes = tmp_path / "classes"
    classes.mkdir()
    cp = subprocess.run([javac, "--release", "17", "-d", str(classes), *[str(p) for p in out.rglob("*.java")]], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr[-800:]
    java = next(t for t in real.probe().tools if t.name == "java").path
    summary = _scenario_check(f'"{Path(java).as_posix()}" -cp "{classes.as_posix()}" dev.rebuild.ledger.Main')
    first = summary.splitlines()[0]
    assert not first.startswith("summary: 11 passed") and "FAIL amount_parsing" in summary, summary[:600]


def test_timeout_is_disclosed_as_partial_with_not_attempted_classes(real, tmp_path, monkeypatch):
    from rebuild_controller.backends import jvm as jvm_mod
    from rebuild_controller.jobs.runner import StageError

    def boom(*a, **k):
        raise StageError("timeout after 1s running java", retry=False)
    monkeypatch.setattr(jvm_mod, "run_bounded", lambda ctx, cmd, **k: boom() if "-jar" in cmd and "--outputdir" in cmd else _orig(ctx, cmd, **k))
    r = real.decompile(JAR, tmp_path / "o")
    assert not r.ok and r.truncated and "no Java sources" in r.error


_orig = jvm.run_bounded


def test_decompile_class_file_and_failed_method_markers(real, tmp_path):
    cls_dir = tmp_path / "c"
    cls_dir.mkdir()
    cls = cls_dir / "SmokeProbe.class"
    import base64
    cls.write_bytes(base64.b64decode(jvm._SMOKE_CLASS_B64))               # noqa: SLF001
    r = real.decompile(cls, tmp_path / "out")
    assert r.ok and r.data["recovery_report"]["format"] == "class" and r.data["recovery_report"]["java_files"] == 1
    assert "return 42;" in (tmp_path / "out" / "SmokeProbe.java").read_text(encoding="utf-8")
    # marker scanner on text CFR emits for undecompilable methods
    scan_dir = tmp_path / "scan" / "p"
    scan_dir.mkdir(parents=True)
    (scan_dir / "A.java").write_text("/*\n * This method has failed to decompile.  When submitting a bug report\n */\nthrow new IllegalStateException(\"Decompilation failed\");\n"
                                     "/* WARNING - Removed try catching itself - possible behaviour change. */\n", encoding="utf-8")
    s = jvm.scan_java_output(tmp_path / "scan", engine="cfr", cap=100)
    assert s["totals"]["failed_markers"] == 2 and s["totals"]["warning_markers"] == 1 and s["per_file"]["p/A.java"] == (2, 1)


def test_scan_truncation_flag(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.java").write_text("x")
    s = jvm.scan_java_output(tmp_path, engine="cfr", cap=3)
    assert s["scan_truncated"] is True and len(s["files"]) == 3


# ---------------------------------------------------------------------------------------------------------------------
# real jadx on the minimal APK
# ---------------------------------------------------------------------------------------------------------------------
def test_jadx_recovers_apk_code_and_decodes_manifest(with_jadx, studio, case, cases, tmp_path):
    out = tmp_path / "apk_out"
    r = with_jadx.decompile(MINAPP, out, studio=studio, case_id=case["case_id"])
    assert r.ok, r.error
    rep = r.data["recovery_report"]
    assert rep["engine"] == "jadx" and rep["tool"]["version"] == "1.5.6" and rep["status"] == "ok" and rep["java_files"] >= 2
    assert rep["resources_decoded"]["manifest_decoded"] is True and rep["android"]["dex_count"] == 1 and rep["classes_total"] == 2
    src = (out / "sources" / "com" / "example" / "hello" / "MainActivity.java").read_text(encoding="utf-8")
    assert "public class MainActivity extends Activity" in src and "Hello from MinApp" in src and "* 31" in src
    assert 'package="com.example.hello"' in (out / "resources" / "AndroidManifest.xml").read_text(encoding="utf-8")
    ev = cases.get_evidence(r.evidence_ids[0])
    assert ev["kind"] == "jvm.recovery_report" and cases.evidence_body(ev["evidence_id"])["tool"]["name"] == "jadx"


def test_doctor_smoke_includes_jvm_and_triage(real, tmp_path):
    from rebuild_controller.adapters.registry import BackendRegistry
    from rebuild_controller.backends import register_backends
    reg = BackendRegistry(tmp_path)
    register_backends(reg, real.settings)
    rep = reg.doctor(smoke=True)
    by = {b["backend_id"]: b for b in rep["backends"]}
    assert by["jvm"]["availability"] == "usable" and by["jvm"]["smoke"]["availability"] == "usable"
    assert by["triage"]["availability"] == "usable" and by["triage"]["experimental"] is True
