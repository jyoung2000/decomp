"""AI model ladder views, presets, project AI policy and the plain-English activity feed (docs/AI_LADDER.md sections 2-5).

This module adds no second router: ladders ARE the existing task routes (``task_routes`` via ``ConnectionStore.set_route``);
every change bumps the monotonically increasing ``config_revision`` with a stored snapshot (``ai_config_revisions``). The router
(``providers/router.py``) consumes the effective ladder computed here.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from .connections import TASKS, ConnectionStore, ConnectionStoreError, locality_of
from .pricing import PriceTable
from .secrets import redact
from ..events import EventLog
from ..ids import now_iso

PRESETS = ("local_first", "cloud_first", "all_local", "all_cloud", "no_ai")
POLICY_MODES = ("no_ai", "inherit", "custom", "assisted", "assist_on_failure")
AI_ON_MODES = ("assisted", "assist_on_failure", "inherit", "custom")
LOCALITIES = ("any", "local_only", "cloud_only")
MAX_PRESET_ENTRIES = 4
POLICY_HASH_FIELDS = ("mode", "ladder_overrides", "locality", "budget_usd", "approve_unknown_pricing", "max_attempts",
                      "max_output_tokens")

TASK_VERB = {"interpretation": "Interpreting", "repair": "Repairing", "visual_review": "Reviewing screenshots of",
             "verification_assist": "Assisting verification of", "knowledge": "Extracting reusable knowledge from"}


# ====================================================================================== per-entry facts
def model_entry(conn: Mapping[str, Any], model: str) -> dict[str, Any]:
    for m in conn.get("models") or []:
        if isinstance(m, Mapping) and m.get("id") == model:
            return dict(m)
    return {}


def _tri(v: Any) -> bool | None:
    if v in ("supported", True):
        return True
    if v in ("rejected", "unsupported", False):
        return False
    return None


def capabilities(conn: Mapping[str, Any], model: str) -> dict[str, Any]:
    """``{vision, tools, context_window}``: True/False only when observed (probe or Ollama /api/show), else None."""
    me = model_entry(conn, model)
    mc = me.get("capabilities") if isinstance(me.get("capabilities"), Mapping) else {}
    cc = conn.get("capabilities") or {}
    vision = mc.get("vision") if isinstance(mc.get("vision"), bool) else _tri(cc.get("images"))
    tools = mc.get("tools") if isinstance(mc.get("tools"), bool) else _tri(cc.get("tools"))
    cw = me.get("context_window") if isinstance(me.get("context_window"), int) else None
    return {"vision": vision, "tools": tools, "context_window": cw}


def availability(conn: Mapping[str, Any] | None, model: str) -> dict[str, Any]:
    if conn is None:
        return {"state": "missing", "probed_at": None, "detail": "connection no longer exists"}
    me = model_entry(conn, model)
    a = me.get("availability") if isinstance(me.get("availability"), Mapping) else None
    if a and a.get("state"):
        return {"state": a["state"], "probed_at": conn.get("last_probe"), "detail": a.get("detail") or ""}
    return {"state": conn.get("state") or "unprobed", "probed_at": conn.get("last_probe"),
            "detail": ((conn.get("capabilities") or {}).get("probe_detail") or "")[:300]}


def price_view(prices: PriceTable, conn: Mapping[str, Any], model: str) -> tuple[dict[str, Any], bool]:
    p = prices.lookup(conn["provider"], model, connection=conn)
    free = bool(p.known and p.input_per_mtok == 0 and p.output_per_mtok == 0)
    return ({"known": bool(p.known), "input_per_mtok": p.input_per_mtok if p.known else None,
             "output_per_mtok": p.output_per_mtok if p.known else None, "source": p.source}, free)


def entry_view(store: ConnectionStore, conn: Mapping[str, Any] | None, model: str, position: int,
               connection_id: str | None = None) -> dict[str, Any]:
    if conn is None:
        return {"position": position, "connection_id": connection_id, "connection_label": None, "provider": None, "model": model,
                "locality": None, "availability": availability(None, model),
                "capabilities": {"vision": None, "tools": None, "context_window": None},
                "price": {"known": False, "input_per_mtok": None, "output_per_mtok": None, "source": ""}, "free": False}
    price, free = price_view(store.prices, conn, model)
    loc = locality_of(conn)
    return {"position": position, "connection_id": conn["connection_id"], "connection_label": conn["label"], "provider": conn["provider"],
            "model": model, "locality": loc, "runs_on": "this PC" if loc == "local" else "cloud",
            "availability": availability(conn, model), "capabilities": capabilities(conn, model), "price": price, "free": free}


# ====================================================================================== ladder views
def task_ladder(store: ConnectionStore, task: str, entries: Iterable[tuple[str, str]] | None = None,
                rationale: str | None = None) -> dict[str, Any]:
    cands = store.ladder_candidates(task, None, entries)
    return {"entries": [entry_view(store, c["conn"], c["model"], c["position"], c["connection_id"]) for c in cands],
            "rationale": rationale or store.rationale(task)}


def ladder_view(store: ConnectionStore) -> dict[str, Any]:
    return {"config_revision": store.config_revision(), "tasks": {t: task_ladder(store, t) for t in TASKS}}


def put_ladder(store: ConnectionStore, task: str, entries: list[Mapping[str, Any]], *, allow_unlisted: bool = True,
               rationale: str = "user") -> dict[str, Any]:
    if task not in TASKS:
        raise ConnectionStoreError(f"unknown task {task!r}; expected one of {TASKS}")
    pairs = _pairs(entries)
    if not pairs:
        if not store.delete_route(task, rationale=rationale):
            store.bump_revision(f"ladder for {task} cleared", {task: rationale})
    else:
        (pc, pm), rest = pairs[0], pairs[1:]
        store.set_route(task, pc, pm, [{"connection": c, "model": m} for c, m in rest], allow_unlisted=allow_unlisted, rationale=rationale)
    return {"config_revision": store.config_revision(), "task": task, **task_ladder(store, task)}


def _pairs(entries: Iterable[Mapping[str, Any]] | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for e in entries or []:
        if not isinstance(e, Mapping):
            raise ConnectionStoreError("each ladder entry must be an object {connection_id, model}")
        cid = e.get("connection_id") or e.get("connection")
        model = e.get("model")
        if not isinstance(cid, str) or not cid or not isinstance(model, str) or not model.strip():
            raise ConnectionStoreError("each ladder entry needs connection_id and an explicit model id")
        out.append((cid, model.strip()))
    return out


# ====================================================================================== model catalog
def model_catalog(store: ConnectionStore, q: str | None = None, task: str | None = None) -> list[dict[str, Any]]:
    """Union of every connection's discovered/explicit models (manual ids already used in ladders are explicit entries)."""
    ql = (q or "").strip().lower()
    out: list[dict[str, Any]] = []
    for conn in store.list():
        if conn["auth_mode"] == "subscription_handoff":
            continue
        for m in conn["models"]:
            mid = m.get("id")
            if not mid:
                continue
            hay = f"{mid} {conn['label']} {conn['provider']} {m.get('display_name') or ''}".lower()
            if ql and ql not in hay:
                continue
            caps = capabilities(conn, mid)
            price, free = price_view(store.prices, conn, mid)
            row = {"connection_id": conn["connection_id"], "connection_label": conn["label"], "provider": conn["provider"], "model": mid,
                   "source": m.get("source"), "locality": locality_of(conn), "capabilities": caps, "price": price, "free": free,
                   "availability": availability(conn, mid)}
            if task:
                row["suitable"], row["suitability_note"] = _suitable(task, caps)
            out.append(row)
    if task:
        out.sort(key=lambda r: (not r["suitable"], -_score(task, r["model"], r["capabilities"])))
    return out


