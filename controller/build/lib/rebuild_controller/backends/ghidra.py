"""Optional full-Ghidra headless backend.

Probe-only unless ``GHIDRA_INSTALL_DIR`` points at a Ghidra installation. When it does, ``op_decompile`` runs
``analyzeHeadless`` with a small bundled GhidraScript (Java) that decompiles one function and writes JSON. Output is stored
as untrusted evidence (producer ``ghidra``). This backend is marked experimental: it has not been exercised on this host
because Ghidra is not installed here.
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


def _java() -> str | None:
    jh = os.environ.get("JAVA_HOME")
    if jh:
        p = Path(jh) / "bin" / ("java.exe" if os.name == "nt" else "java")
        if p.is_file():
            return str(p)
    return shutil.which("java")


class GhidraBackend(BackendAdapter):
    backend_id = "ghidra"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def install_dir(self) -> Path | None:
        env = os.environ.get("GHIDRA_INSTALL_DIR")
        return Path(env) if env else None

    def tool_probe(self) -> ToolProbe:
        common = dict(license=GHIDRA_LICENSE, source=GHIDRA_SOURCE, pinned=GHIDRA_PINNED, integration="cli",
                      prerequisites=["GHIDRA_INSTALL_DIR", "JDK 21+"])
        install = self.install_dir()
        if install is None:
            return ToolProbe("ghidra", Availability.MISSING,
                             detail="GHIDRA_INSTALL_DIR is not set; full Ghidra headless is optional (rizin covers native analysis)",
                             **common)
        headless = _headless(install)
        if not headless.is_file():
            return ToolProbe("ghidra", Availability.MISSING, path=str(install),
                             detail=f"GHIDRA_INSTALL_DIR={install} has no {headless.relative_to(install)}", **common)
        version = _ghidra_version(install)
        java = _java()
        if not java:
            return ToolProbe("ghidra", Availability.DETECTED, path=str(headless), version=version,
                             detail="Ghidra found but no Java runtime (set JAVA_HOME or put java on PATH)", **common)
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
                                  {"case_id": "str", "module_id": "str", "function": "str"}, {"decompiled": "object"})],
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
            kwargs: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
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
