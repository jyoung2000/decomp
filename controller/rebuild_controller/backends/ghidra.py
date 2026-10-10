"""Optional full-Ghidra headless backend (second decompiler next to rz-ghidra).

Ghidra is found at ``GHIDRA_INSTALL_DIR`` or at ``<tools>/ghidra`` (the pinned ``ghidra`` entry of docs/dependency-lock.json,
installed from the Tools page); it needs a JDK 21+, preferably the pinned ``temurin-jdk21`` at ``<tools>/jdk21`` (else
JAVA_HOME / PATH). ``op_decompile`` runs ``analyzeHeadless`` with a bundled GhidraScript that decompiles one function;
``op_decompile_all`` decompiles a whole program in one headless run (largest functions first, bounded count and per-function
timeout). Output is untrusted evidence (producer ``ghidra``). Verified on Windows with Ghidra 12.1.4 + Temurin 21.0.12.1
(tests/test_ghidra_live.py, opt-in ``-m live``).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..adapters.contract import Availability, BackendAdapter, BackendInfo, Operation, OperationResult, ToolProbe
from ..config import Settings, get_settings
from ..jobs.runner import kill_tree
from . import native

GHIDRA_PINNED = "12.1.4"
GHIDRA_LICENSE = "Apache-2.0"
GHIDRA_SOURCE = "https://github.com/NationalSecurityAgency/ghidra"
SYMBOL_RE = re.compile(r"^[A-Za-z_.?@][A-Za-z0-9_.?@:\-]{0,255}$")
HEX_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}$")

DECOMPILE_SCRIPT = r"""
// Rebuild Studio: decompile one function and write JSON. Args: <0xADDR|name> <out.json> <timeoutSeconds>
//@category RebuildStudio
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.listing.Function;
import ghidra.program.model.address.Address;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;

public class RebuildDecompile extends GhidraScript {
    private static String q(String s) {
        if (s == null) return "null";
        StringBuilder b = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c)); else b.append(c);
            }
        }
        return b.append('"').toString();
    }

    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        String target = a[0];
        String out = a[1];
        int timeout = Integer.parseInt(a[2]);
        Function f = null;
        if (target.startsWith("0x")) {
            Address addr = toAddr(Long.parseUnsignedLong(target.substring(2), 16));
            f = getFunctionContaining(addr);
        } else {
            for (Function g : currentProgram.getFunctionManager().getFunctions(true)) {
                if (g.getName().equals(target)) { f = g; break; }
            }
        }
        String json;
        if (f == null) {
            json = "{\"ok\":false,\"error\":\"function not found\"}";
        } else {
            DecompInterface di = new DecompInterface();
            di.openProgram(currentProgram);
            DecompileResults r = di.decompileFunction(f, timeout, monitor);
            boolean ok = r.decompileCompleted() && r.getDecompiledFunction() != null;
            json = "{\"ok\":" + ok + ",\"name\":" + q(f.getName()) + ",\"entry\":" + q("0x" + f.getEntryPoint().toString())
                + ",\"signature\":" + q(f.getPrototypeString(false, false))
                + ",\"code\":" + q(ok ? r.getDecompiledFunction().getC() : null)
                + ",\"error\":" + q(ok ? null : r.getErrorMessage()) + "}";
            di.dispose();
        }
        try (OutputStreamWriter w = new OutputStreamWriter(new FileOutputStream(out), StandardCharsets.UTF_8)) {
            w.write(json);
        }
    }
}
"""


DECOMPILE_ALL_SCRIPT = r"""
// Rebuild Studio: decompile up to N functions (largest first) and write JSON lines. Args: <out.jsonl> <max> <timeoutSeconds>
//@category RebuildStudio
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.listing.Function;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

