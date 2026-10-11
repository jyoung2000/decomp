"""R5 model scoreboard over the R0 corpus (used by ``scripts/benchmark.py --models``).

Each configured local model is run through the PRODUCT's own AI paths (connection store, router, ladder, budget, annotation store),
never through a parallel client:

* ``naming``  - the ``ai_naming`` job on a bench row whose ground truth has the real function names: the same functions (largest own
                functions that rizin left with automatic names) for every model; a proposal is scored ``usable`` when a valid name
                was written as a model-proposed annotation, ``exact`` when it equals the real name, ``match`` when it shares a
                meaningful word with it (e.g. ``crc32_update`` vs ``bench_crc32``).
* ``repair``  - one native-language repair (R4 path: AI repairs only what fails): the recovered C# of ``dotnetapp`` with the
                ILSpy null-case rule switched off fails the EOF scenario (8/9); the model gets one attempt; the verifier decides.

Rows: ``{task, model, row, ok, compiled, verified, truth_match, exact, latency_ms, cost_usd, ...}`` - also importable into the app's
scoreboard (``POST /ai/scoreboard/import``). A model that is not pulled is reported as ``not run``, never as 0.
"""
from __future__ import annotations

import json
import re
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / "fixtures" / "bench"
STOP_WORDS = {"get", "set", "do", "the", "of", "to", "and", "func", "function", "fn", "process", "handle", "data", "value", "main",
              "helper", "impl", "internal", "util", "bench", "cmd", "run", "a", "an", "is"}


def _words(name: str) -> set[str]:
    parts = re.split(r"[_\W]+|(?<=[a-z])(?=[A-Z])", name or "")
    return {p.lower() for p in parts if len(p) > 1} - STOP_WORDS


def name_scores(proposed: str | None, truth: str | None) -> tuple[bool, bool]:
    """(exact, match): exact ignores case / leading underscores; match = a shared meaningful word."""
    if not proposed or not truth:
        return False, False
    norm = lambda s: s.lower().lstrip("_")  # noqa: E731
    exact = norm(proposed) == norm(truth)
    pw, tw = _words(proposed), _words(truth)
    # a shared word, or one word abbreviating the other ("hist" / "histogram", "crc" / "crc32"; at least 3 letters)
    near = any(a == b or (min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a))) for a in pw for b in tw)
    return exact, exact or near


def _studio(tmp: Path, timeout: int = 1800):
    from rebuild_controller.config import Limits, Settings, set_settings
    from rebuild_controller.services import StudioServices
    s = Settings(data_dir=tmp / "data", limits=Limits(lease_timeout_seconds=900, worker_heartbeat_seconds=0, max_stage_seconds=timeout))
    s.ensure_dirs()
    set_settings(s)
    st = StudioServices(s)
    st.ai.advisor = None
    return st


def _drain(st, rounds: int = 400) -> None:
    from rebuild_controller.jobs import JobState
    for _ in range(rounds):
        if st.runner.run_pending() == 0 and not st.jobs.list(None, [JobState.QUEUED, JobState.RUNNING]):
            return


def _local(st, endpoint: str) -> dict[str, Any]:
    c = st.connections.create("local", "Ollama (this PC)", endpoint=endpoint, auth_mode="local")
    return st.connections.probe(c["connection_id"])


