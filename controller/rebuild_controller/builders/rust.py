from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

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
    env = dict(os.environ)
    env.setdefault("CARGO_TERM_COLOR", "never")
    env.setdefault("CARGO_INCREMENTAL", "0")
    args = [cg, "build", "--release", "--locked"] if (source_dir / "Cargo.lock").exists() else [cg, "build", "--release"]
    if target_triple:
        args += ["--target", target_triple]
    ctx.log(f"cargo build in {source_dir}")
    res = ctx.run(args, cwd=str(source_dir), env=env, timeout=ctx.limits.max_stage_seconds)
    log = (res.stdout + b"\n" + res.stderr).decode("utf-8", "replace")
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
            "locked": (source_dir / "Cargo.lock").exists()}


def _rustc_version(ctx: StageContext) -> str:
    try:
        r = ctx.run([shutil.which("rustc") or "rustc", "--version"], timeout=30)
        return r.text.strip()
    except Exception:
        return "unknown"
