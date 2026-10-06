"""Price table. Unknown pricing is never zero: it yields a conservative explicit limit and ``approval_required``.

Sources and check dates are recorded per entry. Only Anthropic entries are populated from a source that was readable on
2026-10-06 (https://platform.claude.com/docs/en/about-claude/pricing). OpenAI, Gemini and OpenRouter prices are
deliberately NOT hard-coded: their price pages were unreachable from the implementation host, so those models are
``known=False`` until the user supplies a price on the connection's model entry (``{"id":..., "price": {...}}``) or the
endpoint reports one (OpenRouter ``/models`` pricing, OpenRouter ``usage.cost``).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
ANTHROPIC_CHECKED = "2026-10-06"

# Conservative stand-in used for the *reservation ceiling* when a price is unknown. Above every listed current model.
UNKNOWN_INPUT_PER_MTOK = 30.0
UNKNOWN_OUTPUT_PER_MTOK = 150.0

IMAGE_TOKEN_ESTIMATE = 2000      # per image, deliberately generous
CHARS_PER_TOKEN_ESTIMATE = 3.0   # deliberately low (=> higher estimate) so ceilings are not underestimated


@dataclass(frozen=True)
class Price:
    provider: str
    model: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None       # 5-minute write
    cache_write_1h_per_mtok: float | None = None
    known: bool = True
    source: str = ""
    checked_on: str = ""
    note: str = ""

    @property
    def approval_required(self) -> bool:
        return not self.known

    def cost(self, usage: Any) -> float:
        """Dollar cost of a Usage. Uses cache-aware rates where this price defines them."""
        total_in = int(getattr(usage, "input_tokens", 0) or 0)
        cached = int(getattr(usage, "cached_tokens", 0) or 0)
        wrote = int(getattr(usage, "cache_write_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        fresh = max(0, total_in - cached - wrote)
        read_rate = self.cache_read_per_mtok if self.cache_read_per_mtok is not None else self.input_per_mtok
        ttl = getattr(usage, "cache_write_ttl", "5m")
        w_rate = self.cache_write_1h_per_mtok if ttl == "1h" else self.cache_write_per_mtok
        if w_rate is None:
            w_rate = self.input_per_mtok
        total = fresh * self.input_per_mtok + cached * read_rate + wrote * w_rate + out * self.output_per_mtok
        return total / 1_000_000.0

    def ceiling(self, est_input_tokens: int, max_output_tokens: int, *, cache_write: bool = False) -> float:
        """Upper-bound reservation: every input token at the dearest applicable rate, every output slot used."""
        in_rate = self.input_per_mtok
        if cache_write:
            in_rate = max(in_rate, self.cache_write_per_mtok or 0.0, self.cache_write_1h_per_mtok or 0.0)
        return (est_input_tokens * in_rate + max_output_tokens * self.output_per_mtok) / 1_000_000.0

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "input_per_mtok": self.input_per_mtok,
                "output_per_mtok": self.output_per_mtok, "cache_read_per_mtok": self.cache_read_per_mtok,
                "cache_write_per_mtok": self.cache_write_per_mtok, "known": self.known, "source": self.source,
                "checked_on": self.checked_on, "approval_required": self.approval_required, "note": self.note}


def _a(model: str, i: float, o: float, w5: float, w1h: float, r: float) -> Price:
    return Price("anthropic", model, i, o, cache_read_per_mtok=r, cache_write_per_mtok=w5, cache_write_1h_per_mtok=w1h,
                 known=True, source=ANTHROPIC_SOURCE, checked_on=ANTHROPIC_CHECKED)


# (input, output, 5m write, 1h write, cache hit) USD per million tokens, from the Anthropic pricing page.
_ANTHROPIC = [
    _a("claude-fable-5-1", 10, 50, 12.5, 20, 0.25),
    _a("claude-mythos-5-1", 10, 50, 12.5, 20, 0.25),
    _a("claude-fable-5", 10, 50, 12.5, 20, 1.0),
    _a("claude-mythos-5", 10, 50, 12.5, 20, 1.0),
    _a("claude-opus-5-5", 4, 20, 5, 8, 0.20),
    _a("claude-opus-5", 5, 25, 6.25, 10, 0.50),
    _a("claude-opus-4-8", 5, 25, 6.25, 10, 0.50),
    _a("claude-opus-4-7", 5, 25, 6.25, 10, 0.50),
    _a("claude-opus-4-6", 5, 25, 6.25, 10, 0.50),
    _a("claude-opus-4-5", 5, 25, 6.25, 10, 0.50),
    _a("claude-opus-4-1", 15, 75, 18.75, 30, 1.50),
    _a("claude-opus-4", 15, 75, 18.75, 30, 1.50),
    _a("claude-sonnet-5-5", 2, 10, 2.5, 4, 0.20),
    _a("claude-sonnet-5", 2, 10, 2.5, 4, 0.20),
    _a("claude-sonnet-4-6", 3, 15, 3.75, 6, 0.30),
    _a("claude-sonnet-4-5", 3, 15, 3.75, 6, 0.30),
    _a("claude-sonnet-4", 3, 15, 3.75, 6, 0.30),
    _a("claude-haiku-4-5", 1, 5, 1.25, 2, 0.10),
    _a("claude-haiku-3-5", 0.8, 4, 1, 1.6, 0.08),
]

_DATED = re.compile(r"-\d{8}$")


def unknown_price(provider: str, model: str, reason: str = "model not in price table") -> Price:
    return Price(provider, model, UNKNOWN_INPUT_PER_MTOK, UNKNOWN_OUTPUT_PER_MTOK, known=False,
                 source="conservative default (not a real price)", note=reason)


def local_price(model: str) -> Price:
    return Price("local", model, 0.0, 0.0, known=True, source="local endpoint: no metered cost assumed",
                 note="user hardware; if this endpoint is actually a metered remote service, set a model price")


class PriceTable:
    def __init__(self, extra: list[Price] | None = None):
        self._by_key: dict[tuple[str, str], Price] = {(p.provider, p.model): p for p in _ANTHROPIC}
        for p in extra or []:
            self._by_key[(p.provider, p.model)] = p

    def register(self, price: Price) -> None:
        self._by_key[(price.provider, price.model)] = price

    def lookup(self, provider: str, model: str, *, connection: Mapping[str, Any] | None = None) -> Price:
        """Resolve a price. Order: user/discovered price on the connection's model entry, built-in table, local, unknown."""
        if connection:
            for m in connection.get("models") or []:
                if isinstance(m, Mapping) and m.get("id") == model and isinstance(m.get("price"), Mapping):
                    p = m["price"]
                    try:
                        i, o = float(p["input_per_mtok"]), float(p["output_per_mtok"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not (math.isfinite(i) and math.isfinite(o)) or i < 0 or o < 0:
                        continue
                    src = "user-supplied" if m.get("source") == "explicit" else "reported by endpoint"
                    return Price(provider, model, i, o,
                                 cache_read_per_mtok=_f(p.get("cache_read_per_mtok")),
                                 cache_write_per_mtok=_f(p.get("cache_write_per_mtok")),
                                 known=True, source=f"{src} model entry", checked_on=str(p.get("checked_on", "")))
        hit = self._by_key.get((provider, model))
        if hit is None and provider == "anthropic":
            stripped = _DATED.sub("", model)
            hit = self._by_key.get((provider, stripped))
        if hit is not None:
            return hit
        if provider == "local" and (connection is None or connection.get("auth_mode") in ("local", "none")):
            return local_price(model)
        return unknown_price(provider, model)


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------------- token estimation
def estimate_request_tokens(request: Any) -> int:
    """Deliberately generous input-token estimate used only for reservation ceilings (never for billing)."""
    chars = len(request.system or "")
    images = 0
    for m in request.messages:
        content = m.content
        if isinstance(content, str):
            chars += len(content)
            continue
        for p in content:
            t = getattr(p, "type", "")
            if t == "text":
                chars += len(p.text)
            elif t == "image":
                images += 1
            elif t == "tool_use":
                chars += len(json.dumps(p.input, default=str)) + len(p.name)
            elif t == "tool_result":
                chars += len(p.content or "")
    for t in request.tools:
        chars += len(t.name) + len(t.description) + len(json.dumps(t.parameters, default=str))
    if request.json_schema is not None:
        chars += len(json.dumps(request.json_schema.schema, default=str))
    return int(chars / CHARS_PER_TOKEN_ESTIMATE) + images * IMAGE_TOKEN_ESTIMATE + 64


class ApprovalRequired(Exception):
    """A call would use a model with unknown pricing. Re-issue with approve_unknown_pricing=True to spend up to the
    conservative ceiling (the reservation still has to fit the budget)."""

    def __init__(self, models: list[tuple[str, str]], ceiling_usd: float | None = None):
        self.models = models
        self.ceiling_usd = ceiling_usd
        super().__init__("pricing unknown for " + ", ".join(f"{p}:{m}" for p, m in models) +
                         "; approval required (never priced at zero)")
