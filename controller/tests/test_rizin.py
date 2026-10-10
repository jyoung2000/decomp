"""Native backend tests against real rizin (no mocks).

Samples: tests/data/sample_pe.exe (mingw) and tests/data/sample_elf (gcc), built from tests/data/sample.c by
tests/data/build_samples.sh. Committed so the tests do not need cross compilers; rebuilt here if missing.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from rebuild_controller.adapters.contract import Availability
from rebuild_controller.backends import native
from rebuild_controller.backends.ghidra import GhidraBackend
from rebuild_controller.backends.rizin_worker import (RIZIN_STATIC_SHA256, RizinBackend, builtin_sample_elf,
                                                      find_rizin, parse_json)
from rebuild_controller.config import Limits, Settings
from rebuild_controller.ids import sha256_file, stable_json_hash

DATA = Path(__file__).parent / "data"
SAMPLES = {"pe": DATA / "sample_pe.exe", "elf": DATA / "sample_elf"}

pytestmark = pytest.mark.skipif(find_rizin(Settings()) is None, reason="rizin not installed on this host")


def _ensure_samples() -> None:
    if all(p.is_file() for p in SAMPLES.values()):
        return
    if not (shutil.which("x86_64-w64-mingw32-gcc") and shutil.which("gcc")):
        pytest.skip("samples missing and no compilers to build them")
    subprocess.run(["sh", str(DATA / "build_samples.sh")], check=True)


@pytest.fixture(scope="module")
def sample_root(tmp_path_factory) -> Path:
    """One source root shared by all tests so the module-scoped backend reuses one rizin session per sample."""
    _ensure_samples()
    root = tmp_path_factory.mktemp("native-src")
    for p in SAMPLES.values():
        shutil.copy2(p, root / p.name)
    return root


@pytest.fixture(scope="module")
def backend() -> RizinBackend:
    b = RizinBackend(Settings(data_dir=Path("/nonexistent-unused"), limits=Limits()))
    yield b
    b.close()
    native.close_default_backends()


@pytest.fixture
def case(cases, sample_root, tmp_path):
    c = cases.create_case(name="native", source_root=str(sample_root), output_root=str(tmp_path / "out"),
                          target_language="rust", output_type="cli")
    mods = {}
    for kind, p in SAMPLES.items():
        q = sample_root / p.name
        mods[kind] = cases.add_module(c["case_id"], p.name, sha256_file(q), q.stat().st_size, kind,
                                      "native_pe" if kind == "pe" else "native_elf")
    return c["case_id"], mods


# ------------------------------------------------------------------------------------------- probe / smoke
def test_probe_reports_rizin_and_rz_ghidra(backend):
    info = backend.probe()
    rz = info.tools[0]
    assert rz.name == "rizin" and rz.availability == Availability.INSTALLED
    assert rz.version == "0.9.1" and rz.pinned == "v0.9.1"
    assert rz.license == "LGPL-3.0" and rz.source == "https://github.com/rizinorg/rizin"
    tool = backend.tool()
    assert rz.integrity == (RIZIN_STATIC_SHA256 if tool.static else "")
    opt = info.resources["optional_tools"]
    assert opt[0]["name"] == "rz-ghidra" and opt[0]["availability"] in ("missing", "detected", "installed")
    assert opt[0]["pinned"] == "v0.9.0"
    assert info.availability == Availability.INSTALLED   # optional plugin never drags the backend to "missing"
    ops = {o.name: o for o in info.operations}
    assert ops["admin_raw"].dangerous and not ops["decompile"].dangerous


def test_smoke_is_usable(backend):
    p = backend.smoke()
    assert p.availability == Availability.USABLE, p.detail
    assert "functions" in p.detail
    assert builtin_sample_elf()[:4] == b"\x7fELF"


# ------------------------------------------------------------------------------------------- typed extraction
def test_info_imports_exports_sections_pe(backend, cases, case):
    case_id, mods = case
    r = backend.op_info(cases, case_id=case_id, module_id=mods["pe"])
    assert r.ok, r.error
    assert r.data["info"]["bintype"] == "pe" and r.data["info"]["bits"] == 64
    assert "pe64" in r.data["headers"]
    imp = backend.op_imports(cases, case_id=case_id, module_id=mods["pe"])
    names = {i["name"] for i in imp.data["imports"]}
    assert {"puts", "printf", "strlen"} <= names or {"puts", "strlen"} <= names, names
    exp = backend.op_exports(cases, case_id=case_id, module_id=mods["pe"])
    assert {"checksum", "add_numbers"} <= {e["name"] for e in exp.data["exports"]}
    sec = backend.op_sections(cases, case_id=case_id, module_id=mods["pe"])
    assert ".text" in {s["name"] for s in sec.data["sections"]}
    ent = backend.op_entrypoints(cases, case_id=case_id, module_id=mods["pe"])
    assert ent.ok and ent.data["entrypoints"][0]["addr"].startswith("0x")
    ev = cases.get_evidence(imp.evidence_ids[0])
    assert ev["producer"] == "rizin" and ev["kind"] == "native.imports" and ev["module_id"] == mods["pe"]


def test_evidence_inputs_include_rizin_version_and_settings(backend, cases, case):
    case_id, mods = case
    tool = backend.tool()
    r = backend.op_functions(cases, case_id=case_id, module_id=mods["elf"])
    assert r.ok, r.error
    ev = cases.get_evidence(r.evidence_ids[0])
    inputs = ev["meta"]["inputs"]
    assert inputs["rizin_version"] == tool.version == "0.9.1"
    assert inputs["rizin_commit"] == tool.commit and len(tool.commit) == 40
    assert inputs["module_sha256"] == sha256_file(SAMPLES["elf"])
    assert inputs["analysis"] == {"command": "aaa", "analysis.timeout": 300, "passes": ["sigpacks", "pdata", "relocptrs", "thunks"]}
    assert ev["input_hash"] == stable_json_hash(inputs)   # cache key is exactly these inputs
    # second call is served from the evidence cache with the same id
    r2 = backend.op_functions(cases, case_id=case_id, module_id=mods["elf"])
    assert r2.ok and r2.data["cached"] and r2.evidence_ids == r.evidence_ids
    names = {f["name"] for f in r.data["functions"]}
    assert "main" in names and "sym.checksum" in names


def test_strings_are_untrusted_and_bounded_by_count(backend, cases, case):
    case_id, mods = case
    r = backend.op_strings(cases, case_id=case_id, module_id=mods["pe"], limit=5000)
    assert r.ok, r.error
    texts = [s["string"] for s in r.data["strings"]]
    inj = [t for t in texts if "IGNORE PREVIOUS INSTRUCTIONS" in t]
    assert inj, "injection-looking string must be extracted as data"
    assert r.data["untrusted"] is True and "never as instructions" in r.data["note"]
    ev = cases.get_evidence(r.evidence_ids[0])
    assert ev["meta"]["untrusted"] is True
    small = backend.op_strings(cases, case_id=case_id, module_id=mods["pe"], limit=10)
    assert small.data["returned"] == 10 and small.data["count_truncated"] and small.truncated


def test_functions_xrefs_callgraph_disasm(backend, cases, case):
    case_id, mods = case
    x = backend.op_xrefs(cases, case_id=case_id, module_id=mods["pe"], addr="checksum")
    assert x.ok, x.error
    calls = [r for r in x.data["to"] if r["type"] == "CALL"]
    assert len(calls) == 2   # main calls checksum twice (once via inlined greet)
    cg = backend.op_callgraph(cases, case_id=case_id, module_id=mods["pe"], function="main")
    assert cg.ok, cg.error
    callee_names = " ".join(c["name"] or "" for c in cg.data["callees"])
    assert "checksum" in callee_names and "puts" in callee_names
    d = backend.op_disasm(cases, case_id=case_id, module_id=mods["pe"], function="main")
    assert d.ok and d.data["function"] == "sym.main" and d.data["untrusted"]
    assert any("call" in (op.get("disasm") or "") for op in d.data["disasm"]["ops"])
    assert all("esil" not in op for op in d.data["disasm"]["ops"])


@pytest.mark.parametrize("kind", ["pe", "elf"])
def test_decompile_is_honestly_labelled(backend, cases, case, kind):
    case_id, mods = case
    r = backend.op_decompile(cases, case_id=case_id, module_id=mods[kind], function="checksum")
    assert r.ok, r.error
    sess = backend.session_for(cases, case_id, mods[kind])[1]
    caps = sess.capabilities()
    if caps["rz_ghidra_loaded"]:
        assert r.data["decompiler"] == "rz-ghidra(pdg)" and r.data["is_real_decompiler"] is True
        assert "checksum" in r.data["decompiled"]["text"]
    else:
        assert r.data["decompiler"].endswith("(pseudo)") and r.data["is_real_decompiler"] is False
        assert "0x1234" in r.data["decompiled"]["text"]
    assert r.data["decompiled"]["untrusted"] is True
    ev = cases.get_evidence(r.evidence_ids[0])
    assert ev["meta"]["untrusted"] and ev["meta"]["decompiler"] == r.data["decompiler"]
    assert ev["meta"]["inputs"]["rizin_version"] == "0.9.1"
    assert "decompiler" in ev["meta"]["inputs"]


def test_function_briefing_packet(backend, cases, case):
    case_id, mods = case
    r = backend.op_function_briefing(cases, case_id=case_id, module_id=mods["pe"], function="main")
    assert r.ok, r.error
    b = r.data
    assert b["function"]["name"] == "sym.main" and b["function"]["size"] > 0
    assert "main" in b["function"]["signature"]
    callee_names = " ".join(c["name"] or "" for c in b["callees"])
    assert "checksum" in callee_names
    assert {"puts", "strlen"} <= set(b["imports_used"]), b["imports_used"]
    assert any(c["name"] and "tmainCRTStartup" in c["name"] for c in b["callers"])
    strs = [s["string"] for s in b["strings"]["items"]]
    assert "RebuildStudio sample v1" in strs and any("IGNORE PREVIOUS" in s for s in strs)
    assert b["strings"]["untrusted"] and b["decompiled"]["untrusted"]
    assert b["decompiled"]["decompiler"]
    assert b["evidence_ids"] and all(cases.get_evidence(e) for e in b["evidence_ids"])
    assert cases.get_evidence(b["evidence_ids"][0])["kind"] == "native.briefing"
    # truncation is flagged, never silent
    t = backend.op_function_briefing(cases, case_id=case_id, module_id=mods["pe"], function="main", max_decompiled_bytes=256)
    assert t.ok and t.data["decompiled"]["truncated"] and t.data["any_truncated"] and t.truncated
    assert len(t.data["decompiled"]["text"].encode()) <= 256


def test_function_briefing_entry_point_used_by_services(cases, case):
    case_id, mods = case
    out = native.function_briefing(cases, case_id, mods["elf"], "add_numbers")
    assert out["ok"], out["error"]
    assert out["function"]["name"] == "sym.add_numbers" and out["evidence_ids"]
    bad = native.function_briefing(cases, case_id, mods["elf"], "add_numbers; !id")
    assert not bad["ok"] and "invalid" in bad["error"]


# ------------------------------------------------------------------------------------------- safety
@pytest.mark.parametrize("target", ["main; !id", "`id`", "$(id)", "a|b", "main @ 0", "x>/tmp/pwn", "sym.main\n!id", "main\n",
                                    "' or 1", "*", ""])
def test_symbols_with_metacharacters_are_rejected_before_rizin(backend, cases, case, target):
    case_id, mods = case
    sess = backend.session_for(cases, case_id, mods["elf"])[1]
    before = sess.commands_run
    for op in (backend.op_disasm, backend.op_decompile, backend.op_callgraph):
        r = op(cases, case_id=case_id, module_id=mods["elf"], function=target)
        assert not r.ok and "invalid" in r.error
    r = backend.op_xrefs(cases, case_id=case_id, module_id=mods["elf"], addr=target)
    assert not r.ok and "invalid" in r.error
    assert sess.commands_run == before   # nothing reached the rizin process


def test_invalid_addresses_are_rejected(backend, cases, case):
    case_id, mods = case
    for addr in ("0xdeadbeef", 0x10, 2 ** 70, -5, "0x" + "f" * 16):
        r = backend.op_xrefs(cases, case_id=case_id, module_id=mods["pe"], addr=addr)
        assert not r.ok and "invalid target" in r.error, (addr, r.error)
    r = backend.op_disasm(cases, case_id=case_id, module_id=mods["pe"], function="0xdeadbeef")
    assert not r.ok and "not inside any mapped" in r.error
    r = backend.op_disasm(cases, case_id=case_id, module_id=mods["pe"], function="no_such_symbol")
    assert not r.ok and "unknown symbol" in r.error
    # mapped but not code inside a function
    sess = backend.session_for(cases, case_id, mods["pe"])[1]
    data_sec = next(s for s in sess.sections()[0] if s["name"] == ".rdata")
    r = backend.op_disasm(cases, case_id=case_id, module_id=mods["pe"], function=data_sec["vaddr"])
    assert not r.ok and "no analysed function" in r.error


def test_admin_raw_is_not_reachable_through_call(backend, cases, case):
    case_id, mods = case
    r = backend.call("admin_raw", cases, case_id=case_id, module_id=mods["elf"], cmd="?V")
    assert not r.ok and "no operation" in r.error
    r = backend.admin_raw(cases, case_id=case_id, module_id=mods["elf"], cmd="?V")
    assert not r.ok and "dangerous" in r.error
    r = backend.admin_raw(cases, case_id=case_id, module_id=mods["elf"], cmd="echo admin-echo", dangerous_ok=True)
    assert r.ok and "admin-echo" in r.data["text"] and r.data["dangerous"]


def test_module_changed_on_disk_is_detected(cases, tmp_path):
    src = tmp_path / "src2"; src.mkdir()
    shutil.copy2(SAMPLES["elf"], src / "a.elf")
    c = cases.create_case(name="x", source_root=str(src), output_root=str(tmp_path / "o2"), target_language="rust", output_type="cli")
    mid = cases.add_module(c["case_id"], "a.elf", "0" * 64, 1, "elf", "native_elf")
    b = RizinBackend(cases.settings)
    try:
        r = b.op_info(cases, case_id=c["case_id"], module_id=mid)
        assert not r.ok and "changed on disk" in r.error
    finally:
        b.close()


# ------------------------------------------------------------------------------------------- worker lifecycle
def test_concurrent_callers_are_serialized(backend, cases, case):
    case_id, mods = case
    targets = ["main", "checksum", "add_numbers", "sym.__tmainCRTStartup"]
    sess = backend.session_for(cases, case_id, mods["pe"])[1]
    sess.analyze()
    expected = {t: sess.resolve(t, need_function=True)[1]["name"] for t in targets}
    errors: list[str] = []
    pids: set[int] = set()
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        try:
            barrier.wait()
            for j in range(6):
                t = targets[(i + j) % len(targets)]
                if j % 3 == 0:
                    r = backend.op_disasm(cases, case_id=case_id, module_id=mods["pe"], function=t)
                    if not r.ok or r.data["function"] != expected[t]:
                        errors.append(f"disasm {t}: {r.error or r.data['function']}")
                elif j % 3 == 1:
                    data, _ = sess.disasm(sess.resolve(t, need_function=True)[1]["offset"])
                    if data["name"] != expected[t]:
                        errors.append(f"raw disasm {t}: got {data['name']}")
                else:
                    info = sess.function_info(sess.resolve(t, need_function=True)[0])
                    if info["name"] != expected[t]:
                        errors.append(f"afij {t}: got {info['name']}")
                if sess.pid:
                    pids.add(sess.pid)
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not errors, errors[:5]
    assert len(pids) == 1, f"one rizin process must serve the module, saw {pids}"


def _gone(pid: int, wait: float = 3.0) -> bool:
    """Portable 'process has exited'. os.kill(pid, 0) is a liveness probe on POSIX but *terminates* the process on Windows."""
    deadline = time.time() + wait
    while True:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = ctypes.c_void_p
            h = k32.OpenProcess(0x00100000 | 0x1000, False, pid)   # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
            alive = bool(h) and k32.WaitForSingleObject(ctypes.c_void_p(h), 0) == 0x102
            if h:
                k32.CloseHandle(ctypes.c_void_p(h))
        else:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
        if not alive:
            return True
        if time.time() > deadline:
            return False
        time.sleep(0.1)


def _hard_kill(pid: int) -> None:
    os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))   # SIGTERM -> TerminateProcess on Windows


def test_crashed_rizin_is_recreated(backend, cases, case):
    case_id, mods = case
    r = backend.op_functions(cases, case_id=case_id, module_id=mods["elf"])
    assert r.ok
    sess = backend.session_for(cases, case_id, mods["elf"])[1]
    sess.analyze()
    pid, gen = sess.pid, sess.generation
    assert pid
    _hard_kill(pid)
    time.sleep(0.2)
    d = backend.op_disasm(cases, case_id=case_id, module_id=mods["elf"], function="add_numbers")
    assert d.ok, d.error
    assert d.data["function"] == "sym.add_numbers"
    assert sess.pid and sess.pid != pid and sess.generation == gen + 1


def test_output_is_bounded(cases, case):
    case_id, mods = case
    s = Settings(data_dir=cases.settings.data_dir, limits=Limits(max_subprocess_output_bytes=8192))
    b = RizinBackend(s)
    try:
        r = b.op_strings(cases, case_id=case_id, module_id=mods["pe"], limit=20000)
        assert r.ok, r.error
        assert r.truncated and r.data["output_truncated"]
        assert 0 < r.data["returned"] < 1389          # salvaged complete items before the cap
        assert cases.get_evidence(r.evidence_ids[0])["meta"]["truncated"] is True
        sess = b.session_for(cases, case_id, mods["pe"])[1]
        text, trunc = sess.admin_raw("izz")
        assert trunc and len(text.encode()) <= 8192
        # the protocol stays in sync after a truncated reply
        info, _ = sess.info()
        assert info["bintype"] == "pe"
    finally:
        b.close()


def test_idle_timeout_closes_process(cases, case):
    case_id, mods = case
    b = RizinBackend(cases.settings, idle_timeout=0.6)
    try:
        assert b.op_info(cases, case_id=case_id, module_id=mods["elf"]).ok
        sess = b.session_for(cases, case_id, mods["elf"])[1]
        pid = sess.pid
        assert pid
        deadline = time.time() + 10
        while sess.pid and time.time() < deadline:
            time.sleep(0.2)
        assert sess.pid is None
        assert _gone(pid)
        # transparently reopened on next use
        assert b.op_sections(cases, case_id=case_id, module_id=mods["elf"]).ok and sess.pid
    finally:
        b.close()


def test_cancellation_kills_process_tree(cases, case):
    case_id, mods = case
    b = RizinBackend(cases.settings)

    class Cancelled(Exception):
        pass

    calls = []

    def poll():
        calls.append(time.time())
        raise Cancelled("job cancelled")

    try:
        sess = b.session_for(cases, case_id, mods["pe"])[1]
        sess.info()
        pid = sess.pid
        with pytest.raises(Cancelled):
            sess.analyze(poll)          # PE aaa takes seconds; first poll cancels it
        assert calls
        time.sleep(0.2)
        assert _gone(pid)
        assert sess.pid is None
        assert sess.info()[0]["bintype"] == "pe"   # recreated on demand
    finally:
        b.close()


def test_parse_json_salvages_truncated_lists():
    items, trunc = parse_json('[{"a":1},{"b":2},{"c":', True)
    assert items == [{"a": 1}, {"b": 2}] and trunc


# ------------------------------------------------------------------------------------------- registry / ghidra
def test_backend_registers(settings):
    from rebuild_controller.adapters.registry import BackendRegistry
    from rebuild_controller.backends import register_backends
    reg = BackendRegistry()
    register_backends(reg, settings)
    rz = reg.get("rizin")
    assert isinstance(rz, RizinBackend)
    assert reg.info("rizin").availability == Availability.INSTALLED
    gh = reg.get("ghidra")
    assert isinstance(gh, GhidraBackend)
    rz.close()


def test_ghidra_reports_missing_cleanly(monkeypatch, tmp_path, settings, cases, case):
    monkeypatch.delenv("GHIDRA_INSTALL_DIR", raising=False)
    monkeypatch.setattr(settings, "tools_dir", tmp_path / "no-tools")   # no <tools>/ghidra either
    g = GhidraBackend(settings)
    p = g.probe()
    assert p.availability == Availability.MISSING and "GHIDRA_INSTALL_DIR" in p.tools[0].detail
    assert g.smoke().availability == Availability.MISSING
    monkeypatch.setenv("GHIDRA_INSTALL_DIR", str(tmp_path / "nope"))
    assert g.probe().tools[0].availability == Availability.MISSING
    case_id, mods = case
    r = g.op_decompile(cases, case_id=case_id, module_id=mods["elf"], function="main")
    assert not r.ok and "ghidra not available" in r.error
    r = g.op_decompile(cases, case_id=case_id, module_id=mods["elf"], function="main;rm")
    assert not r.ok and "invalid target" in r.error
