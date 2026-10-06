"""ConnectionStore: connections, secrets, probing and task routes.

State machine for ``connections.state``: unprobed -> ok | auth_failed | unreachable | limited (re-probe or a live call
moves it). Capabilities are recorded from observed behaviour only; nothing is assumed from a provider name.
Models: ``explicit`` (typed by the user) or ``discovered`` (listed by the endpoint). A model id is never derived from a
connection label or provider name, and routes must name a model.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import httpx

from ..budget import BudgetExhausted, BudgetLedger
from ..events import EventLog
from ..ids import new_id, now_iso
from ..store.db import Database, loads
from .anthropic import AnthropicAdapter
from .base import (AuthError, ProviderAdapter, ProviderError, RateLimit, Request, Timeout, Unreachable, Usage,
                   UsageLimit, new_capabilities)
from .gemini import GeminiAdapter
from .openai_responses import OpenAIResponsesAdapter
from .pricing import PriceTable
from .secrets import SecretStore, install_redacting_filter, redact
from . import subscription

log = logging.getLogger("rebuild.connections")
install_redacting_filter(log)

PROVIDERS = ("openai", "anthropic", "gemini", "openrouter", "local")
AUTH_MODES = ("api_key", "subscription_handoff", "local", "none")
STATES = ("unprobed", "ok", "auth_failed", "unreachable", "limited", "no_credits")
TASKS = ("interpretation", "repair", "visual_review", "verification_assist", "knowledge")
DIALECTS = ("responses", "chat", "auto")
HANDOFF_FOR_PROVIDER = {"openai": "openai_siwc", "anthropic": "claude_agent_sdk", "gemini": "gemini_cli"}
PROBE_BUDGET_USD = 0.50           # lifetime ceiling for capability probes per connection (budget id probe:<connection_id>)
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}
_BAD_CAPS = {"rejected", "unsupported"}


class ConnectionStoreError(ValueError):
    pass


def locality_of(conn: Mapping[str, Any]) -> str:
    """``local`` when the connection is a local provider or its endpoint is a loopback address ("runs on this PC")."""
    if conn.get("provider") == "local":
        return "local"
    host = urlparse(conn.get("endpoint") or "").hostname or ""
    return "local" if host in _LOOPBACK or host.startswith("127.") else "cloud"


class ModelNotListed(ConnectionStoreError):
    pass


class HandoffOnly(ConnectionStoreError):
    """The connection is a subscription handoff: there is no API adapter to call."""


def _norm_models(models: Iterable[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in models or []:
        if isinstance(m, str):
            m = {"id": m}
        if not isinstance(m, Mapping) or not isinstance(m.get("id"), str) or not m["id"].strip():
            raise ConnectionStoreError(f"invalid model entry {m!r}: need a non-empty string id")
        entry = {"id": m["id"].strip(), "source": "explicit"}
        if isinstance(m.get("price"), Mapping):
            entry["price"] = dict(m["price"])
        out.append(entry)
    return out


def _validate_endpoint(provider: str, endpoint: str) -> str:
    endpoint = (endpoint or "").strip()
    if not endpoint:
        if provider == "local":
            raise ConnectionStoreError("a local connection needs an endpoint, e.g. http://localhost:1234/v1")
        return ""
    u = urlparse(endpoint)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ConnectionStoreError("endpoint must be an http(s) URL")
    if u.scheme == "http" and u.hostname not in _LOOPBACK and provider != "local":
        raise ConnectionStoreError("refusing plain http to a non-loopback host for a keyed provider")
    return endpoint.rstrip("/")


class ConnectionStore:
    def __init__(self, db: Database, events: EventLog, settings: Any, *, secrets: SecretStore | None = None,
                 transport: httpx.BaseTransport | None = None, prices: PriceTable | None = None,
                 ledger: BudgetLedger | None = None):
        self.db = db
        self.events = events
        self.settings = settings
        self.secrets = secrets or SecretStore(settings.data_dir)
        self.transport = transport
        self.prices = prices or PriceTable()
        self.ledger = ledger or BudgetLedger(db, events)
        self._overrides: dict[str, ProviderAdapter] = {}

    # ------------------------------------------------------------------ rows
    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        r = dict(row)
        r["has_secret"] = bool(r.pop("secret_ref", None))
        r["models"] = loads(r.get("models"), [])
        r["capabilities"] = loads(r.get("capabilities"), {})
        r["limits"] = loads(r.get("limits"), {})
        return r

    def _row(self, connection_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM connections WHERE connection_id=?", (connection_id,))
        if row is None:
            raise ConnectionStoreError(f"unknown connection {connection_id}")
        return row

    def get(self, connection_id: str) -> dict[str, Any]:
        return self._public(self._row(connection_id))

    def list(self) -> list[dict[str, Any]]:
        return [self._public(r) for r in self.db.query("SELECT * FROM connections ORDER BY created_at, connection_id")]

    # ------------------------------------------------------------------ crud
    def create(self, provider: str, label: str, endpoint: str = "", auth_mode: str = "api_key", api_key: str | None = None,
               models: Iterable[Any] | None = None, dialect: str | None = None) -> dict[str, Any]:
        if provider not in PROVIDERS:
            raise ConnectionStoreError(f"unknown provider {provider!r}; expected one of {PROVIDERS}")
        if auth_mode not in AUTH_MODES:
            raise ConnectionStoreError(f"unknown auth_mode {auth_mode!r}; expected one of {AUTH_MODES}")
        if not (label or "").strip():
            raise ConnectionStoreError("label is required")
        endpoint = _validate_endpoint(provider, endpoint)
        limits: dict[str, Any] = {}
        if auth_mode == "subscription_handoff":
            key = HANDOFF_FOR_PROVIDER.get(provider)
            if key is None:
                raise ConnectionStoreError(f"{provider} has no subscription handoff mode")
            limits["handoff"] = subscription.get_mode(key).to_dict()
            if api_key:
                raise ConnectionStoreError("a subscription handoff never stores credentials")
        elif auth_mode == "api_key" and not api_key:
            raise ConnectionStoreError("auth_mode api_key requires api_key")
        elif auth_mode in ("local", "none") and provider != "local" and api_key:
            raise ConnectionStoreError("api_key given with auth_mode local/none")
        if dialect is not None and dialect not in DIALECTS:
            raise ConnectionStoreError(f"unknown dialect {dialect!r}")
        if provider in ("openai", "openrouter", "local"):
            limits["dialect"] = dialect or {"openai": "responses", "openrouter": "chat", "local": "auto"}[provider]
        elif dialect:
            raise ConnectionStoreError("dialect only applies to OpenAI-style providers")
        cid = new_id("conn")
        ref = self.secrets.put(api_key) if api_key else None
        now = now_iso()
        try:
            self.db.insert("connections", {"connection_id": cid, "provider": provider, "label": label.strip(), "endpoint": endpoint,
                                           "auth_mode": auth_mode, "secret_ref": ref, "models": _norm_models(models),
                                           "capabilities": new_capabilities(), "limits": limits, "state": "unprobed",
                                           "last_probe": None, "created_at": now, "updated_at": now})
        except Exception:
            if ref:
                self.secrets.delete(ref)
            raise
        return self.get(cid)

    def update(self, connection_id: str, *, label: str | None = None, endpoint: str | None = None, api_key: str | None = None,
               models: Iterable[Any] | None = None, dialect: str | None = None) -> dict[str, Any]:
        row = self._row(connection_id)
        fields: dict[str, Any] = {"updated_at": now_iso()}
        if label is not None:
            fields["label"] = label.strip()
        if endpoint is not None:
            fields["endpoint"] = _validate_endpoint(row["provider"], endpoint)
            fields["state"] = "unprobed"
        if api_key:
            if row["auth_mode"] != "api_key" and not (row["provider"] == "local"):
                raise ConnectionStoreError("this connection does not take an api key")
            old = row.get("secret_ref")
            fields["secret_ref"] = self.secrets.put(api_key)
            if old:
                self.secrets.delete(old)
            fields["state"] = "unprobed"
        if models is not None:
            keep = [m for m in loads(row["models"], []) if m.get("source") == "discovered"]
            fields["models"] = _norm_models(models) + keep
        if dialect is not None:
            if dialect not in DIALECTS:
                raise ConnectionStoreError(f"unknown dialect {dialect!r}")
            lim = loads(row["limits"], {})
            lim["dialect"] = dialect
            fields["limits"] = lim
        self.db.update("connections", "connection_id", connection_id, fields)
        self._overrides.pop(connection_id, None)
        return self.get(connection_id)

    def delete(self, connection_id: str) -> bool:
        row = self.db.query_one("SELECT * FROM connections WHERE connection_id=?", (connection_id,))
        if row is None:
            return False
        routes_changed = False
        with self.db.transaction():
            for r in self.get_routes():
                changed = False
                prim, pm = r["primary_connection"], r["primary_model"]
                if prim == connection_id:
                    prim = pm = None
                    changed = True
                fb = [f for f in r["fallbacks"] if f.get("connection") != connection_id]
                if len(fb) != len(r["fallbacks"]):
                    changed = True
                if changed:
                    self.db.upsert("task_routes", {"task": r["task"], "primary_connection": prim, "primary_model": pm,
                                                   "fallbacks": fb, "updated_at": now_iso()}, "task")
                    routes_changed = True
            self.db.execute("DELETE FROM connections WHERE connection_id=?", (connection_id,))
            if routes_changed:
                self.bump_revision(f"connection {connection_id} deleted")
        if row.get("secret_ref"):
            self.secrets.delete(row["secret_ref"])
        self._overrides.pop(connection_id, None)
        return True

    def set_state(self, connection_id: str, state: str, *, detail: str | None = None) -> None:
        if state not in STATES:
            raise ConnectionStoreError(f"bad state {state!r}")
        row = self.db.query_one("SELECT state FROM connections WHERE connection_id=?", (connection_id,))
        if row is None or row["state"] == state:
            return
        self.db.update("connections", "connection_id", connection_id, {"state": state, "updated_at": now_iso()})
        if detail:
            log.info("connection %s -> %s (%s)", connection_id, state, redact(detail)[:200])

    # ------------------------------------------------------------------ adapters
    def set_adapter_override(self, connection_id: str, adapter: ProviderAdapter | None) -> None:
        """Tests and the knowledge-reuse demo inject mock/raising adapters here."""
        if adapter is None:
            self._overrides.pop(connection_id, None)
        else:
            self._overrides[connection_id] = adapter

    def adapter(self, connection_id: str, *, dialect: str | None = None) -> ProviderAdapter:
        if connection_id in self._overrides:
            return self._overrides[connection_id]
        row = self._row(connection_id)
        conn = self._public(row)
        if conn["auth_mode"] == "subscription_handoff":
            raise HandoffOnly("subscription handoff connections have no API adapter; use providers.subscription.launch_external")
        key = self.secrets.get(row.get("secret_ref")) if row.get("secret_ref") else None
        if conn["auth_mode"] == "api_key" and not key:
            raise AuthError(f"connection {connection_id}: no API key stored", provider=conn["provider"])
        p, ep = conn["provider"], conn["endpoint"] or None
        lim = conn["limits"]
        if p == "anthropic":
            return AnthropicAdapter(endpoint=ep, api_key=key, transport=self.transport, betas=lim.get("betas"))
        if p == "gemini":
            return GeminiAdapter(endpoint=ep, api_key=key, transport=self.transport)
        d = dialect or lim.get("dialect") or "responses"
        if d == "auto":
            d = "chat"  # until a probe has chosen, the widely supported dialect
        return OpenAIResponsesAdapter(endpoint=ep, api_key=key, dialect=d, provider_name=p, transport=self.transport)

    # ------------------------------------------------------------------ probe
    def probe(self, connection_id: str, *, capabilities: bool = True, model: str | None = None,
              approve_unknown_pricing: bool = False) -> dict[str, Any]:
        conn = self.get(connection_id)
        detail: list[str] = []
        caps = dict(new_capabilities())
        caps.update({k: v for k, v in conn["capabilities"].items() if k in caps})
        state = "unprobed"
        models = list(conn["models"])
        limits = dict(conn["limits"])
        if conn["auth_mode"] == "subscription_handoff":
            mode = subscription.get_mode(limits["handoff"]["key"])
            found = subscription.cli_available(mode)
            state = "ok" if found else "unreachable"
            detail.append(f"{mode.cli} {'found on PATH' if found else 'not found on PATH'}; this checks the CLI only, not your login")
            return self._store_probe(connection_id, state, caps, models, limits, detail, extra={"handoff": True})
        try:
            adapter = self.adapter(connection_id)
        except AuthError as e:
            return self._store_probe(connection_id, "auth_failed", caps, models, limits, [str(e)])
        # 1. model discovery (free) - also the cheapest auth check
        auth_proven = False
        try:
            disc = adapter.discover_models()
            if disc.supported:
                caps["discovery"] = "supported"
                auth_proven = True
                explicit = [m for m in models if m.get("source") == "explicit"]
                ids = {m["id"] for m in explicit}
                discovered = [m.to_dict() for m in disc.models if m.id not in ids]
                models = explicit + discovered
                listed = {m.id for m in disc.models}
                for m in models:
                    if m["id"] in listed:
                        m.pop("availability", None)
                    elif disc.models:
                        m["availability"] = {"state": "model_unavailable", "at": now_iso(),
                                             "detail": "not listed by the endpoint's model discovery (not pulled / not offered)"}
                detail.append(f"discovered {len(disc.models)} model(s)")
            else:
                caps["discovery"] = "unsupported"
                detail.append(f"discovery unsupported: {disc.error}; use explicit model ids")
        except AuthError as e:
            return self._store_probe(connection_id, "auth_failed", caps, models, limits, [str(e)])
        except (RateLimit, UsageLimit) as e:
            return self._store_probe(connection_id, "limited", caps, models, limits, [str(e)])
        except ProviderError as e:
            return self._store_probe(connection_id, "unreachable", caps, models, limits, [f"{e.kind}: {e}"])
        state = "ok" if auth_proven else "unprobed"
        # 1b. local Ollama server: capabilities (vision/tools/completion) and context length per model, free and spend-less
        if auth_proven and locality_of(conn) == "local" and conn["endpoint"] and connection_id not in self._overrides:
            from .ollama import enrich
            info = enrich(conn["endpoint"], [m["id"] for m in models], transport=self.transport)
            if info:
                limits["server"] = "ollama"
                if info.get("version"):
                    limits["server_version"] = info["version"]
                n = 0
                for m in models:
                    e = info["models"].get(m["id"])
                    if not e:
                        continue
                    n += 1
                    m["capabilities"] = e["capabilities"]
                    if e.get("context_window"):
                        m["context_window"] = e["context_window"]
                    if e.get("details"):
                        m.setdefault("meta", {})["ollama"] = e["details"]
                detail.append(f"Ollama server detected; capabilities and context length read for {n} model(s)")
        # 2. capability probe (spends tokens: budgeted, never against an unpriced model without approval)
        probed_model = None
        if capabilities:
            probed_model = model or next((m["id"] for m in models if m.get("source") == "explicit"), None) \
                or next((m["id"] for m in models), None)
            if not probed_model:
                detail.append("capability probe skipped: no model id (add one explicitly; none is guessed)")
            else:
                st, adapter = self._run_capability_probe(connection_id, conn, adapter, probed_model, caps, limits, detail,
                                                         approve_unknown_pricing)
                if st is not None:
                    state = st
        return self._store_probe(connection_id, state, caps, models, limits, detail,
                                 extra={"model_probed": probed_model} if probed_model else None)

    def _run_capability_probe(self, connection_id: str, conn: dict[str, Any], adapter: ProviderAdapter, model: str,
                              caps: dict[str, Any], limits: dict[str, Any], detail: list[str], approve: bool) -> tuple[str | None, ProviderAdapter]:
        price = self.prices.lookup(conn["provider"], model, connection=conn)
        if price.approval_required and not approve:
            detail.append(f"capability probe skipped: no known price for {model}; pass approve_unknown_pricing to spend up to "
                          f"the conservative ceiling")
            caps["probe_skipped"] = "pricing_unknown"
            return None, adapter
        # dialect auto-detection for OpenAI-style endpoints
        if isinstance(adapter, OpenAIResponsesAdapter) and limits.get("dialect") == "auto" and connection_id not in self._overrides:
            for d in ("responses", "chat"):
                cand = self.adapter(connection_id, dialect=d)
                try:
                    rep = cand.probe_capabilities(model, test=("basic",))
                except ProviderError:
                    continue
                if rep.capabilities["streaming"] == "supported":
                    limits["dialect"] = d
                    adapter = cand
                    detail.append(f"dialect resolved to {d}")
                    break
            else:
                limits["dialect"] = "chat"
                detail.append("neither responses nor chat dialect answered a basic request")
        ceiling = 4 * price.ceiling(400, getattr(adapter, "PROBE_MAX_TOKENS", 256))
        bid = f"probe:{connection_id}"
        self.ledger.ensure(bid, bid, PROBE_BUDGET_USD)
        try:
            rsv = self.ledger.reserve(bid, ceiling, f"probe:{connection_id}:{new_id('p')}")
        except BudgetExhausted as e:
            detail.append(f"capability probe skipped: {e}")
            caps["probe_skipped"] = "probe_budget_exhausted"
            return None, adapter
        try:
            report = adapter.probe_capabilities(model)
        except BaseException:
            self.ledger.settle(rsv["reservation_id"], rsv["amount_usd"], {"reason": "probe_crashed_assumed_spent"})
            raise
        spent = report.usage.reported_cost_usd
        if spent is None:
            spent = price.cost(report.usage) if report.usage.known else rsv["amount_usd"]
        self.ledger.settle(rsv["reservation_id"], max(spent, 0.0), report.usage.to_dict())
        caps.update({k: v for k, v in report.capabilities.items() if k != "discovery"})  # discovery is probed separately
        caps.pop("probe_skipped", None)
        if report.detail:
            detail.append(report.detail)
        if report.state != "ok":
            detail.append(f"probe state {report.state}")
        return report.state if report.state != "error" else None, adapter

    def _store_probe(self, connection_id: str, state: str, caps: dict[str, Any], models: list[dict[str, Any]],
                     limits: dict[str, Any], detail: list[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
        caps = {**caps, "probe_detail": redact("; ".join(detail))[:1500], **(extra or {})}
        now = now_iso()
        self.db.update("connections", "connection_id", connection_id,
                       {"state": state, "capabilities": caps, "models": models, "limits": limits, "last_probe": now, "updated_at": now})
        return self.get(connection_id)

    # ------------------------------------------------------------------ routes
    @staticmethod
    def _route_view(row: dict[str, Any]) -> dict[str, Any]:
        r = dict(row)
        r["fallbacks"] = loads(r.get("fallbacks"), [])
        return r

    def get_routes(self) -> list[dict[str, Any]]:
        return [self._route_view(r) for r in self.db.query("SELECT * FROM task_routes ORDER BY task")]

    def get_route(self, task: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM task_routes WHERE task=?", (task,))
        return self._route_view(row) if row else None

    def _check_target(self, connection_id: str, model: str, allow_unlisted: bool) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ConnectionStoreError("a route needs an explicit model id (none is inferred)")
        conn = self.get(connection_id)
        ids = {m["id"] for m in conn["models"]}
        has_discovered = any(m.get("source") == "discovered" for m in conn["models"])
        if model not in ids:
            if has_discovered and not allow_unlisted:
                raise ModelNotListed(f"{model!r} is not among the models {conn['label']!r} reports; re-probe or pass allow_unlisted")
            self.db.update("connections", "connection_id", connection_id,
                           {"models": conn["models"] + [{"id": model, "source": "explicit"}], "updated_at": now_iso()})

    def set_route(self, task: str, primary_connection: str, primary_model: str,
                  fallbacks: Iterable[Mapping[str, str]] | None = None, *, allow_unlisted: bool = False, rationale: str = "user",
                  bump: bool = True) -> dict[str, Any]:
        if task not in TASKS:
            raise ConnectionStoreError(f"unknown task {task!r}; expected one of {TASKS}")
        fb = [{"connection": f["connection"], "model": f["model"]} for f in (fallbacks or [])]
        seen: set[tuple[str, str]] = set()
        with self.db.transaction():
            self._check_target(primary_connection, primary_model, allow_unlisted)
            for f in fb:
                self._check_target(f["connection"], f["model"], allow_unlisted)
            for pair in [(primary_connection, primary_model)] + [(f["connection"], f["model"]) for f in fb]:
                if pair in seen:
                    raise ConnectionStoreError(f"duplicate route target {pair}")
                seen.add(pair)
            self.db.upsert("task_routes", {"task": task, "primary_connection": primary_connection, "primary_model": primary_model,
                                           "fallbacks": fb, "updated_at": now_iso()}, "task")
            if bump:
                self.bump_revision(f"ladder for {task} changed", {task: rationale})
        return self.get_route(task)  # type: ignore[return-value]

    def delete_route(self, task: str, *, rationale: str = "user", bump: bool = True) -> bool:
        with self.db.transaction():
            gone = self.db.execute("DELETE FROM task_routes WHERE task=?", (task,)).rowcount > 0
            if gone and bump:
                self.bump_revision(f"ladder for {task} cleared", {task: rationale})
        return gone

    # ------------------------------------------------------------------ configuration revisions (docs/AI_LADDER.md section 2)
    def config_revision(self) -> int:
        r = self.db.query_one("SELECT MAX(revision) AS r FROM ai_config_revisions")
        return int(r["r"] or 0) if r else 0

    def revision_snapshot(self, revision: int | None = None) -> dict[str, Any]:
        rev = self.config_revision() if revision is None else int(revision)
        r = self.db.query_one("SELECT * FROM ai_config_revisions WHERE revision=?", (rev,))
        if r is None:
            return {"revision": rev, "tasks": {}}
        return {"revision": r["revision"], "created_at": r["created_at"], "reason": r["reason"], **loads(r["snapshot"], {})}

    def revisions(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.db.query("SELECT revision, created_at, reason FROM ai_config_revisions ORDER BY revision DESC LIMIT ?", (limit,))

    def rationale(self, task: str) -> str:
        snap = self.revision_snapshot()
        return str(((snap.get("tasks") or {}).get(task) or {}).get("rationale") or "user")

    def bump_revision(self, reason: str, rationale: Mapping[str, str] | None = None) -> int:
        """Store a snapshot of every task ladder under the next (monotonic) revision number. ``rationale`` sets the per-task
        rationale ("user" | "preset:<name>" | "auto") for the tasks it names; other tasks keep theirs."""
        rationale = dict(rationale or {})
        with self.db.transaction():
            prev_tasks = self.revision_snapshot().get("tasks") or {}
            tasks: dict[str, Any] = {}
            for r in self.get_routes():
                entries = [{"connection_id": c, "model": m} for c, m in self.route_entries(r["task"])]
                why = rationale.get(r["task"]) or (prev_tasks.get(r["task"]) or {}).get("rationale") or "user"
                tasks[r["task"]] = {"entries": entries, "rationale": why}
            for t, why in rationale.items():
                tasks.setdefault(t, {"entries": [], "rationale": why})
            rev = self.config_revision() + 1
            self.db.insert("ai_config_revisions", {"revision": rev, "created_at": now_iso(), "reason": reason[:300],
                                                   "snapshot": {"tasks": tasks}})
        self.events.emit("ai.ladder", {"config_revision": rev, "reason": reason[:300]})
        return rev

    # ------------------------------------------------------------------ per (connection, model) availability
    def mark_model(self, connection_id: str, model: str, state: str, *, detail: str | None = None) -> None:
        """Flag a ladder entry (e.g. ``model_unavailable``) or clear the flag (``ok``). Stored on the connection's model entry
        as ``availability`` so every ladder that uses the same (connection, model) sees it."""
        row = self.db.query_one("SELECT models FROM connections WHERE connection_id=?", (connection_id,))
        if row is None:
            return
        models = loads(row["models"], [])
        changed = found = False
        for m in models:
            if m.get("id") != model:
                continue
            found = True
            cur = (m.get("availability") or {}).get("state")
            if state == "ok":
                if cur is not None:
                    m.pop("availability", None)
                    changed = True
            elif cur != state:
                m["availability"] = {"state": state, "at": now_iso(), "detail": redact(detail or "")[:300]}
                changed = True
        if not found and state != "ok":
            models.append({"id": model, "source": "explicit",
                           "availability": {"state": state, "at": now_iso(), "detail": redact(detail or "")[:300]}})
            changed = True
        if changed:
            self.db.update("connections", "connection_id", connection_id, {"models": models, "updated_at": now_iso()})

    def route_entries(self, task: str) -> list[tuple[str, str]]:
        route = self.get_route(task)
        if route is None or not route["primary_connection"]:
            return []
        return [(route["primary_connection"], route["primary_model"])] + [(f["connection"], f["model"]) for f in route["fallbacks"]]

    def ladder_candidates(self, task: str, needs: Iterable[str] | None = None,
                          entries: Iterable[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
        """Every ladder entry in order with its 1-based ``position``. ``conn`` is the public connection row (None when it no
        longer exists); ``skip`` is the plain-English reason the entry cannot be used at all and ``skip_outcome`` its taxonomy
        label. ``entries`` replaces the global ladder (a project's ladder override)."""
        ents = list(entries) if entries is not None else self.route_entries(task)
        out: list[dict[str, Any]] = []
        for i, (cid, model) in enumerate(ents, start=1):
            item: dict[str, Any] = {"position": i, "connection_id": cid, "model": model, "conn": None, "skip": None, "skip_outcome": None}
            out.append(item)
            if not cid or not model:
                item.update(skip="incomplete route entry", skip_outcome="unusable")
                continue
            row = self.db.query_one("SELECT * FROM connections WHERE connection_id=?", (cid,))
            if row is None:
                item.update(skip="connection no longer exists", skip_outcome="unusable")
                continue
            conn = self._public(row)
            item["conn"] = conn
            if conn["auth_mode"] == "subscription_handoff":
                item.update(skip="subscription handoff has no API path", skip_outcome="unusable")
            elif conn["state"] == "auth_failed":
                item.update(skip="auth failed; update the key or re-probe", skip_outcome="auth_failed")
            else:
                mcaps = next((m.get("capabilities") for m in conn["models"] if m.get("id") == model
                              and isinstance(m.get("capabilities"), Mapping)), None) or {}
                for n in needs or ():
                    own = mcaps.get({"images": "vision"}.get(n, n))
                    if isinstance(own, bool):        # per-model facts (Ollama /api/show) beat a connection-wide probe of one model
                        if not own:
                            item.update(skip=f"{model} does not support {n} (reported by the server)", skip_outcome="capability_unsupported")
                            break
                        continue
                    if conn["capabilities"].get(n) in _BAD_CAPS:
                        item.update(skip=f"endpoint {conn['capabilities'][n]} {n} when probed", skip_outcome="capability_unsupported")
                        break
        return out

    def resolve_detailed(self, task: str, needs: Iterable[str] | None = None,
                         entries: Iterable[tuple[str, str]] | None = None) -> tuple[list[tuple[dict[str, Any], str]], list[dict[str, str]]]:
        ents = list(entries) if entries is not None else self.route_entries(task)
        if not ents:
            return [], [{"reason": f"no route configured for task {task!r}"}]
        usable: list[tuple[dict[str, Any], str]] = []
        skipped: list[dict[str, str]] = []
        for c in self.ladder_candidates(task, needs, ents):
            if c["skip"]:
                skipped.append({"connection_id": str(c["connection_id"]), "model": str(c["model"]), "reason": c["skip"]})
            else:
                usable.append((c["conn"], c["model"]))
        return usable, skipped

    def resolve(self, task: str, needs: Iterable[str] | None = None) -> list[tuple[dict[str, Any], str]]:
        """Deterministic: primary first, then fallbacks in stored order; unusable entries are skipped, never reordered."""
        return self.resolve_detailed(task, needs)[0]