def pick_naming_functions(row: str, n: int, endpoint: str) -> list[dict[str, Any]]:
    """The same functions for every model: the n largest own functions that rizin leaves with an automatic name."""
    from rebuild_controller.ai_naming import is_auto_name
    truth = json.loads((BENCH / row / "truth" / "truth.json").read_text("utf-8"))
    own = {int(f["start"], 16): f for f in truth["functions"] if f.get("own") and f.get("size", 0) >= 40}
    with tempfile.TemporaryDirectory(prefix="rs-bench-r5-", ignore_cleanup_errors=True) as tmp:
        st = _studio(Path(tmp))
        try:
            sess = st.re.open(path=str(next((BENCH / row / "bin").iterdir())))
            r = st.re.backend().op_functions(st, case_id=sess["case_id"], module_id=sess["module_id"])
            found = {f["offset"]: f for f in r.data.get("functions") or [] if isinstance(f.get("offset"), int)}
        finally:
            st.stop()
    cands = [{"addr": f"0x{a:x}", "truth": t["name"], "size": t["size"], "rizin_name": found[a].get("name")}
             for a, t in own.items() if a in found and is_auto_name(found[a].get("name"))]
    cands.sort(key=lambda c: -c["size"])
    return cands[:n]


def run_naming(model: str, row: str, functions: list[dict[str, Any]], endpoint: str, *, timeout_s: float = 300) -> list[dict[str, Any]]:
    from rebuild_controller.jobs import JobState
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with tempfile.TemporaryDirectory(prefix="rs-bench-r5-", ignore_cleanup_errors=True) as tmp:
        st = _studio(Path(tmp))
        try:
            p = _local(st, endpoint)
            if model not in {m["id"] for m in p["models"]}:
                return [{"task": "naming", "model": model, "row": row, "status": f"not run: {model} is not pulled on this Ollama"}]
            st.connections.set_route("naming", p["connection_id"], model)
            sess = st.re.open(path=str(next((BENCH / row / "bin").iterdir())))
            cid, mid = sess["case_id"], sess["module_id"]
            st.cases.db.update("cases", "case_id", cid, {"ai_policy": {"mode": "assisted", "request_timeout_s": timeout_s, "max_retries": 0}})
            st.jobs.create(cid, "ai_naming", f"bench naming {model}", {"module_id": mid, "functions": [f["addr"] for f in functions],
                                                                       "limit": len(functions)}, max_attempts=1)
            _drain(st)
            job = [j for j in st.jobs.list(cid) if j.stage == "ai_naming"][-1]
            if job.state != JobState.COMPLETED:
                return [{"task": "naming", "model": model, "row": row, "status": f"failed: {str(job.error)[:200]}"}]
            by = {f["addr"]: f for f in functions}
            out = []
            for res in job.result["results"]:
                t = by.get(res["function"], {})
                prop = (res.get("proposed") or {}).get("name")
                exact, match = name_scores(prop, t.get("truth"))
                out.append({"task": "naming", "model": model, "row": row, "status": "ok", "function": res["function"], "truth": t.get("truth"),
                            "proposed": prop, "comment": (res.get("proposed") or {}).get("comment"), "ok": res["status"] == "proposed",
                            "truth_match": match, "exact": exact, "latency_ms": res.get("latency_ms"), "cost_usd": res.get("cost_usd") or 0.0,
                            "locality": "local", "problem": res.get("problem"), "measured_at": stamp})
            return out
        finally:
            st.stop()


