from __future__ import annotations

import os
import shutil
import time
import tomllib
from pathlib import Path
from typing import Any

from .. import livelog, sandbox
from ..jobs.runner import StageContext, StageError


TOOL_NAME = "rust"                                   # key in dependency-lock.json / the Tools page
TOOL_TITLE = "Rust compiler (private)"               # tool_setup.FRIENDLY["rust"][0]
BLOCKER = (f"open Tools and install '{TOOL_TITLE}' (needed to build rebuilt programs as Windows .exe files), then resume; "
           "or put cargo on PATH")


def cargo() -> str | None:
    return shutil.which("cargo") or (str(Path.home() / ".cargo" / "bin" / "cargo") if (Path.home() / ".cargo" / "bin" / "cargo").exists() else None)


def private_rust(tools_dir: Path | str | None = None) -> dict[str, str] | None:
    """The toolchain installed by Tools setup: <tools>/rust/rustup/toolchains/<ver>-<host>/bin/cargo.exe (the proxy in
    cargo/bin is deliberately not used). None when it is not (completely) there."""
    if tools_dir is None:
        from ..config import get_settings
        tools_dir = get_settings().tools_dir
    root = Path(tools_dir) / TOOL_NAME
    exe = ".exe" if os.name == "nt" else ""
    tcs = root / "rustup" / "toolchains"
    try:
        cands = sorted((d for d in tcs.iterdir() if (d / "bin" / f"cargo{exe}").is_file() and (d / "bin" / f"rustc{exe}").is_file()), reverse=True)
    except OSError:
        return None
    if not cands:
        return None
    tc = cands[0]
    return {"cargo": str(tc / "bin" / f"cargo{exe}"), "rustc": str(tc / "bin" / f"rustc{exe}"), "bin": str(tc / "bin"),
            "toolchain": tc.name, "rustup_home": str(root / "rustup"), "cargo_home": str(root / "cargo")}


def _tools_dir(ctx: StageContext) -> Path | None:
    st = (getattr(ctx, "services", None) or {}).get("studio")
    s = getattr(st, "settings", None)
    return Path(s.tools_dir) if s is not None and getattr(s, "tools_dir", None) else None


def toolchain_available(tools_dir: Path | str | None = None) -> bool:
    """Used by the forecast / capabilities text: can a Rust candidate be built here at all?"""
    return private_rust(tools_dir) is not None or cargo() is not None


def _first_cargo_error(log: str) -> str:
    for ln in log.splitlines():
        if ln.startswith("error"):
            return livelog.clean(ln, 200)
    return "see the details below"


