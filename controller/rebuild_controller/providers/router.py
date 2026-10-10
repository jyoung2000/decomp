"""Deterministic routing and the single AI call path.

``AIClient.call`` is the only place model calls are made for case work:
  project policy -> resolve ladder (global or project override) -> pre-call skips (policy locality, known capability /
  context window) -> (optional advisory reorder) -> price -> reserve budget -> send -> settle actual -> log ``ai_calls`` ->
  emit ``ai.call`` + plain-English ``ai.activity``.
Rules enforced here (docs/AI_LADDER.md section 1):
* A project with AI policy ``no_ai`` is refused BEFORE any adapter is created: nothing can touch the network.
* Nothing is sent before a reservation succeeds. Exhausted budget => no request leaves the process.
* Unknown pricing is never zero: it needs explicit approval and reserves a conservative ceiling.
* Retries are bounded: at most ONE re-send by default, and only when the failure confirms the request was not processed
  (rate limit, connect failure). Read timeouts and cut-off streams are never re-sent to the same endpoint; their
  reservation is settled at the full reserved amount because the provider may have billed. Moving to the next
  configured fallback is bounded by the ladder length and each attempt has its own reservation.
* Credits exhausted / usage limit / auth / model unavailable / capability: spend released, connection or ladder entry state
  recorded, next candidate. After a FREE candidate runs out of credits the router never silently moves to a paid model: a
  paid fallback must have a known price (or explicit approval) and fit the budget, otherwise it is skipped with a reason.
* Every attempt record carries ``reason`` (plain English), ``position``, ``locality``, ``config_revision`` and the policy
  hash; the winner carries ``took_over_from``.
* Every string that can carry provider text or secrets is redacted before it is logged or emitted.
* R9 (docs/AI_LADDER.md section 9): per-rung failure rules (next rung | wait and retry | stop and ask) and a provider
  cooldown after credits / usage exhaustion that every task honours; ``dry_run`` explains the ladder without sending.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from .base import (AmbiguousCompletion, AuthError, CapabilityError, ContextWindowExceeded, CreditsExhausted, InvalidRequest,
                   Message, ModelUnavailable, OnText,
                   ProviderError, ProviderUnavailable, RateLimit, Request, Response, Timeout, Unreachable, Usage, UsageLimit,
                   with_model)
from .connections import (COOLDOWN_OUTCOMES, ConnectionStore, ConnectionStoreError, HandoffOnly, cooldown_text, locality_of,
                          normalize_task)
from .ladder import (FAILURE_KINDS, TASK_VERB, capabilities as model_caps, chain_text, emit_activity, entry_view, locality_block, money,
                     normalize_policy, override_entries, policy_hash, rule_text)
from .pricing import ApprovalRequired, Price, PriceTable, estimate_request_tokens
from .secrets import install_redacting_filter, redact
from ..budget import BudgetExhausted, BudgetLedger, DuplicateReservation
from ..events import EventLog
from ..ids import new_id, now_iso
from ..store.db import Database, loads

log = logging.getLogger("rebuild.ai")
install_redacting_filter(log)


class NoRoute(Exception):
    def __init__(self, task: str, skipped: list[dict[str, str]], attempts: list[dict[str, Any]] | None = None,
                 recovery: str | None = None):
        self.task, self.skipped = task, skipped
        self.attempts = attempts or []
        self.recovery = recovery or recovery_action(self.attempts) or \
            f"Add a connection and a ladder entry for '{task}' (Settings > AI models), or change the project's AI policy."
        super().__init__(f"no usable route for task {task!r}: " + "; ".join(s.get("reason", "") for s in skipped))


class AIDisabled(NoRoute):
    """The project's AI policy is ``no_ai``: refused before any adapter was created."""

    def __init__(self, task: str):
        super().__init__(task, [{"reason": "project AI policy is no_ai"}],
                         recovery="AI is off for this project. Change the project's AI policy (mode) to use a model.")


class BudgetRequired(Exception):
    pass


class AllCandidatesFailed(Exception):
    def __init__(self, task: str, attempts: list[dict[str, Any]], last: BaseException | None):
        self.task, self.attempts, self.last = task, attempts, last
        self.recovery = recovery_action(attempts)
        super().__init__(f"all routes failed for task {task!r}: " +
                         "; ".join(f"{a['provider']}:{a['model']} {a['outcome']}" for a in attempts))


class StopRequested(AllCandidatesFailed):
    """A per-rung rule said "stop and ask me" for this failure: nothing further is tried; ``message`` is plain English."""

    def __init__(self, task: str, attempts: list[dict[str, Any]], last: BaseException | None, *, kind: str, model: str, label: str,
                 reason: str):
        super().__init__(task, attempts, last)
        self.kind, self.model, self.label = kind, model, label
        what = FAILURE_KINDS.get(kind, (kind.replace("_", " "), False))[0]
        self.message = (f"Stopped and waiting for you: {reason}. Your rule for {model} ({label}) says to stop when it {what} "
                        f"instead of trying the next model. {self.recovery}".strip())


def rule_kind(e: BaseException | None, outcome: str) -> str:
    """The failure-rule kind for an outcome (``context_exceeded`` is split out of ``capability_unsupported``)."""
    if isinstance(e, ContextWindowExceeded):
        return "context_exceeded"
    return {"timeout_not_sent": "unreachable"}.get(outcome, outcome)


