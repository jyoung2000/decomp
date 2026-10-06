"""Deterministic routing and the single AI call path.

``AIClient.call`` is the only place model calls are made for case work:
  resolve route -> (optional advisory reorder) -> price -> reserve budget -> send -> settle actual -> log ``ai_calls`` ->
  emit ``ai.call``.
Rules enforced here:
* Nothing is sent before a reservation succeeds. Exhausted budget => no request leaves the process.
* Unknown pricing is never zero: it needs explicit approval and reserves a conservative ceiling.
* Retries are bounded: at most ONE re-send, and only when the failure confirms the request was not processed
  (rate limit, connect failure). Read timeouts and cut-off streams are never re-sent to the same endpoint; their
  reservation is settled at the full reserved amount because the provider may have billed. Moving to the next
  configured fallback is bounded by the route length and each attempt has its own reservation.
* Every string that can carry provider text or secrets is redacted before it is logged or emitted.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from .base import (AmbiguousCompletion, AuthError, InvalidRequest, OnText, ProviderError, ProviderUnavailable, RateLimit,
                   Request, Response, Timeout, Unreachable, Usage, UsageLimit, with_model)
from .connections import ConnectionStore, ConnectionStoreError, HandoffOnly
from .pricing import ApprovalRequired, Price, PriceTable, estimate_request_tokens
from .secrets import install_redacting_filter, redact
from ..budget import BudgetExhausted, BudgetLedger, DuplicateReservation
from ..events import EventLog
from ..ids import new_id, now_iso
from ..store.db import Database

log = logging.getLogger("rebuild.ai")
install_redacting_filter(log)


class NoRoute(Exception):
    def __init__(self, task: str, skipped: list[dict[str, str]]):
        self.task, self.skipped = task, skipped
        super().__init__(f"no usable route for task {task!r}: " + "; ".join(s.get("reason", "") for s in skipped))


class BudgetRequired(Exception):
    pass


class AllCandidatesFailed(Exception):
    def __init__(self, task: str, attempts: list[dict[str, Any]], last: BaseException | None):
        self.task, self.attempts, self.last = task, attempts, last
        super().__init__(f"all routes failed for task {task!r}: " +
                         "; ".join(f"{a['provider']}:{a['model']} {a['outcome']}" for a in attempts))


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


def record_ai_call(db: Database, events: EventLog, *, call_id: str, task: str, provider: str, model: str, outcome: str,
                   usage: Usage | None = None, cost_usd: float | None = 0.0, cost_known: bool = True, latency_ms: int | None = None,
                   case_id: str | None = None, job_id: str | None = None, extra: dict[str, Any] | None = None) -> None:
    u = usage or Usage()
    db.insert("ai_calls", {"call_id": call_id, "case_id": case_id, "job_id": job_id, "provider": provider, "model": model,
                           "task": task, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                           "cached_tokens": u.cached_tokens, "cost_usd": cost_usd, "cost_known": int(bool(cost_known)),
                           "outcome": outcome, "latency_ms": latency_ms, "created_at": now_iso()})
    payload = {"call_id": call_id, "task": task, "provider": provider, "model": model, "outcome": outcome,
               "input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "cached_tokens": u.cached_tokens,
               "cost_usd": cost_usd, "cost_known": bool(cost_known), "latency_ms": latency_ms}
    for k, v in (extra or {}).items():
        payload[k] = redact(v)[:300] if isinstance(v, str) else v
    events.emit("ai.call", payload, case_id=case_id, job_id=job_id)


# outcome label, release-reservation?, mark connection state
def _classify(e: ProviderError) -> tuple[str, bool]:
    if isinstance(e, AuthError):
        return "auth_failed", True
    if isinstance(e, UsageLimit):
        return "usage_limit", True
    if isinstance(e, RateLimit):
        return "rate_limit", True
    if isinstance(e, Unreachable):
        return "unreachable", True
    if isinstance(e, InvalidRequest):
        return "invalid_request", True
    if isinstance(e, Timeout):
        return ("timeout_not_sent", True) if (e.retry_safe and not e.request_sent) else ("timeout", False)
    if isinstance(e, AmbiguousCompletion):
        return "ambiguous", False
    if isinstance(e, ProviderUnavailable):
        return "unavailable", True
    return "error", False


class AIClient:
    MAX_RESEND = 1

    def __init__(self, connections: ConnectionStore, budgets: BudgetLedger, events: EventLog, db: Database, *,
                 prices: PriceTable | None = None, advisor: Advisor | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_retry_wait_s: float = 30.0,
                 fallback_on_ambiguous: bool = True, monotonic: Callable[[], float] = time.monotonic):
        self.connections, self.budgets, self.events, self.db = connections, budgets, events, db
        self.prices = prices or connections.prices
        self.advisor = advisor
        self._sleep = sleep
        self.max_retry_wait_s = max_retry_wait_s
        self.fallback_on_ambiguous = fallback_on_ambiguous
        self._mono = monotonic

    # ------------------------------------------------------------------ planning
    def plan(self, task: str, request: Request) -> tuple[list[tuple[dict[str, Any], str]], list[dict[str, str]]]:
        """The deterministic candidate order for ``task`` (no spend, no advisor)."""
        return self.connections.resolve_detailed(task, request.needs())

    def _advise(self, task: str, usable: list[tuple[dict[str, Any], str]], request: Request, est_in: int, case_id: str | None,
                job_id: str | None) -> tuple[list[tuple[dict[str, Any], str]], dict[str, Any] | None]:
        if self.advisor is None or len(usable) < 2:
            return usable, None
        cands = []
        for i, (c, m) in enumerate(usable):
            p = self.prices.lookup(c["provider"], m, connection=c)
            cands.append({"index": i, "connection_id": c["connection_id"], "provider": c["provider"], "model": m,
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

    # ------------------------------------------------------------------ the call
    def call(self, task: str, request: Request, *, job_id: str | None = None, case_id: str | None = None,
             budget: str | None = None, approve_unknown_pricing: bool = False, request_key: str | None = None,
             on_text: OnText | None = None, max_retries: int | None = None, retry_unavailable: bool = False,
             backoff_base_s: float = 1.0) -> CallResult:
        """``max_retries``/``retry_unavailable``/``backoff_base_s`` opt in to the unattended-loop policy: up to
        ``max_retries`` re-sends per route with exponential backoff (``Retry-After`` wins when the provider sends it) on
        429 and, with ``retry_unavailable``, on 5xx. 5xx reservations are released (provider processed nothing), so a
        retry never double-reserves. Without them the conservative default (one re-send, 429/connect only) applies."""
        usable, skipped = self.connections.resolve_detailed(task, request.needs())
        if not usable:
            raise NoRoute(task, skipped)
        est_in = estimate_request_tokens(request)
        usable, adv = self._advise(task, usable, request, est_in, case_id, job_id)
        call_id = new_id("call")
        base_key = request_key or f"call:{call_id}"
        attempts: list[dict[str, Any]] = []
        last_exc: BaseException | None = None
        budget_err: BudgetExhausted | None = None
        unpriced: list[tuple[str, str]] = []
        provider_failed = False

        def note(conn: dict[str, Any], model: str, outcome: str, **kw: Any) -> None:
            attempts.append({"connection_id": conn["connection_id"], "provider": conn["provider"], "model": model,
                             "outcome": outcome, **kw})

        for conn, model in usable:
            cid, provider = conn["connection_id"], conn["provider"]
            price = self.prices.lookup(provider, model, connection=conn)
            if price.approval_required and not approve_unknown_pricing:
                unpriced.append((provider, model))
                note(conn, model, "approval_required")
                record_ai_call(self.db, self.events, call_id=new_id("aic"), task=task, provider=provider, model=model,
                               outcome="approval_required", case_id=case_id, job_id=job_id, extra={"connection_id": cid})
                continue
            amount = price.ceiling(est_in, request.max_output_tokens, cache_write=request.cache)
            try:
                adapter = self.connections.adapter(cid)
            except (AuthError, HandoffOnly, ConnectionStoreError) as e:
                last_exc, provider_failed = e, True
                note(conn, model, "auth_failed" if isinstance(e, AuthError) else "no_adapter", error=redact(str(e))[:200])
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
                        note(conn, model, "budget_exhausted", requested=amount)
                        record_ai_call(self.db, self.events, call_id=new_id("aic"), task=task, provider=provider, model=model,
                                       outcome="budget_exhausted", case_id=case_id, job_id=job_id,
                                       extra={"connection_id": cid, "requested_usd": amount})
                        break
                    except DuplicateReservation:
                        raise
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
                    note(conn, model, outcome, error=redact(str(e))[:200], retry=tries)
                    record_ai_call(self.db, self.events, call_id=aic, task=task, provider=provider, model=model, outcome=outcome,
                                   usage=e.partial_usage, cost_usd=cost, cost_known=known, latency_ms=latency, case_id=case_id,
                                   job_id=job_id, extra={"connection_id": cid, "error": str(e), "attempt": tries,
                                                         "retry_safe": e.retry_safe, "error_kind": e.kind})
                    log.info("ai call failed task=%s %s:%s outcome=%s", task, provider, model, outcome)
                    if isinstance(e, AuthError):
                        self.connections.set_state(cid, "auth_failed")
                    elif isinstance(e, UsageLimit):
                        self.connections.set_state(cid, "limited")
                    elif isinstance(e, Unreachable):
                        self.connections.set_state(cid, "unreachable")
                    limit = self.MAX_RESEND if max_retries is None else max(0, int(max_retries))
                    resend = tries <= limit and ((e.retry_safe and isinstance(e, (RateLimit, Timeout, Unreachable)))
                                                 or (retry_unavailable and isinstance(e, ProviderUnavailable) and release))
                    if resend:
                        default_wait = 1.0 if max_retries is None else float(backoff_base_s) * (2 ** (tries - 1))
                        wait = min(e.retry_after if e.retry_after is not None else default_wait, self.max_retry_wait_s)
                        self._sleep(max(0.0, wait))
                        continue
                    if not release and not self.fallback_on_ambiguous:
                        raise AllCandidatesFailed(task, attempts, e) from e
                    break
                except Exception as e:  # not a provider error: we cannot prove nothing was sent, so assume the ceiling was spent
                    if rsv is not None:
                        self.budgets.settle(rsv["reservation_id"], amount, {"outcome": "internal_error", "assumed_spent": True,
                                                                           "error_type": type(e).__name__})
                    record_ai_call(self.db, self.events, call_id=aic, task=task, provider=provider, model=model,
                                   outcome="internal_error", cost_usd=amount if rsv is not None else 0.0, cost_known=False,
                                   latency_ms=int((self._mono() - t0) * 1000), case_id=case_id, job_id=job_id,
                                   extra={"connection_id": cid, "error": f"{type(e).__name__}: {e}"})
                    raise
                latency = int((self._mono() - t0) * 1000)
                cost, known = self._cost(price, resp.usage, amount)
                if rsv is not None:
                    self.budgets.settle(rsv["reservation_id"], cost, {**resp.usage.to_dict(), "cost_known": known})
                note(conn, model, "ok", cost_usd=cost, cost_known=known)
                record_ai_call(self.db, self.events, call_id=aic, task=task, provider=provider, model=model, outcome="ok",
                               usage=resp.usage, cost_usd=cost, cost_known=known, latency_ms=latency, case_id=case_id,
                               job_id=job_id, extra={"connection_id": cid, "attempt": tries, "stop_reason": resp.stop_reason,
                                                     "price_known": price.known})
                if conn["state"] != "ok":
                    self.connections.set_state(cid, "ok")
                return CallResult(resp, cid, provider, model, cost, known, aic, attempts, adv)
        if unpriced and len(unpriced) == len(usable):
            raise ApprovalRequired(unpriced)
        if budget_err is not None and not provider_failed:
            raise budget_err
        raise AllCandidatesFailed(task, attempts, last_exc) from last_exc

    @staticmethod
    def _cost(price: Price, usage: Usage, reserved: float) -> tuple[float, bool]:
        if price.known and price.input_per_mtok == 0 and price.output_per_mtok == 0:
            return 0.0, True
        if usage.reported_cost_usd is not None:
            return float(usage.reported_cost_usd), True
        if usage.known:
            return price.cost(usage), bool(price.known)
        return reserved, False     # usage never reported: assume the ceiling was spent
