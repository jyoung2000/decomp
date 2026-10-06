"""JeV advisory router (optional).

JeV only ADVISES the order of candidates the deterministic router already resolved; it can never add a connection or a
model, and every failure path returns the deterministic order. Configuration is environment-only:

  JEV_API_KEY   secret, read from the environment at call time, never stored in the DB/ledger/logs
  JEV_ENDPOINT  base URL of an OpenAI-compatible ``/chat/completions`` service (ASSUMPTION - no JeV API documentation was
                available; the shape is explicit in docs/PROVIDERS.md and every decision is flagged ``unverified``)
  JEV_MODEL     model id sent to that endpoint
  JEV_PRICE_INPUT_PER_MTOK / JEV_PRICE_OUTPUT_PER_MTOK  optional prices; absent => unknown => conservative ceiling

Spend caps are stored as budgets and enforced by the ledger: ``jev:setup`` ($0.05, one-time verification) and
``jev:monthly:<yyyy-mm>`` ($1.00). Decisions are cached by request hash in ``<data_dir>/jev/decisions.json`` (atomic
writes). No request content leaves the machine: only task name, capability needs, token estimates and candidate
descriptors (ids/providers/models/prices).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets as _secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from ..budget import BudgetExhausted, BudgetLedger
from ..events import EventLog
from ..ids import new_id
from .base import (JsonSchema, Message, ProviderError, Request, Usage)
from .openai_responses import OpenAIResponsesAdapter
from .pricing import Price, unknown_price
from .router import record_ai_call
from .secrets import install_redacting_filter, redact, register_secret

log = logging.getLogger("rebuild.jev")
install_redacting_filter(log)

SETUP_CAP_USD = 0.05
MONTHLY_CAP_USD = 1.00
MIN_CONFIDENCE = 0.6
MAX_OUTPUT_TOKENS = 200
MAX_CACHE_ENTRIES = 2000

_SCHEMA = JsonSchema("jev_order", {
    "type": "object",
    "properties": {"order": {"type": "array", "items": {"type": "integer"}},
                   "confidence": {"type": "number"}, "reason": {"type": "string"}},
    "required": ["order", "confidence", "reason"], "additionalProperties": False})

_SYSTEM = ("You are a routing advisor for an offline code-recovery tool. Given a task name, required capabilities, a size "
           "estimate and a numbered list of candidate model routes with prices, return JSON {order, confidence, reason}: "
           "`order` is the list of candidate indices from best to worst for cost-effective success, using ONLY the indices "
           "given; `confidence` is 0..1. If unsure, give low confidence.")


@dataclass
class AdvisorDecision:
    order: list[int] | None
    source: str                     # jev | cache | fallback
    confidence: float | None = None
    reason: str = ""
    unverified: bool = True
    cached: bool = False
    spent_usd: float = 0.0
    key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class JeVConfig:
    api_key: str | None = field(default=None, repr=False)
    endpoint: str | None = None
    model: str | None = None
    price_in: float | None = None
    price_out: float | None = None
    unverified: bool = True         # endpoint/model/pricing come from the environment and are never verified by this code

    @staticmethod
    def from_env(env: Mapping[str, str] | None = None) -> "JeVConfig":
        e = os.environ if env is None else env

        def num(name: str) -> float | None:
            try:
                v = float(e[name])
                return v if v >= 0 else None
            except (KeyError, ValueError):
                return None

        key = e.get("JEV_API_KEY") or None
        if key:
            register_secret(key)
        return JeVConfig(api_key=key, endpoint=(e.get("JEV_ENDPOINT") or "").rstrip("/") or None, model=e.get("JEV_MODEL") or None,
                         price_in=num("JEV_PRICE_INPUT_PER_MTOK"), price_out=num("JEV_PRICE_OUTPUT_PER_MTOK"))

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.endpoint and self.model)

    def missing(self) -> list[str]:
        return [n for n, v in (("JEV_API_KEY", self.api_key), ("JEV_ENDPOINT", self.endpoint), ("JEV_MODEL", self.model)) if not v]

    def price(self) -> Price:
        if self.price_in is not None and self.price_out is not None:
            return Price("jev", self.model or "", self.price_in, self.price_out, known=True,
                         source="environment (JEV_PRICE_*), unverified")
        return unknown_price("jev", self.model or "", "JEV_PRICE_INPUT_PER_MTOK/OUTPUT not set")


class DecisionLedger:
    """JSON file {key: decision}, atomic writes (tmp + fsync + os.replace), tolerant of a corrupt file."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            try:
                self.path.replace(self.path.with_suffix(".corrupt"))
            except OSError:
                pass
            return {}

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._load().get(key)

    def put(self, key: str, decision: dict[str, Any]) -> None:
        with self._lock:
            data = self._load()
            data[key] = decision
            if len(data) > MAX_CACHE_ENTRIES:
                for k in sorted(data, key=lambda k: data[k].get("ts", 0))[: len(data) - MAX_CACHE_ENTRIES]:
                    del data[k]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{_secrets.token_hex(4)}.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)