public class RebuildDecompileAll extends GhidraScript {
    private static String q(String s) {
        if (s == null) return "null";
        StringBuilder b = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c)); else b.append(c);
            }
        }
        return b.append('"').toString();
    }

    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        String out = a[0];
        int max = Integer.parseInt(a[1]);
        int timeout = Integer.parseInt(a[2]);
        List<Function> fs = new ArrayList<>();
        for (Function g : currentProgram.getFunctionManager().getFunctions(true)) {
            if (!g.isThunk() && !g.isExternal()) fs.add(g);
        }
        fs.sort((x, y) -> Long.compare(y.getBody().getNumAddresses(), x.getBody().getNumAddresses()));
        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);
        try (OutputStreamWriter w = new OutputStreamWriter(new FileOutputStream(out), StandardCharsets.UTF_8)) {
            w.write("{\"total\":" + fs.size() + "}\n");
            int n = 0;
            for (Function f : fs) {
                if (n++ >= max || monitor.isCancelled()) break;
                DecompileResults r = di.decompileFunction(f, timeout, monitor);
                boolean ok = r.decompileCompleted() && r.getDecompiledFunction() != null;
                w.write("{\"ok\":" + ok + ",\"name\":" + q(f.getName()) + ",\"entry\":" + q("0x" + f.getEntryPoint().toString())
                    + ",\"size\":" + f.getBody().getNumAddresses()
                    + ",\"code\":" + q(ok ? r.getDecompiledFunction().getC() : null)
                    + ",\"error\":" + q(ok ? null : r.getErrorMessage()) + "}\n");
            }
        }
        di.dispose();
    }
}
"""


def _headless(install: Path) -> Path:
    return install / "support" / ("analyzeHeadless.bat" if os.name == "nt" else "analyzeHeadless")


def _ghidra_version(install: Path) -> str | None:
    props = install / "Ghidra" / "application.properties"
    try:
        for line in props.read_text("utf-8", "replace").splitlines():
            if line.startswith("application.version="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


_JAVA_EXE = "java.exe" if os.name == "nt" else "java"
_JAVA_VERSIONS: dict[str, int | None] = {}


def java_major(java: str) -> int | None:
    """Major version of a java executable (``java -version``), cached per path."""
    if java not in _JAVA_VERSIONS:
        try:
            r = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=30)
            m = re.search(r'version "(\d+)(?:\.(\d+))?', (r.stderr or "") + (r.stdout or ""))
            v = int(m.group(1)) if m else None
            _JAVA_VERSIONS[java] = (int(m.group(2)) if v == 1 and m.group(2) else v) if m else None
        except (OSError, subprocess.SubprocessError):
            _JAVA_VERSIONS[java] = None
    return _JAVA_VERSIONS[java]


def _java(tools_dir: Path | None = None) -> str | None:
    """Java for Ghidra: the pinned JDK 21 under <tools>/jdk21 first, then JAVA_HOME, then PATH."""
    cands: list[Path] = []
    if tools_dir is not None:
        cands.append(Path(tools_dir) / "jdk21" / "bin" / _JAVA_EXE)
    jh = os.environ.get("JAVA_HOME")
    if jh:
        cands.append(Path(jh) / "bin" / _JAVA_EXE)
    for p in cands:
        if p.is_file():
            return str(p)
    return shutil.which("java")


class GhidraBackend(BackendAdapter):
    backend_id = "ghidra"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def install_dir(self) -> Path | None:
        env = os.environ.get("GHIDRA_INSTALL_DIR")
        if env:
            return Path(env)
        local = Path(self.settings.tools_dir) / "ghidra"
        return local if _headless(local).is_file() else None

    def java(self) -> str | None:
        return _java(Path(self.settings.tools_dir))

    def tool_probe(self) -> ToolProbe:
        common = dict(license=GHIDRA_LICENSE, source=GHIDRA_SOURCE, pinned=GHIDRA_PINNED, integration="cli",
                      prerequisites=["GHIDRA_INSTALL_DIR or Tools page: Ghidra", "JDK 21+ (Tools page: Temurin JDK 21)"])
        install = self.install_dir()
        if install is None:
            return ToolProbe("ghidra", Availability.MISSING,
                             detail="GHIDRA_INSTALL_DIR is not set and Ghidra is not installed in the tools folder; full Ghidra "
                                    "headless is optional (rizin covers native analysis)",
                             **common)
        headless = _headless(install)
        if not headless.is_file():
            return ToolProbe("ghidra", Availability.MISSING, path=str(install),
                             detail=f"GHIDRA_INSTALL_DIR={install} has no {headless.relative_to(install)}", **common)
        version = _ghidra_version(install)
        java = self.java()
        if not java:
            return ToolProbe("ghidra", Availability.DETECTED, path=str(headless), version=version,
                             detail="Ghidra found but no Java runtime (install Temurin JDK 21 from the Tools page, or set JAVA_HOME)", **common)
        major = java_major(java)
        if major is not None and major < 21:
            return ToolProbe("ghidra", Availability.DETECTED, path=str(headless), version=version,
                             detail=f"Ghidra {version} needs JDK 21+, found Java {major} at {java} (install Temurin JDK 21 from the Tools page)",
                             **common)
        if not version:
            return ToolProbe("ghidra", Availability.DETECTED, path=str(headless),
                             detail="Ghidra found but application.properties has no application.version", **common)
        detail = f"java {java}"
        if version != GHIDRA_PINNED:
            detail += f"; WARNING version {version} differs from pinned {GHIDRA_PINNED}"
        return ToolProbe("ghidra", Availability.INSTALLED, path=str(headless), version=version, detail=detail, **common)

    def probe(self) -> BackendInfo:
        return BackendInfo(
            backend_id=self.backend_id, title="Ghidra headless (optional)", formats=["pe", "elf", "macho"],
            platforms=["linux", "windows", "macos"], profiles=["native_pe", "native_elf"],
            operations=[Operation("decompile", "Decompile one function with Ghidra headless; untrusted output",
                                  {"case_id": "str", "module_id": "str", "function": "str"}, {"decompiled": "object"}),
                        Operation("decompile_all", "Whole-program Ghidra headless run: decompile up to N functions, largest first",
                                  {"case_id": "str", "module_id": "str", "max_functions": "int"}, {"functions": "list"})],
            tools=[self.tool_probe()],
            resources={"ram_mb": 4096, "disk_mb": 1500, "time": "minutes per module (full auto-analysis)"},
            experimental=True,
        )

    # -- execution ----------------------------------------------------------------
    def _run_headless(self, binary: Path, extra: list[str], *, timeout: float, poll=None) -> tuple[int, str]:
        probe = self.tool_probe()
        if probe.availability not in (Availability.INSTALLED, Availability.USABLE, Availability.VERIFIED):
            raise RuntimeError(f"ghidra not available: {probe.detail}")
        install = self.install_dir()
        assert install is not None
        with tempfile.TemporaryDirectory(prefix="rs-ghidra-") as proj:
            argv = [str(_headless(install)), proj, "rs", "-import", str(binary), "-deleteProject", *extra]
            env = dict(os.environ)
            java = self.java()
            if java:   # make Ghidra's launcher pick this JDK (PATH java first, JAVA_HOME second)
                jhome = Path(java).parent.parent
                env["JAVA_HOME"] = str(jhome)
                env["PATH"] = str(jhome / "bin") + os.pathsep + env.get("PATH", "")
            kwargs: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "env": env}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(argv, **kwargs)
            cap = self.settings.limits.max_subprocess_output_bytes
            try:
                out, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                proc.communicate()
                raise RuntimeError(f"ghidra headless timed out after {timeout:.0f}s")
            except BaseException:
                kill_tree(proc)
                raise
            return proc.returncode, out[-cap:].decode("utf-8", "replace")

    def decompile_all_path(self, binary: Path, *, max_functions: int = 200, per_function_timeout: int = 60,
                           timeout: float = 3600) -> dict[str, Any]:
        """Whole-program Ghidra run on a file: auto-analysis, then decompile up to ``max_functions`` (largest first)."""
        import time as _time
        with tempfile.TemporaryDirectory(prefix="rs-ghidra-all-") as sd:
            script_dir = Path(sd)
            (script_dir / "RebuildDecompileAll.java").write_text(DECOMPILE_ALL_SCRIPT, "utf-8")
            out = script_dir / "out.jsonl"
            t0 = _time.monotonic()
            rc, log = self._run_headless(Path(binary), ["-scriptPath", str(script_dir), "-postScript", "RebuildDecompileAll.java",
                                                        str(out), str(int(max_functions)), str(int(per_function_timeout))], timeout=timeout)
            secs = round(_time.monotonic() - t0, 1)
            if not out.is_file():
                raise RuntimeError(f"ghidra produced no output (exit {rc}): {log[-600:]}")
            lines = [json.loads(x) for x in out.read_text("utf-8").splitlines() if x.strip()]
        head, funcs = (lines[0], lines[1:]) if lines and "total" in lines[0] else ({"total": None}, lines)
        return {"total_functions": head.get("total"), "functions": funcs, "decompiled": sum(1 for f in funcs if f.get("ok")),
                "failed": sum(1 for f in funcs if not f.get("ok")), "seconds": secs, "exit_code": rc,
                "ghidra_version": self.tool_probe().version}

    def op_decompile_all(self, ctx: Any, *, case_id: str, module_id: str, max_functions: int = 200,
                         per_function_timeout: int = 60, timeout: float = 3600) -> OperationResult:
        try:
            cases = native.cases_of(ctx)
            module, path = native.module_file(cases, case_id, module_id)
            res = self.decompile_all_path(path, max_functions=max(1, min(int(max_functions), 5000)),
                                          per_function_timeout=max(5, min(int(per_function_timeout), 600)), timeout=timeout)
            cap = self.settings.limits.max_subprocess_output_bytes
            items = [{**{k: f.get(k) for k in ("ok", "name", "entry", "size", "error")},
                      "decompiled": native.untrusted_text(f.get("code"), cap)} for f in res["functions"]]
            body = {**{k: v for k, v in res.items() if k != "functions"}, "functions": items, "decompiler": f"ghidra-{res['ghidra_version']}"}
            inputs = {"op": "decompile_all", "module_sha256": module["sha256"], "ghidra_version": res["ghidra_version"],
                      "max_functions": max_functions}
            ev = native.store_evidence(cases, case_id, module_id, "native.decompile_all.ghidra",
                                       f"Ghidra whole-program decompile ({res['decompiled']} functions)", body, inputs,
                                       untrusted=True, producer="ghidra")
            return OperationResult(ok=True, data=body, evidence_ids=[ev["evidence_id"]])
        except Exception as e:
            return OperationResult(ok=False, error=f"{type(e).__name__}: {e}")

    def smoke(self) -> ToolProbe:
        probe = self.tool_probe()
        if probe.availability != Availability.INSTALLED:
            return probe
        from .rizin_worker import builtin_sample_elf
        with tempfile.TemporaryDirectory(prefix="rs-ghidra-smoke-") as d:
            sample = Path(d) / "smoke.elf"
            sample.write_bytes(builtin_sample_elf())
            try:
                rc, out = self._run_headless(sample, ["-noanalysis"], timeout=600)
            except Exception as e:
                probe.detail += f"; smoke failed: {e}"
                return probe
        if rc == 0:
            probe.availability = Availability.USABLE
            probe.detail += "; smoke: headless import of built-in ELF succeeded"
        else:
            probe.detail += f"; smoke: analyzeHeadless exit {rc}: {out[-400:]}"
        return probe

    def op_decompile(self, ctx: Any, *, case_id: str, module_id: str, function: str | int,
                     timeout: float = 1800) -> OperationResult:
        if isinstance(function, int) and not isinstance(function, bool):
            target = f"0x{function:x}"
        elif isinstance(function, str) and (HEX_RE.fullmatch(function) or SYMBOL_RE.fullmatch(function)):
            target = function.lower() if HEX_RE.fullmatch(function) else function
        else:
            return OperationResult(ok=False, error=f"invalid target: {str(function)[:80]!r}")
        try:
            cases = native.cases_of(ctx)
            module, path = native.module_file(cases, case_id, module_id)  # same path policy as rizin
            version = self.tool_probe().version
            inputs = {"op": "decompile", "args": {"target": target}, "module_sha256": module["sha256"],
                      "ghidra_version": version, "analysis": "analyzeHeadless default auto-analysis"}
            hit = native.cached_evidence(cases, case_id, module_id, "native.decompile.ghidra", inputs)
            if hit is not None and hit.get("blob_sha"):
                return OperationResult(ok=True, data={**cases.blobs.get_json(hit["blob_sha"]), "cached": True},
                                       evidence_ids=[hit["evidence_id"]])
            with tempfile.TemporaryDirectory(prefix="rs-ghidra-script-") as sd:
                script_dir = Path(sd)
                (script_dir / "RebuildDecompile.java").write_text(DECOMPILE_SCRIPT, "utf-8")
                out_json = script_dir / "out.json"
                rc, log = self._run_headless(path, ["-scriptPath", str(script_dir), "-postScript", "RebuildDecompile.java",
                                                    target, str(out_json), "300"], timeout=timeout)
                if not out_json.is_file():
                    return OperationResult(ok=False, error=f"ghidra produced no output (exit {rc}): {log[-600:]}")
                result = json.loads(out_json.read_text("utf-8"))
            if not result.get("ok"):
                return OperationResult(ok=False, error=f"ghidra decompile failed: {result.get('error')}")
            env = native.untrusted_text(result.get("code"), self.settings.limits.max_subprocess_output_bytes)
            body = {"function": result.get("name"), "addr": result.get("entry"), "signature": result.get("signature"),
                    "decompiler": f"ghidra-{version}", "is_real_decompiler": True, "decompiled": env}
            ev = native.store_evidence(cases, case_id, module_id, "native.decompile.ghidra", f"Ghidra decompile {target}",
                                       body, inputs, untrusted=True, truncated=env["truncated"], producer="ghidra",
                                       extra_meta={"decompiler": body["decompiler"]})
            return OperationResult(ok=True, data=body, evidence_ids=[ev["evidence_id"]], truncated=env["truncated"])
        except Exception as e:
            return OperationResult(ok=False, error=f"{type(e).__name__}: {e}")