def run_repair(model: str, endpoint: str, *, attempts: int = 1, timeout_s: float = 900) -> dict[str, Any]:
    """dotnetapp, IL null-case rule off -> 8/9; one local repair attempt from the recovered C#; the verifier decides."""
    import rebuild_controller.native_rebuild as nr
    from rebuild_controller.jobs import JobState
    sys_path = str(REPO / "scripts")
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    from benchmark import merged_tools_dir   # noqa: E402 - same tool folder merge as the R4 column
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fix = REPO / "fixtures" / "dotnetapp"
    saved = nr.apply_il_switch_rule
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="rs-bench-r5r-", ignore_cleanup_errors=True) as tmp:
        tools, links = merged_tools_dir(Path(tmp) / "tools")
        st = None
        try:
            nr.apply_il_switch_rule = lambda *a, **k: []      # reproduce the ILSpy quirk the AI has to repair
            from rebuild_controller.config import Limits, Settings, set_settings
            from rebuild_controller.services import StudioServices
            s = Settings(data_dir=Path(tmp) / "data", limits=Limits(lease_timeout_seconds=900, worker_heartbeat_seconds=0,
                                                                     max_stage_seconds=int(timeout_s) + 600))
            s.tools_dir = tools
            s.ensure_dirs()
            set_settings(s)
            st = StudioServices(s)
            st.ai.advisor = None
            p = _local(st, endpoint)
            if model not in {m["id"] for m in p["models"]}:
                return {"task": "repair", "model": model, "row": "dotnetapp", "status": f"not run: {model} is not pulled on this Ollama"}
            st.connections.set_route("implementation", p["connection_id"], model)
            st.connections.set_route("repair", p["connection_id"], model)
            case = st.create_case(name="bench-r5-repair", source_root=str(fix / "original"), output_root=str(Path(tmp) / "out"),
                                  target_language="csharp", output_type="exe",
                                  ai_policy={"mode": "assisted", "max_attempts": attempts, "request_timeout_s": timeout_s, "max_retries": 0},
                                  launch_profile={"baseline_file": str(fix / "expected" / "scenarios.json")})
            cid = case["case_id"]
            st.start_rebuild(cid)
            _drain(st)
            jobs = {j.stage: j for j in st.jobs.list(cid)}
            n = jobs.get("native_rebuild")
            if n is None or n.state != JobState.COMPLETED:
                return {"task": "repair", "model": model, "row": "dotnetapp", "status": f"failed: {(n.error if n else 'no native_rebuild')}"[:200]}
            start = n.result
            loop = jobs.get("implement_loop")
            if loop is None:
                return {"task": "repair", "model": model, "row": "dotnetapp", "status": "not run: no AI repair was needed or allowed",
                        "start": {"built": start.get("built"), "passed": start.get("passed"), "scenarios": start.get("scenarios")}}
            res = loop.result or {}
            recs = []
            for ev in st.cases.list_evidence(cid, kind="ai_attempt"):
                if (ev.get("meta") or {}).get("counted"):
                    recs.append(st.cases.evidence_body(ev["evidence_id"]))
            a = recs[-1] if recs else {}
            call = a.get("call") or {}
            lat = st.db.query_one("SELECT latency_ms FROM ai_calls WHERE call_id=?", (call.get("call_id"),)) if call.get("call_id") else None
            v = a.get("verdict") or {}
            return {"task": "repair", "model": model, "row": "dotnetapp (IL null-case rule off)", "status": "ok", "ok": bool(a.get("files")),
                    "compiled": (a.get("build") or {}).get("status") == "built", "verified": bool(res.get("verified")),
                    "passed": v.get("passed"), "scenarios": v.get("scenarios"), "start_passed": start.get("passed"),
                    "build": (a.get("build") or {}).get("status"), "prompt_mode": (a.get("prompt") or {}).get("mode"),
                    "prompt_chars": (a.get("prompt") or {}).get("chars"), "files": a.get("files"), "stop_reason": res.get("stop_reason"),
                    "latency_ms": (lat or {}).get("latency_ms"), "tokens_in": (call.get("usage") or {}).get("input_tokens"),
                    "tokens_out": (call.get("usage") or {}).get("output_tokens"), "cost_usd": call.get("cost_usd") or 0.0, "locality": "local",
                    "seconds": round(time.perf_counter() - t0, 1), "measured_at": stamp}
        finally:
            nr.apply_il_switch_rule = saved
            if st is not None:
                st.stop()
            import os
            for ln in links:
                try:
                    os.rmdir(ln)
                except OSError:
                    pass


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if r.get("status") != "ok":
            continue
        k = (r["model"], r["task"])
        s = out.setdefault(k, {"model": r["model"], "task": r["task"], "runs": 0, "ok": 0, "exact": 0, "match": 0, "compiled": 0, "verified": 0,
                               "_lat": []})
        s["runs"] += 1
        s["ok"] += int(bool(r.get("ok")))
        s["exact"] += int(bool(r.get("exact")))
        s["match"] += int(bool(r.get("truth_match")))
        s["compiled"] += int(bool(r.get("compiled")))
        s["verified"] += int(bool(r.get("verified")))
        if r.get("latency_ms") is not None:
            s["_lat"].append(r["latency_ms"])
    res = []
    for s in out.values():
        lat = s.pop("_lat")
        s["median_latency_s"] = round(statistics.median(lat) / 1000, 1) if lat else None
        res.append(s)
    return sorted(res, key=lambda s: (s["task"], -s["match"] - s["verified"] * 10, s["model"]))


