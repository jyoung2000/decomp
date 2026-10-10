"""Readable + machine-readable parity report and project-plan export."""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from ..ids import now_iso


def build_report(st, case_id: str, candidate_id: str | None) -> dict[str, Any]:
    case = st.cases.get_case(case_id)
    feats = st.ledger.list(case_id)
    summary = st.ledger.summary(case_id)
    comps = st.verifier.comparisons(case_id, candidate_id) if candidate_id else []
    cand = st.candidates.get(candidate_id) if candidate_id else None
    jobs = st.jobs.list(case_id)
    unresolved = [{"feature": f["title"], "impl": f["impl_status"], "verify": f["verify_status"], "critical": f["critical"]} for f in feats if f["verify_status"] != "verified" or f["impl_status"] in ("blocked", "unsupported")]
    blockers = [{"job": j.title, "blocker": j.blocker} for j in jobs if j.blocker]
    ai = st.db.query("SELECT provider, model, task, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, SUM(cached_tokens) AS cached_tokens, SUM(cost_usd) AS cost_usd, SUM(1-cost_known) AS unknown_cost FROM ai_calls WHERE case_id=? GROUP BY provider, model, task", (case_id,))
    from ..outcome import case_outcome
    try:
        outcome = case_outcome(st, case_id)
    except Exception:  # noqa: BLE001
        outcome = None
    attempts = []
    for ev in st.cases.list_evidence(case_id, kind="ai_attempt"):
        if (ev.get("meta") or {}).get("counted"):
            b = st.cases.evidence_body(ev["evidence_id"]) or {}
            c = b.get("call") or {}
            attempts.append({"attempt": b.get("attempt"), "candidate_id": b.get("candidate_id"), "model": c.get("model"), "prompt_sha256": c.get("prompt_sha256"), "tokens": c.get("usage"),
                             "cost_usd": c.get("cost_usd"), "cost_known": c.get("cost_known"), "build": (b.get("build") or {}).get("status"), "verdict": (b.get("verdict") or {}).get("state"),
                             "passed": (b.get("verdict") or {}).get("passed"), "scenarios": (b.get("verdict") or {}).get("scenarios"), "evidence_id": ev["evidence_id"]})
    return {
        "outcome": ({"state": outcome["state"], "label": outcome["label"], "scaffold_only": outcome["scaffold_only"], "scope_statement": outcome["scope_statement"],
                     "outstanding": outcome["outstanding"]} if outcome else None), "ai_attempts": attempts,
        "generated_at": now_iso(), "case": {k: case[k] for k in ("case_id", "name", "source_root", "output_root", "target_language", "output_type", "status", "ai_policy")},
        "candidate": ({k: cand[k] for k in ("candidate_id", "revision", "build_hash", "build_status", "verification", "last_known_good")}
                      | {"author": cand["meta"].get("author"), "origin": cand["meta"].get("origin"), "target_language": cand.get("target_language"),
                         "deterministic_repairs": list(cand["meta"].get("deterministic_repairs") or [])}) if cand else None,
        "parity": {"full_parity": summary["full_parity"], "features_total": summary["total"], "verified": summary["verify"]["verified"], "partial": summary["verify"]["partial"],
                   "failed": summary["verify"]["failed"], "untested": summary["verify"]["untested"], "stale": summary["verify"]["stale"],
                   "critical_incomplete": summary["critical_incomplete"], "scope_note": "feature counts are semantic features, not files/functions; undiscovered scope is not counted"},
        "features": [{k: f[k] for k in ("feature_id", "title", "origin", "critical", "impl_status", "verify_status", "verify_candidate", "user_review")} for f in feats],
        "comparisons": [{k: c[k] for k in ("comparison_id", "feature_id", "channel", "rule", "tolerance", "verdict", "original_hash", "candidate_hash", "command", "environment", "created_at")} | {"details": _trim(c["details"])} for c in comps],
        "unresolved": unresolved, "blockers": blockers, "ai_usage": ai, "jobs": st.jobs.counts(case_id),
        "reproduction": {"cli": f"rebuildctl rebuild --source \"{case['source_root']}\" --output \"{case['output_root']}\" --language {case['target_language']} --type {case['output_type']}",
                         "verify": f"rebuildctl verify {case_id} {candidate_id}" if candidate_id else None},
        "host": comps[0]["environment"] if comps else None,
    }


def _trim(d: Any) -> Any:
    s = json.dumps(d, default=str)
    return d if len(s) < 4000 else {"truncated": True, "excerpt": s[:4000]}


