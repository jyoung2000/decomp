"""CLI channel comparator: runs one scenario (sequence of steps) against a launch spec in an isolated work dir."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..ids import sha256_bytes, sha256_file
from .base import ComparisonResult, normalize_text


def host_launcher(launch: dict[str, Any], root: Path) -> tuple[list[str], dict[str, str], str]:
    """Translate a launch spec into argv for this host. Returns (argv_prefix, env, runner_label)."""
    kind = launch.get("type", "exe")
    env = dict(os.environ)
    env.update(launch.get("env", {}))
    if kind == "exe":
        exe = root / launch["path"]
        if os.name != "nt" and exe.suffix.lower() == ".exe":
            wine = shutil.which("wine") or shutil.which("wine64")
            if not wine:
                raise RuntimeError("PE executable cannot run on this host: wine not installed (Windows gate)")
            env.setdefault("WINEDEBUG", "-all")
            env.setdefault("WINEPREFIX", os.environ.get("REBUILD_WINEPREFIX", "/opt/rebuild-tools/wineprefix"))
            return [wine, str(exe)], env, "wine (non-certifying host runner)"
        return [str(exe)], env, "native"
    if kind == "dotnet":
        dll = root / launch["path"]
        dotnet = shutil.which("dotnet")
        if not dotnet:
            raise RuntimeError(".NET runtime not installed")
        env.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1"); env.setdefault("DOTNET_NOLOGO", "1")
        return [dotnet, str(dll)], env, "dotnet"
    if kind == "command":
        return [str(root / c) if i == 0 and not Path(c).is_absolute() and (root / c).exists() else c for i, c in enumerate(launch["command"])], env, "command"
    raise RuntimeError(f"unsupported launch type {kind}")


def run_steps(launch: dict[str, Any], root: Path, steps: list[dict[str, Any]], work: Path, *, timeout: float = 60,
              stdin_mode: str = "pipe", setup_files: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run steps sequentially in `work` (fresh dir). Each step: {args:[..], stdin?: str, env?: {}} with `{work}` templating."""
    work.mkdir(parents=True, exist_ok=True)
    for name, spec in (setup_files or {}).items():
        dest = work / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(spec, dict) and "hex" in spec:
            dest.write_bytes(bytes.fromhex(spec["hex"]))
        elif isinstance(spec, dict) and "text" in spec:
            dest.write_text(spec["text"], encoding="utf-8", newline="")
        elif isinstance(spec, str):
            dest.write_text(spec, encoding="utf-8", newline="")
    prefix, env, runner = host_launcher(launch, root)
    results = []
    for st in steps:
        args = [a.replace("{work}", str(work)) if isinstance(a, str) else a for a in st.get("args", [])]
        if os.name != "nt" and launch.get("type") == "exe" and (root / launch["path"]).suffix.lower() == ".exe":
            # wine wants Windows-style paths for arguments pointing at files; keep posix (wine maps / to Z:) for simplicity
            pass
        senv = dict(env); senv.update({k: v.replace("{work}", str(work)) for k, v in st.get("env", {}).items()})
        stdin = (st.get("stdin") or "").encode("utf-8")
        try:
            p = subprocess.run(prefix + args, cwd=str(work), env=senv, input=stdin, capture_output=True, timeout=timeout)
            results.append({"args": args, "exit_code": p.returncode, "stdout": p.stdout.decode("utf-8", "replace"),
                            "stderr": p.stderr.decode("utf-8", "replace"), "runner": runner, "timed_out": False})
        except subprocess.TimeoutExpired as e:
            results.append({"args": args, "exit_code": None, "stdout": (e.stdout or b"").decode("utf-8", "replace"),
                            "stderr": (e.stderr or b"").decode("utf-8", "replace"), "runner": runner, "timed_out": True})
            break
    return results


def snapshot_work(work: Path) -> dict[str, str]:
    out = {}
    for p in sorted(work.rglob("*")):
        if p.is_file():
            out[p.relative_to(work).as_posix()] = sha256_file(p)
    return out


def compare_cli_scenario(scenario: dict[str, Any], baseline: dict[str, Any], candidate_launch: dict[str, Any], candidate_root: Path,
                         work: Path, *, timeout: float = 60) -> list[ComparisonResult]:
    """Compare a candidate run against the frozen baseline for one scenario. Baseline is never modified."""
    steps = scenario["steps"]
    norm = scenario.get("normalize", {})
    cand_runs = run_steps(candidate_launch, candidate_root, steps, work, timeout=timeout, setup_files=scenario.get("setup_files"))
    base_runs = baseline["steps"]
    out: list[ComparisonResult] = []
    cmd = " && ".join(" ".join(r["args"]) for r in cand_runs)
    for i, (b, c) in enumerate(zip(base_runs, cand_runs)):
        tag = f"step{i}"
        rule_exit = "exact"
        out.append(ComparisonResult("exit_code", rule_exit, "pass" if b["exit_code"] == c["exit_code"] else "fail",
                                    {"step": i, "expected": b["exit_code"], "actual": c["exit_code"], "timed_out": c["timed_out"], "runner": c["runner"]}, command=cmd))
        for ch in ("stdout", "stderr"):
            if ch not in scenario.get("channels", ["exit_code", "stdout", "files"]):
                continue
            rules = norm.get(ch, ["crlf"])
            be, ce = normalize_text(b[ch], rules), normalize_text(c[ch], rules)
            out.append(ComparisonResult(ch, "normalize:" + ",".join(rules) if rules else "exact", "pass" if be == ce else "fail",
                                        {"step": i, "expected_excerpt": be[:2000], "actual_excerpt": ce[:2000], "diff_at": _first_diff(be, ce)},
                                        original_hash=sha256_bytes(be.encode()), candidate_hash=sha256_bytes(ce.encode()), command=cmd))
    if len(cand_runs) < len(base_runs):
        out.append(ComparisonResult("exit_code", "exact", "fail", {"error": "candidate run stopped early (timeout)", "steps_run": len(cand_runs)}, command=cmd))
    if "files" in scenario.get("channels", ["exit_code", "stdout", "files"]):
        expected_files = baseline.get("files", {})
        actual_files = snapshot_work(work)
        ignore = set(scenario.get("ignore_files", []))
        # setup files the oracle does not list as final are expected to be unchanged: compare against their setup content
        for name, spec in (scenario.get("setup_files") or {}).items():
            if name not in expected_files and isinstance(spec, dict) and spec.get("sha256"):
                expected_files = {**expected_files, name: spec["sha256"]}
        exp = {k: v for k, v in expected_files.items() if k not in ignore}
        act = {k: v for k, v in actual_files.items() if k not in ignore}
        mism = {k: {"expected": exp.get(k), "actual": act.get(k)} for k in sorted(set(exp) | set(act)) if exp.get(k) != act.get(k)}
        out.append(ComparisonResult("files", "sha256:exact", "pass" if not mism else "fail", {"mismatches": mism, "expected_count": len(exp), "actual_count": len(act)},
                                    original_hash=sha256_bytes(repr(sorted(exp.items())).encode()), candidate_hash=sha256_bytes(repr(sorted(act.items())).encode()), command=cmd))
    return out


def _first_diff(a: str, b: str) -> int | None:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return None if len(a) == len(b) else min(len(a), len(b))
