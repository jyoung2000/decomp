"""R5 model scoreboard: what each model actually achieved on this PC, per task (docs/AI_LADDER.md section 10).

Rows come from two sources and are never invented:
* ``case``      - every implement / repair attempt (did the answer build? did the verifier pass it?) and every naming call
                  (was a valid name proposed and written as an annotation?) made by the app itself;
* ``benchmark`` - rows imported from ``scripts/benchmark.py --models`` (the R0 corpus run through each configured model; naming
                  answers there are also checked against the corpus ground-truth names).
The ladder presets use the summary: a model that has compiled / named something here is proposed first, an unmeasured model
keeps the size/coder heuristic, and a model that was tried for code and never compiled anything is placed last with a warning.
The verifier stays the only judge of behaviour: ``compiled`` and ``verified`` are copied from the build and the verifier's verdict.
"""
from __future__ import annotations

import json
import statistics
from typing import Any, Iterable, Mapping

from .ids import new_id, now_iso

CODE_TASKS = ("implementation", "repair")
SOURCES = ("case", "benchmark")
MAX_IMPORT_ROWS = 5000
_COLS = ("score_id", "created_at", "source", "task", "provider", "model", "connection_id", "locality", "case_id", "row", "ok", "compiled",
         "verified", "truth_match", "latency_ms", "cost_usd", "cost_known", "tokens_in", "tokens_out", "detail")


def ensure(db: Any) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS model_scores (score_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source TEXT NOT NULL, "
               "task TEXT NOT NULL, provider TEXT, model TEXT NOT NULL, connection_id TEXT, locality TEXT, case_id TEXT, row TEXT, "
               "ok INTEGER NOT NULL DEFAULT 0, compiled INTEGER, verified INTEGER, truth_match INTEGER, latency_ms INTEGER, "
               "cost_usd REAL, cost_known INTEGER NOT NULL DEFAULT 1, tokens_in INTEGER, tokens_out INTEGER, detail TEXT)")
    db.execute("CREATE INDEX IF NOT EXISTS model_scores_model ON model_scores(model, task)")


def _b(v: Any) -> int | None:
    return None if v is None else int(bool(v))


def record(db: Any, *, task: str, model: str | None, ok: bool, provider: str | None = None, connection_id: str | None = None,
           locality: str | None = None, source: str = "case", case_id: str | None = None, row: str | None = None,
           compiled: bool | None = None, verified: bool | None = None, truth_match: bool | None = None, latency_ms: int | None = None,
           cost_usd: float | None = None, cost_known: bool = True, tokens_in: int | None = None, tokens_out: int | None = None,
           detail: Mapping[str, Any] | None = None) -> str | None:
    """Store one measured outcome. Never raises (the scoreboard is informational and must not fail the work it measures)."""
    if not model:
        return None
    try:
        ensure(db)
        sid = new_id("msc")
        db.insert("model_scores", {"score_id": sid, "created_at": now_iso(), "source": source if source in SOURCES else "case",
                                   "task": str(task), "provider": provider, "model": str(model)[:200], "connection_id": connection_id,
                                   "locality": locality, "case_id": case_id, "row": row, "ok": int(bool(ok)), "compiled": _b(compiled),
                                   "verified": _b(verified), "truth_match": _b(truth_match),
                                   "latency_ms": int(latency_ms) if latency_ms is not None else None,
                                   "cost_usd": float(cost_usd) if cost_usd is not None else None, "cost_known": int(bool(cost_known)),
                                   "tokens_in": tokens_in, "tokens_out": tokens_out, "detail": dict(detail or {})})
        return sid
    except Exception:  # noqa: BLE001
        return None


def call_facts(db: Any, call_id: str | None) -> dict[str, Any]:
    """Latency / tokens / cost of one router call (``ai_calls`` row), for a scoreboard record."""
    if not call_id:
        return {}
    try:
        r = db.query_one("SELECT latency_ms, input_tokens, output_tokens, cost_usd, cost_known FROM ai_calls WHERE call_id=?", (call_id,))
    except Exception:  # noqa: BLE001
        return {}
    if not r:
        return {}
    return {"latency_ms": r["latency_ms"], "tokens_in": r["input_tokens"], "tokens_out": r["output_tokens"], "cost_usd": r["cost_usd"],
            "cost_known": bool(r["cost_known"])}