def _suitable(task: str, caps: Mapping[str, Any]) -> tuple[bool, str]:
    if task == "visual_review":
        if caps.get("vision") is False:
            return False, "cannot take images"
        if caps.get("vision") is None:
            return True, "image support not verified"
    return True, ""


_CODER = re.compile(r"(?i)(coder|code|codestral|devstral|starcoder|codellama|codegemma|deepseek-v|qwen3-coder)")
_SIZE = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*b\b")


def _size_b(model: str, details: Mapping[str, Any] | None = None) -> float:
    for src in ((details or {}).get("parameter_size") or "", model):
        m = _SIZE.search(str(src))
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return 0.0


def _score(task: str, model: str, caps: Mapping[str, Any], details: Mapping[str, Any] | None = None) -> float:
    s = min(_size_b(model, details), 200.0) / 10.0
    if task in ("interpretation", "repair") and _CODER.search(model):
        s += 5.0
    if task == "visual_review":
        s += 10.0 if caps.get("vision") else 0.0
    if caps.get("tools"):
        s += 0.5
    return s


# ====================================================================================== presets
def preset(store: ConnectionStore, name: str, *, apply: bool = False, tasks: Iterable[str] | None = None) -> dict[str, Any]:
    """Deterministic ladders from the available connections. ``apply=False`` is a preview (nothing stored)."""
    if name not in PRESETS:
        raise ConnectionStoreError(f"unknown preset {name!r}; expected one of {PRESETS}")
    tasks = [t for t in (tasks or TASKS) if t in TASKS]
    pool: list[dict[str, Any]] = []
    for conn in store.list():
        if conn["auth_mode"] == "subscription_handoff" or conn["state"] in ("auth_failed",):
            continue
        for m in conn["models"]:
            mid = m.get("id")
            if not mid or (m.get("availability") or {}).get("state") == "model_unavailable":
                continue
            if (m.get("capabilities") or {}).get("completion") is False:
                continue          # embedding-only models (Ollama reports completion=false)
            pool.append({"conn": conn, "model": mid, "locality": locality_of(conn), "caps": capabilities(conn, mid),
                         "details": (m.get("meta") or {}).get("ollama") or {}})
    warnings: list[str] = []
    proposed: dict[str, list[tuple[str, str]]] = {}
    has_local = any(p["locality"] == "local" for p in pool)
    has_cloud = any(p["locality"] == "cloud" for p in pool)
    if name in ("local_first", "all_local") and not has_local:
        warnings.append("No local model is available (add a local connection such as Ollama at http://127.0.0.1:11434/v1 and probe it).")
    if name in ("cloud_first", "all_cloud") and not has_cloud:
        warnings.append("No cloud connection is available.")
    for task in tasks:
        if name == "no_ai":
            proposed[task] = []
            continue
        cands = [p for p in pool if _suitable(task, p["caps"])[0]]
        if task == "visual_review":
            known = [p for p in cands if p["caps"].get("vision")]
            cands = known or cands
        loc = [p for p in cands if p["locality"] == "local"]
        cld = [p for p in cands if p["locality"] == "cloud"]
        key = lambda p: (-_score(task, p["model"], p["caps"], p["details"]), p["conn"]["created_at"], p["conn"]["connection_id"], p["model"])  # noqa: E731
        loc.sort(key=key)
        cld.sort(key=key)
        order = {"local_first": loc + cld, "cloud_first": cld + loc, "all_local": loc, "all_cloud": cld}[name]
        chosen = order[:MAX_PRESET_ENTRIES]
        proposed[task] = [(p["conn"]["connection_id"], p["model"]) for p in chosen]
        if not chosen:
            where = {"all_local": "local ", "all_cloud": "cloud "}.get(name, "")
            if task == "visual_review":
                warnings.append(f"No {where}model supports images; visual review has no route.")
            else:
                warnings.append(f"No {where}model is available for {task}; it has no route.")
        elif task == "visual_review" and not any(p["caps"].get("vision") for p in chosen):
            warnings.append("No model has verified image support; visual review may fail until one is probed.")
        for p in chosen:
            price, free = price_view(store.prices, p["conn"], p["model"])
            if not price["known"]:
                warnings.append(f"{p['model']} ({p['conn']['label']}) has no known price: it needs a price, an output-token cap or approval before use.")
    if name == "no_ai":
        warnings.append("AI is off: projects produce recovered evidence and a scaffold only, never an AI implementation.")
    warnings = list(dict.fromkeys(warnings))
    rev = store.config_revision()
    if apply:
        why = f"preset:{name}"
        with store.db.transaction():
            for task, pairs in proposed.items():
                if pairs:
                    (pc, pm), rest = pairs[0], pairs[1:]
                    store.set_route(task, pc, pm, [{"connection": c, "model": m} for c, m in rest], allow_unlisted=True, bump=False)
                else:
                    store.delete_route(task, bump=False)
            rev = store.bump_revision(f"preset {name} applied", {t: why for t in proposed})
    out_tasks = {t: task_ladder(store, t, pairs, f"preset:{name}") for t, pairs in proposed.items()}
    return {"preset": name, "applied": bool(apply), "config_revision": rev, "tasks": out_tasks, "warnings": warnings}


