"""`rebuildctl`: command line front end over StudioServices (same typed operations the GUI and the MCP server use).

Exit codes: 0 success, 1 the requested operation failed/was incomplete, 2 usage error, 3 controller/environment unavailable.
"""
from __future__ import annotations

import argparse
import html
import inspect
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import __version__
from . import controller_status

ID_RE = re.compile(r"^[a-z]{2,8}_[0-9a-f]{12,40}$")
LANGUAGES = ("auto", "rust", "rust_bevy", "web")
OUTPUT_TYPES = ("exe", "installer", "portable", "web", "pwa")
AI_MODES = ("no_ai", "assist_on_failure", "assisted")
ACTIVE_STATES = {"queued", "running", "blocked"}


class CliError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------------------------------- helpers
def _id(value: str) -> str:
    if not ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(f"not a valid id: {value!r}")
    return value


def _jprint(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, default=str, ensure_ascii=False) + "\n")


def _make_settings(data_dir: str | None):
    from ..config import Settings, get_settings, set_settings
    if data_dir:
        s = Settings(data_dir=Path(data_dir))
        set_settings(s)
        return s
    return get_settings()


def _open_studio(args: argparse.Namespace, *, start_runner: bool = False):
    settings = _make_settings(getattr(args, "data_dir", None))
    try:
        from ..services import StudioServices
        return StudioServices(settings, start_runner=start_runner)
    except Exception as exc:  # partial install, locked data dir, ...
        raise CliError(f"controller unavailable: {type(exc).__name__}: {exc}", 3) from exc


def _case_or_job(studio: Any, ident: str) -> dict[str, str]:
    if ident.startswith("job_"):
        return {"job_id": ident}
    if ident.startswith("case_"):
        return {"case_id": ident}
    raise CliError(f"expected a case_... or job_... id, got {ident!r}", 2)


def _counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for j in jobs:
        out[j["state"]] = out.get(j["state"], 0) + 1
    return out


def _job_dicts(studio: Any, case_id: str | None = None, states: list[str] | None = None) -> list[dict[str, Any]]:
    from ..jobs import JobState
    st = [JobState(s) for s in states] if states else None
    return [j.to_dict() for j in studio.jobs.list(case_id, st)]


def _fmt_progress(p: Any) -> str:
    if not isinstance(p, dict) or not p:
        return ""
    done, total = p.get("done"), p.get("total")
    if done is not None:
        return f"{done}/{total if total is not None else '?'}"
    return json.dumps(p, default=str)[:60]