def write_reports(st, case_id: str, candidate_id: str | None, rep_dir: Path) -> dict[str, Any]:
    rep = build_report(st, case_id, candidate_id)
    rep_dir.mkdir(parents=True, exist_ok=True)
    (rep_dir / "parity-report.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    (rep_dir / "parity-report.md").write_text(render_markdown(rep), encoding="utf-8")
    (rep_dir / "unresolved.json").write_text(json.dumps({"unresolved": rep["unresolved"], "blockers": rep["blockers"]}, indent=1), encoding="utf-8")
    rep["summary"] = rep["parity"]
    return rep


def render_markdown(rep: dict[str, Any]) -> str:
    p = rep["parity"]
    lines = [f"# Parity report — {rep['case']['name']}", "", f"Generated {rep['generated_at']}. Target: {rep['case']['target_language']} / {rep['case']['output_type']}.", ""]
    o = rep.get("outcome")
    if o:
        lines += [f"**Outcome: {o['label']}** (`{o['state']}`). {o['scope_statement']}"]
        if o["scaffold_only"]:
            lines += ["", "> **SCAFFOLD ONLY.** The delivered program does not implement the original's behaviour; it exits as 'unimplemented'. It is not a remake."]
        lines += [f"- {x}" for x in o["outstanding"]] + [""]
    lines.append(f"**Full parity:** {'YES' if p['full_parity'] else 'NO'}  — features: {p['features_total']} total, {p['verified']} verified, {p['partial']} partial, {p['failed']} failed, {p['untested']} untested, {p['stale']} stale.")
    lines.append(f"_{p['scope_note']}_")
    if p["critical_incomplete"]:
        lines += ["", "## Critical incomplete features"] + [f"- **{c['title']}** — impl {c['impl_status']}, verify {c['verify_status']}" for c in p["critical_incomplete"]]
    if rep["candidate"]:
        c = rep["candidate"]
        lines += ["", f"## Candidate r{c['revision']} `{c['candidate_id']}`", f"build hash `{c['build_hash']}`, build {c['build_status']}, verification **{c['verification']}**, last known good: {c['last_known_good']}"]
        if c.get("origin") == "native_recovered":
            lang = {"csharp": "C#", "java": "Java"}.get(c.get("target_language") or "", c.get("target_language") or "")
            lines.append(f"Native-language rebuild: the {lang} recovered from the original by a decompiler, rebuilt as-is"
                         + (f"; deterministic fixes ({len(c['deterministic_repairs'])}):" if c.get("deterministic_repairs") else "; no fixes were needed."))
            lines += [f"- {x}" for x in c.get("deterministic_repairs") or []]
        if c.get("author"):
            lines.append(f"Source authored by: **{c['author']}**" + (" (an external model client proposed these files through the MCP interface; the verifier decided)" if c["author"] == "model" else ""))
    lines += ["", "## Features", "", "| Feature | Origin | Critical | Implementation | Verification |", "|---|---|---|---|---|"]
    lines += [f"| {f['title']} | {f['origin']} | {'yes' if f['critical'] else ''} | {f['impl_status']} | {f['verify_status']} |" for f in rep["features"]]
    if rep["comparisons"]:
        lines += ["", "## Comparisons", "", "| Feature | Channel | Rule | Verdict |", "|---|---|---|---|"]
        lines += [f"| {c['feature_id'] or ''} | {c['channel']} | {c['rule']} | {c['verdict']} |" for c in rep["comparisons"]]
        if rep["host"]:
            h = rep["host"]
            runners = sorted({str(c.get("details", {}).get("runner")) for c in rep["comparisons"] if isinstance(c.get("details"), dict) and c["details"].get("runner")})
            lines += ["", f"Environment: {h.get('os')} {h.get('os_release')} {h.get('machine')}; runners used: {', '.join(runners) or 'n/a'}; host_certifies_windows={h.get('host_certifies_windows')}"]
    if rep["unresolved"]:
        lines += ["", "## Unresolved"] + [f"- {u['feature']}: impl {u['impl']}, verify {u['verify']}{' (critical)' if u['critical'] else ''}" for u in rep["unresolved"]]
    if rep["blockers"]:
        lines += ["", "## Blockers"] + [f"- {b['job']}: {b['blocker']}" for b in rep["blockers"]]
    if rep.get("ai_attempts"):
        lines += ["", "## AI attempts", "", "| # | Candidate | Model | Prompt sha256 | Tokens in/out | Cost | Build | Verdict |", "|---|---|---|---|---|---|---|---|"]
        for a in rep["ai_attempts"]:
            t = a.get("tokens") or {}
            lines.append(f"| {a['attempt']} | {a.get('candidate_id') or ''} | {a.get('model') or ''} | {(a.get('prompt_sha256') or '')[:12]} | {t.get('input_tokens', '?')}/{t.get('output_tokens', '?')} | "
                         f"${a.get('cost_usd') or 0:.4f}{'' if a.get('cost_known') else ' (est.)'} | {a.get('build')} | {a.get('verdict') or 'n/a'}"
                         f"{' ' + str(a['passed']) + '/' + str(a['scenarios']) if a.get('scenarios') else ''} |")
    lines += ["", "## AI usage"]
    if rep["ai_usage"]:
        lines += [f"- {a['provider']}/{a['model']} ({a['task']}): {a['calls']} calls, {a['input_tokens']} in / {a['output_tokens']} out / {a['cached_tokens']} cached tokens, cost ${a['cost_usd'] or 0:.4f}" + (" (some costs unknown)" if a['unknown_cost'] else "") for a in rep["ai_usage"]]
    else:
        lines.append("- no AI calls were made through the app's model routes for this case" + (" (the candidate was authored by an external client over MCP)" if rep["candidate"] and rep["candidate"].get("author") == "model" else ""))
    lines += ["", "## Reproduction", "", "```", rep["reproduction"]["cli"], "```"]
    return "\n".join(lines) + "\n"


def export_plan(st, case_id: str, rep_dir: Path) -> dict[str, str]:
    rep_dir.mkdir(parents=True, exist_ok=True)
    plan = st.plan.export(case_id)
    plan["feedback"] = st.feedback.list(case_id)
    plan["previews"] = st.previews.list(case_id)
    plan["features"] = st.ledger.list(case_id)
    (rep_dir / "project-plan.json").write_text(json.dumps(plan, indent=1, default=str), encoding="utf-8")
    (rep_dir / "project-plan.html").write_text(render_plan_html(plan), encoding="utf-8")
    return {"json": str(rep_dir / "project-plan.json"), "html": str(rep_dir / "project-plan.html")}


def render_plan_html(plan: dict[str, Any]) -> str:
    e = html.escape
    rows = []
    for it in plan["items"]:
        rows.append(f"<tr><td><code>{e(it['item_id'].split(':', 1)[-1])}</code></td><td>{e(it['title'])}</td><td>{e(it['kind'])}</td><td class='s-{e(it['status'])}'>{e(it['status'])}</td>"
                    f"<td>{e(it.get('owner') or '')}</td><td>{e('; '.join(it['blockers']))}</td><td>{e(', '.join(it['acceptance']))}</td></tr>")
    prog = plan["progress"]
    g = "".join(f"<li><b>{e(k)}</b>: {v['done']} / {v['total'] if v['total'] is not None else 'unknown'} {e(v.get('unit', ''))}</li>" for k, v in prog["groups"].items())
    revs = "".join(f"<li>r{r['revision']} — {e(r['created_at'])} — {e(r['reason'])}</li>" for r in plan["revisions"])
    fb = "".join(f"<li>[{e(f['status'])}] {e(f['classification'])} ({e(f['priority'])}): {e(f['comment'][:200])}</li>" for f in plan.get("feedback", []))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Project plan r{plan['revision']}</title>
<style>body{{font-family:-apple-system,Segoe UI,system-ui,sans-serif;margin:32px;color:#1d1d1f}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #e5e5ea;padding:6px 8px;text-align:left;font-size:14px}}
.s-completed{{color:#1b7f3b}}.s-blocked,.s-failed{{color:#b42318}}.s-running{{color:#0b57d0}}code{{font-size:12px}}</style></head><body>
<h1>Project plan — revision {plan['revision']}</h1><p>Exported {e(plan['exported_at'])}. Scope known: {prog['scope_known']}. ETA: {e(json.dumps(prog['eta']) if prog['eta'] else 'remaining time unknown')}</p>
<h2>Exact progress</h2><ul>{g}</ul><p>Jobs: {e(json.dumps(prog['jobs']))}</p>
<h2>Items</h2><table><tr><th>ID</th><th>Title</th><th>Kind</th><th>Status</th><th>Owner</th><th>Blockers</th><th>Acceptance</th></tr>{''.join(rows)}</table>
<h2>Revisions</h2><ul>{revs}</ul><h2>Feedback</h2><ul>{fb or '<li>none</li>'}</ul></body></html>"""