class JeVRouter:
    def __init__(self, ledger: BudgetLedger, events: EventLog, data_dir: Path | str, *, env: Mapping[str, str] | None = None,
                 transport: httpx.BaseTransport | None = None, now: Callable[[], float] = time.time,
                 min_confidence: float = MIN_CONFIDENCE, timeout_s: float = 15.0):
        self.ledger, self.events = ledger, events
        self.dir = Path(data_dir) / "jev"
        self._env = env
        self.transport = transport
        self._now = now
        self.min_confidence = min_confidence
        self.timeout_s = timeout_s
        self.cache = DecisionLedger(self.dir / "decisions.json")

    # -- config is re-read from the environment at call time (a key set later takes effect; a removed key stops spend)
    def config(self) -> JeVConfig:
        return JeVConfig.from_env(self._env)

    def month_budget_id(self) -> str:
        return "jev:monthly:" + time.strftime("%Y-%m", time.gmtime(self._now()))

    def _ensure_budgets(self) -> None:
        self.ledger.ensure("jev:setup", "jev:setup", SETUP_CAP_USD)
        mb = self.month_budget_id()
        self.ledger.ensure(mb, mb, MONTHLY_CAP_USD)

    def status(self) -> dict[str, Any]:
        cfg = self.config()
        self._ensure_budgets()
        return {"enabled": cfg.enabled, "missing": cfg.missing(), "unverified": cfg.unverified, "endpoint": cfg.endpoint,
                "model": cfg.model, "price_known": cfg.price().known, "caps": {"setup_usd": SETUP_CAP_USD, "monthly_usd": MONTHLY_CAP_USD},
                "setup": self.ledger.get("jev:setup"), "monthly": self.ledger.get(self.month_budget_id())}

    # -- the one JeV request (setup verification and advice share it)
    def _ask(self, cfg: JeVConfig, budget_id: str, task: str, prompt: str, *, case_id: str | None, job_id: str | None) -> tuple[dict[str, Any] | None, float, str]:
        price = cfg.price()
        adapter = OpenAIResponsesAdapter(endpoint=cfg.endpoint, api_key=cfg.api_key, dialect="chat", provider_name="jev",
                                         transport=self.transport, timeout_s=self.timeout_s)
        req = Request(model=cfg.model or "", system=_SYSTEM, messages=[Message.user(prompt)], json_schema=_SCHEMA,
                      max_output_tokens=MAX_OUTPUT_TOKENS, temperature=None)
        from .pricing import estimate_request_tokens
        amount = price.ceiling(estimate_request_tokens(req), MAX_OUTPUT_TOKENS)
        try:
            rsv = self.ledger.reserve(budget_id, amount, f"jev:{new_id('r')}")
        except BudgetExhausted:
            return None, 0.0, "budget_exhausted"
        cid = new_id("aic")
        t0 = time.monotonic()
        try:
            resp = adapter.complete(req)
        except ProviderError as e:
            from .router import _classify
            outcome, release = _classify(e)
            if release:
                self.ledger.release(rsv["reservation_id"])
                spent, known = 0.0, True
            else:
                self.ledger.settle(rsv["reservation_id"], amount, {"assumed_spent": True, "outcome": outcome})
                spent, known = amount, False
            record_ai_call(self.ledger.db, self.events, call_id=cid, task="jev_advice", provider="jev", model=cfg.model or "",
                           outcome=outcome, cost_usd=spent, cost_known=known, latency_ms=int((time.monotonic() - t0) * 1000),
                           case_id=case_id, job_id=job_id, extra={"error": str(e), "budget_id": budget_id})
            return None, spent, f"provider_error:{e.kind}"
        spent = resp.usage.reported_cost_usd if resp.usage.reported_cost_usd is not None else (
            price.cost(resp.usage) if resp.usage.known else amount)
        self.ledger.settle(rsv["reservation_id"], spent, resp.usage.to_dict())
        record_ai_call(self.ledger.db, self.events, call_id=cid, task="jev_advice", provider="jev", model=cfg.model or "", outcome="ok",
                       usage=resp.usage, cost_usd=spent, cost_known=price.known and resp.usage.known,
                       latency_ms=int((time.monotonic() - t0) * 1000), case_id=case_id, job_id=job_id,
                       extra={"budget_id": budget_id, "unverified": True})
        try:
            data = json.loads(resp.text)
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return None, spent, "unparseable_response"
        return data, spent, "ok"

    def setup(self, *, case_id: str | None = None) -> dict[str, Any]:
        """One verification call funded by ``jev:setup`` ($0.05 cap). Records whether the endpoint answered with the
        expected JSON. Does not enable anything by itself."""
        cfg = self.config()
        self._ensure_budgets()
        if not cfg.enabled:
            return {"ok": False, "reason": "not_configured", "missing": cfg.missing()}
        data, spent, why = self._ask(cfg, "jev:setup", "jev_setup", 'Candidates: 0: a/m1, 1: b/m2. Task: setup. Reply order [0,1].',
                                     case_id=case_id, job_id=None)
        ok = bool(data) and isinstance(data.get("order"), list)
        doc = {"ok": ok, "reason": why if not ok else "ok", "spent_usd": spent, "unverified": True, "ts": self._now()}
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "setup.json").write_text(json.dumps(doc), encoding="utf-8")
        return doc

    # -- advice
    @staticmethod
    def _key(task: str, candidates: list[dict[str, Any]], summary: dict[str, Any], model: str | None) -> str:
        doc = {"task": task, "model": model, "summary": {k: summary[k] for k in sorted(summary)},
               "c": [{k: c.get(k) for k in ("connection_id", "provider", "model", "price_known", "input_per_mtok", "output_per_mtok")}
                     for c in candidates]}
        return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()

    def _fallback(self, reason: str, key: str = "") -> AdvisorDecision:
        return AdvisorDecision(order=None, source="fallback", reason=reason, key=key)

    def advise(self, task: str, candidates: list[dict[str, Any]], summary: dict[str, Any], *, case_id: str | None = None,
               job_id: str | None = None) -> AdvisorDecision:
        n = len(candidates)
        if n < 2:
            return self._fallback("single_candidate")
        cfg = self.config()
        key = self._key(task, candidates, summary, cfg.model)
        hit = self.cache.get(key)
        if hit is not None:
            order, conf = hit.get("order"), hit.get("confidence")
            if self._valid(order, n) and isinstance(conf, (int, float)) and conf >= self.min_confidence:
                return AdvisorDecision(order=list(order), source="cache", confidence=float(conf), reason=hit.get("reason", ""),
                                       cached=True, key=key)
            return self._fallback("cached_low_confidence", key)
        if not cfg.enabled:
            return self._fallback("jev_not_configured:" + ",".join(cfg.missing()), key)
        self._ensure_budgets()
        prompt = json.dumps({"task": task, "needs": summary.get("needs"), "est_input_tokens": summary.get("est_input_tokens"),
                             "max_output_tokens": summary.get("max_output_tokens"),
                             "candidates": [{"index": c["index"], "provider": c["provider"], "model": c["model"],
                                             "price_known": c["price_known"], "input_per_mtok": c["input_per_mtok"],
                                             "output_per_mtok": c["output_per_mtok"]} for c in candidates]})
        try:
            data, spent, why = self._ask(cfg, self.month_budget_id(), task, prompt, case_id=case_id, job_id=job_id)
        except Exception as e:  # never let advice break routing
            log.warning("jev failure %s", type(e).__name__)
            return self._fallback(f"jev_error:{type(e).__name__}", key)
        if data is None:
            return AdvisorDecision(order=None, source="fallback", reason=why, spent_usd=spent, key=key)
        order, conf = data.get("order"), data.get("confidence")
        if not self._valid(order, n) or not isinstance(conf, (int, float)) or isinstance(conf, bool) or not 0 <= conf <= 1:
            return AdvisorDecision(order=None, source="fallback", reason="invalid_advice", spent_usd=spent, key=key)
        self.cache.put(key, {"order": order, "confidence": float(conf), "reason": redact(str(data.get("reason", "")))[:300],
                             "ts": self._now(), "unverified": True})
        if conf < self.min_confidence:
            return AdvisorDecision(order=None, source="fallback", confidence=float(conf), reason="low_confidence", spent_usd=spent, key=key)
        return AdvisorDecision(order=list(order), source="jev", confidence=float(conf), reason=redact(str(data.get("reason", "")))[:300],
                               spent_usd=spent, key=key)

    @staticmethod
    def _valid(order: Any, n: int) -> bool:
        return (isinstance(order, list) and len(order) == n and all(isinstance(i, int) and not isinstance(i, bool) for i in order)
                and sorted(order) == list(range(n)))