def _table(rows: list[Sequence[Any]], header: Sequence[str]) -> str:
    cols = [header] + [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(str(r[i])) for r in cols) for i in range(len(header))]
    return "\n".join("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip() for r in cols)


# ----------------------------------------------------------------------------------------------- commands
def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        studio = _open_studio(args)
        report = studio.doctor(smoke=args.smoke, verify=args.verify)
        mode = "full"
    except CliError:
        # Backend registry alone needs no database or sub-stores: still useful when the rest of the install is broken.
        try:
            from ..adapters.registry import BackendRegistry
            from ..backends import register_backends
            settings = _make_settings(getattr(args, "data_dir", None))
            reg = BackendRegistry(settings.data_dir)
            register_backends(reg, settings)
            report = reg.doctor(smoke=args.smoke, verify=args.verify)
            mode = "registry-only"
        except Exception as exc:
            raise CliError(f"doctor failed: {type(exc).__name__}: {exc}", 3) from exc
    report = {"mode": mode, "version": __version__, **report}
    if args.json:
        _jprint(report)
        return 0
    rows = []
    for b in report.get("backends", []):
        tools = ", ".join(f"{t['name']}={t['availability']}" for t in b.get("tools", [])) or "-"
        rows.append((b["backend_id"], b["availability"], tools))
    print(_table(rows, ("backend", "availability", "tools")))
    print("summary:", ", ".join(f"{k}={v}" for k, v in report.get("summary", {}).items()), f"(mode: {mode})")
    return 0


def _wait_for_case(studio: Any, case_id: str, args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.timeout if args.timeout else None
    last = ""
    stuck_polls = 0
    try:
        while True:
            jobs = _job_dicts(studio, case_id)
            counts = _counts(jobs)
            line = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no jobs yet"
            if line != last and not args.json:
                print(f"[{time.strftime('%H:%M:%S')}] {line}", file=sys.stderr)
                last = line
            if jobs and not any(s in ACTIVE_STATES for s in counts):
                break
            # Only blocked jobs left (nothing queued or running): nothing will progress until the user acts. Stop waiting
            # and report the blockers instead of sitting until --timeout.
            if jobs and not any(s in ("queued", "running") for s in counts):
                stuck_polls += 1
                if stuck_polls >= 3:
                    break
            else:
                stuck_polls = 0
            if deadline and time.monotonic() > deadline:
                raise CliError(f"timed out after {args.timeout}s waiting for case {case_id}", 1)
            time.sleep(1.0)
    except KeyboardInterrupt:
        studio.cancel(case_id=case_id)
        print("interrupted: cancellation requested; use `rebuildctl resume` to continue", file=sys.stderr)
        return 130
    jobs = _job_dicts(studio, case_id)
    counts = _counts(jobs)
    ok = set(counts) <= {"completed"}
    final = {"case_id": case_id, "jobs": counts, "ok": ok,
             "failed": [{"job_id": j["job_id"], "stage": j["stage"], "error": j.get("error")} for j in jobs if j["state"] == "failed"],
             "blocked": [{"job_id": j["job_id"], "stage": j["stage"], "blocker": j.get("blocker")} for j in jobs if j["state"] == "blocked"]}
    if args.json:
        _jprint(final)
    else:
        print("completed" if ok else f"finished with problems: {counts}")
        for f in final["failed"]:
            print(f"  failed {f['stage']} ({f['job_id']}): {f['error']}")
        for b in final["blocked"]:
            print(f"  blocked {b['stage']} ({b['job_id']}): {b['blocker']}")
    return 0 if ok else 1


def cmd_rebuild(args: argparse.Namespace) -> int:
    studio = _open_studio(args, start_runner=args.wait)
    try:
        ai: dict[str, Any] = {"mode": args.ai}
        if args.budget_usd is not None:
            ai["budget_usd"] = args.budget_usd
        name = args.name or Path(args.source).name or "rebuild"
        case = studio.create_case(name=name, source_root=args.source, output_root=args.output, target_language=args.language,
                                  output_type=args.type, ai_policy=ai, launch_profile={"execute_original": bool(args.execute_original)})
        case_id = case["case_id"]
        started = studio.start_rebuild(case_id)
        if not args.json:
            print(f"case {case_id} created; rebuild scheduled", file=sys.stderr)
        if args.wait:
            return _wait_for_case(studio, case_id, args)
        running = controller_status(studio.settings.data_dir)["running"]
        out = {"case_id": case_id, "started": started, "controller_running": running}
        if args.json:
            _jprint(out)
        else:
            print(case_id)
            if not running:
                print("note: no controller is running; jobs stay queued until the app or `rebuildctl serve` runs "
                      "(or re-run with --wait).", file=sys.stderr)
        return 0
    finally:
        if args.wait:
            studio.stop()


def cmd_status(args: argparse.Namespace) -> int:
    studio = _open_studio(args)
    try:
        case = studio.cases.get_case(args.case_id)
    except KeyError:
        raise CliError(f"unknown case {args.case_id}", 1)
    jobs = _job_dicts(studio, args.case_id)
    out: dict[str, Any] = {"case": {k: case.get(k) for k in ("case_id", "name", "status", "target_language", "output_type", "source_root",
                                                             "output_root", "updated_at")},
                           "jobs": _counts(jobs),
                           "evidence": len(studio.cases.list_evidence(args.case_id))}
    for key, getter in (("progress", lambda: studio.plan.progress(args.case_id)), ("features", lambda: studio.ledger.summary(args.case_id))):
        try:
            out[key] = getter()
        except Exception:
            out[key] = None
    out["controller"] = controller_status(studio.settings.data_dir)
    if args.json:
        _jprint(out)
        return 0
    c = out["case"]
    print(f"{c['case_id']}  {c['name']}  [{c['status']}]  {c['target_language']}/{c['output_type']}")
    print("jobs:", ", ".join(f"{k}={v}" for k, v in sorted(out["jobs"].items())) or "none")
    print("evidence items:", out["evidence"])
    if out["features"]:
        print("features:", json.dumps(out["features"], default=str))
    print("controller:", "running" if out["controller"]["running"] else "not running")
    return 0


def _cancel_resume(op: str, args: argparse.Namespace) -> int:
    studio = _open_studio(args)
    ids = getattr(studio, op)(**_case_or_job(studio, args.id))
    if args.json:
        _jprint({"action": op, "job_ids": ids})
    else:
        print(f"{op}: {len(ids)} job(s)" + ("".join(f"\n  {i}" for i in ids)))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    return _cancel_resume("cancel", args)


def cmd_resume(args: argparse.Namespace) -> int:
    return _cancel_resume("resume", args)


def cmd_jobs(args: argparse.Namespace) -> int:
    studio = _open_studio(args)
    jobs = _job_dicts(studio, args.case, args.state or None)[: args.limit]
    if args.json:
        _jprint(jobs)
        return 0
    rows = [(j["job_id"], j["case_id"], j["stage"], j["state"], j["attempt"], _fmt_progress(j.get("progress")), j.get("blocker") or j.get("error") or "")
            for j in jobs]
    print(_table(rows, ("job", "case", "stage", "state", "try", "progress", "note")))
    return 0


def cmd_evidence_search(args: argparse.Namespace) -> int:
    studio = _open_studio(args)
    rows = studio.search_evidence(args.case_id, args.query, kinds=args.kind or None, limit=args.limit)
    if args.json:
        _jprint(rows)
    else:
        print(_table([(r["evidence_id"], r["kind"], r.get("module_id") or "", r["title"][:70]) for r in rows], ("evidence", "kind", "module", "title")))
    return 0


def cmd_evidence_get(args: argparse.Namespace) -> int:
    studio = _open_studio(args)
    try:
        ev = studio.get_evidence(args.evidence_id, max_bytes=args.max_bytes)
    except KeyError:
        raise CliError(f"unknown evidence {args.evidence_id}", 1)
    _jprint(ev)
    return 0


def _plan_html(plan: dict[str, Any]) -> str:
    esc = lambda v: html.escape("" if v is None else str(v))  # noqa: E731
    rows = "".join(f"<tr><td>{esc(i.get('item_id'))}</td><td>{esc(i.get('kind'))}</td><td>{esc(i.get('title'))}</td><td>{esc(i.get('status'))}</td></tr>"
                   for i in plan.get("items", []))
    prog = html.escape(json.dumps(plan.get("progress"), indent=2, default=str))
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>Project plan</title></head><body>"
            f"<h1>Project plan</h1><p>case {esc(plan.get('case_id'))}, revision {esc(plan.get('revision'))}, exported {esc(plan.get('exported_at'))}</p>"
            f"<table border=\"1\" cellpadding=\"4\"><tr><th>id</th><th>kind</th><th>title</th><th>status</th></tr>{rows}</table>"
            f"<h2>Progress (raw counts)</h2><pre>{prog}</pre></body></html>")


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def cmd_export_plan(args: argparse.Namespace) -> int:
    studio = _open_studio(args)
    try:
        case = studio.cases.get_case(args.case_id)
    except KeyError:
        raise CliError(f"unknown case {args.case_id}", 1)
    plan = studio.plan.export(args.case_id)
    out_root = Path(case["output_root"]).resolve()
    reports = out_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    if reports.resolve().parent != out_root:  # `reports` must not be a link leaving the output root
        raise CliError("reports directory resolves outside the output root", 1)
    jp, hp = reports / "project-plan.json", reports / "project-plan.html"
    _atomic_write(jp, json.dumps(plan, indent=2, default=str))
    _atomic_write(hp, _plan_html(plan))
    res = {"json": str(jp), "html": str(hp), "revision": plan.get("revision")}
    if args.json:
        _jprint(res)
    else:
        print(jp)
        print(hp)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        from ..api.server import serve  # written by the integrator; absent in partial checkouts
    except ImportError as exc:
        raise CliError(f"the HTTP API server is not available in this install ({exc})", 3)
    data_dir = args.data_dir
    kwargs = {"port": args.port, "data_dir": data_dir, "host": "127.0.0.1"}
    params = inspect.signature(serve).parameters
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in params}
    if data_dir:
        os.environ["REBUILD_STUDIO_DATA"] = str(data_dir)
    result = serve(**kwargs)
    if inspect.iscoroutine(result):
        import asyncio
        asyncio.run(result)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from ..mcp.server import main as mcp_main
    return mcp_main([a for a in args.mcp_args if a != "--"])