# ====================================================================================== project policy (section 3)
def normalize_policy(p: Mapping[str, Any] | None) -> dict[str, Any]:
    d = dict(p or {})
    d["mode"] = str(d.get("mode") or "no_ai")
    d["locality"] = str(d.get("locality") or "any")
    ov = d.get("ladder_overrides")
    d["ladder_overrides"] = dict(ov) if isinstance(ov, Mapping) else {}
    return d


def policy_hash(p: Mapping[str, Any] | None) -> str:
    n = normalize_policy(p)
    doc = {k: n.get(k) for k in POLICY_HASH_FIELDS}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()[:16]


def ai_enabled(p: Mapping[str, Any] | None) -> bool:
    return normalize_policy(p)["mode"] in AI_ON_MODES


def override_entries(p: Mapping[str, Any] | None, task: str) -> list[tuple[str, str]] | None:
    """The project's own ladder for ``task`` (None = use the global ladder). Overrides apply unless mode is ``inherit``."""
    n = normalize_policy(p)
    if n["mode"] == "inherit":
        return None
    ov = n["ladder_overrides"].get(task)
    if not ov:
        return None
    return _pairs(ov)


def locality_block(p: Mapping[str, Any] | None, loc: str | None) -> str | None:
    n = normalize_policy(p)
    if n["locality"] == "local_only" and loc != "local":
        return "this project is set to local models only and this model runs in the cloud"
    if n["locality"] == "cloud_only" and loc != "cloud":
        return "this project is set to cloud models only and this model runs on this PC"
    return None


