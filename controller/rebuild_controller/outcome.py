"""Single derived "outcome" for a case: what was actually achieved, separated into pipeline progress and measured behaviour.

Why this exists: a published file is not a behavioural match. "Delivered" says the output folder was written; it says nothing about
whether the rebuilt program behaves like the original. This module derives one summary from evidence only (scenarios declared,
passed, failed, untested; features with no oracle; unsupported modules; build/deliver flags; scaffold-only flag). Nothing here
writes verdicts (only the Verifier does); it just reads them and refuses to round up.

State model (headline `state`, strongest evidence wins):
  fully_matched     every declared scenario passed on the current candidate (still only within declared coverage)
  partially_matched some declared scenarios passed, others failed/errored/were not run
  tested            scenarios were run and none passed
  scaffolded        the implementation is a scaffold that exits "unimplemented" (never a working remake)
  delivered         output was published, behaviour not measured
  built             a candidate built, not delivered, behaviour not measured
  recovered         original was analysed/recovered, nothing rebuilt yet
  untested          nothing has been measured and nothing has been produced yet
`verification.state` is the behavioural axis on its own: untested | tested | partially_matched | fully_matched.
"""
from __future__ import annotations

from typing import Any

STATES = ("fully_matched", "partially_matched", "tested", "scaffolded", "delivered", "built", "recovered", "untested")

LABELS = {
    "fully_matched": "Fully matched within declared coverage",
    "partially_matched": "Partially matched",
    "tested": "Tested: does not match",
    "scaffolded": "Scaffold only: not a working remake",
    "delivered": "Delivered — not verified",
    "built": "Built — not verified",
    "recovered": "Recovered — nothing rebuilt yet",
    "untested": "Nothing measured yet",
}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def derive_outcome(f: dict[str, Any]) -> dict[str, Any]:
    """Pure derivation from collected facts (see collect_facts for the keys). Missing keys mean unknown/none."""
    declared = int(f.get("scenarios_declared") or 0)
    verdicts: list[str] = list(f.get("scenario_verdicts") or [])
    stale = bool(f.get("stale"))
    counted = [] if stale else verdicts            # stale results are never counted as measurements
    passed = sum(1 for v in counted if v == "pass")
    failed = sum(1 for v in counted if v in ("fail", "error"))
    ran = passed + failed
    untested = max(0, declared - ran)
    if ran > declared:                             # a report can never claim more than was declared: clamp, do not trust
        declared = ran
        untested = 0

    if declared == 0 or ran == 0:
        vstate, vverdict = "untested", "untested"
    elif passed == declared and failed == 0 and untested == 0:
        vstate, vverdict = "fully_matched", "verified"
    elif passed > 0:
        vstate, vverdict = "partially_matched", "partial"
    else:
        vstate, vverdict = "tested", "failed"

    features = list(f.get("features") or [])
    no_oracle = [x.get("title") or x.get("id", "") for x in features if not x.get("has_scenario")]
    unsupported = list(f.get("unsupported_modules") or [])
    unknown_scope = bool(f.get("unknown_scope"))
    scaffold_only = bool(f.get("scaffold_only"))
    built, delivered, recovered = bool(f.get("built")), bool(f.get("delivered")), bool(f.get("recovered"))

    if scaffold_only:
        state = "scaffolded"            # a scaffold can never be "matched", whatever else is true
        if vstate == "fully_matched":   # contradictory evidence is surfaced, not rounded up
            vstate, vverdict = "partially_matched", "partial"
    elif vstate != "untested":
        state = vstate
    elif delivered:
        state = "delivered"
    elif built:
        state = "built"
    elif recovered:
        state = "recovered"
    else:
        state = "untested"

    steps_done, steps_total = int(f.get("pipeline_done") or 0), int(f.get("pipeline_total") or 0)
    outstanding: list[str] = []
    if scaffold_only:
        outstanding.append("The implementation is a scaffold that exits as unimplemented; no behaviour has been rebuilt yet.")
    if declared == 0:
        outstanding.append("No scenarios are declared, so no behaviour can be compared. Declare scenarios or provide a baseline.")
    else:
        if stale:
            outstanding.append("Earlier verification results are out of date and are not counted; run verification again.")
        if failed:
            outstanding.append(f"{_plural(failed, 'scenario')} failed or errored.")
        if untested and not stale:
            outstanding.append(f"{_plural(untested, 'declared scenario')} not run yet.")
    if no_oracle:
        outstanding.append(f"{_plural(len(no_oracle), 'discovered feature')} with no scenario to check it against.")
    if unsupported:
        outstanding.append(f"{_plural(len(unsupported), 'unsupported module')}: {', '.join(unsupported[:5])}.")
    if unknown_scope:
        outstanding.append("Discovery is not finished; undiscovered behaviour is not counted anywhere.")

    scope_statement = (f"Scenario results cover only the {_plural(declared, 'declared scenario')}; behaviour outside them is not measured."
                       if declared else "No scenarios are declared, so nothing about behaviour is measured.")
    return {
        "state": state,
        "label": LABELS[state],
        "can_claim_complete": state == "fully_matched",
        "scaffold_only": scaffold_only,
        "verification": {"state": vstate, "verdict": vverdict, "declared": declared, "passed": passed, "failed": failed, "untested": untested,
                         "stale": stale, "text": f"Behavior verified: {passed} of {declared} scenarios" if declared else "Behavior verified: no scenarios declared",
                         "scope": "declared_scenarios_only"},
        "pipeline": {"recovered": recovered, "scaffolded": scaffold_only, "built": built, "delivered": delivered,
                     "steps_done": steps_done, "steps_total": steps_total,
                     "text": f"Pipeline: {steps_done}/{steps_total} steps" if steps_total else "Pipeline: steps not reported"},
        "coverage": {"features_total": len(features), "features_without_oracle": len(no_oracle), "features_without_oracle_titles": no_oracle,
                     "unsupported_modules": len(unsupported), "unsupported_modules_titles": unsupported, "unknown_scope": unknown_scope},
        "outstanding": outstanding,
        "scope_statement": scope_statement,
    }