class Advisor(Protocol):
    def advise(self, task: str, candidates: list[dict[str, Any]], summary: dict[str, Any], *, case_id: str | None = None,
               job_id: str | None = None) -> Any: ...


@dataclass
class CallResult:
    response: Response
    connection_id: str
    provider: str
    model: str
    cost_usd: float
    cost_known: bool
    call_id: str
    attempts: list[dict[str, Any]] = field(default_factory=list)
    advisor: dict[str, Any] | None = None
    config_revision: int = 0
    policy_hash: str | None = None
    took_over_from: list[dict[str, Any]] = field(default_factory=list)
    locality: str = "cloud"
    position: int = 1


DETAIL_KEYS = ("connection_id", "reason", "position", "locality", "config_revision", "policy_hash", "took_over_from", "prompt_sha256",
               "attempt", "error_kind", "effective_context", "prompt_tokens", "notes")


def record_ai_call(db: Database, events: EventLog, *, call_id: str, task: str, provider: str, model: str, outcome: str,
                   usage: Usage | None = None, cost_usd: float | None = 0.0, cost_known: bool = True, latency_ms: int | None = None,
                   case_id: str | None = None, job_id: str | None = None, extra: dict[str, Any] | None = None) -> None:
    u = usage or Usage()
    clean = {k: (redact(v)[:300] if isinstance(v, str) else v) for k, v in (extra or {}).items()}
    detail = {k: clean[k] for k in DETAIL_KEYS if k in clean}
    db.insert("ai_calls", {"call_id": call_id, "case_id": case_id, "job_id": job_id, "provider": provider, "model": model,
                           "task": task, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                           "cached_tokens": u.cached_tokens, "cost_usd": cost_usd, "cost_known": int(bool(cost_known)),
                           "outcome": outcome, "latency_ms": latency_ms, "created_at": now_iso(), "detail": detail})
    payload = {"call_id": call_id, "task": task, "provider": provider, "model": model, "outcome": outcome,
               "input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "cached_tokens": u.cached_tokens,
               "cost_usd": cost_usd, "cost_known": bool(cost_known), "latency_ms": latency_ms}
    payload.update(clean)
    events.emit("ai.call", payload, case_id=case_id, job_id=job_id)


# outcome label, release-reservation?
def _classify(e: ProviderError) -> tuple[str, bool]:
    if isinstance(e, AuthError):
        return "auth_failed", True
    if isinstance(e, CreditsExhausted):
        return "credits_exhausted", True
    if isinstance(e, UsageLimit):
        return "usage_limit", True
    if isinstance(e, RateLimit):
        return "rate_limit", True
    if isinstance(e, Unreachable):
        return "unreachable", True
    if isinstance(e, ModelUnavailable):
        return "model_unavailable", True
    if isinstance(e, CapabilityError):
        return "capability_unsupported", True
    if isinstance(e, InvalidRequest):
        return "invalid_request", True
    if isinstance(e, Timeout):
        return ("timeout_not_sent", True) if (e.retry_safe and not e.request_sent) else ("timeout", False)
    if isinstance(e, AmbiguousCompletion):
        return "ambiguous", False
    if isinstance(e, ProviderUnavailable):
        return "unavailable", True
    return "error", False


# ====================================================================================== plain-English reasons
def describe(outcome: str, *, model: str, label: str = "", status: int | None = None, retry_after: float | None = None,
             detail: str | None = None, requested: float | None = None) -> str:
    on = f" on {label}" if label else ""
    if outcome == "rate_limit":
        return f"{model}{on} is rate-limited (HTTP 429)" + (f"; it asked to wait {retry_after:g}s" if retry_after is not None else "")
    if outcome == "usage_limit":
        return f"{model}{on} hit its usage limit"
    if outcome == "credits_exhausted":
        return f"{label or model} has run out of credits" + (f" for {model}" if label else "")
    if outcome == "auth_failed":
        return f"{label or model} rejected the API key" if not detail else f"{model}{on} cannot be used ({detail})"
    if outcome == "model_unavailable":
        return f"{model} is not available{on} (model not found)"
    if outcome == "capability_unsupported":
        return f"{model} cannot take this request ({detail or 'unsupported input'})"
    if outcome == "unreachable":
        return f"{label or model} could not be reached (nothing was sent)"
    if outcome == "timeout_not_sent":
        return f"{label or model} timed out before the request was sent"
    if outcome == "unavailable":
        return f"{label or model} returned a server error" + (f" (HTTP {status})" if status else "")
    if outcome == "timeout":
        return (f"{model} timed out after the request was sent; it may have been processed, so it is counted as spent and is "
                f"not sent again")
    if outcome == "ambiguous":
        return f"{model}'s answer was cut off; it may have been processed, so it is counted as spent and is not sent again"
    if outcome == "approval_required":
        return detail or f"{model} has no known price; it is skipped until you set a price or approve unknown pricing"
    if outcome == "budget_exhausted":
        return detail or (f"{model} could cost up to ${requested:.4f}, more than the budget has left" if requested is not None
                          else f"{model} does not fit the budget")
    if outcome == "policy_skipped":
        return f"{model} skipped: {detail or 'excluded by the project AI policy'}"
    if outcome == "invalid_request":
        return f"{label or model} rejected the request as invalid" + (f" ({detail})" if detail else "")
    if outcome in ("unusable", "no_adapter"):
        return f"{model}{on} cannot be used ({detail or 'no API path'})"
    if outcome == "cooldown":
        return detail or f"{label or model} is paused after reporting a limit"
    if outcome == "ok":
        return f"{model} answered"
    return f"{model} failed unexpectedly" + (f" ({detail})" if detail else "")


