"""Java builder (R4 'native-language rebuild'): javac + a deterministic jar for a candidate recovered from a .jar.

javac comes from the pinned Temurin JDK 21 in Tools (``<tools>/jdk21``), else JAVA_HOME, else PATH. The sources are compiled
with ``--release`` matching the original class files (recorded in ``rebuild-java.json``), annotation processing off
(``-proc:none``: no candidate code runs during the build), inside the sandbox. The jar is written by Python (manifest first,
sorted entries, fixed timestamps), so no ``jar`` tool is needed and two builds of the same classes give the same bytes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

from .. import livelog, sandbox
from ..jobs.runner import StageContext, StageError

TOOL_NAME = "temurin-jdk21"                     # key in dependency-lock.json / the Tools page
TOOL_TITLE = "Private Java 21 (optional)"       # tool_setup.FRIENDLY["temurin-jdk21"][0]
BLOCKER = f"open Tools and install '{TOOL_TITLE}' (its javac compiles Java rebuilds), then resume; or put a JDK (javac) on PATH/JAVA_HOME"
PROJECT_FILE = "rebuild-java.json"
SOURCE_DIR = "src"
RESOURCE_DIR = "resources"
CLASSES_DIR = Path("build") / "classes"
_EXE = ".exe" if os.name == "nt" else ""
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)
JAVA_RELEASE = {52: 8, 53: 9, 54: 10, 55: 11, 56: 12, 57: 13, 58: 14, 59: 15, 60: 16, 61: 17, 62: 18, 63: 19, 64: 20, 65: 21}


def release_for_class_major(major: int) -> int | None:
    """javac --release value for a class-file major version (45-51 -> 8: the oldest --release JDK 21 still accepts)."""
    if major <= 52:
        return 8
    return JAVA_RELEASE.get(major)


def _jdk_at(home: Path, where: str) -> dict[str, str] | None:
    javac, java = home / "bin" / f"javac{_EXE}", home / "bin" / f"java{_EXE}"
    if javac.is_file() and java.is_file():
        return {"javac": str(javac), "java": str(java), "home": str(home), "where": where}
    return None


def private_jdk(tools_dir: Path | str | None = None) -> dict[str, str] | None:
    if tools_dir is None:
        from ..config import get_settings
        tools_dir = get_settings().tools_dir
    return _jdk_at(Path(tools_dir) / "jdk21", "private JDK 21")


def system_jdk() -> dict[str, str] | None:
    jh = os.environ.get("JAVA_HOME")
    if jh:
        found = _jdk_at(Path(jh), "JAVA_HOME")
        if found:
            return found
    javac = shutil.which("javac")
    if javac:
        return _jdk_at(Path(javac).resolve().parent.parent, "JDK on PATH")
    return None


def find_jdk(tools_dir: Path | str | None = None) -> dict[str, str] | None:
    return private_jdk(tools_dir) or system_jdk()


def toolchain_available(tools_dir: Path | str | None = None) -> bool:
    return find_jdk(tools_dir) is not None


def _tools_dir(ctx: StageContext) -> Path | None:
    st = (getattr(ctx, "services", None) or {}).get("studio")
    s = getattr(st, "settings", None)
    return Path(s.tools_dir) if s is not None and getattr(s, "tools_dir", None) else None


def read_project(source_dir: Path) -> dict[str, Any]:
    p = source_dir / PROJECT_FILE
    if not p.is_file():
        raise StageError(f"candidate has no {PROJECT_FILE} (main class, Java release, jar name)")
    try:
        proj = json.loads(p.read_text("utf-8"))
    except ValueError as e:
        raise StageError(f"{PROJECT_FILE} is not valid JSON: {e}") from e
    if not isinstance(proj, dict):
        raise StageError(f"{PROJECT_FILE} must be a JSON object")
    return proj


_JAVAC_ERR = re.compile(r"^(.+?\.java):(\d+): error: (.*)$")


def first_error(log: str) -> str:
    for ln in log.splitlines():
        if _JAVAC_ERR.match(ln.strip()) or ln.startswith("error:"):
            return livelog.clean(ln.strip(), 220)
    return "see the details below"


def _manifest(proj: dict[str, Any]) -> bytes:
    lines = ["Manifest-Version: 1.0"]
    if proj.get("main_class"):
        lines.append(f"Main-Class: {proj['main_class']}")
    for k, v in sorted((proj.get("manifest") or {}).items()):
        if k in ("Manifest-Version", "Main-Class", "Created-By") or not re.match(r"^[A-Za-z0-9_-]{1,70}$", str(k)):
            continue
        lines.append(f"{k}: {str(v).replace(chr(13), ' ').replace(chr(10), ' ')}")
    lines.append("Created-By: Rebuild Studio (R4 native-language rebuild)")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


def write_jar(jar: Path, classes: Path, resources: Path | None, proj: dict[str, Any]) -> int:
    """Deterministic jar: manifest first, then every other file sorted by path, fixed timestamps, deflated."""
    entries: dict[str, Path] = {}
    for base in [b for b in (resources, classes) if b is not None and b.is_dir()]:
        for p in base.rglob("*"):
            if p.is_file() and not p.is_symlink():
                rel = p.relative_to(base).as_posix()
                if rel.upper() == "META-INF/MANIFEST.MF":
                    continue
                entries[rel] = p        # classes win over a stale resource of the same name
    jar.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("META-INF/MANIFEST.MF", _FIXED_TIME), _manifest(proj), zipfile.ZIP_DEFLATED)
        for rel in sorted(entries):
            z.writestr(zipfile.ZipInfo(rel, _FIXED_TIME), entries[rel].read_bytes(), zipfile.ZIP_DEFLATED)
    return len(entries) + 1


def build_java(ctx: StageContext, source_dir: Path, dist_dir: Path) -> dict[str, Any]:
    jdk = find_jdk(_tools_dir(ctx))
    if not jdk:
        raise StageError("javac not installed", blocker=BLOCKER)
    proj = read_project(source_dir)
    src_root = source_dir / str(proj.get("source_root") or SOURCE_DIR)
    sources = sorted(p for p in src_root.rglob("*.java") if p.is_file()) if src_root.is_dir() else []
    if not sources:
        raise StageError(f"candidate has no .java files under {src_root.name}/")
    release = int(proj.get("release") or 17)
    classes = source_dir / CLASSES_DIR
    if classes.exists():
        shutil.rmtree(classes)
    classes.mkdir(parents=True)
    # an argument file keeps long source lists under the Windows command-line limit
    argfile = source_dir / "build" / "javac-sources.txt"
    argfile.write_text("\n".join('"' + p.relative_to(source_dir).as_posix() + '"' for p in sources) + "\n", encoding="utf-8")
    args = [jdk["javac"], "--release", str(release), "-encoding", "UTF-8", "-proc:none", "-nowarn", "-Xmaxerrs", "200",
            "-implicit:none", "-d", CLASSES_DIR.as_posix(), "-sourcepath", src_root.relative_to(source_dir).as_posix(), f"@{argfile.relative_to(source_dir).as_posix()}"]
    ctx.log(f"Building the Java candidate (javac --release {release}, {jdk['where']})… javac runs in an isolated process")
    t0 = time.monotonic()
    policy = sandbox.IsolationPolicy.from_spec((ctx.job.inputs or {}).get("isolation") if hasattr(ctx.job, "inputs") else None,
                                               wall_time_s=float(ctx.limits.max_stage_seconds), process_memory_bytes=4 * sandbox.GiB,
                                               job_memory_bytes=6 * sandbox.GiB, max_processes=64, ui_restrictions="strict",
                                               stdout_cap_bytes=ctx.limits.max_subprocess_output_bytes, stderr_cap_bytes=ctx.limits.max_subprocess_output_bytes)
    iso = source_dir / "build" / ".iso"
    for d in (source_dir, iso):
        sandbox.prepare_work_dir(d, policy)
    env = sandbox.build_env(iso, program_dirs=[str(Path(jdk["javac"]).parent)], policy=policy, declared={"JAVA_HOME": jdk["home"]})
    res = sandbox.run(args, work=iso, cwd=source_dir, policy=policy, env=env, poll=ctx.heartbeat, label="build")
    took = time.monotonic() - t0
    log = (res.stdout + b"\n" + res.stderr).decode("utf-8", "replace")
    if res.timed_out:
        ctx.log(f"Build timed out after {ctx.limits.max_stage_seconds}s", "error")
        raise StageError(f"timeout after {ctx.limits.max_stage_seconds}s running javac", retry=False)
    if res.returncode != 0:
        ctx.log(f"Build failed after {took:.1f} s: {first_error(log)}", "error", livelog.tail_lines(res.stderr or res.stdout, 5))
        raise StageError(f"javac failed:\n{log[-12000:]}")
    ctx.log(f"Build passed in {took:.1f} s")
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True)
    jar_name = re.sub(r"[^A-Za-z0-9._-]", "_", str(proj.get("jar") or "app.jar"))
    if not jar_name.lower().endswith(".jar"):
        jar_name += ".jar"
    res_dir = source_dir / str(proj.get("resource_root") or RESOURCE_DIR)
    n = write_jar(dist_dir / jar_name, classes, res_dir if res_dir.is_dir() else None, proj)
    launch = {"type": "java", "jar": jar_name, "java": jdk["java"]}
    return {"launch": launch, "jar": jar_name, "jar_entries": n, "release": release, "build_log": log[-20000:],
            "toolchain": f"javac ({jdk['where']})", "jdk_home": jdk["home"], "private_toolchain": jdk["home"] if jdk["where"].startswith("private") else None,
            "build_isolation": {**res.isolation, "limits_triggered": res.triggered}}