# ----------------------------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable JSON output")
    common.add_argument("--data-dir", default=None, help="data directory (default: REBUILD_STUDIO_DATA or per-user location)")

    p = argparse.ArgumentParser(prog="rebuildctl", description="Rebuild Studio command line.")
    p.add_argument("--version", action="version", version=f"rebuildctl {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    d = sub.add_parser("doctor", parents=[common], help="report backend tool availability")
    d.add_argument("--smoke", action="store_true", help="run one real operation per tool (→ usable)")
    d.add_argument("--verify", action="store_true", help="run each backend's fixture regression and record it (→ verified, bound to tool versions)")
    d.set_defaults(fn=cmd_doctor)

    r = sub.add_parser("rebuild", parents=[common], help="create a case and start a rebuild")
    r.add_argument("--source", required=True, help="folder with the program to analyse")
    r.add_argument("--output", required=True, help="folder for source/ dist/ evidence/ reports/")
    r.add_argument("--language", choices=LANGUAGES, default="auto")
    r.add_argument("--type", choices=OUTPUT_TYPES, default="exe")
    r.add_argument("--ai", choices=AI_MODES, default="no_ai")
    r.add_argument("--budget-usd", type=float, default=None)
    r.add_argument("--name", default=None)
    r.add_argument("--execute-original", action="store_true", help="allow running the original program for capture")
    r.add_argument("--wait", action="store_true", help="run jobs in this process and wait until they finish")
    r.add_argument("--timeout", type=int, default=0, help="with --wait: give up after N seconds (0 = no limit)")
    r.set_defaults(fn=cmd_rebuild)

    s = sub.add_parser("status", parents=[common], help="case status")
    s.add_argument("case_id", type=_id)
    s.set_defaults(fn=cmd_status)

    for name, fn, text in (("cancel", cmd_cancel, "cancel a case or job"), ("resume", cmd_resume, "resume a case or job")):
        c = sub.add_parser(name, parents=[common], help=text)
        c.add_argument("id", type=_id, help="case_... or job_... id")
        c.set_defaults(fn=fn)

    j = sub.add_parser("jobs", parents=[common], help="list jobs")
    j.add_argument("--case", type=_id, default=None)
    j.add_argument("--state", action="append", choices=("queued", "running", "blocked", "failed", "cancelled", "completed", "needs_retest"))
    j.add_argument("--limit", type=int, default=100)
    j.set_defaults(fn=cmd_jobs)

    e = sub.add_parser("evidence", help="evidence commands")
    esub = e.add_subparsers(dest="evidence_command", required=True, metavar="subcommand")
    es = esub.add_parser("search", parents=[common], help="search a case's evidence")
    es.add_argument("case_id", type=_id)
    es.add_argument("query")
    es.add_argument("--kind", action="append")
    es.add_argument("--limit", type=int, default=25)
    es.set_defaults(fn=cmd_evidence_search)
    eg = esub.add_parser("get", parents=[common], help="print one evidence item")
    eg.add_argument("evidence_id", type=_id)
    eg.add_argument("--max-bytes", type=int, default=64 * 1024)
    eg.set_defaults(fn=cmd_evidence_get)

    x = sub.add_parser("export-plan", parents=[common], help="write project-plan.json/html into <output>/reports")
    x.add_argument("case_id", type=_id)
    x.set_defaults(fn=cmd_export_plan)

    sv = sub.add_parser("serve", parents=[common], help="run the loopback HTTP/WebSocket controller")
    sv.add_argument("--port", type=int, default=0, help="0 = pick a free port (written to controller.json)")
    sv.set_defaults(fn=cmd_serve)

    m = sub.add_parser("mcp", help="run the MCP server on stdio (every argument after `mcp` goes to rebuild-mcp)")
    m.set_defaults(fn=cmd_mcp, mcp_args=[], json=False, data_dir=None)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    forwarded: list[str] = []
    if argv and argv[0] == "mcp":  # argparse cannot forward option-like arguments (--toolset ...) reliably; split them off first
        argv, forwarded = argv[:1], argv[1:]
    args = build_parser().parse_args(argv)
    if forwarded or args.command == "mcp":
        args.mcp_args = forwarded
    try:
        return int(args.fn(args) or 0)
    except CliError as exc:
        if getattr(args, "json", False):
            _jprint({"error": {"message": str(exc), "code": exc.code}})
        else:
            print(f"rebuildctl: {exc}", file=sys.stderr)
        return exc.code
    except KeyError as exc:
        print(f"rebuildctl: unknown id {exc}", file=sys.stderr)
        return 1
    except (ValueError, PermissionError) as exc:
        print(f"rebuildctl: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