def recovery_action(attempts: list[dict[str, Any]]) -> str:
    """One precise next step per distinct failing ladder entry, most actionable first."""
    seen: set[tuple[Any, ...]] = set()
    steps: list[str] = []
    for a in attempts:
        o, m, lab = a.get("outcome"), a.get("model"), a.get("connection_label") or a.get("provider") or ""
        key = (o, a.get("connection_id"), m)
        if o == "ok" or key in seen:
            continue
        seen.add(key)
        step = {
            "credits_exhausted": f"add credits to {lab} (or wait for its free tier to reset)",
            "usage_limit": f"wait for {lab}'s usage window to reset or raise its limit",
            "auth_failed": f"update the API key for {lab} in Connections",
            "model_unavailable": (f"pull the model on this PC (ollama pull {m})" if a.get("provider") == "local"
                                  else f"choose a current model instead of {m} on {lab}"),
            "capability_unsupported": f"use a model that can take this input instead of {m}",
            "unreachable": f"start or reconnect {lab}",
            "timeout_not_sent": f"check that {lab} is reachable",
            "unavailable": f"retry later ({lab} had a server error)",
            "rate_limit": f"retry later ({lab} is rate-limited)",
            "timeout": f"check whether {lab} processed the timed-out request before retrying",
            "ambiguous": f"check whether {lab} processed the cut-off request before retrying",
            "approval_required": f"set a price for {m} or approve unknown pricing in the AI policy",
            "budget_exhausted": "raise the project's AI budget",
            "policy_skipped": "change the project's AI locality policy or add a matching model to the ladder",
            "invalid_request": f"check the request settings for {m}",
            "unusable": f"fix or remove the ladder entry {m} on {lab}",
            "no_adapter": f"fix or remove the ladder entry {m} on {lab}",
            "cooldown": f"wait for {lab}'s cooldown to end or clear it in Connections",
        }.get(o or "", f"check {lab}")
        steps.append(step)
    if not steps:
        return ""
    return "To continue: " + "; ".join(dict.fromkeys(steps)) + ". Then resume."