def validate_policy_update(store: ConnectionStore | None, current: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    """Merge ``update`` into ``current`` (keys not given are kept) and validate the additive fields."""
    merged = dict(current or {})
    for k, v in (update or {}).items():
        merged[k] = v
    mode = merged.get("mode") or "no_ai"
    if mode not in POLICY_MODES:
        raise ConnectionStoreError(f"unknown AI mode {mode!r}; expected one of {POLICY_MODES}")
    loc = merged.get("locality") or "any"
    if loc not in LOCALITIES:
        raise ConnectionStoreError(f"unknown locality {loc!r}; expected one of {LOCALITIES}")
    ov = merged.get("ladder_overrides") or {}
    if not isinstance(ov, Mapping):
        raise ConnectionStoreError("ladder_overrides must be an object {task: [{connection_id, model}]}")
    clean: dict[str, list[dict[str, str]]] = {}
    for task, ents in ov.items():
        if task not in TASKS:
            raise ConnectionStoreError(f"unknown task {task!r} in ladder_overrides; expected one of {TASKS}")
        pairs = _pairs(ents)
        if store is not None:
            for cid, _m in pairs:
                store.get(cid)            # raises for an unknown connection
        if pairs:
            clean[task] = [{"connection_id": c, "model": m} for c, m in pairs]
    merged["ladder_overrides"] = clean
    merged["locality"] = loc
    merged["mode"] = mode
    for k in ("budget_usd", "max_output_tokens", "max_attempts"):
        v = merged.get(k)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ConnectionStoreError(f"{k} must be a number") from None
        if not (f == f) or f < 0:
            raise ConnectionStoreError(f"{k} must be >= 0")
    if "approve_unknown_pricing" in merged:
        merged["approve_unknown_pricing"] = bool(merged["approve_unknown_pricing"])
    return merged


def effective_ladders(store: ConnectionStore, p: Mapping[str, Any] | None) -> dict[str, Any]:
    n = normalize_policy(p)
    out: dict[str, Any] = {}
    for t in TASKS:
        if n["mode"] == "no_ai":
            out[t] = {"entries": [], "rationale": "project policy: no AI", "source": "policy"}
            continue
        ov = override_entries(n, t)
        lad = task_ladder(store, t, ov, "project override" if ov is not None else None)
        for e in lad["entries"]:
            why = locality_block(n, e.get("locality"))
            e["policy_skipped"] = why
        lad["source"] = "project" if ov is not None else "global"
        out[t] = lad
    return out


# ====================================================================================== activity feed (section 5)
ACTIVITY_FIELDS = ("at", "kind", "text", "plan_item_id", "job_id", "candidate_id", "evidence_ids", "task", "provider", "model", "locality",
                   "outcome", "tokens_in", "tokens_out", "cost_usd", "cost_known", "fallback_reason", "config_revision", "origin")


def emit_activity(events: EventLog, text: str, *, kind: str, case_id: str | None = None, job_id: str | None = None,
                  **fields: Any) -> dict[str, Any]:
    """One plain-English line for the activity feed (event kind ``ai.activity``). Never carries keys or raw prompts: every
    string is redacted; prompts are referenced only by ``prompt_sha256``."""
    payload: dict[str, Any] = {k: None for k in ACTIVITY_FIELDS}
    payload.update({"at": now_iso(), "kind": kind, "text": redact(text)[:600], "job_id": job_id, "evidence_ids": []})
    for k, v in fields.items():
        if k in ("prompt", "messages", "system"):
            continue          # defensive: never store prompt text
        payload[k] = redact(v)[:600] if isinstance(v, str) else v
    if not isinstance(payload.get("evidence_ids"), list):
        payload["evidence_ids"] = [payload["evidence_ids"]] if payload["evidence_ids"] else []
    events.emit("ai.activity", payload, case_id=case_id, job_id=job_id)
    return payload


def activity(events: EventLog, case_id: str, *, since: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    rows = events.db.query("SELECT seq, ts, case_id, job_id, payload FROM events WHERE kind='ai.activity' AND case_id=? AND seq>? "
                           "ORDER BY seq LIMIT ?", (case_id, int(since), int(min(max(limit, 1), 5000))))
    out = []
    for r in rows:
        p = json.loads(r["payload"])
        p["seq"] = r["seq"]
        out.append(p)
    return out


def money(cost: float | None, known: bool | None, free: bool = False) -> str:
    if free or (cost == 0 and known):
        return "no cost"
    if cost is None:
        return "cost unknown"
    return f"${cost:.4f}" + ("" if known else " (estimate: assumed ceiling)")