def build_rust(ctx: StageContext, source_dir: Path, dist_dir: Path, *, bevy: bool = False, target_triple: str | None = None) -> dict[str, Any]:
    private = private_rust(_tools_dir(ctx))
    cg = private["cargo"] if private else cargo()
    if not cg:
        raise StageError("cargo not installed", blocker=BLOCKER)
    manifest = source_dir / "Cargo.toml"
    if not manifest.exists():
        raise StageError("candidate has no Cargo.toml")
    try:
        meta = tomllib.loads(manifest.read_text("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise StageError(f"Cargo.toml is not valid TOML: {e}") from e
    name = meta.get("package", {}).get("name")
    if not name:
        raise StageError("Cargo.toml has no package name")
    args = [cg, "build", "--release", "--locked"] if (source_dir / "Cargo.lock").exists() else [cg, "build", "--release"]
    if target_triple:
        args += ["--target", target_triple]
    where = "private toolchain" if private else "system toolchain"
    ctx.log(f"Building the Rust candidate (cargo, {where})… build scripts run in an isolated process")
    t0 = time.monotonic()
    res = _isolated_cargo(ctx, args, source_dir, dist_dir, private=private)
    took = time.monotonic() - t0
    log = (res.stdout + b"\n" + res.stderr).decode("utf-8", "replace")
    if res.timed_out:
        ctx.log(f"Build timed out after {ctx.limits.max_stage_seconds}s", "error")
        raise StageError(f"timeout after {ctx.limits.max_stage_seconds}s running cargo build", retry=False)
    if res.returncode != 0:
        ctx.log(f"Build failed after {took:.1f} s: {_first_cargo_error(log)}", "error", livelog.tail_lines(res.stderr or log, 5))
        raise StageError(f"cargo build failed:\n{log[-6000:]}")
    ctx.log(f"Build passed in {took:.1f} s")
    tdir = source_dir / "target" / (target_triple if target_triple else "") / "release"
    bin_name = name.replace("-", "_") if False else name
    exe = None
    for cand in (tdir / bin_name, tdir / f"{bin_name}.exe"):
        if cand.exists():
            exe = cand
            break
    if exe is None:
        raise StageError(f"built binary not found under {tdir}")
    dist_dir.mkdir(parents=True, exist_ok=True)
    out = dist_dir / exe.name
    shutil.copy2(exe, out)
    # ship assets/ if the candidate has them (Bevy, data files)
    for extra in ("assets", "data"):
        if (source_dir / extra).is_dir():
            shutil.copytree(source_dir / extra, dist_dir / extra, dirs_exist_ok=True)
    return {"launch": {"type": "exe", "path": out.name}, "binary": str(out), "build_log": log[-20000:], "toolchain": _rustc_version(ctx, private), "private_toolchain": private["toolchain"] if private else None, "bevy": bevy,
            "locked": (source_dir / "Cargo.lock").exists(), "build_isolation": {**res.isolation, "limits_triggered": res.triggered}}


def _cargo_cache_dir(ctx: StageContext, dist_dir: Path) -> Path:
    """Case-level CARGO_HOME (registry/git cache) writable by the low-integrity build; shared by the case's candidates."""
    st = (ctx.services or {}).get("studio")
    if st is not None and getattr(ctx.job, "case_id", None):
        try:
            return st.cases.case_root(ctx.job.case_id) / "build-cache" / "cargo-home"
        except Exception:  # noqa: BLE001 - fall back to a per-candidate cache
            pass
    return dist_dir.parent / ".build-cache" / "cargo-home"


def _isolated_cargo(ctx: StageContext, args: list[str], source_dir: Path, dist_dir: Path, *, private: dict[str, str] | None = None) -> sandbox.RunResult:
    """cargo runs build.rs / proc-macros from the (AI-generated) candidate: run it in the sandbox. Low integrity on Windows:
    it can write the candidate source/target dir and its own CARGO_HOME cache, not the user profile. Network stays open
    (crates.io downloads); see docs/ISOLATION.md."""
    policy = sandbox.IsolationPolicy.from_spec((ctx.job.inputs or {}).get("isolation") if hasattr(ctx.job, "inputs") else None,
                                               wall_time_s=float(ctx.limits.max_stage_seconds), process_memory_bytes=8 * sandbox.GiB,
                                               job_memory_bytes=16 * sandbox.GiB, max_processes=512, ui_restrictions="strict",
                                               stdout_cap_bytes=ctx.limits.max_subprocess_output_bytes, stderr_cap_bytes=ctx.limits.max_subprocess_output_bytes,
                                               env_passthrough=("RUSTUP_TOOLCHAIN", "RUSTFLAGS", "CARGO_BUILD_TARGET"))
    cargo_home = _cargo_cache_dir(ctx, dist_dir)
    iso_root = cargo_home.parent
    for d in (source_dir, iso_root):
        sandbox.prepare_work_dir(d, policy)
    cargo_home.mkdir(parents=True, exist_ok=True)
    # CARGO_HOME stays the case-local cache either way. With the private toolchain cargo/rustc are called directly from the
    # toolchain dir (no rustup proxy), RUSTUP_HOME points at the private rustup home, and the default host/target is the GNU one.
    rustup_home = private["rustup_home"] if private else (os.environ.get("RUSTUP_HOME") or str(Path.home() / ".rustup"))
    declared = {"CARGO_HOME": str(cargo_home), "RUSTUP_HOME": rustup_home, "CARGO_TERM_COLOR": "never", "CARGO_INCREMENTAL": "0"}
    if private:
        declared["RUSTC"] = private["rustc"]
    env = sandbox.build_env(iso_root, program_dirs=[str(Path(args[0]).parent)], policy=policy, declared=declared)
    if private:
        for k in ("RUSTUP_TOOLCHAIN", "CARGO_BUILD_TARGET"):    # a developer's rustup/target override must not redirect the private toolchain
            env.pop(k, None)
    return sandbox.run(args, work=iso_root, cwd=source_dir, policy=policy, env=env, poll=ctx.heartbeat, label="build")


def _rustc_version(ctx: StageContext, private: dict[str, str] | None = None) -> str:
    try:
        r = ctx.run([(private or {}).get("rustc") or shutil.which("rustc") or "rustc", "--version"], timeout=30)
        return r.text.strip()
    except Exception:
        return "unknown"
