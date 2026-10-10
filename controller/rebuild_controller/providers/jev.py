"""JeV advisor (optional) over the documented TypeSafe contract (docs/AI_LADDER.md section 9).

Contract (the same one the owner's JeV bridge uses, verified against docs.typesafe.ai at 2026-10-03):
  POST https://api.typesafe.ai/v1/systemone      Authorization: Bearer <key>
  body      {model, state, questions}             questions: {<id>: {type: "choice", instructions, criteria: {<choice>: text}}}
  response  {model, answers: {<id>: {type, choice, probabilities: {<choice>: p}, confidence}}, usage: {input_tokens, output_tokens}}
  errors    401/403 unauthenticated, 422 invalid body, 429 throttled / 529 overloaded (back off), other = provider error
  model     pinned ``jev-1.13.0`` (never the ``jev-latest`` alias); the response echoes the versioned id that answered
  price     list price $0.042 per million INPUT tokens (local estimate; output is not billed by the list price)

Invariants (unchanged from the P13 advisor):
* Advisory only: JeV re-orders rungs the deterministic router already resolved and advises retry / switch / stop between
  repair attempts. It never adds a connection or a model and never overrides the verifier.
* Budget-capped: every request reserves against ``jev:monthly:<yyyy-mm>`` (user cap, default $1) or ``jev:setup`` ($0.05
  for the "Test JeV" button) BEFORE it is sent; an exhausted budget sends nothing.
* Deterministic fallback: off, no key, breaker open, budget exhausted, any HTTP/validation failure or low confidence =>
  ``order=None`` / ``advice=None`` and the caller keeps its own order.
* No request content: ``state`` carries only the task name, capability needs, token estimates and candidate descriptors
  (provider / model / runs-on / price / position) - plus, for the between-attempts question, attempt counts and
  build / scenario pass-fail counts. Never prompts, code, file names, program output, connection ids or labels.
* The key lives in the app's credential store (entered in Connections -> JeV advisor), is read at call time, and is never
  logged, returned or written anywhere else. "Use the key from my JeV install" reads ONLY the JeV-documented key file
  (``JEV_SECRET_FILE`` or ``<JEV_RUNTIME_DIR | ~/.jev>/secrets/typesafe.key``) on an explicit user action.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets as _secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from ..budget import BudgetExhausted, BudgetLedger
from ..events import EventLog
from ..ids import new_id
from .router import record_ai_call
from .secrets import SecretStore, install_redacting_filter, redact, register_secret

log = logging.getLogger("rebuild.jev")
install_redacting_filter(log)

API_BASE = "https://api.typesafe.ai/v1"
EVAL_PATH = "/systemone"
MODEL = "jev-1.13.0"
PRICE_PER_MTOK_INPUT = 0.042
SETUP_CAP_USD = 0.05
DEFAULT_MONTHLY_CAP_USD = 1.00
MAX_MONTHLY_CAP_USD = 50.0
MIN_CONFIDENCE = 0.6
MAX_CACHE_ENTRIES = 2000
MAX_RECENT = 50
TIMEOUT_S = 8.0
MAX_TRANSIENT_RETRIES = 1
RETRY_BASE_S = 0.4
MAX_BACKOFF_S = 8.0
BREAKER_THRESHOLD = 4
BREAKER_COOLDOWN_S = 60.0
SECRET_REF = "secret:jev-advisor"
KEY_FILE_MAX_BYTES = 4096
REASSESS_CHOICES = ("RETRY", "SWITCH", "STOP")

# Kept for importers of the P13 module: the monthly default cap.
MONTHLY_CAP_USD = DEFAULT_MONTHLY_CAP_USD


@dataclass
class AdvisorDecision:
    order: list[int] | None
    source: str                     # jev | cache | fallback
    confidence: float | None = None
    reason: str = ""
    cached: bool = False
    spent_usd: float = 0.0
    key: str = ""
    model: str | None = None
    probabilities: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class JeVError(Exception):
    """A typed JeV failure (``code`` is one of the statuses below); never carries the key or raw upstream text."""

    def __init__(self, code: str, message: str, *, spent: bool = False):
        super().__init__(message)
        self.code, self.spent = code, spent


def jev_install_key_path(env: Mapping[str, str] | None = None) -> Path:
    """The key file the JeV bridge documents (core/lib/config.mjs SECRET_FILE)."""
    e = os.environ if env is None else env
    if e.get("JEV_SECRET_FILE"):
        return Path(e["JEV_SECRET_FILE"])
    root = Path(e["JEV_RUNTIME_DIR"]) if e.get("JEV_RUNTIME_DIR") else Path.home() / ".jev"
    return root / "secrets" / "typesafe.key"


def validate_choice(answer: Any, allowed: list[str]) -> tuple[bool, str]:
    """The JeV bridge's client-side validation: unknown choices, missing / non-finite / out-of-range probabilities, a
    distribution that does not sum to 1 (+-0.05) and an out-of-range confidence are rejected."""
    if not isinstance(answer, Mapping):
        return False, "MALFORMED_ANSWER"
    if answer.get("type") != "choice":
        return False, "ANSWER_TYPE_MISMATCH"
    choice = answer.get("choice")
    if not isinstance(choice, str):
        return False, "MISSING_CHOICE"
    if choice not in allowed:
        return False, "UNKNOWN_CHOICE"
    probs = answer.get("probabilities")
    if not isinstance(probs, Mapping):
        return False, "MISSING_PROBABILITIES"
    for k in allowed:
        v = probs.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 or v > 1:
            return False, "INVALID_PROBABILITY"
    total = 0.0
    for v in probs.values():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return False, "INVALID_PROBABILITY"
        total += float(v)
    if abs(total - 1.0) > 0.05:
        return False, "INVALID_DISTRIBUTION"
    conf = answer.get("confidence")
    if conf is not None and (isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf) or not 0 <= conf <= 1):
        return False, "INVALID_CONFIDENCE"
    return True, ""


class _JsonFile:
    """Small JSON document with atomic writes (tmp + fsync + os.replace), tolerant of a corrupt file."""

    def __init__(self, path: Path | str, default: Any):
        self.path = Path(path)
        self.default = default
        self._lock = threading.Lock()

    def _load(self) -> Any:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, type(self.default)) else json.loads(json.dumps(self.default))
        except FileNotFoundError:
            return json.loads(json.dumps(self.default))
        except (OSError, ValueError):
            try:
                self.path.replace(self.path.with_suffix(".corrupt"))
            except OSError:
                pass
            return json.loads(json.dumps(self.default))

    def _save(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{_secrets.token_hex(4)}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def read(self) -> Any:
        with self._lock:
            return self._load()

    def update(self, fn: Callable[[Any], Any]) -> Any:
        with self._lock:
            data = fn(self._load())
            self._save(data)
            return data


class DecisionLedger(_JsonFile):
    """``{key: decision}`` cache of JeV answers (bounded)."""

    def __init__(self, path: Path | str):
        super().__init__(path, {})

    def get(self, key: str) -> dict[str, Any] | None:
        return self.read().get(key)

    def put(self, key: str, decision: dict[str, Any]) -> None:
        def f(data: dict[str, Any]) -> dict[str, Any]:
            data[key] = decision
            if len(data) > MAX_CACHE_ENTRIES:
                for k in sorted(data, key=lambda k: data[k].get("ts", 0))[: len(data) - MAX_CACHE_ENTRIES]:
                    del data[k]
            return data
        self.update(f)


class JeVRouter:
    """The JeV advisor. Name kept for ``services.py`` (``JeVRouter(budgets, events, data_dir)``)."""

    def __init__(self, ledger: BudgetLedger, events: EventLog, data_dir: Path | str, *, secrets: SecretStore | None = None,
                 transport: httpx.BaseTransport | None = None, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, min_confidence: float = MIN_CONFIDENCE, timeout_s: float = TIMEOUT_S,
                 base_url: str = API_BASE, env: Mapping[str, str] | None = None):
        self.ledger, self.events = ledger, events
        self.data_dir = Path(data_dir)
        self.dir = self.data_dir / "jev"
        self._secrets = secrets
        self.transport = transport
        self._now = now
        self._sleep = sleep
        self.min_confidence = min_confidence
        self.timeout_s = timeout_s
        self.base_url = base_url.rstrip("/")
        self._env = env
        self.cache = DecisionLedger(self.dir / "decisions.json")
        self.settings_file = _JsonFile(self.dir / "settings.json", {})
        self.recent_file = _JsonFile(self.dir / "recent.json", [])
        self._breaker = {"failures": 0, "opened_at": 0.0}
        self._blk = threading.Lock()

    # ------------------------------------------------------------------ configuration
    def attach_secrets(self, store: SecretStore) -> None:
        """Share the app's credential store (one writer per file)."""
        self._secrets = store

    @property
    def secrets(self) -> SecretStore:
        if self._secrets is None:
            self._secrets = SecretStore(self.data_dir)
        return self._secrets

    def settings(self) -> dict[str, Any]:
        s = self.settings_file.read()
        cap = s.get("monthly_cap_usd")
        return {"enabled": bool(s.get("enabled", True)),
                "monthly_cap_usd": float(cap) if isinstance(cap, (int, float)) and not isinstance(cap, bool) else DEFAULT_MONTHLY_CAP_USD,
                "key_source": s.get("key_source")}

    def update_settings(self, *, enabled: bool | None = None, monthly_cap_usd: float | None = None) -> dict[str, Any]:
        if monthly_cap_usd is not None:
            try:
                cap = float(monthly_cap_usd)
            except (TypeError, ValueError):
                raise ValueError("monthly_cap_usd must be a number") from None
            if not math.isfinite(cap) or cap < 0 or cap > MAX_MONTHLY_CAP_USD:
                raise ValueError(f"monthly_cap_usd must be between 0 and {MAX_MONTHLY_CAP_USD:g}")

        def f(d: dict[str, Any]) -> dict[str, Any]:
            if enabled is not None:
                d["enabled"] = bool(enabled)
            if monthly_cap_usd is not None:
                d["monthly_cap_usd"] = float(monthly_cap_usd)
            return d
        self.settings_file.update(f)
        self._ensure_budgets()
        return self.status()

    def _key(self) -> str | None:
        try:
            return self.secrets.get(SECRET_REF)
        except Exception:  # an unreadable store means "no key": advice falls back
            return None

    def has_key(self) -> bool:
        try:
            return SECRET_REF in self.secrets.refs()
        except Exception:
            return False

    def set_key(self, key: str | None, *, source: str = "entered") -> dict[str, Any]:
        """Store (or with None remove) the key. Returns the status, never the value."""
        if key is None or not str(key).strip():
            self.secrets.delete(SECRET_REF)
            src = None
        else:
            value = str(key).strip()
            if len(value) < 8 or len(value) > 512 or any(c.isspace() for c in value):
                raise ValueError("that does not look like a JeV / TypeSafe API key")
            self.secrets.put(value, ref=SECRET_REF)
            src = source
        self.settings_file.update(lambda d: {**d, "key_source": src})
        self.reset_breaker()
        return self.status()

    def import_install_key(self) -> dict[str, Any]:
        """Explicit user action: copy the key from the JeV install's documented key file into the app's store. Reads only that
        file (at most 4 KB), never logs or returns the value."""
        path = jev_install_key_path(self._env)
        try:
            if not path.is_file():
                raise JeVError("not_found", f"No JeV key file at {path}. Enter the key in the JeV advisor card instead.")
            with open(path, "rb") as f:
                raw = f.read(KEY_FILE_MAX_BYTES + 1)
        except JeVError:
            raise
        except OSError as e:
            raise JeVError("unreadable", f"The JeV key file at {path} could not be read ({type(e).__name__}).") from None
        if len(raw) > KEY_FILE_MAX_BYTES:
            raise JeVError("invalid", f"The file at {path} is too large to be a key file; nothing was imported.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise JeVError("invalid", f"The file at {path} is not a text key file; nothing was imported.") from None
        line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if not line or line.startswith("#") or "REPLACE" in line:
            raise JeVError("invalid", f"The JeV key file at {path} holds no key (empty or a placeholder); nothing was imported.")
        register_secret(line)
        try:
            self.set_key(line, source="jev_install")
        except ValueError:
            raise JeVError("invalid", f"The JeV key file at {path} does not hold a usable key; nothing was imported.") from None
        return {**self.status(), "imported": True, "path": str(path)}

    def month_budget_id(self) -> str:
        return "jev:monthly:" + time.strftime("%Y-%m", time.gmtime(self._now()))

    def _ensure_budgets(self) -> None:
        self.ledger.ensure("jev:setup", "jev:setup", SETUP_CAP_USD)
        mb = self.month_budget_id()
        cap = self.settings()["monthly_cap_usd"]
        b = self.ledger.ensure(mb, mb, cap)
        if abs(float(b["limit_usd"]) - cap) > 1e-12:
            self.ledger.set_limit(mb, cap)

    def offline_reason(self) -> str | None:
        st = self.settings()
        if not st["enabled"]:
            return "off"
        if not self.has_key():
            return "no_key"
        if self.breaker_state() == "open":
            return "breaker_open"
        return None

    def enabled_now(self) -> bool:
        return self.offline_reason() is None

    def status(self) -> dict[str, Any]:
        st = self.settings()
        self._ensure_budgets()
        path = jev_install_key_path(self._env)
        try:
            found = path.is_file()
        except OSError:
            found = False
        with self._blk:
            br = dict(self._breaker)
        return {"enabled": st["enabled"], "has_key": self.has_key(), "key_source": st["key_source"], "model": MODEL,
                "endpoint": self.base_url + EVAL_PATH, "monthly_cap_usd": st["monthly_cap_usd"], "setup_cap_usd": SETUP_CAP_USD,
                "price": {"input_per_mtok": PRICE_PER_MTOK_INPUT, "source": "TypeSafe list price (local estimate)"},
                "month": self.ledger.get(self.month_budget_id()), "setup": self.ledger.get("jev:setup"),
                "breaker": {"state": self.breaker_state(), "failures": br["failures"],
                            "open_until": (br["opened_at"] + BREAKER_COOLDOWN_S) if self.breaker_state() == "open" else None},
                "offline_reason": self.offline_reason(), "min_confidence": self.min_confidence,
                "jev_install": {"key_file_found": found, "path": str(path)}, "last_decisions": self.recent(20)}

    # ------------------------------------------------------------------ circuit breaker (in process)
    def breaker_state(self) -> str:
        with self._blk:
            if self._breaker["failures"] < BREAKER_THRESHOLD:
                return "closed"
            if self._now() - self._breaker["opened_at"] < BREAKER_COOLDOWN_S:
                return "open"
            return "half_open"           # one probe allowed; a failure re-opens it at once

    def _fail(self) -> None:
        with self._blk:
            self._breaker["failures"] += 1
            self._breaker["opened_at"] = self._now()

    def _ok(self) -> None:
        with self._blk:
            self._breaker = {"failures": 0, "opened_at": 0.0}

    def reset_breaker(self) -> None:
        self._ok()

    # ------------------------------------------------------------------ the one request
    def _evaluate(self, *, state: str, qid: str, question: dict[str, Any], allowed: list[str], budget_id: str, purpose: str,
                  case_id: str | None, job_id: str | None) -> tuple[dict[str, Any], float]:
        """POST /systemone once (plus bounded back-off on 429/529). Returns (answer + usage + model, spent_usd); raises JeVError."""
        key = self._key()
        if not key:
            raise JeVError("no_key", "no JeV key is stored")
        if self.breaker_state() == "open":
            raise JeVError("breaker_open", "circuit breaker open after repeated JeV failures")
        body = json.dumps({"model": MODEL, "state": state, "questions": {qid: question}}, sort_keys=True)
        est_tokens = max(1, math.ceil(len(body) / 3))               # conservative (the bridge uses chars/4)
        amount = est_tokens * PRICE_PER_MTOK_INPUT / 1e6
        try:
            rsv = self.ledger.reserve(budget_id, amount, f"jev:{new_id('r')}")
        except BudgetExhausted:
            raise JeVError("budget_exhausted", "the JeV budget has no room for this request") from None
        t0 = time.monotonic()
        call_id = new_id("aic")
        settled = {"done": False}

        def finish(outcome: str, spent: float, known: bool, usage_in: int | None = None, usage_out: int | None = None,
                   model: str | None = None, detail: str = "") -> None:
            if not settled["done"]:
                if spent > 0:
                    self.ledger.settle(rsv["reservation_id"], spent, {"outcome": outcome, "input_tokens": usage_in})
                else:
                    self.ledger.release(rsv["reservation_id"])
                settled["done"] = True
            from .base import Usage
            u = Usage(input_tokens=usage_in or 0, output_tokens=usage_out or 0) if usage_in is not None else None
            record_ai_call(self.ledger.db, self.events, call_id=call_id, task=f"jev_{purpose}", provider="jev", model=model or MODEL,
                           outcome=outcome, usage=u, cost_usd=spent, cost_known=known, latency_ms=int((time.monotonic() - t0) * 1000),
                           case_id=case_id, job_id=job_id, extra={"reason": detail, "notes": f"budget {budget_id}"})

        url = self.base_url + EVAL_PATH
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        attempt = 0
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout_s) as client:
                while True:
                    try:
                        resp = client.post(url, content=body.encode("utf-8"), headers=headers)
                    except httpx.TimeoutException as e:
                        sent = not isinstance(e, (httpx.ConnectTimeout, httpx.PoolTimeout))
                        if not sent and attempt < MAX_TRANSIENT_RETRIES:
                            attempt += 1
                            self._sleep(min(RETRY_BASE_S * 2 ** attempt, MAX_BACKOFF_S))
                            continue
                        self._fail()
                        # a read timeout may have been processed and billed: counted at the reserved amount, never re-sent
                        finish("timeout" if sent else "timeout_not_sent", amount if sent else 0.0, not sent, detail="request timed out")
                        raise JeVError("timeout", "JeV did not answer in time", spent=sent) from None
                    except httpx.TransportError:
                        if attempt < MAX_TRANSIENT_RETRIES:
                            attempt += 1
                            self._sleep(min(RETRY_BASE_S * 2 ** attempt, MAX_BACKOFF_S))
                            continue
                        self._fail()
                        finish("unreachable", 0.0, True, detail="network failure contacting JeV")
                        raise JeVError("unavailable", "JeV could not be reached") from None
                    code = resp.status_code
                    if code in (401, 403):
                        self._fail()
                        finish("auth_failed", 0.0, True, detail=f"HTTP {code}")
                        raise JeVError("unauthenticated", f"JeV rejected the key (HTTP {code}); update it in Connections")
                    if code == 422:
                        finish("invalid_request", 0.0, True, detail="HTTP 422")
                        raise JeVError("invalid_request", "JeV rejected the request body (HTTP 422)")
                    if code in (429, 529):
                        ra = resp.headers.get("retry-after")
                        try:
                            wait = float(ra) if ra is not None else None
                        except ValueError:
                            wait = None
                        wait = wait if wait is not None and wait > 0 else RETRY_BASE_S * 2 ** attempt
                        if attempt < MAX_TRANSIENT_RETRIES and wait <= MAX_BACKOFF_S:
                            attempt += 1
                            self._sleep(wait)
                            continue
                        self._fail()
                        finish("rate_limit", 0.0, True, detail=f"HTTP {code}")
                        raise JeVError("rate_limited" if code == 429 else "overloaded",
                                       f"JeV is {'throttling requests' if code == 429 else 'overloaded'} (HTTP {code}); try later")
                    if code < 200 or code >= 300:
                        self._fail()
                        finish("unavailable", 0.0, True, detail=f"HTTP {code}")
                        raise JeVError("provider_error", f"JeV returned HTTP {code}")
                    break
        except JeVError:
            raise
        except Exception as e:  # never let advice break routing; the request may have been sent
            self._fail()
            finish("internal_error", amount, False, detail=type(e).__name__)
            raise JeVError("internal_error", f"JeV request failed ({type(e).__name__})", spent=True) from None
        # ---- 2xx: billed. Parse, validate, settle at the reported input tokens (else the reservation).
        try:
            data = resp.json()
        except ValueError:
            data = None
        usage = data.get("usage") if isinstance(data, Mapping) else None
        tin = usage.get("input_tokens") if isinstance(usage, Mapping) else None
        tout = usage.get("output_tokens") if isinstance(usage, Mapping) else None
        known = isinstance(tin, int) and not isinstance(tin, bool) and tin >= 0
        spent = (tin * PRICE_PER_MTOK_INPUT / 1e6) if known else amount
        model = data.get("model") if isinstance(data, Mapping) and isinstance(data.get("model"), str) else None
        answers = data.get("answers") if isinstance(data, Mapping) else None
        if not isinstance(answers, Mapping) or not answers:
            self._fail()
            finish("invalid_response", spent, known, tin if known else None, tout if isinstance(tout, int) else None, model, "no answers map")
            raise JeVError("invalid_response", "JeV's answer had no answers map", spent=True)
        ans = answers.get(qid, next(iter(answers.values())))
        ok, err = validate_choice(ans, allowed)
        if not ok:
            self._fail()
            finish("invalid_response", spent, known, tin if known else None, tout if isinstance(tout, int) else None, model, err)
            raise JeVError("invalid_response", f"JeV's answer was rejected ({err})", spent=True)
        self._ok()
        finish("ok", spent, known, tin if known else None, tout if isinstance(tout, int) else None, model, f"choice {ans['choice']}")
        probs = {k: float(v) for k, v in ans["probabilities"].items() if isinstance(v, (int, float))}
        conf = ans.get("confidence")
        if conf is None:
            conf = max((probs.get(k, 0.0) for k in allowed), default=0.0)
        return {"choice": ans["choice"], "probabilities": probs, "confidence": float(conf), "model": model or MODEL,
                "input_tokens": tin if known else None}, spent

    # ------------------------------------------------------------------ test button
    def setup(self, *, case_id: str | None = None) -> dict[str, Any]:
        """One small verification request funded by ``jev:setup`` ($0.05 cap)."""
        self._ensure_budgets()
        if not self.has_key():
            return {"ok": False, "reason": "no_key", "message": "Enter a JeV key first."}
        try:
            ans, spent = self._evaluate(state="connection check from Rebuild Studio; answer OK", qid="check",
                                        question={"type": "choice", "instructions": "Reply OK if this request was received.",
                                                  "criteria": {"OK": "The request was received.", "NOT_OK": "The request is unclear."}},
                                        allowed=["OK", "NOT_OK"], budget_id="jev:setup", purpose="setup", case_id=case_id, job_id=None)
        except JeVError as e:
            return {"ok": False, "reason": e.code, "message": str(e), "spent_usd": None}
        doc = {"ok": True, "reason": "ok", "model": ans["model"], "spent_usd": spent, "ts": self._now(),
               "message": f"JeV answered ({ans['model']}); the advisor is ready."}
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "setup.json").write_text(json.dumps(doc), encoding="utf-8")
        return doc

    # ------------------------------------------------------------------ advice: rung order
    @staticmethod
    def _cache_key(kind: str, payload: Mapping[str, Any]) -> str:
        return hashlib.sha256(json.dumps({"kind": kind, "model": MODEL, **payload}, sort_keys=True, default=str).encode()).hexdigest()

    def _fallback(self, reason: str, key: str = "", spent: float = 0.0, confidence: float | None = None) -> AdvisorDecision:
        return AdvisorDecision(order=None, source="fallback", reason=reason, key=key, spent_usd=spent, confidence=confidence)

    @staticmethod
    def _descriptor(c: Mapping[str, Any], i: int) -> str:
        loc = c.get("locality")
        where = "runs on the user's PC" if loc == "local" else "cloud service" if loc == "cloud" else "location unknown"
        if not c.get("price_known"):
            price = "price unknown"
        elif (c.get("input_per_mtok") or 0) == 0 and (c.get("output_per_mtok") or 0) == 0:
            price = "free"
        else:
            price = f"${c.get('input_per_mtok')}/M input, ${c.get('output_per_mtok')}/M output tokens"
        return f"ladder position {i + 1}: provider {c.get('provider')}, model {c.get('model')}, {where}, {price}"

    def advise(self, task: str, candidates: list[dict[str, Any]], summary: dict[str, Any], *, case_id: str | None = None,
               job_id: str | None = None) -> AdvisorDecision:
        n = len(candidates)
        if n < 2:
            return self._fallback("single_candidate")
        allowed = [f"R{i + 1}" for i in range(n)]
        payload = {"task": task, "needs": sorted(summary.get("needs") or []), "est_input_tokens": summary.get("est_input_tokens"),
                   "max_output_tokens": summary.get("max_output_tokens"),
                   "c": [{k: c.get(k) for k in ("provider", "model", "locality", "price_known", "input_per_mtok", "output_per_mtok")}
                         for c in candidates]}
        key = self._cache_key("order", payload)
        if not self.settings()["enabled"]:
            return self._fallback("jev_off", key)          # off means off: not even a cached decision re-orders
        hit = self.cache.get(key)
        if hit is not None:
            order, conf = hit.get("order"), hit.get("confidence")
            if self._valid(order, n) and isinstance(conf, (int, float)) and conf >= self.min_confidence:
                return AdvisorDecision(order=list(order), source="cache", confidence=float(conf), reason=hit.get("reason", ""),
                                       cached=True, key=key, model=hit.get("model"))
            return self._fallback("cached_low_confidence", key)
        why = self.offline_reason()
        if why is not None:
            return self._fallback({"off": "jev_off", "no_key": "jev_not_configured", "breaker_open": "breaker_open"}[why], key)
        self._ensure_budgets()
        state = "\n".join([f"task: {task}", f"capability needs: {', '.join(payload['needs']) or 'none'}",
                           f"estimated input tokens: {payload['est_input_tokens']}", f"max output tokens: {payload['max_output_tokens']}",
                           f"candidate model routes: {n} (described in the criteria)"])
        question = {"type": "choice",
                    "instructions": ("You advise the order in which an offline code-recovery tool tries its configured model routes for one "
                                     "task. Choose the route most likely to complete the task successfully at the lowest cost; your "
                                     "probabilities rank the others. Judge only from the descriptors. You cannot add routes."),
                    "criteria": {a: self._descriptor(c, i) for i, (a, c) in enumerate(zip(allowed, candidates))}}
        try:
            ans, spent = self._evaluate(state=state, qid="route", question=question, allowed=allowed, budget_id=self.month_budget_id(),
                                        purpose="advice", case_id=case_id, job_id=job_id)
        except JeVError as e:
            self._remember({"kind": "order", "task": task, "source": "fallback", "reason": e.code, "case_id": case_id})
            if case_id and e.code not in ("no_key", "budget_exhausted"):
                self._say(case_id, job_id, task, f"JeV could not advise ({e}); using your ladder order", "fallback")
            return self._fallback(e.code if e.code in ("budget_exhausted",) else f"jev_error:{e.code}", key, 0.0)
        probs = ans["probabilities"]
        order = sorted(range(n), key=lambda i: (-probs.get(allowed[i], 0.0), i))
        conf = ans["confidence"]
        top = candidates[order[0]]
        self.cache.put(key, {"order": order, "confidence": conf, "reason": f"choice {ans['choice']}", "ts": self._now(),
                             "model": ans["model"]})
        rec = {"kind": "order", "task": task, "choice": ans["choice"], "confidence": round(conf, 4), "model": ans["model"],
               "suggested_first": f"{top.get('model')}", "source": "jev", "spent_usd": spent, "case_id": case_id}
        if conf < self.min_confidence:
            self._remember({**rec, "source": "fallback", "reason": "low_confidence"})
            if case_id:
                self._say(case_id, job_id, task, f"JeV was unsure (confidence {conf:.2f}); keeping your ladder order", "advice",
                          confidence=conf)
            return AdvisorDecision(order=None, source="fallback", confidence=conf, reason="low_confidence", spent_usd=spent, key=key,
                                   model=ans["model"], probabilities=probs)
        self._remember(rec)
        if case_id:
            moved = order != list(range(n))
            self._say(case_id, job_id, task, (f"JeV suggests trying {top.get('model')} first (confidence {conf:.2f})" if moved else
                                              f"JeV agrees with your ladder order (confidence {conf:.2f})")
                      + "; it only re-orders your rungs", "advice", confidence=conf)
        return AdvisorDecision(order=order, source="jev", confidence=conf, reason=f"choice {ans['choice']}", spent_usd=spent, key=key,
                               model=ans["model"], probabilities=probs)

    # ------------------------------------------------------------------ advice: between repair attempts
    def reassess(self, task: str, *, attempt: int, max_attempts: int, build: str | None, scenarios: int | None = None,
                 passed: int | None = None, current: Mapping[str, Any] | None = None, other_rungs: int = 0,
                 case_id: str | None = None, job_id: str | None = None) -> dict[str, Any]:
        """``{advice: "retry"|"switch"|"stop"|None, confidence, source, reason}``. None = deterministic (keep going as planned).
        Only counts and outcomes are sent; never code or program output."""
        allowed = list(REASSESS_CHOICES) if other_rungs > 0 else ["RETRY", "STOP"]
        payload = {"task": task, "attempt": attempt, "max_attempts": max_attempts, "build": build, "scenarios": scenarios,
                   "passed": passed, "current": {k: (current or {}).get(k) for k in ("provider", "model", "locality")},
                   "other_rungs": other_rungs}
        key = self._cache_key("reassess", payload)
        out: dict[str, Any] = {"advice": None, "confidence": None, "source": "fallback", "reason": ""}
        if not self.settings()["enabled"]:
            return {**out, "reason": "off"}
        hit = self.cache.get(key)
        if hit is not None and hit.get("choice") in allowed:
            conf = float(hit.get("confidence") or 0)
            if conf >= self.min_confidence:
                return {"advice": hit["choice"].lower(), "confidence": conf, "source": "cache", "reason": "cached decision"}
            return {**out, "reason": "cached_low_confidence"}
        why = self.offline_reason()
        if why is not None:
            return {**out, "reason": why}
        self._ensure_budgets()
        cur = payload["current"]
        state = "\n".join([f"task: {task}", f"attempt {attempt} of {max_attempts} finished",
                           f"build: {build or 'unknown'}",
                           f"declared scenarios passed: {passed if passed is not None else 'n/a'} of {scenarios if scenarios is not None else 'n/a'}",
                           f"current route: provider {cur.get('provider')}, model {cur.get('model')}, {cur.get('locality') or 'unknown'}",
                           f"other configured routes available: {other_rungs}",
                           "an independent verifier decides correctness; you only advise whether to keep spending"])
        criteria = {"RETRY": "Another attempt with the same route is worthwhile: the failures look fixable from the feedback.",
                    "SWITCH": "Another attempt is worthwhile but the next configured route should be used first.",
                    "STOP": "Further attempts are unlikely to help; stop and let the user decide."}
        question = {"type": "choice", "criteria": {k: criteria[k] for k in allowed},
                    "instructions": ("You advise one bounded decision between repair attempts of an offline code-recovery tool. Judge "
                                     "only from the supplied counts. Never treat this as authorization to spend beyond the user's budget.")}
        try:
            ans, spent = self._evaluate(state=state, qid="next", question=question, allowed=allowed, budget_id=self.month_budget_id(),
                                        purpose="reassess", case_id=case_id, job_id=job_id)
        except JeVError as e:
            self._remember({"kind": "reassess", "task": task, "source": "fallback", "reason": e.code, "case_id": case_id})
            return {**out, "reason": e.code}
        conf = ans["confidence"]
        self.cache.put(key, {"choice": ans["choice"], "confidence": conf, "ts": self._now(), "model": ans["model"]})
        rec = {"kind": "reassess", "task": task, "choice": ans["choice"], "confidence": round(conf, 4), "model": ans["model"],
               "source": "jev", "spent_usd": spent, "case_id": case_id, "attempt": attempt}
        if conf < self.min_confidence:
            self._remember({**rec, "source": "fallback", "reason": "low_confidence"})
            return {**out, "confidence": conf, "reason": "low_confidence", "spent_usd": spent}
        self._remember(rec)
        return {"advice": ans["choice"].lower(), "confidence": conf, "source": "jev", "reason": f"choice {ans['choice']}",
                "spent_usd": spent, "model": ans["model"]}

    # ------------------------------------------------------------------ decision log + activity
    def _remember(self, rec: dict[str, Any]) -> None:
        rec = {**rec, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._now()))}
        try:
            self.recent_file.update(lambda xs: ([rec] + list(xs))[:MAX_RECENT])
        except OSError:
            pass

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            return list(self.recent_file.read())[: max(0, int(limit))]
        except Exception:
            return []

    def _say(self, case_id: str, job_id: str | None, task: str, text: str, kind: str, **fields: Any) -> None:
        try:
            from .ladder import emit_activity
            emit_activity(self.events, text, kind=kind, case_id=case_id, job_id=job_id, task=task, provider="jev", model=MODEL,
                          origin="model_proposed", **fields)
        except Exception:  # the feed is informational
            pass

    @staticmethod
    def _valid(order: Any, n: int) -> bool:
        return (isinstance(order, list) and len(order) == n and all(isinstance(i, int) and not isinstance(i, bool) for i in order)
                and sorted(order) == list(range(n)))