def collect_facts(studio: Any, case_id: str) -> dict[str, Any]:
    """Read evidence for one case from the studio services. Read-only."""
    case = studio.cases.get_case(case_id)
    items = studio.plan.items(case_id)
    by_short = {i["item_id"].split(":", 1)[-1]: i for i in items}
    milestones = [i for i in items if i["kind"] == "milestone"]
    cands = studio.candidates.list(case_id)
    cand = cands[-1] if cands else None
    meta = (cand or {}).get("meta") or {}
    scaffold_only = bool(cand and meta.get("origin") == "scaffold" and not meta.get("proposed_files") and not meta.get("author"))

    # the declared scenarios come from the frozen baseline (written by trusted producers only)
    scenario_ids: list[str] = []
    scenario_feature_ids: set[str] = set()
    try:
        _, baseline = studio.verifier.load_baseline(case_id)
        for sc in baseline.get("scenarios", []):
            scenario_ids.append(sc.get("id"))
            if sc.get("feature_id"):
                scenario_feature_ids.add(sc["feature_id"])
    except Exception:
        pass

    verdicts: list[str] = []
    stale = False
    if cand:
        stale = cand.get("verification") == "stale"
        for ev in reversed(studio.cases.list_evidence(case_id, kind="verification_report", include_stale=True)):
            body = studio.cases.evidence_body(ev["evidence_id"])
            if isinstance(body, dict) and body.get("candidate_id") == cand["candidate_id"] and body.get("build_hash") == cand.get("build_hash"):
                declared_ids = set(scenario_ids)
                verdicts = [s.get("verdict", "error") for s in body.get("scenarios", []) if not declared_ids or s.get("scenario") in declared_ids]
                break

    feats = studio.ledger.list(case_id)
    features = [{"id": x["feature_id"], "title": x["title"], "has_scenario": x["feature_id"] in scenario_feature_ids, "verify": x["verify_status"]} for x in feats]
    unsupported = [x["title"] for x in feats if x["impl_status"] == "unsupported"] + [i["title"] for i in items if i["kind"] == "unsupported"]
    unknown = any(i["kind"] == "discovery" and i["status"] != "completed" for i in items)
    return {
        "scenarios_declared": len(scenario_ids), "scenario_verdicts": verdicts, "stale": stale,
        "features": features, "unsupported_modules": unsupported, "unknown_scope": unknown, "scaffold_only": scaffold_only,
        "built": bool(cand and cand.get("build_status") == "built"),
        "delivered": case.get("status") == "delivered" or by_short.get("M-DELIVER", {}).get("status") == "completed",
        "recovered": by_short.get("M-RECOVERY", {}).get("status") == "completed",
        "pipeline_done": sum(1 for i in milestones if i["status"] == "completed"), "pipeline_total": len(milestones),
    }


def case_outcome(studio: Any, case_id: str) -> dict[str, Any]:
    return derive_outcome(collect_facts(studio, case_id))