def render(ms: dict[str, Any]) -> list[str]:
    if not ms:
        return []
    L = ["", "## Model scoreboard (R5)", "",
         f"Measured {ms.get('generated_utc')} with the product's own AI paths against {ms.get('endpoint')} ({ms.get('gpu') or 'local GPU'}); "
         f"local models, $0. Naming row `{ms.get('naming_row')}`: the same {ms.get('naming_functions')} functions for every model (the largest own "
         "functions rizin leaves with an automatic name); `match` = shares a meaningful word with the real name, `exact` = the real name. "
         "Repair: one attempt on the recovered C# of dotnetapp with the ILSpy null-case rule switched off (8/9 before the repair); "
         "only the verifier's verdict counts.", "",
         "| Model | Task | Runs | Usable | Matches real name | Exact | Compiled | Verified | Median latency |", "|---|---|---|---|---|---|---|---|---|"]
    for s in ms.get("summary") or []:
        naming = s["task"] == "naming"
        lat = f"{s['median_latency_s']} s" if s.get("median_latency_s") is not None else "-"
        L.append(f"| {s['model']} | {s['task']} | {s['runs']} | {s['ok']} | {s['match'] if naming else '-'} | {s['exact'] if naming else '-'} | "
                 f"{'-' if naming else s['compiled']} | {'-' if naming else s['verified']} | {lat} |")
    nn = [r for r in ms.get("rows") or [] if r.get("task") == "naming" and r.get("status") == "ok"]
    if nn:
        L += ["", "Naming proposals (function: real name -> proposed, latency):", ""]
        for r in nn:
            L.append(f"* {r['model']} {r['function']}: `{r['truth']}` -> `{r.get('proposed') or '(none: ' + str(r.get('problem'))[:60] + ')'}`"
                     f"{' (exact)' if r.get('exact') else ' (match)' if r.get('truth_match') else ''}, "
                     f"{(r.get('latency_ms') or 0) / 1000:.1f} s")
    rr = [r for r in ms.get("rows") or [] if r.get("task") == "repair"]
    for r in rr:
        if r.get("status") != "ok":
            L.append(f"* repair {r['model']}: {r['status']}")
            continue
        L.append(f"* repair {r['model']} on {r['row']}: answer {'usable' if r.get('ok') else 'unusable'}, build {r.get('build')}, verifier "
                 f"{r.get('passed')}/{r.get('scenarios')} (was {r.get('start_passed')}/{r.get('scenarios')}), "
                 f"{'verified' if r.get('verified') else 'not verified'}; prompt {r.get('prompt_mode')} {r.get('prompt_chars')} chars, "
                 f"{r.get('tokens_in')} tokens in / {r.get('tokens_out')} out, {(r.get('latency_ms') or 0) / 1000:.0f} s")
    skipped = [r for r in ms.get("rows") or [] if r.get("status") not in (None, "ok") and r.get("task") == "naming"]
    for r in skipped:
        L.append(f"* naming {r['model']}: {r['status']}")
    L += ["", "Import into the app's scoreboard: `POST /ai/scoreboard/import` with `{\"rows\": <model_scoreboard.rows of benchmark.json>}`; "
          "the ladder presets then propose the measured-best local model per task."]
    return L
