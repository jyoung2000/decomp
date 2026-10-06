"""User-declared behaviour scenarios for command-line programs.

A scenario is "run the program with these arguments / typed input, then compare these outputs". The user declares it
(title, steps, which outputs to compare, normalisation); recording it runs the ORIGINAL program through the existing isolated
runner (``comparators.cli.run_steps``, role="original", which needs recorded per-case consent) and freezes the result through
``Verifier.freeze_baseline``. Frozen baselines are never edited: every recording creates a NEW baseline evidence revision that
carries over the earlier scenarios (``Verifier.load_baseline`` always uses the newest frozen revision).

Storage: table ``user_scenarios`` (created on first use with CREATE TABLE IF NOT EXISTS, so no schema-version bump is needed).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .ids import new_id, now_iso, stable_json_hash
from .sandbox import OriginalExecutionNotPermitted
from .store.db import Database, loads

_DDL = """
CREATE TABLE IF NOT EXISTS user_scenarios (
  scenario_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  title TEXT NOT NULL,
  feature_id TEXT,
  steps TEXT NOT NULL,
  compare TEXT NOT NULL,
  normalize TEXT NOT NULL,
  timeout REAL NOT NULL DEFAULT 60,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  recorded_at TEXT,
  recorded_evidence TEXT,
  recorded_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_scenarios_case ON user_scenarios(case_id);
"""

MAX_STEPS, MAX_ARGS, MAX_ARG_LEN, MAX_STDIN = 20, 64, 4096, 100_000
DEFAULT_COMPARE = {"exit_code": True, "output": True, "files": False}
DEFAULT_NORMALIZE = {"line_endings": True, "trailing_spaces": False, "trim": False, "ignore_timestamps": False}
# ISO-like date-times and clock times; applied through comparators.base.normalize_text "mask:" rules to BOTH sides.
TIMESTAMP_RE = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?|\b\d{1,2}:\d{2}:\d{2}\b"


class ScenarioError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400, affected: str | None = None, next_action: str | None = None):
        super().__init__(message)
        self.code, self.status, self.affected, self.next_action = code, status, affected, next_action


def clean_definition(d: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise a scenario definition (title, feature_id, steps, compare, normalize, timeout)."""
    title = str(d.get("title") or "").strip()
    if not title:
        raise ScenarioError("scenario_title", "The scenario has no title.", affected="title", next_action="Give it a short name such as “Add an item”.")
    if len(title) > 200:
        raise ScenarioError("scenario_title", "The scenario title is longer than 200 characters.", affected="title", next_action="Shorten the title.")
    steps_in = d.get("steps")
    if not isinstance(steps_in, list) or not steps_in:
        raise ScenarioError("scenario_steps", "The scenario has no steps.", affected="steps", next_action="Add at least one step (a run of the program).")
    if len(steps_in) > MAX_STEPS:
        raise ScenarioError("scenario_steps", f"A scenario can have at most {MAX_STEPS} steps.", affected="steps", next_action="Split it into several scenarios.")
    steps = []
    for i, st in enumerate(steps_in):
        args, stdin = (st or {}).get("args", []), (st or {}).get("stdin") or ""
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ScenarioError("scenario_args", f"Step {i + 1}: arguments must be a list of text values.", affected=f"steps[{i}].args", next_action="Enter one argument per item.")
        if len(args) > MAX_ARGS or any(len(a) > MAX_ARG_LEN or "\x00" in a for a in args):
            raise ScenarioError("scenario_args", f"Step {i + 1}: too many or too long arguments.", affected=f"steps[{i}].args", next_action=f"Use at most {MAX_ARGS} arguments of {MAX_ARG_LEN} characters.")
        if not isinstance(stdin, str) or len(stdin) > MAX_STDIN or "\x00" in stdin:
            raise ScenarioError("scenario_stdin", f"Step {i + 1}: the typed input is too long or not text.", affected=f"steps[{i}].stdin", next_action=f"Keep it under {MAX_STDIN} characters.")
        steps.append({"args": list(args), "stdin": stdin})
    compare = {**DEFAULT_COMPARE, **{k: bool(v) for k, v in (d.get("compare") or {}).items() if k in DEFAULT_COMPARE}}
    if not any(compare.values()):
        raise ScenarioError("scenario_compare", "Nothing is selected to compare.", affected="compare", next_action="Choose at least one of: exit code, printed text, files it writes.")
    normalize = {**DEFAULT_NORMALIZE, **{k: bool(v) for k, v in (d.get("normalize") or {}).items() if k in DEFAULT_NORMALIZE}}
    try:
        timeout = 60.0 if d.get("timeout") is None else float(d["timeout"])
    except (TypeError, ValueError):
        timeout = -1.0
    if not (1 <= timeout <= 300):
        raise ScenarioError("scenario_timeout", "The time limit must be between 1 and 300 seconds.", affected="timeout", next_action="Enter a number of seconds from 1 to 300.")
    return {"title": title, "feature_id": d.get("feature_id") or None, "steps": steps, "compare": compare, "normalize": normalize, "timeout": timeout}


def definition_hash(s: dict[str, Any]) -> str:
    """Hash of everything that changes what is run or compared (not the title or the feature link)."""
    return stable_json_hash({k: s[k] for k in ("steps", "compare", "normalize", "timeout")})


def comparator_fields(s: dict[str, Any]) -> dict[str, Any]:
    """Map the plain-language options to the CLI comparator's scenario fields."""
    c, n = s["compare"], s["normalize"]
    channels = (["exit_code"] if c["exit_code"] else []) + (["stdout", "stderr"] if c["output"] else []) + (["files"] if c["files"] else [])
    rules = (["crlf"] if n["line_endings"] else []) + (["trailing_ws"] if n["trailing_spaces"] else []) + (["strip"] if n["trim"] else []) \
        + ([f"mask:{TIMESTAMP_RE}"] if n["ignore_timestamps"] else [])
    return {"channels": channels, "normalize": {"stdout": rules, "stderr": rules}}


class UserScenarioStore:
    def __init__(self, db: Database):
        self.db = db
        with self.db._lock:
            self.db._conn.executescript(_DDL)

    @staticmethod
    def _row(r: dict[str, Any]) -> dict[str, Any]:
        for k in ("steps", "compare", "normalize"):
            r[k] = loads(r[k], [] if k == "steps" else {})
        return r

    def list(self, case_id: str) -> list[dict[str, Any]]:
        return [self._row(r) for r in self.db.query("SELECT * FROM user_scenarios WHERE case_id=? ORDER BY created_at, scenario_id", (case_id,))]

    def get(self, case_id: str, scenario_id: str) -> dict[str, Any]:
        r = self.db.query_one("SELECT * FROM user_scenarios WHERE case_id=? AND scenario_id=?", (case_id, scenario_id))
        if not r:
            raise ScenarioError("scenario_not_found", f"There is no scenario {scenario_id!r} in this project.", status=404, affected=scenario_id,
                                next_action="Refresh the Scenarios list.")
        return self._row(r)

    def create(self, case_id: str, d: dict[str, Any]) -> dict[str, Any]:
        sid, ts = new_id("usc"), now_iso()
        self.db.insert("user_scenarios", {"scenario_id": sid, "case_id": case_id, **d, "created_at": ts, "updated_at": ts})
        return self.get(case_id, sid)

    def update(self, case_id: str, scenario_id: str, d: dict[str, Any]) -> dict[str, Any]:
        self.get(case_id, scenario_id)
        self.db.update("user_scenarios", "scenario_id", scenario_id, {**d, "updated_at": now_iso()})
        return self.get(case_id, scenario_id)

    def delete(self, case_id: str, scenario_id: str) -> None:
        self.get(case_id, scenario_id)
        self.db.execute("DELETE FROM user_scenarios WHERE scenario_id=?", (scenario_id,))

    def mark_recorded(self, scenario_id: str, evidence_id: str, def_hash: str) -> None:
        self.db.update("user_scenarios", "scenario_id", scenario_id, {"recorded_at": now_iso(), "recorded_evidence": evidence_id, "recorded_hash": def_hash})


# ------------------------------------------------------------------------------------------------ status
def scenario_statuses(studio: Any, case_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Annotate scenarios with status (no_baseline | changed | passed | failed | not_run) and counts.

    Verification results are read like outcome.collect_facts: only the newest candidate's report for its current build, and
    nothing is counted when that candidate's verification is stale.
    """
    baseline_ids: set[str] = set()
    try:
        _, bl = studio.verifier.load_baseline(case_id)
        baseline_ids = {sc.get("id") for sc in bl.get("scenarios", [])}
    except Exception:
        pass
    verdicts: dict[str, str] = {}
    cands = studio.candidates.list(case_id)
    cand = cands[-1] if cands else None
    if cand and cand.get("verification") != "stale":
        for ev in reversed(studio.cases.list_evidence(case_id, kind="verification_report", include_stale=True)):
            body = studio.cases.evidence_body(ev["evidence_id"])
            if isinstance(body, dict) and body.get("candidate_id") == cand["candidate_id"] and body.get("build_hash") == cand.get("build_hash"):
                verdicts = {s.get("scenario"): s.get("verdict", "error") for s in body.get("scenarios", [])}
                break
    out = []
    counts = {"declared": len(rows), "no_baseline": 0, "changed": 0, "recorded": 0, "passed": 0, "failed": 0, "not_run": 0}
    for r in rows:
        h = definition_hash(r)
        if not r.get("recorded_hash") or r["scenario_id"] not in baseline_ids:
            status = "no_baseline"
        elif r["recorded_hash"] != h:
            status = "changed"
        else:
            v = verdicts.get(r["scenario_id"])
            status = "not_run" if v is None else ("passed" if v == "pass" else "failed")
        counts[status] += 1
        out.append({**r, "status": status})
    counts["recorded"] = counts["passed"] + counts["failed"] + counts["not_run"]   # has a current baseline entry
    return {"scenarios": out, "counts": counts}


# ------------------------------------------------------------------------------------------------ recording
def resolve_launch(studio: Any, case: dict[str, Any], prev: dict[str, Any] | None, program: str | None) -> dict[str, Any]:
    lp = case.get("launch_profile") or {}
    root = Path(case["source_root"])
    if program:
        p = (root / program).resolve()
        try:
            p.relative_to(root.resolve())
        except ValueError:
            raise ScenarioError("program_outside", "The program must be inside the project's source folder.", affected=program, next_action="Give a path relative to the source folder.")
        if not p.is_file():
            raise ScenarioError("program_missing", f"The program {program!r} was not found in the source folder.", affected=program, next_action="Check the path (relative to the source folder).")
        rel = p.relative_to(root.resolve()).as_posix()
        return {"type": "dotnet" if rel.lower().endswith(".dll") else "exe", "path": rel}
    if lp.get("launch") and lp.get("kind", "cli") != "web":
        return lp["launch"]
    if prev and prev.get("kind") == "cli" and prev.get("launch"):
        return prev["launch"]
    mods = [m for m in studio.db.query("SELECT rel_path, profile FROM modules WHERE case_id=?", (case["case_id"],)) if m["profile"] in ("native_pe", "dotnet")]
    if len(mods) == 1:
        return {"type": "dotnet" if mods[0]["profile"] == "dotnet" else "exe", "path": mods[0]["rel_path"]}
    raise ScenarioError("launch_unknown", "Rebuild Studio does not know which program to run.", status=409, affected="the original program",
                        next_action="Tell it which file to run (for example pecli.exe, relative to the source folder).")


def record(studio: Any, case_id: str, store: UserScenarioStore, *, scenario_ids: list[str] | None = None, program: str | None = None) -> dict[str, Any]:
    from .cases import require_original_execution_consent
    from .comparators.cli import run_steps, snapshot_work
    case = studio.cases.get_case(case_id)
    all_rows = store.list(case_id)
    rows = [store.get(case_id, s) for s in scenario_ids] if scenario_ids else all_rows
    if not rows:
        raise ScenarioError("no_scenarios", "There are no scenarios to record.", affected="scenarios", next_action="Add a scenario first.")
    lp = case.get("launch_profile") or {}
    if lp.get("kind") == "web":
        raise ScenarioError("web_not_supported", "Scenarios can only be authored for command-line programs so far.", status=409, affected="this web project",
                            next_action="Use the scenarios declared when the project was created.")
    try:
        consent = require_original_execution_consent(case)
    except OriginalExecutionNotPermitted as e:
        raise ScenarioError("original_execution_not_permitted", str(e), status=409, affected="running the original program",
                            next_action="Grant permission for this project (Scenarios or Overview tab), or supply a baseline file instead.") from e
    prev = None
    try:
        _, prev = studio.verifier.load_baseline(case_id)
    except Exception:
        pass
    if prev and prev.get("kind") == "web":
        raise ScenarioError("web_not_supported", "This project's baseline is for a web app; command-line scenarios cannot be added to it.", status=409,
                            affected="the existing baseline", next_action="Use the scenarios declared when the project was created.")
    launch = resolve_launch(studio, case, prev, program)
    root = Path(case["source_root"])
    work_root = studio.cases.case_root(case_id) / "capture-user"
    new_entries, hashes = [], {}
    for r in rows:
        w = work_root / r["scenario_id"]
        try:
            runs = run_steps(launch, root, r["steps"], w, timeout=float(r["timeout"]), setup_files=None, role="original", consent=consent)
        except OriginalExecutionNotPermitted:
            raise
        except Exception as e:
            raise ScenarioError("original_run_failed", f"Running the original for “{r['title']}” failed: {type(e).__name__}: {e}"[:1500], status=502,
                                affected=r["title"], next_action="Check the program path and that the program can run on this computer, then try again.") from e
        if len(runs) < len(r["steps"]) or any(x.get("timed_out") for x in runs):
            raise ScenarioError("original_timed_out", f"The original did not finish “{r['title']}” within {r['timeout']:g} seconds, so nothing was recorded.",
                                status=422, affected=r["title"], next_action="Raise the time limit or simplify the steps, then record again.")
        from .comparators.cli import mask_work_path
        for x in runs:
            x["stdout"], x["stderr"] = mask_work_path(x["stdout"], w), mask_work_path(x["stderr"], w)
        new_entries.append({"id": r["scenario_id"], "feature_id": r["feature_id"], "title": r["title"], "user_declared": True,
                            "steps": [{"args": s["args"], "stdin": s["stdin"]} for s in r["steps"]], "timeout": r["timeout"], **comparator_fields(r),
                            "expected": {"steps": runs, "files": snapshot_work(w)}})
        hashes[r["scenario_id"]] = definition_hash(r)
    existing_ids = {x["scenario_id"] for x in all_rows}
    redone = {e["id"] for e in new_entries}
    kept = [sc for sc in (prev or {}).get("scenarios", []) if sc.get("id") not in redone and (not sc.get("user_declared") or sc.get("id") in existing_ids)]
    bl = {k: v for k, v in (prev or {}).items() if k not in ("scenarios", "frozen", "frozen_at", "launch")}
    bl.update({"kind": "cli", "launch": launch, "scenarios": kept + new_entries})
    bl.setdefault("tolerance", lp.get("tolerance", {}))
    n_prev = len(studio.cases.list_evidence(case_id, kind="baseline"))
    ev = studio.verifier.freeze_baseline(case_id, bl, producer="capture_original", title=f"Baseline revision {n_prev + 1} (with your scenarios)")
    for r in rows:
        store.mark_recorded(r["scenario_id"], ev["evidence_id"], hashes[r["scenario_id"]])
        if r.get("feature_id"):
            try:
                f = studio.ledger.get(r["feature_id"])
                studio.ledger.set_impl(r["feature_id"], f["impl_status"], evidence_ids=[ev["evidence_id"]])
            except KeyError:
                pass
    if prev is not None:
        studio.verifier.invalidate(case_id, "baseline revised: user scenarios recorded")
    studio.events.emit("scenarios.recorded", {"evidence_id": ev["evidence_id"], "recorded": sorted(redone), "scenarios_in_baseline": len(bl["scenarios"])}, case_id=case_id)
    return {"evidence_id": ev["evidence_id"], "baseline_revision": n_prev + 1, "recorded": sorted(redone), "scenarios_in_baseline": len(bl["scenarios"]),
            "previous_baseline_kept": prev is not None}
