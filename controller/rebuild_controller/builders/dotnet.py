"""C# builder (R4 'native-language rebuild'): ``dotnet build`` of a candidate's .csproj with the private .NET SDK.

The SDK comes from Tools (``<tools>/dotnet-sdk``, pinned official Microsoft zip; see dependency-lock.json) and falls back to a
.NET 8+ SDK on PATH. MSBuild evaluates targets from the candidate's project file (recovered from the original, possibly edited
by a model), so the build runs in the sandbox like cargo: low integrity on Windows, scrubbed environment, the SDK's first-run
and NuGet caches redirected into the case folder, no build servers or reused MSBuild nodes left behind.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .. import livelog, sandbox
from ..jobs.runner import StageContext, StageError

TOOL_NAME = "dotnet-sdk"                       # key in dependency-lock.json / the Tools page
TOOL_TITLE = ".NET SDK (private)"              # tool_setup.FRIENDLY["dotnet-sdk"][0]
MIN_SDK_MAJOR = 8
BLOCKER = (f"open Tools and install '{TOOL_TITLE}' (needed to rebuild .NET programs in C#), then resume; "
           f"or install a .NET {MIN_SDK_MAJOR}+ SDK so 'dotnet' is on PATH")
OUT_DIR = Path("bin") / "rebuild-out"
_EXE = ".exe" if os.name == "nt" else ""
_SYS_CACHE: dict[str, dict[str, str] | None] = {}


def _sdk_versions(root: Path) -> list[str]:
    try:
        return sorted((d.name for d in (root / "sdk").iterdir() if d.is_dir() and re.match(r"^\d+\.\d+\.\d+", d.name)),
                      key=lambda v: [int(x) for x in re.findall(r"\d+", v)[:3]], reverse=True)
    except OSError:
        return []


def _major(v: str) -> int:
    m = re.match(r"^(\d+)", v)
    return int(m.group(1)) if m else 0


def private_sdk(tools_dir: Path | str | None = None) -> dict[str, str] | None:
    """The SDK installed by Tools setup: <tools>/dotnet-sdk/dotnet.exe with sdk/<version>/. None when it is not there."""
    if tools_dir is None:
        from ..config import get_settings
        tools_dir = get_settings().tools_dir
    root = Path(tools_dir) / "dotnet-sdk"
    exe = root / f"dotnet{_EXE}"
    vers = [v for v in _sdk_versions(root) if _major(v) >= MIN_SDK_MAJOR]
    if not exe.is_file() or not vers:
        return None
    return {"dotnet": str(exe), "root": str(root), "sdk": vers[0], "where": "private SDK"}


def system_sdk() -> dict[str, str] | None:
    """A .NET 8+ SDK reachable through 'dotnet' on PATH (cached per process)."""
    dn = shutil.which("dotnet")
    if not dn:
        return None
    if dn in _SYS_CACHE:
        return _SYS_CACHE[dn]
    found = None
    try:
        r = subprocess.run([dn, "--list-sdks"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                           env={**os.environ, "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"})
        vers = []
        for ln in (r.stdout or "").splitlines():
            m = re.match(r"^(\d+\.\d+\.\d+\S*)\s+\[(.+)\]$", ln.strip())
            if m and _major(m.group(1)) >= MIN_SDK_MAJOR:
                vers.append(m.group(1))
        if vers:
            vers.sort(key=lambda v: [int(x) for x in re.findall(r"\d+", v)[:3]], reverse=True)
            root = Path(dn).resolve().parent
            found = {"dotnet": str(Path(dn).resolve()), "root": str(root), "sdk": vers[0], "where": "SDK on PATH"}
    except (OSError, subprocess.SubprocessError):
        found = None
    _SYS_CACHE[dn] = found
    return found


def find_sdk(tools_dir: Path | str | None = None) -> dict[str, str] | None:
    return private_sdk(tools_dir) or system_sdk()


def toolchain_available(tools_dir: Path | str | None = None) -> bool:
    return find_sdk(tools_dir) is not None


def _tools_dir(ctx: StageContext) -> Path | None:
    st = (getattr(ctx, "services", None) or {}).get("studio")
    s = getattr(st, "settings", None)
    return Path(s.tools_dir) if s is not None and getattr(s, "tools_dir", None) else None


def project_file(source_dir: Path) -> Path | None:
    """The candidate's project: the .csproj at the top of the source folder (several: the one named like the folder's
    'main' marker, else the first by name)."""
    projs = sorted(source_dir.glob("*.csproj"))
    if not projs:
        return None
    marker = source_dir / ".rebuild-main-project"
    if marker.is_file():
        want = marker.read_text("utf-8", "replace").strip()
        for p in projs:
            if p.name == want:
                return p
    return projs[0]


def assembly_name(proj: Path) -> str:
    try:
        root = ET.fromstring(proj.read_text("utf-8-sig", "replace"))
        for el in root.iter():
            if el.tag.split("}")[-1] == "AssemblyName" and (el.text or "").strip():
                return el.text.strip()
    except ET.ParseError:
        pass
    return proj.stem


_ERR = re.compile(r"error\s+(CS\d{4}|MSB\d{4}|NETSDK\d{4}|NU\d{4})", re.I)


def first_error(log: str) -> str:
    for ln in log.splitlines():
        if _ERR.search(ln):
            return livelog.clean(ln.strip(), 220)
    return "see the details below"


def _cache_dir(ctx: StageContext, dist_dir: Path) -> Path:
    st = (ctx.services or {}).get("studio")
    if st is not None and getattr(ctx.job, "case_id", None):
        try:
            return st.cases.case_root(ctx.job.case_id) / "build-cache" / "dotnet"
        except Exception:  # noqa: BLE001
            pass
    return dist_dir.parent / ".build-cache" / "dotnet"


def build_env_vars(sdk: dict[str, str], cache: Path) -> dict[str, str]:
    """Declared environment of a sandboxed SDK run: no telemetry, no first-run, no build servers, caches inside the case."""
    return {"DOTNET_ROOT": sdk["root"], "DOTNET_MULTILEVEL_LOOKUP": "0", "DOTNET_CLI_HOME": str(cache / "cli-home"),
            "NUGET_PACKAGES": str(cache / "nuget-packages"), "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
            "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_GENERATE_ASPNET_CERTIFICATE": "false", "DOTNET_ADD_GLOBAL_TOOLS_TO_PATH": "false",
            "DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE": "1", "DOTNET_CLI_USE_MSBUILD_SERVER": "0", "MSBUILDDISABLENODEREUSE": "1",
            "UseSharedCompilation": "false", "NUGET_XMLDOC_MODE": "skip", "DOTNET_CLI_UI_LANGUAGE": "en"}


def build_csharp(ctx: StageContext, source_dir: Path, dist_dir: Path) -> dict[str, Any]:
    sdk = find_sdk(_tools_dir(ctx))
    if not sdk:
        raise StageError(".NET SDK not installed", blocker=BLOCKER)
    proj = project_file(source_dir)
    if proj is None:
        raise StageError("candidate has no .csproj project file")
    out = source_dir / OUT_DIR
    if out.exists():
        shutil.rmtree(out)
    args = [sdk["dotnet"], "build", proj.name, "-c", "Release", "-o", str(out), "--nologo", "-v:quiet", "-nodeReuse:false",
            "--disable-build-servers", "-p:UseSharedCompilation=false", "-p:ContinuousIntegrationBuild=true", "-p:GenerateDocumentationFile=false",
            "-clp:NoSummary"]
    ctx.log(f"Building the C# candidate (dotnet build, {sdk['where']} {sdk['sdk']})… MSBuild runs in an isolated process")
    t0 = time.monotonic()
    policy = sandbox.IsolationPolicy.from_spec((ctx.job.inputs or {}).get("isolation") if hasattr(ctx.job, "inputs") else None,
                                               wall_time_s=float(ctx.limits.max_stage_seconds), process_memory_bytes=4 * sandbox.GiB,
                                               job_memory_bytes=8 * sandbox.GiB, max_processes=256, ui_restrictions="strict",
                                               stdout_cap_bytes=ctx.limits.max_subprocess_output_bytes, stderr_cap_bytes=ctx.limits.max_subprocess_output_bytes)
    cache = _cache_dir(ctx, dist_dir)
    for d in (source_dir, cache):
        sandbox.prepare_work_dir(d, policy)
    env = sandbox.build_env(cache, program_dirs=[sdk["root"]], policy=policy, declared=build_env_vars(sdk, cache))
    res = sandbox.run(args, work=cache, cwd=source_dir, policy=policy, env=env, poll=ctx.heartbeat, label="build")
    took = time.monotonic() - t0
    log = (res.stdout + b"\n" + res.stderr).decode("utf-8", "replace")
    if res.timed_out:
        ctx.log(f"Build timed out after {ctx.limits.max_stage_seconds}s", "error")
        raise StageError(f"timeout after {ctx.limits.max_stage_seconds}s running dotnet build", retry=False)
    if res.returncode != 0:
        ctx.log(f"Build failed after {took:.1f} s: {first_error(log)}", "error", livelog.tail_lines(res.stdout or res.stderr, 5))
        raise StageError(f"dotnet build failed:\n{log[-12000:]}")
    asm = assembly_name(proj)
    dll = out / f"{asm}.dll"
    if not dll.is_file():
        raise StageError(f"built assembly {asm}.dll not found under {out}")
    ctx.log(f"Build passed in {took:.1f} s")
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    shutil.copytree(out, dist_dir)
    launch: dict[str, Any] = {"type": "dotnet", "path": f"{asm}.dll", "dotnet": sdk["dotnet"],
                              "env": {"DOTNET_ROOT": sdk["root"], "DOTNET_MULTILEVEL_LOOKUP": "0"}}
    if (dist_dir / f"{asm}{_EXE}").is_file():
        launch["apphost"] = f"{asm}{_EXE}"
    return {"launch": launch, "assembly": f"{asm}.dll", "build_log": log[-20000:], "toolchain": f".NET SDK {sdk['sdk']} ({sdk['where']})",
            "sdk": sdk["sdk"], "private_toolchain": sdk["sdk"] if sdk["where"] == "private SDK" else None,
            "build_isolation": {**res.isolation, "limits_triggered": res.triggered}}
