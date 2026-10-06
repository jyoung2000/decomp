from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

from .. import sandbox
from ..jobs.runner import StageContext, StageError


def cargo() -> str | None:
    return shutil.which("cargo") or (str(Path.home() / ".cargo" / "bin" / "cargo") if (Path.home() / ".cargo" / "bin" / "cargo").exists() else None)


def build_rust(ctx: StageContext, source_dir: Path, dist_dir: Path, *, bevy: bool = False, target_triple: str | None = None) -> dict[str, Any]:
    cg = cargo()
    if not cg:
        raise StageError("cargo not installed", blocker="install Rust toolchain (rustup) to build Rust candidates")
    manifest = source_dir / "Cargo.toml"
    if not manifest.exists():
        raise StageError("candidate has no Cargo.toml")
    meta = tomllib.loads(manifest.read_text())
    name = meta.get("package", {}).get("name")
    if not name:
        raise StageError("Cargo.toml has no package name")
    args = [cg, "build", "--release", "--locked"] if (source_dir / "Cargo.lock").exists() else [cg, "build", "--release"]
    if target_triple:
        args += ["--target", target_triple]
    ctx.log(f"cargo build in {source_dir} (isolated: build scripts and proc-macros are untrusted code)")
    res = _isolated_cargo(ctx, args, source_dir, dist_dir)
    log = (res.stdout + b"\n" + res.stderr).decode("utf-8", "replace")
    if res.timed_out:
        raise StageError(f"timeout after {ctx.limits.max_stage_seconds}s running cargo build", retry=False)
    if res.returncode != 0:
        raise StageError(f"cargo build failed:\n{log[-6000:]}")
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
    return {"launch": {"type": "exe", "path": out.name}, "binary": str(out), "build_log": log[-20000:], "toolchain": _rustc_version(ctx), "bevy": bevy,
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


def _isolated_cargo(ctx: StageContext, args: list[str], source_dir: Path, dist_dir: Path) -> sandbox.RunResult:
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
    rustup_home = os.environ.get("RUSTUP_HOME") or str(Path.home() / ".rustup")
    env = sandbox.build_env(iso_root, program_dirs=[str(Path(args[0]).parent)], policy=policy,
                            declared={"CARGO_HOME": str(cargo_home), "RUSTUP_HOME": rustup_home, "CARGO_TERM_COLOR": "never", "CARGO_INCREMENTAL": "0"})
    return sandbox.run(args, work=iso_root, cwd=source_dir, policy=policy, env=env, poll=ctx.heartbeat, label="build")


def _rustc_version(ctx: StageContext) -> str:
    try:
        r = ctx.run([shutil.which("rustc") or "rustc", "--version"], timeout=30)
        return r.text.strip()
    except Exception:
        return "unknown"
