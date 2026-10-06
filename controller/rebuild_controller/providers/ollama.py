"""Ollama enrichment for local OpenAI-compatible connections (docs/AI_LADDER.md section 6).

The OpenAI-compatible ``/v1/models`` listing only gives ids. When the endpoint is an Ollama server (``GET {root}/api/tags``
answers), ``POST {root}/api/show`` tells us, per model, what it can actually take (``capabilities``: completion / vision /
tools, newer servers) and its trained context length (``model_info["<arch>.context_length"]``). Nothing is guessed from a
model name: anything the server does not report stays ``None`` (unknown).
"""
from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

MAX_MODELS = 40


def ollama_root(endpoint: str) -> str | None:
    """``http://127.0.0.1:11434/v1`` -> ``http://127.0.0.1:11434``."""
    ep = (endpoint or "").rstrip("/")
    u = urlparse(ep)
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    return ep[: -len("/v1")] if ep.endswith("/v1") else ep


def _context_length(model_info: Any) -> int | None:
    if not isinstance(model_info, dict):
        return None
    for k, v in model_info.items():
        if isinstance(k, str) and k.endswith(".context_length") and isinstance(v, int) and v > 0:
            return v
    return None


def _caps(show: dict[str, Any]) -> dict[str, Any]:
    caps = show.get("capabilities")
    if isinstance(caps, list):
        names = {str(c).lower() for c in caps}
        return {"completion": "completion" in names, "vision": "vision" in names, "tools": "tools" in names, "source": "ollama /api/show"}
    # older servers: no capabilities list; a vision projector is still visible in projector_info / model_info
    info = show.get("model_info") if isinstance(show.get("model_info"), dict) else {}
    vision = True if (show.get("projector_info") or any(".vision." in str(k) for k in info)) else None
    return {"completion": None, "vision": vision, "tools": None, "source": "ollama /api/show (no capabilities list)"}


def enrich(endpoint: str, model_ids: Iterable[str], *, transport: httpx.BaseTransport | None = None,
           timeout_s: float = 8.0) -> dict[str, Any] | None:
    """Return ``{"server": "ollama", "version": str|None, "models": {id: {capabilities, context_window, details}}}`` or None
    when the endpoint is not an Ollama server. Never raises for network/shape problems."""
    root = ollama_root(endpoint)
    if root is None:
        return None
    try:
        with httpx.Client(transport=transport, timeout=httpx.Timeout(timeout_s, connect=min(3.0, timeout_s))) as c:
            r = c.get(f"{root}/api/tags")
            if r.status_code != 200:
                return None
            tags = r.json()
            if not isinstance(tags, dict) or not isinstance(tags.get("models"), list):
                return None
            details = {str(m.get("name") or m.get("model")): (m.get("details") or {}) for m in tags["models"] if isinstance(m, dict)}
            version = None
            try:
                v = c.get(f"{root}/api/version")
                if v.status_code == 200:
                    version = (v.json() or {}).get("version")
            except (httpx.HTTPError, ValueError):
                pass
            out: dict[str, Any] = {"server": "ollama", "version": version, "models": {}}
            for mid in list(model_ids)[:MAX_MODELS]:
                entry: dict[str, Any] = {"capabilities": {"completion": None, "vision": None, "tools": None, "source": "unknown"},
                                         "context_window": None}
                d = details.get(mid)
                if isinstance(d, dict) and d:
                    entry["details"] = {k: d.get(k) for k in ("family", "parameter_size", "quantization_level", "format") if d.get(k)}
                try:
                    s = c.post(f"{root}/api/show", json={"model": mid})
                    if s.status_code == 200 and isinstance(s.json(), dict):
                        show = s.json()
                        entry["capabilities"] = _caps(show)
                        entry["context_window"] = _context_length(show.get("model_info"))
                except Exception:  # noqa: BLE001 - one model's metadata never fails the probe
                    pass
                out["models"][mid] = entry
            return out
    except Exception:  # noqa: BLE001 - enrichment is optional
        return None