def rows(db: Any, *, model: str | None = None, task: str | None = None, source: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    ensure(db)
    where, args = [], []
    for col, v in (("model", model), ("task", task), ("source", source)):
        if v:
            where.append(f"{col}=?")
            args.append(v)
    sql = "SELECT * FROM model_scores" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
    out = db.query(sql, (*args, int(max(1, min(limit, 5000)))))
    for r in out:
        try:
            r["detail"] = json.loads(r["detail"]) if r.get("detail") else {}
        except ValueError:
            r["detail"] = {}
    return out


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None


def summary(db: Any) -> dict[str, Any]:
    """``{models: [{model, provider, locality, tasks:{<task>:{runs, ok, compiled, verified, truth_match, ...}}, ever_compiled, code_runs}]}``."""
    ensure(db)
    data = db.query("SELECT task, provider, model, locality, source, ok, compiled, verified, truth_match, latency_ms, cost_usd, cost_known "
                    "FROM model_scores")
    by: dict[str, dict[str, Any]] = {}
    for r in data:
        m = by.setdefault(r["model"], {"model": r["model"], "provider": r["provider"], "locality": r["locality"], "tasks": {}, "_lat": {}})
        m["provider"] = m["provider"] or r["provider"]
        m["locality"] = m["locality"] or r["locality"]
        t = m["tasks"].setdefault(r["task"], {"runs": 0, "ok": 0, "compiled": 0, "verified": 0, "truth_checked": 0, "truth_match": 0,
                                              "cost_usd": 0.0, "cost_known": True, "sources": []})
        t["runs"] += 1
        t["ok"] += int(r["ok"] or 0)
        t["compiled"] += int(r["compiled"] or 0)
        t["verified"] += int(r["verified"] or 0)
        if r["truth_match"] is not None:
            t["truth_checked"] += 1
            t["truth_match"] += int(r["truth_match"])
        t["cost_usd"] = round(t["cost_usd"] + float(r["cost_usd"] or 0.0), 6)
        t["cost_known"] = t["cost_known"] and bool(r["cost_known"])
        if r["source"] not in t["sources"]:
            t["sources"].append(r["source"])
        if r["latency_ms"] is not None:
            m["_lat"].setdefault(r["task"], []).append(int(r["latency_ms"]))
    out = []
    for m in by.values():
        lat = m.pop("_lat")
        for task, t in m["tasks"].items():
            ls = lat.get(task) or []
            t["median_latency_ms"] = int(statistics.median(ls)) if ls else None
            t["ok_rate"] = _rate(t["ok"], t["runs"])
            t["compile_rate"] = _rate(t["compiled"], t["runs"]) if task in CODE_TASKS else None
            t["verify_rate"] = _rate(t["verified"], t["runs"]) if task in CODE_TASKS else None
            t["truth_rate"] = _rate(t["truth_match"], t["truth_checked"])
            t["score"] = task_score(task, t)
        code = [m["tasks"][k] for k in CODE_TASKS if k in m["tasks"]]
        m["code_runs"] = sum(t["runs"] for t in code)
        m["ever_compiled"] = any(t["compiled"] for t in code)
        out.append(m)
    out.sort(key=lambda m: (-max([t["score"] for t in m["tasks"].values()] or [0.0]), m["model"]))
    return {"models": out, "rows": len(data)}


def task_score(task: str, t: Mapping[str, Any]) -> float:
    """0..3: code tasks = 2 x verified rate + compiled rate; naming = valid rate + 2 x ground-truth match rate (when checked);
    other tasks = answer rate."""
    runs = int(t.get("runs") or 0)
    if not runs:
        return 0.0
    if task in CODE_TASKS:
        return round(2 * t["verified"] / runs + t["compiled"] / runs, 3)
    s = t["ok"] / runs
    if t.get("truth_checked"):
        s += 2 * t["truth_match"] / t["truth_checked"]
    return round(s, 3)


def measured(summary_doc: Mapping[str, Any], task: str, model: str) -> dict[str, Any] | None:
    """The scoreboard facts for (task, model). Repairs also count implementation runs and vice versa (the same skill: code that builds)."""
    m = next((x for x in summary_doc.get("models") or [] if x["model"] == model), None)
    if m is None:
        return None
    t = m["tasks"].get(task)
    if t is None and task in CODE_TASKS:
        t = m["tasks"].get("repair" if task == "implementation" else "implementation")
    if t is None:
        return None
    return {**t, "ever_compiled": m["ever_compiled"], "code_runs": m["code_runs"]}


def rank_key(summary_doc: Mapping[str, Any], task: str, model: str) -> tuple[int, float]:
    """Sort key part for presets: (bucket, -score). 0 = measured and it worked here, 1 = not measured, 2 = measured, never worked."""
    t = measured(summary_doc, task, model)
    if t is None or not t.get("runs"):
        return (1, 0.0)
    if t["score"] > 0:
        return (0, -t["score"])
    return (2, 0.0)


def describe(task: str, t: Mapping[str, Any] | None) -> str:
    if not t or not t.get("runs"):
        return "not measured here yet"
    if task in CODE_TASKS:
        return (f"{t['runs']} code attempt(s) here: {t['compiled']} compiled, {t['verified']} verified"
                + (f", median {t['median_latency_ms'] / 1000:.0f}s" if t.get("median_latency_ms") else ""))
    out = f"{t['runs']} run(s) here: {t['ok']} usable"
    if t.get("truth_checked"):
        out += f", {t['truth_match']}/{t['truth_checked']} matched the real names"
    if t.get("median_latency_ms"):
        out += f", median {t['median_latency_ms'] / 1000:.1f}s"
    return out


def import_rows(db: Any, items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Import benchmark rows (``scripts/benchmark.py --models`` -> ``model_scoreboard.rows``). Each row needs task, model, ok."""
    n, bad = 0, []
    for i, r in enumerate(items):
        if i >= MAX_IMPORT_ROWS:
            bad.append(f"more than {MAX_IMPORT_ROWS} rows: the rest was ignored")
            break
        if not isinstance(r, Mapping) or not isinstance(r.get("task"), str) or not isinstance(r.get("model"), str) or "ok" not in r:
            bad.append(f"row {i}: needs task, model and ok")
            continue
        sid = record(db, source="benchmark", task=r["task"][:40], model=r["model"], ok=bool(r["ok"]), provider=r.get("provider"),
                     locality=r.get("locality"), row=str(r.get("row") or "")[:120] or None, compiled=r.get("compiled"),
                     verified=r.get("verified"), truth_match=r.get("truth_match"),
                     latency_ms=r.get("latency_ms") if isinstance(r.get("latency_ms"), (int, float)) else None,
                     cost_usd=r.get("cost_usd") if isinstance(r.get("cost_usd"), (int, float)) else None,
                     cost_known=bool(r.get("cost_known", True)), detail={k: r.get(k) for k in ("function", "proposed", "truth", "measured_at")
                                                                          if r.get(k) is not None})
        n += 1 if sid else 0
    return {"imported": n, "problems": bad[:20]}