class AIClient:
    MAX_RESEND = 1

    def __init__(self, connections: ConnectionStore, budgets: BudgetLedger, events: EventLog, db: Database, *,
                 prices: PriceTable | None = None, advisor: Advisor | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_retry_wait_s: float = 30.0,
                 fallback_on_ambiguous: bool = True, monotonic: Callable[[], float] = time.monotonic,
                 max_rule_wait_s: float = 4 * 3600.0):
        self.connections, self.budgets, self.events, self.db = connections, budgets, events, db
        self.prices = prices or connections.prices
        self.advisor = advisor
        self._sleep = sleep
        self.max_retry_wait_s = max_retry_wait_s
        self.fallback_on_ambiguous = fallback_on_ambiguous
        self._mono = monotonic
        self.max_rule_wait_s = max_rule_wait_s

    # ------------------------------------------------------------------ planning
    def plan(self, task: str, request: Request) -> tuple[list[tuple[dict[str, Any], str]], list[dict[str, str]]]:
        """The deterministic candidate order for ``task`` (no spend, no advisor)."""
        return self.connections.resolve_detailed(task, request.needs())

    def _policy_for(self, case_id: str | None, policy: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if policy is not None:
            return normalize_policy(policy)
        if case_id:
            row = self.db.query_one("SELECT ai_policy FROM cases WHERE case_id=?", (case_id,))
            if row is not None:
                return normalize_policy(loads(row["ai_policy"], {}))
        return None

    def _advise(self, task: str, usable: list[dict[str, Any]], request: Request, est_in: int, case_id: str | None,
                job_id: str | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        if self.advisor is None or len(usable) < 2:
            return usable, None
        cands = []
        for i, c in enumerate(usable):
            p = self.prices.lookup(c["conn"]["provider"], c["model"], connection=c["conn"])
            cands.append({"index": i, "connection_id": c["conn"]["connection_id"], "provider": c["conn"]["provider"], "model": c["model"],
                          "price_known": p.known, "input_per_mtok": p.input_per_mtok, "output_per_mtok": p.output_per_mtok})
        summary = {"task": task, "needs": sorted(request.needs()), "est_input_tokens": est_in,
                   "max_output_tokens": request.max_output_tokens, "messages": len(request.messages)}
        try:
            d = self.advisor.advise(task, cands, summary, case_id=case_id, job_id=job_id)
            order = list(d.order) if getattr(d, "order", None) else None
            info = d.to_dict() if hasattr(d, "to_dict") else {"source": "advisor"}
        except Exception as e:  # advisory only: never blocks a call
            log.warning("advisor failed (%s); using deterministic order", type(e).__name__)
            return usable, {"source": "fallback", "reason": f"advisor_error:{type(e).__name__}"}
        if not order or sorted(order) != list(range(len(usable))):
            return usable, info
        return [usable[i] for i in order], info

    @staticmethod
    def _precall_skip(cand: dict[str, Any], request: Request, est_in: int, pol: dict[str, Any] | None) -> tuple[str, str] | None:
        """(outcome, detail) when the entry is known to be unusable for this request without sending anything."""
        return AIClient._precall_facts(cand, set(request.needs()), est_in, int(request.max_output_tokens), pol)

    @staticmethod
    def _precall_facts(cand: dict[str, Any], needs: set[str], est_in: int, max_out: int,
                       pol: dict[str, Any] | None) -> tuple[str, str] | None:
        conn, model = cand["conn"], cand["model"]
        if pol is not None:
            why = locality_block(pol, locality_of(conn))
            if why:
                return "policy_skipped", why
        caps = model_caps(conn, model)
        if "images" in needs and caps["vision"] is False:
            return "capability_unsupported", "it cannot take images"
        if "tools" in needs and caps["tools"] is False:
            return "capability_unsupported", "it cannot call tools"
        cw = caps["context_window"]
        if cw and est_in + max_out > cw:
            return "capability_unsupported", (f"the request (about {est_in} input + {max_out} output tokens) does not fit "
                                              f"its {cw}-token context window")
        return None

    # ------------------------------------------------------------------ R9: dry run (no tokens, nothing sent)
    def dry_run(self, task: str, *, case_id: str | None = None, policy: Mapping[str, Any] | None = None, needs: list[str] | None = None,
                est_input_tokens: int = 8000, max_output_tokens: int = 4096, budget: str | None = None,
                approve_unknown_pricing: bool | None = None) -> dict[str, Any]:
        """Which rung would answer right now, and why every other rung would be skipped or waits. Never creates an adapter,
        never reserves budget, never calls an advisor."""
        task = normalize_task(task)
        pol = self._policy_for(case_id, policy)
        rev = self.connections.config_revision()
        out: dict[str, Any] = {"task": task, "config_revision": rev, "policy_hash": policy_hash(pol) if pol is not None else None,
                               "tokens_spent": 0, "sent": False, "answer": None, "rungs": [], "source": "global",
                               "est_input_tokens": int(est_input_tokens), "max_output_tokens": int(max_output_tokens)}
        if pol is not None and pol["mode"] == "no_ai":
            out.update(source="policy", summary="AI is off for this project (policy: no AI); no model would be asked.", chain=chain_text([]))
            return out
        approve = bool(approve_unknown_pricing if approve_unknown_pricing is not None else (pol or {}).get("approve_unknown_pricing"))
        override = override_entries(pol, task) if pol is not None else None
        out["source"] = "project" if override is not None else "global"
        need_set = {n for n in (needs or []) if n in ("images", "tools")}
        left = None
        if budget and self.budgets.exists(budget):
            b = self.budgets.get(budget)
            left = float(b["limit_usd"]) - float(b["spent_usd"]) - float(b.get("reserved_usd") or 0.0)
        ladder = self.connections.ladder_candidates(task, need_set, override)
        views = []
        for cand in ladder:
            conn = cand["conn"]
            label = (conn or {}).get("label", "")
            v = entry_view(self.connections, conn, cand["model"], cand["position"], cand["connection_id"], task)
            views.append(v)
            status, outcome, reason = "standby", None, ""
            if cand["skip"]:
                status, outcome = "skipped", cand["skip_outcome"] or "unusable"
                reason = describe(outcome, model=cand["model"], label=label, detail=cand["skip"])
            else:
                sk = self._precall_facts(cand, need_set, int(est_input_tokens), int(max_output_tokens), pol)
                price = self.prices.lookup(conn["provider"], cand["model"], connection=conn)
                amount = price.ceiling(int(est_input_tokens), int(max_output_tokens))
                if sk:
                    status, outcome = "skipped", sk[0]
                    reason = describe(sk[0], model=cand["model"], label=label, detail=sk[1])
                elif price.approval_required and not approve:
                    status, outcome = "skipped", "approval_required"
                    reason = describe("approval_required", model=cand["model"], label=label)
                elif left is not None and amount > left + 1e-12:
                    status, outcome = "skipped", "budget_exhausted"
                    reason = describe("budget_exhausted", model=cand["model"], requested=amount)
                elif out["answer"] is None:
                    status, outcome = "would_answer", "ok"
                    reason = (f"{cand['model']} would be asked first" + (f" (up to ${amount:.4f})" if amount > 0 else " (no cost)"))
                    out["answer"] = cand["position"]
                else:
                    reason = f"used only if the rungs above fail (up to ${amount:.4f})" if amount > 0 else "used only if the rungs above fail (no cost)"
            rules = v.get("rules") or {}
            out["rungs"].append({**v, "status": status, "outcome": outcome, "reason": reason,
                                 "rules_text": [rule_text(k, r) for k, r in rules.items()]})
        out["chain"] = chain_text(views)
        win = next((r for r in out["rungs"] if r["status"] == "would_answer"), None)
        skipped = [r for r in out["rungs"] if r["status"] == "skipped"]
        if not ladder:
            out["summary"] = f"No model is configured for {task}; nothing would be sent."
        elif win is None:
            out["summary"] = "No rung could answer right now: " + "; ".join(r["reason"] for r in skipped) + "."
        else:
            out["summary"] = (f"Right now position {win['position']} ({win['model']}"
                              + (f", {win['connection_label']}" if win.get("connection_label") else "") + ") would answer"
                              + (f"; skipped: " + "; ".join(r["reason"] for r in skipped) if skipped else "") + ".")
        if self.advisor is not None and hasattr(self.advisor, "enabled_now") and self.advisor.enabled_now():
            out["advisor"] = "JeV may re-order the usable rungs when the real call is made; it never adds a model."
        return out

    # ------------------------------------------------------------------ the call
    def call(self, task: str, request: Request, *, job_id: str | None = None, case_id: str | None = None,
             budget: str | None = None, approve_unknown_pricing: bool = False, request_key: str | None = None,
             on_text: OnText | None = None, max_retries: int | None = None, retry_unavailable: bool = False,
             backoff_base_s: float = 1.0, policy: Mapping[str, Any] | None = None,
             activity: Mapping[str, Any] | None = None, demote: list[tuple[str, str]] | None = None) -> CallResult:
        """``max_retries``/``retry_unavailable``/``backoff_base_s`` opt in to the unattended-loop policy: up to
        ``max_retries`` re-sends per route with exponential backoff (``Retry-After`` wins when the provider sends it) on
        429 and, with ``retry_unavailable``, on 5xx. 5xx reservations are released (provider processed nothing), so a
        retry never double-reserves. Without them the conservative default (one re-send, 429/connect only) applies.

        ``policy`` is the project's effective AI policy (loaded from the case when omitted and ``case_id`` names a case).
        ``activity`` adds context to the activity feed lines (``plan_item_id``, ``subject``, ``origin``, ``candidate_id``).
        ``demote`` moves those (connection_id, model) rungs to the end of the usable order (an advisor's "switch" between repair
        attempts); it can never add a rung."""
        task = normalize_task(task)
        pol = self._policy_for(case_id, policy)
        rev = self.connections.config_revision()
        phash = policy_hash(pol) if pol is not None else None
        sha = request.fingerprint()
        act_ctx = {k: v for k, v in (activity or {}).items() if k in ("plan_item_id", "candidate_id", "evidence_ids", "origin")}
        subject = (activity or {}).get("subject") or "the request"

        def say(text: str, kind: str, **fields: Any) -> None:
            emit_activity(self.events, text, kind=kind, case_id=case_id, job_id=job_id, task=task, config_revision=rev,
                          policy_hash=phash, prompt_sha256=sha, **{**act_ctx, **fields})

        if pol is not None and pol["mode"] == "no_ai":
            # refused before any adapter is created: nothing can reach the network
            say(f"AI is off for this project (policy: no AI); {subject} was not sent to any model.", "refused", outcome="policy_skipped")
            raise AIDisabled(task)
        override = override_entries(pol, task) if pol is not None else None
        ladder = self.connections.ladder_candidates(task, request.needs(), override)
        if not ladder:
            say(f"No model is configured for {task}; nothing was sent.", "refused", outcome="no_route")
            raise NoRoute(task, [{"reason": f"no route configured for task {task!r}"}])
        est_in = estimate_request_tokens(request)
        call_id = new_id("call")
        base_key = request_key or f"call:{call_id}"
        attempts: list[dict[str, Any]] = []
        last_exc: BaseException | None = None
        budget_err: BudgetExhausted | None = None
        unpriced: list[tuple[str, str]] = []
        provider_failed = False
        free_ran_out: str | None = None
        pending: list[str] = []            # failure phrases waiting for "; trying <next>"

        def note(cand: dict[str, Any], outcome: str, reason: str, **kw: Any) -> dict[str, Any]:
            conn = cand.get("conn") or {}
            rec = {"connection_id": cand.get("connection_id"), "connection_label": conn.get("label"), "provider": conn.get("provider"),
                   "model": cand["model"], "outcome": outcome, "reason": reason, "position": cand["position"],
                   "locality": locality_of(conn) if conn else None, "config_revision": rev, "policy_hash": phash, **kw}
            attempts.append(rec)
            if outcome != "ok":
                pending.append(reason)
            return rec

        def start_line(cand: dict[str, Any], paid_note: str = "") -> None:
            conn, model = cand["conn"], cand["model"]
            loc = locality_of(conn)
            where = "local model" if loc == "local" else "cloud model"
            if pending:
                text = "; ".join(pending) + f"; trying {model} ({'runs on this PC' if loc == 'local' else conn['label']})" + paid_note
                say(text, "fallback", provider=conn["provider"], model=model, locality=loc, position=cand["position"],
                    fallback_reason=pending[-1], outcome=attempts[-1]["outcome"] if attempts else None)
                pending.clear()
            else:
                say(f"{TASK_VERB.get(task, 'Working on')} {subject} with {where} {model}" + ("" if loc == "local" else f" ({conn['label']})"),
                    "start", provider=conn["provider"], model=model, locality=loc, position=cand["position"])

        # ---- pre-call skips (nothing sent, nothing reserved)
        usable: list[dict[str, Any]] = []
        for cand in ladder:
            if cand["skip"]:
                o = cand["skip_outcome"] or "unusable"
                note(cand, o, describe(o, model=cand["model"], label=(cand["conn"] or {}).get("label", ""), detail=cand["skip"]))
                if o == "cooldown" and (cand.get("cooldown") or {}).get("scope") == "free_models":
                    free_ran_out = (cand["conn"] or {}).get("label") or free_ran_out    # paid fallbacks keep the free->paid rules
                continue
            sk = self._precall_skip(cand, request, est_in, pol)
            if sk:
                note(cand, sk[0], describe(sk[0], model=cand["model"], label=cand["conn"]["label"], detail=sk[1]))
                record_ai_call(self.db, self.events, call_id=new_id("aic"), task=task, provider=cand["conn"]["provider"], model=cand["model"],
                               outcome=sk[0], case_id=case_id, job_id=job_id,
                               extra={"connection_id": cand["connection_id"], "reason": attempts[-1]["reason"], "position": cand["position"],
                                      "locality": attempts[-1]["locality"], "config_revision": rev, "policy_hash": phash,
                                      "prompt_sha256": sha})
                continue
            usable.append(cand)
        if not usable:
            skipped = [{"connection_id": str(a["connection_id"]), "model": str(a["model"]), "reason": a["reason"]} for a in attempts]
            err = NoRoute(task, skipped, attempts)
            say(("; ".join(pending) if pending else f"No usable model for {task}") + f". Nothing was sent. {err.recovery}", "stopped",
                outcome="no_route")
            raise err
        usable, adv = self._advise(task, usable, request, est_in, case_id, job_id)
        if demote:
            dem = {tuple(d) for d in demote}
            usable = [c for c in usable if (c["connection_id"], c["model"]) not in dem] + \
                     [c for c in usable if (c["connection_id"], c["model"]) in dem]

        def cool_down(cid: str, conn: Mapping[str, Any], outcome: str, why: str, e: ProviderError | None, free: bool = False) -> None:
            if outcome not in COOLDOWN_OUTCOMES:
                return
            c = self.connections.set_cooldown(cid, outcome, reason=why, seconds=getattr(e, "retry_after", None), free_only=free)
            if c is not None and attempts:      # shown as a badge + ``ai.cooldown`` event; the next call's skip reason says it
                attempts[-1]["cooldown_until"] = c["until"]
                attempts[-1]["cooldown_scope"] = c.get("scope")

        for idx, cand in enumerate(usable):
            conn, model = cand["conn"], cand["model"]
            cid, provider, label = conn["connection_id"], conn["provider"], conn["label"]
            loc = locality_of(conn)
            price = self.prices.lookup(provider, model, connection=conn)
            free = bool(price.known and price.input_per_mtok == 0 and price.output_per_mtok == 0)
            base_extra = {"connection_id": cid, "position": cand["position"], "locality": loc, "config_revision": rev,
                          "policy_hash": phash, "prompt_sha256": sha}
            cool = self.connections.cooldown(cid, model)   # set earlier in this call by another rung of the same connection
            if cool is not None:
                note(cand, "cooldown", cooldown_text(conn, cool))
                if cool.get("scope") == "free_models":
                    free_ran_out = label
                continue
            rules = self.connections.rules_for(task, cid, model)
            rule_tries = 0
            if free_ran_out and not free and not price.known and not approve_unknown_pricing:
                why = (f"{free_ran_out} ran out of free credits; {model} costs money and has no known price, so it is not used "
                       f"without your approval")
                note(cand, "approval_required", why)
                unpriced.append((provider, model))
                record_ai_call(self.db, self.events, call_id=new_id("aic"), task=task, provider=provider, model=model,
                               outcome="approval_required", case_id=case_id, job_id=job_id, extra={**base_extra, "reason": why})
                continue
            if price.approval_required and not approve_unknown_pricing:
                unpriced.append((provider, model))
                why = describe("approval_required", model=model, label=label)
                note(cand, "approval_required", why)
                record_ai_call(self.db, self.events, call_id=new_id("aic"), task=task, provider=provider, model=model,
                               outcome="approval_required", case_id=case_id, job_id=job_id, extra={**base_extra, "reason": why})
                continue
            amount = price.ceiling(est_in, request.max_output_tokens, cache_write=request.cache)
            if budget is None and amount > 0 and (provider_failed or free_ran_out):
                why = (f"{model} costs money (up to ${amount:.4f}) but no budget was given, so it is not used"
                       + (f" after {free_ran_out} ran out of free credits" if free_ran_out else ""))
                note(cand, "budget_exhausted", why, requested=amount)
                continue
            try:
                adapter = self.connections.adapter(cid)
            except (AuthError, HandoffOnly, ConnectionStoreError) as e:
                last_exc, provider_failed = e, True
                o = "auth_failed" if isinstance(e, AuthError) else "no_adapter"
                note(cand, o, describe(o, model=model, label=label, detail=redact(str(e))[:120] if o != "auth_failed" else None),
                     error=redact(str(e))[:200])
                if isinstance(e, AuthError):
                    self.connections.set_state(cid, "auth_failed")
                continue
            tries = 0
            while True:
                tries += 1
                rkey = f"{base_key}:{cid}:{model}:{tries}"
                rsv = None
                if budget is None:
                    if amount > 0:
                        raise BudgetRequired(f"{provider}:{model} costs money; pass a budget id (ceiling ${amount:.6f})")
                else:
                    try:
                        rsv = self.budgets.reserve(budget, amount, rkey)
                    except BudgetExhausted as e:
                        budget_err = e
                        why = describe("budget_exhausted", model=model, requested=amount)
                        note(cand, "budget_exhausted", why, requested=amount)
                        record_ai_call(self.db, self.events, call_id=new_id("aic"), task=task, provider=provider, model=model,
                                       outcome="budget_exhausted", case_id=case_id, job_id=job_id,
                                       extra={**base_extra, "requested_usd": amount, "reason": why})
                        break
                    except DuplicateReservation:
                        raise
                if tries == 1:
                    start_line(cand, f" - a paid model, up to ${amount:.4f} from the budget" if free_ran_out and not free else "")
                t0 = self._mono()
                aic = new_id("aic")
                try:
                    resp = adapter.complete(with_model(request, model), on_text)
                except ProviderError as e:
                    last_exc = e
                    latency = int((self._mono() - t0) * 1000)
                    outcome, release = _classify(e)
                    cost, known = (0.0, True) if release else (amount, False)
                    if rsv is not None:
                        usage_doc = {"outcome": outcome, **({"partial_usage": e.partial_usage.to_dict()} if e.partial_usage else {})}
                        if release:
                            self.budgets.release(rsv["reservation_id"])
                        else:
                            self.budgets.settle(rsv["reservation_id"], amount, {**usage_doc, "assumed_spent": True})
                    elif not release:
                        cost, known = 0.0, True
                    provider_failed = True
                    detail = redact(str(e))[:160] if outcome in ("capability_unsupported", "invalid_request") else None
                    why = describe(outcome, model=model, label=label, status=e.status, retry_after=e.retry_after, detail=detail)
                    note(cand, outcome, why, error=redact(str(e))[:200], retry=tries, assumed_spent=not release,
                         cost_usd=cost, cost_known=known)
                    record_ai_call(self.db, self.events, call_id=aic, task=task, provider=provider, model=model, outcome=outcome,
                                   usage=e.partial_usage, cost_usd=cost, cost_known=known, latency_ms=latency, case_id=case_id,
                                   job_id=job_id, extra={**base_extra, "error": str(e), "attempt": tries, "reason": why,
                                                         "retry_safe": e.retry_safe, "error_kind": e.kind})
                    log.info("ai call failed task=%s %s:%s outcome=%s", task, provider, model, outcome)
                    if isinstance(e, AuthError):
                        self.connections.set_state(cid, "auth_failed")
                    elif isinstance(e, CreditsExhausted):
                        self.connections.set_state(cid, "no_credits")
                        if free:
                            free_ran_out = label
                    elif isinstance(e, UsageLimit):
                        self.connections.set_state(cid, "limited")
                    elif isinstance(e, Unreachable):
                        self.connections.set_state(cid, "unreachable")
                    elif isinstance(e, ModelUnavailable):
                        self.connections.mark_model(cid, model, "model_unavailable", detail=str(e))
                    rkind = rule_kind(e, outcome)
                    rule = rules.get(rkind) or {}
                    if rule.get("action") == "stop":
                        cool_down(cid, conn, outcome, why, e, free)
                        stop = StopRequested(task, attempts, e, kind=rkind, model=model, label=label, reason=why)
                        say(stop.message, "stopped", provider=provider, model=model, locality=loc, outcome=outcome, position=cand["position"],
                            fallback_reason=why)
                        raise stop from e
                    if rule.get("action") == "wait" and release:
                        rule_tries += 1
                        if rule_tries <= int(rule.get("max_tries") or 1):
                            mins = float(rule.get("wait_minutes") or 1.0)
                            pending.pop()            # not a fallback: the same rung is tried again
                            say(f"{why}; your rule says wait {mins:g} min and try {model} again ({rule_tries} of {int(rule.get('max_tries') or 1)})",
                                "retry", provider=provider, model=model, locality=loc, outcome=outcome, position=cand["position"],
                                fallback_reason=why)
                            self._sleep(min(mins * 60.0, self.max_rule_wait_s))
                            continue
                        cool_down(cid, conn, outcome, why, e, free)
                        break
                    limit = self.MAX_RESEND if max_retries is None else max(0, int(max_retries))
                    resend = tries <= limit and ((e.retry_safe and isinstance(e, (RateLimit, Timeout, Unreachable))
                                                  and not isinstance(e, UsageLimit))
                                                 or (retry_unavailable and isinstance(e, ProviderUnavailable) and release))
                    if resend:
                        default_wait = 1.0 if max_retries is None else float(backoff_base_s) * (2 ** (tries - 1))
                        wait = min(e.retry_after if e.retry_after is not None else default_wait, self.max_retry_wait_s)
                        pending.pop()            # not a fallback: the same entry is tried again
                        say(f"{why}; waiting {max(0.0, wait):g}s and sending again ({tries + 1} of {limit + 1})", "retry",
                            provider=provider, model=model, locality=loc, outcome=outcome, position=cand["position"],
                            fallback_reason=why)
                        self._sleep(max(0.0, wait))
                        continue
                    if not release and not self.fallback_on_ambiguous:
                        exc = AllCandidatesFailed(task, attempts, e)
                        say(f"{why}. Not trying another model (fallback after an uncertain outcome is off). {exc.recovery}",
                            "stopped", provider=provider, model=model, locality=loc, outcome=outcome)
                        raise exc from e
                    cool_down(cid, conn, outcome, why, e, free)
                    break
                except Exception as e:  # not a provider error: we cannot prove nothing was sent, so assume the ceiling was spent
                    if rsv is not None:
                        self.budgets.settle(rsv["reservation_id"], amount, {"outcome": "internal_error", "assumed_spent": True,
                                                                           "error_type": type(e).__name__})
                    record_ai_call(self.db, self.events, call_id=aic, task=task, provider=provider, model=model,
                                   outcome="internal_error", cost_usd=amount if rsv is not None else 0.0, cost_known=False,
                                   latency_ms=int((self._mono() - t0) * 1000), case_id=case_id, job_id=job_id,
                                   extra={**base_extra, "error": f"{type(e).__name__}: {e}"})
                    raise
                latency = int((self._mono() - t0) * 1000)
                cost, known = self._cost(price, resp.usage, amount)
                if rsv is not None:
                    self.budgets.settle(rsv["reservation_id"], cost, {**resp.usage.to_dict(), "cost_known": known})
                took = _took_over(attempts)
                why = (f"{model} answered" + (f" after {len(took)} other option(s) failed" if took else ""))
                note(cand, "ok", why, cost_usd=cost, cost_known=known, took_over_from=took)
                record_ai_call(self.db, self.events, call_id=aic, task=task, provider=provider, model=model, outcome="ok",
                               usage=resp.usage, cost_usd=cost, cost_known=known, latency_ms=latency, case_id=case_id,
                               job_id=job_id, extra={**base_extra, "attempt": tries, "stop_reason": resp.stop_reason,
                                                     "price_known": price.known, "reason": why, "took_over_from": took,
                                                     **({"effective_context": resp.meta["num_ctx"]} if resp.meta.get("num_ctx") else {}),
                                                     **({"prompt_tokens": resp.meta["prompt_tokens"]} if resp.meta.get("prompt_tokens") else {}),
                                                     **({"notes": "; ".join(resp.notes)[:300]} if resp.notes else {})})
                u = resp.usage
                ctx_txt = f", context {resp.meta['num_ctx']} tokens" if resp.meta.get("num_ctx") else ""
                say(f"{model} answered ({u.input_tokens} tokens in, {u.output_tokens} out, {money(cost, known, free)}, "
                    f"{latency / 1000:.1f}s{ctx_txt})", "answer", provider=provider, model=model, locality=loc, outcome="ok",
                    tokens_in=u.input_tokens, tokens_out=u.output_tokens, cost_usd=cost, cost_known=known, position=cand["position"],
                    took_over_from=took, call_id=aic)
                if conn["state"] != "ok":
                    self.connections.set_state(cid, "ok")
                self.connections.mark_model(cid, model, "ok")
                return CallResult(resp, cid, provider, model, cost, known, aic, attempts, adv, config_revision=rev, policy_hash=phash,
                                  took_over_from=took, locality=loc, position=cand["position"])
        exc_final: Exception
        if unpriced and len(unpriced) == len(usable):
            exc_final = ApprovalRequired(unpriced)
            exc_final.attempts = attempts  # type: ignore[attr-defined]
        elif budget_err is not None and not provider_failed:
            exc_final = budget_err
        else:
            exc_final = AllCandidatesFailed(task, attempts, last_exc)
        rec = recovery_action(attempts)
        say(("; ".join(pending) + "; no other model is left to try. " if pending else "No model answered. ") + rec,
            "stopped", outcome=attempts[-1]["outcome"] if attempts else None, fallback_reason=pending[-1] if pending else None)
        if isinstance(exc_final, AllCandidatesFailed):
            raise exc_final from last_exc
        raise exc_final

    @staticmethod
    def _cost(price: Price, usage: Usage, reserved: float) -> tuple[float, bool]:
        if price.known and price.input_per_mtok == 0 and price.output_per_mtok == 0:
            return 0.0, True
        if usage.reported_cost_usd is not None:
            return float(usage.reported_cost_usd), True
        if usage.known:
            return price.cost(usage), bool(price.known)
        return reserved, False     # usage never reported: assume the ceiling was spent


def _took_over(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The candidates that failed (or were skipped) before the winner, one per ladder position (its final outcome)."""
    by_pos: dict[Any, dict[str, Any]] = {}
    for a in attempts:
        if a["outcome"] == "ok":
            continue
        by_pos[a["position"]] = {"position": a["position"], "connection_id": a["connection_id"], "provider": a["provider"],
                                 "model": a["model"], "outcome": a["outcome"], "reason": a["reason"]}
    return [by_pos[k] for k in sorted(by_pos, key=lambda p: (p is None, p))]
