"""CLI channel comparator: runs one scenario (sequence of steps) against a launch spec in a fresh work dir.

Every step runs through :mod:`rebuild_controller.sandbox` (Job Object + low integrity + scrubbed env on Windows, process
group + rlimits on POSIX). The work dir is a scratch folder, not a security boundary; see docs/ISOLATION.md for what the
isolation does and does not guarantee. Running the user's ORIGINAL program needs recorded per-case consent (role="original").
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable

from .. import sandbox
from ..ids import sha256_bytes, sha256_file
from ..sandbox import IsolationPolicy, OriginalExecutionNotPermitted
from .base import ComparisonResult, normalize_text


def host_launcher(launch: dict[str, Any], root: Path) -> tuple[list[str], dict[str, str], str]:
    """Translate a launch spec into argv for this host. Returns (argv_prefix, declared_env, runner_label).

    ``declared_env`` holds only what the launch spec / runtime needs (never the host environment); the caller merges it
    into the sandbox allowlist environment.
    """
    kind = launch.get("type", "exe")
    env: dict[str, str] = {str(k): str(v) for k, v in (launch.get("env") or {}).items()}
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
        argv = [str(root / c) if i == 0 and not Path(c).is_absolute() and (root / c).exists() else c for i, c in enumerate(launch["command"])]
        if argv and not Path(argv[0]).is_absolute():
            found = shutil.which(argv[0])   # resolve against the HOST path before the environment is scrubbed
            if found:
                argv[0] = found
        return argv, env, "command"
    raise RuntimeError(f"unsupported launch type {kind}")


def original_consent_error(launch: dict[str, Any] | None = None, root: Path | None = None) -> OriginalExecutionNotPermitted:
    what = (launch or {}).get("path") or " ".join((launch or {}).get("command") or []) or "the original program"
    where = f" from {root}" if root else ""
    return OriginalExecutionNotPermitted(
        f"Original execution needs your permission: Rebuild Studio wants to run {what}{where} to record its behaviour. "
        "It runs with limited isolation (on Windows: Job Object, low integrity, scrubbed environment); the network is NOT blocked "
        "and the program can still read your files (see docs/ISOLATION.md). Allow it for this project "
        "(PUT /cases/{case_id}/consent/original-execution with {\"allow\": true}) or supply a baseline file instead.")


def isolation_policy(launch: dict[str, Any], *, timeout: float, overrides: dict[str, Any] | None = None) -> IsolationPolicy:
    spec = {**(launch.get("isolation") or {}), **(overrides or {})}
    return IsolationPolicy.from_spec(spec, wall_time_s=timeout, ui_restrictions="strict")


def run_steps(launch: dict[str, Any], root: Path, steps: list[dict[str, Any]], work: Path, *, timeout: float = 60,
              stdin_mode: str = "pipe", setup_files: dict[str, Any] | None = None, role: str = "candidate",
              consent: dict[str, Any] | None = None, isolation: dict[str, Any] | None = None,
              poll: Callable[[], None] | None = None) -> list[dict[str, Any]]:
    """Run steps sequentially in `work` (fresh dir), each isolated. Each step: {args:[..], stdin?: str, env?: {}} with `{work}` templating.

    role="original" refuses to run unless ``consent`` is a recorded grant ({"allowed": True, ...}).
    The isolated home (TEMP/APPDATA/USERPROFILE...) lives in a sibling folder so it never pollutes the compared files.
    """
    if role == "original" and not (consent or {}).get("allowed"):
        raise original_consent_error(launch, root)
    policy = isolation_policy(launch, timeout=timeout, overrides=isolation)
    iso_root = work.parent / f".{work.name}.isohome"
    if iso_root.exists():
        shutil.rmtree(iso_root, ignore_errors=True)
    for d in (work, iso_root):
        sandbox.prepare_work_dir(d, policy)
    for name, spec in (setup_files or {}).items():
        dest = work / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(spec, dict) and "hex" in spec:
            dest.write_bytes(bytes.fromhex(spec["hex"]))
        elif isinstance(spec, dict) and "text" in spec:
            dest.write_text(spec["text"], encoding="utf-8", newline="")
        elif isinstance(spec, str):
            dest.write_text(spec, encoding="utf-8", newline="")
    prog_root = sandbox.stage_program_dir(root, iso_root) if sandbox.program_staging_needed(policy) else root
    prefix, declared_env, runner = host_launcher(launch, prog_root)
    program_dirs = [str(Path(prefix[0]).parent)] if prefix and Path(prefix[0]).is_absolute() else []
    if str(prog_root) not in program_dirs:
        program_dirs.append(str(prog_root))
    results = []
    for st in steps:
        args = [a.replace("{work}", str(work)) if isinstance(a, str) else a for a in st.get("args", [])]
        senv = dict(declared_env); senv.update({k: v.replace("{work}", str(work)) for k, v in st.get("env", {}).items()})
        env = sandbox.build_env(iso_root, program_dirs=program_dirs, policy=policy, declared=senv)
        stdin = (st.get("stdin") or "").encode("utf-8")
        r = sandbox.run(prefix + args, work=iso_root, cwd=work, policy=policy, env=env, stdin=stdin, poll=poll, label=role)
        results.append({"args": args, "exit_code": r.returncode, "stdout": r.stdout.decode("utf-8", "replace"),
                        "stderr": r.stderr.decode("utf-8", "replace"), "runner": runner, "timed_out": r.timed_out,
                        "stdout_truncated": r.stdout_truncated, "stderr_truncated": r.stderr_truncated, "triggered": r.triggered,
                        "limits": r.limits, "isolation": r.isolation, "duration_s": round(r.duration_s, 3), "role": role})
        if r.timed_out:
            break
    return results


def snapshot_work(work: Path, *, crlf_text_files: set[str] | frozenset[str] = frozenset()) -> dict[str, str]:
    """sha256 per file. Files named in ``crlf_text_files`` are hashed with CRLF -> LF (declared text files only)."""
    out = {}
    for p in sorted(work.rglob("*")):
        if p.is_file():
            rel = p.relative_to(work).as_posix()
            out[rel] = sha256_bytes(p.read_bytes().replace(b"\r\n", b"\n")) if rel in crlf_text_files else sha256_file(p)
    return out


def compare_cli_scenario(scenario: dict[str, Any], baseline: dict[str, Any], candidate_launch: dict[str, Any], candidate_root: Path,
                         work: Path, *, timeout: float = 60) -> list[ComparisonResult]:
    """Compare a candidate run against the frozen baseline for one scenario. Baseline is never modified."""
    steps = scenario["steps"]
    norm = scenario.get("normalize", {})
    cand_runs = run_steps(candidate_launch, candidate_root, steps, work, timeout=timeout, setup_files=scenario.get("setup_files"),
                          role="candidate", isolation=scenario.get("isolation"))
    base_runs = baseline["steps"]
    out: list[ComparisonResult] = []
    cmd = " && ".join(" ".join(r["args"]) for r in cand_runs)
    for i, (b, c) in enumerate(zip(base_runs, cand_runs)):
        rule_exit = "exact"
        out.append(ComparisonResult("exit_code", rule_exit, "pass" if b["exit_code"] == c["exit_code"] else "fail",
                                    {"step": i, "expected": b["exit_code"], "actual": c["exit_code"], "timed_out": c["timed_out"], "runner": c["runner"],
                                     "limits_triggered": c.get("triggered", []), "isolation": c.get("isolation")}, command=cmd))
        for ch in ("stdout", "stderr"):
            if ch not in scenario.get("channels", ["exit_code", "stdout", "files"]):
                continue
            rules = norm.get(ch, ["crlf"])
            be, ce = normalize_text(b[ch], rules), normalize_text(c[ch], rules)
            out.append(ComparisonResult(ch, "normalize:" + ",".join(rules) if rules else "exact", "pass" if be == ce else "fail",
                                        {"step": i, "expected_excerpt": be[:2000], "actual_excerpt": ce[:2000], "diff_at": _first_diff(be, ce),
                                         "truncated": bool(c.get(f"{ch}_truncated"))},
                                        original_hash=sha256_bytes(be.encode()), candidate_hash=sha256_bytes(ce.encode()), command=cmd))
    if len(cand_runs) < len(base_runs):
        out.append(ComparisonResult("exit_code", "exact", "fail", {"error": "candidate run stopped early (timeout)", "steps_run": len(cand_runs)}, command=cmd))
    if "files" in scenario.get("channels", ["exit_code", "stdout", "files"]):
        expected_files = baseline.get("files", {})
        crlf_files = set(norm.get("text_files", [])) if "crlf" in norm.get("files", []) else set()
        actual_files = snapshot_work(work, crlf_text_files=crlf_files)
        files_rule = "sha256:crlf-normalized" if crlf_files else "sha256:exact"
        ignore = set(scenario.get("ignore_files", []))
        # setup files the oracle does not list as final are expected to be unchanged: compare against their setup content
        for name, spec in (scenario.get("setup_files") or {}).items():
            if name not in expected_files and isinstance(spec, dict) and spec.get("sha256"):
                expected_files = {**expected_files, name: spec["sha256"]}
        exp = {k: v for k, v in expected_files.items() if k not in ignore}
        act = {k: v for k, v in actual_files.items() if k not in ignore}
        mism = {k: {"expected": exp.get(k), "actual": act.get(k)} for k in sorted(set(exp) | set(act)) if exp.get(k) != act.get(k)}
        out.append(ComparisonResult("files", files_rule, "pass" if not mism else "fail", {"mismatches": mism, "expected_count": len(exp), "actual_count": len(act),
                                                                                     "crlf_normalized_files": sorted(crlf_files)},
                                    original_hash=sha256_bytes(repr(sorted(exp.items())).encode()), candidate_hash=sha256_bytes(repr(sorted(act.items())).encode()), command=cmd))
    return out


def _first_diff(a: str, b: str) -> int | None:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return None if len(a) == len(b) else min(len(a), len(b))
