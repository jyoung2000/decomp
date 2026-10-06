"""Provider adapter contract: typed requests/responses, typed errors, SSE + HTTP plumbing, capability probing.

Design rules
------------
* Adapters are synchronous (the job runner is thread-based) and take an injectable ``httpx`` transport so tests never
  touch the network.
* Streaming is the default transport; usage is accounted from the stream. A stream that dies after the provider has
  started answering raises ``AmbiguousCompletion`` (carrying any partial usage) - the request may have been billed.
* Assistant turns returned by a provider keep the provider's own content blocks verbatim (``Message.provider_blocks``)
  and are re-emitted verbatim when replayed to the *same* adapter family (thinking signatures, encrypted reasoning,
  Gemini thought signatures). Replayed to a different provider they degrade to the typed ``content`` parts.
* No capability is assumed. ``probe_capabilities`` records what an endpoint actually did, with tri-state-plus strings:
  supported | rejected | accepted_no_call | accepted_invalid_output | accepted_unverified | untested | error.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import zlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterator, Literal, Mapping, Union

import httpx

from .secrets import redact

# ======================================================================================= errors
class ProviderError(Exception):
    """Base class. ``retry_safe`` is True ONLY when the provider (or the network layer) confirms the request was not
    processed, i.e. a single re-send cannot double-bill."""

    def __init__(self, message: str, *, provider: str = "", status: int | None = None, retry_safe: bool = False,
                 request_sent: bool = True, partial_usage: "Usage | None" = None, retry_after: float | None = None):
        super().__init__(redact(message)[:600])
        self.provider = provider
        self.status = status
        self.retry_safe = retry_safe
        self.request_sent = request_sent
        self.partial_usage = partial_usage
        self.retry_after = retry_after

    @property
    def kind(self) -> str:
        return type(self).__name__


class AuthError(ProviderError):
    def __init__(self, message: str, **kw: Any):
        kw.setdefault("retry_safe", True)
        super().__init__(message, **kw)


class RateLimit(ProviderError):
    def __init__(self, message: str, **kw: Any):
        kw.setdefault("retry_safe", True)
        super().__init__(message, **kw)


class UsageLimit(ProviderError):
    """Quota/credit/subscription limit reached. Retrying will not help until the window resets or credit is added."""

    def __init__(self, message: str, **kw: Any):
        kw.setdefault("retry_safe", True)
        super().__init__(message, **kw)


class InvalidRequest(ProviderError):
    def __init__(self, message: str, **kw: Any):
        kw.setdefault("retry_safe", True)
        super().__init__(message, **kw)


class CapabilityError(InvalidRequest):
    """The adapter refused to build a request the provider/model is known to reject (raised before any send)."""

    def __init__(self, message: str, **kw: Any):
        kw.setdefault("request_sent", False)
        super().__init__(message, **kw)


class Unreachable(ProviderError):
    """Connection could not be established. Nothing was sent."""

    def __init__(self, message: str, **kw: Any):
        kw.setdefault("retry_safe", True)
        kw.setdefault("request_sent", False)
        super().__init__(message, **kw)


class Timeout(ProviderError):
    """``request_sent=False``/``retry_safe=True`` only for connect/pool timeouts; read/write timeouts are not retry-safe."""


class ProviderUnavailable(ProviderError):
    """5xx / overloaded. Not retry-safe: the provider has not confirmed the request was not processed."""


class AmbiguousCompletion(ProviderError):
    """The response was cut off or malformed after the provider began answering. Possibly billed; never auto-retried."""


# ======================================================================================= typed content
@dataclass(frozen=True)
class TextPart:
    text: str
    type: str = field(default="text", init=False)


@dataclass(frozen=True)
class ImagePart:
    media_type: str = "image/png"
    data: str | None = None           # base64
    url: str | None = None
    type: str = field(default="image", init=False)

    @staticmethod
    def from_bytes(raw: bytes, media_type: str = "image/png") -> "ImagePart":
        return ImagePart(media_type=media_type, data=base64.b64encode(raw).decode("ascii"))

    def data_url(self) -> str:
        if self.data is None:
            raise ValueError("image has no inline data")
        return f"data:{self.media_type};base64,{self.data}"


@dataclass(frozen=True)
class ToolUsePart:
    id: str
    name: str
    input: dict
    type: str = field(default="tool_use", init=False)


@dataclass(frozen=True)
class ToolResultPart:
    tool_use_id: str
    content: str
    is_error: bool = False
    name: str | None = None           # needed by Gemini (functionResponse is keyed by function name)
    type: str = field(default="tool_result", init=False)


ContentPart = Union[TextPart, ImagePart, ToolUsePart, ToolResultPart]


@dataclass
class Message:
    role: Literal["user", "assistant"]
    content: Union[str, list[ContentPart]]
    provider: str | None = None                 # adapter family that produced provider_blocks
    provider_blocks: list[dict] | None = None   # verbatim provider content, replayed as-is to the same family

    def parts(self) -> list[ContentPart]:
        return [TextPart(self.content)] if isinstance(self.content, str) else list(self.content)

    @staticmethod
    def user(text: str) -> "Message":
        return Message("user", text)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    strict: bool = False


@dataclass(frozen=True)
class JsonSchema:
    name: str
    schema: dict
    strict: bool = True


@dataclass(frozen=True)
class Reasoning:
    effort: str | None = None          # provider vocabulary: low|medium|high|xhigh|max (anthropic/openai), low|high (gemini level)
    budget_tokens: int | None = None   # budget-style thinking (older Claude, Gemini thinkingBudget)
    summary: str | None = None         # request a readable reasoning summary where supported
    mode: str | None = None            # force "adaptive"|"budget" for models this code does not recognise


@dataclass
class Request:
    model: str
    messages: list[Message]
    system: str | None = None
    tools: list[Tool] = field(default_factory=list)
    tool_choice: Union[str, dict, None] = None     # "auto" | "none" | "required" | {"name": ...}
    json_schema: JsonSchema | None = None
    reasoning: Reasoning | None = None
    max_output_tokens: int = 4096
    temperature: float | None = None
    stream: bool = True
    cache: bool = False
    cache_ttl: str = "5m"                          # "5m" | "1h" (anthropic)
    timeout_s: float | None = None
    metadata: dict = field(default_factory=dict)

    def needs(self) -> set[str]:
        n: set[str] = set()
        if self.tools:
            n.add("tools")
        if self.json_schema is not None:
            n.add("json_schema")
        for m in self.messages:
            if not isinstance(m.content, str) and any(getattr(p, "type", "") == "image" for p in m.content):
                n.add("images")
        return n

    def fingerprint(self) -> str:
        """Stable hash of everything that shapes the answer (not the model, not transport options)."""
        def part(p: Any) -> Any:
            d = dict(p.__dict__)
            d["type"] = p.type
            return d
        doc = {
            "system": self.system,
            "messages": [{"role": m.role, "content": m.content if isinstance(m.content, str) else [part(p) for p in m.content]}
                         for m in self.messages],
            "tools": [t.__dict__ for t in self.tools], "tool_choice": self.tool_choice,
            "json_schema": self.json_schema.__dict__ if self.json_schema else None,
            "reasoning": self.reasoning.__dict__ if self.reasoning else None,
            "max": self.max_output_tokens, "temp": self.temperature,
        }
        return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class Usage:
    """Normalised usage. ``input_tokens`` is the TOTAL prompt size including cached/cache-write tokens."""
    input_tokens: int = 0
    output_tokens: int = 0            # includes reasoning/thinking tokens (all providers bill them as output)
    cached_tokens: int = 0            # subset of input_tokens served from cache
    cache_write_tokens: int = 0       # subset of input_tokens written to cache (anthropic)
    reasoning_tokens: int = 0         # informational subset of output_tokens when reported
    reported_cost_usd: float | None = None   # when the endpoint itself reports a charge (OpenRouter usage.cost)
    cache_write_ttl: str = "5m"
    known: bool = True                # False when the provider never reported usage

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Response:
    provider: str
    model: str
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str                       # end_turn | tool_use | max_tokens | refusal | content_filter | other:<raw>
    usage: Usage
    request_id: str | None = None
    provider_blocks: list[dict] | None = None
    thinking: str = ""
    notes: list[str] = field(default_factory=list)
    provider_key: str | None = None

    def assistant_message(self) -> Message:
        parts: list[ContentPart] = []
        if self.text:
            parts.append(TextPart(self.text))
        parts.extend(ToolUsePart(c.id, c.name, c.input) for c in self.tool_calls)
        return Message("assistant", parts, provider=self.provider_key, provider_blocks=self.provider_blocks)


@dataclass
class ModelInfo:
    id: str
    display_name: str | None = None
    source: str = "discovered"             # discovered | explicit
    context_window: int | None = None
    max_output: int | None = None
    price: dict | None = None              # {"input_per_mtok","output_per_mtok"} only when the endpoint reported it
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "source": self.source}
        for k in ("display_name", "context_window", "max_output", "price"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.meta:
            d["meta"] = self.meta
        return d


@dataclass
class ModelDiscovery:
    supported: bool
    models: list[ModelInfo] = field(default_factory=list)
    error: str | None = None
    status: int | None = None


@dataclass
class ProbeReport:
    state: str                              # ok | auth_failed | unreachable | limited | error
    capabilities: dict[str, Any]
    usage: Usage
    detail: str = ""


def new_capabilities() -> dict[str, Any]:
    return {"streaming": "untested", "usage_in_stream": "untested", "tools": "untested", "json_schema": "untested",
            "images": "untested", "discovery": "untested"}


# ======================================================================================= error classification
_USAGE_RE = re.compile(r"(?i)(insufficient[_ ]quota|exceeded your current quota|credit balance|usage limit|quota exceeded|"
                       r"billing|out of credits|spend limit|spending limit|plan limit|limit reached|resource_exhausted.*quota)")
_AUTH_RE = re.compile(r"(?i)(api[_ ]key (is )?(not valid|invalid)|API_KEY_INVALID|invalid x-api-key|incorrect api key|"
                      r"invalid api key|authentication)")


def _error_fields(body_text: str) -> tuple[str, str]:
    """Return (message, code) from the common provider error envelopes."""
    msg, code = body_text.strip()[:500], ""
    try:
        data = json.loads(body_text)
    except ValueError:
        return msg, code
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        msg = str(err.get("message") or msg)
        code = str(err.get("code") or err.get("type") or err.get("status") or "")
    elif isinstance(err, str):
        msg = err
    elif isinstance(data, dict) and data.get("message"):
        msg = str(data["message"])
    return msg, code


def _retry_after(headers: Mapping[str, str]) -> float | None:
    for k, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        v = headers.get(k)
        if v:
            try:
                return max(0.0, float(v) * scale)
            except ValueError:
                pass
    return None


def classify_error(provider: str, status: int, body_text: str, headers: Mapping[str, str] | None = None) -> ProviderError:
    headers = headers or {}
    msg, code = _error_fields(body_text)
    text = f"{provider} HTTP {status}: {msg}" + (f" [{code}]" if code else "")
    kw = {"provider": provider, "status": status}
    if status in (401, 403) or _AUTH_RE.search(msg) or _AUTH_RE.search(code):
        return AuthError(text, **kw)
    if status == 402 or (status in (400, 429) and (_USAGE_RE.search(msg) or _USAGE_RE.search(code))):
        return UsageLimit(text, **kw)
    if status == 429:
        return RateLimit(text, retry_after=_retry_after(headers), **kw)
    if status in (408, 504):
        return Timeout(text, retry_safe=False, **kw)
    if status >= 500:
        return ProviderUnavailable(text, retry_safe=False, **kw)
    if 400 <= status < 500:
        return InvalidRequest(text, **kw)
    return ProviderError(text, **kw)


# ======================================================================================= sse
def iter_sse(resp: httpx.Response) -> Iterator[tuple[str | None, str]]:
    event: str | None = None
    data: list[str] = []
    for line in resp.iter_lines():
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = None, []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
    if data:
        yield event, "\n".join(data)


def parse_json_args(raw: str | None) -> tuple[dict, bool]:
    """Parse tool-call arguments. Returns (value, ok). Invalid/truncated JSON is surfaced, never silently coerced."""
    if raw is None or raw.strip() == "":
        return {}, True
    try:
        v = json.loads(raw)
    except ValueError:
        return {"_raw_arguments": raw}, False
    return (v, True) if isinstance(v, dict) else ({"_value": v}, True)


# ======================================================================================= adapter base
OnText = Callable[[str], None]


def _tiny_png(rgb: tuple[int, int, int], size: int = 16) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


RED_PNG = _tiny_png((255, 0, 0))


class ProviderAdapter(ABC):
    provider_key: str = "base"      # adapter family; keys provider_blocks replay
    provider_name: str = "base"     # connection provider this instance serves (openai|openrouter|local|...)

    def __init__(self, *, endpoint: str = "", api_key: str | None = None, transport: httpx.BaseTransport | None = None,
                 timeout_s: float = 120.0, extra_headers: Mapping[str, str] | None = None):
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self.transport = transport
        self.timeout_s = timeout_s
        self.extra_headers = dict(extra_headers or {})

    def __repr__(self) -> str:  # never print the key
        return f"<{type(self).__name__} {self.provider_key} endpoint={self.endpoint!r}>"

    # -- required surface
    @abstractmethod
    def complete(self, request: Request, on_text: OnText | None = None) -> Response: ...

    @abstractmethod
    def discover_models(self) -> ModelDiscovery: ...

    # -- http plumbing
    def _client(self, timeout_s: float | None) -> httpx.Client:
        t = timeout_s or self.timeout_s
        return httpx.Client(transport=self.transport, timeout=httpx.Timeout(t, connect=min(10.0, t)),
                            follow_redirects=False)

    def _send(self, method: str, url: str, *, body: dict | None, headers: Mapping[str, str], stream: bool,
              timeout_s: float | None, handler: Callable[[httpx.Response], Any]) -> Any:
        """Open the request, map HTTP/transport failures to typed errors, hand the 2xx response to ``handler``.

        Failure taxonomy: nothing sent (connect error/timeout, pool timeout) => retry-safe. Headers received and 4xx
        => provider says it did not process => retry-safe for 401/403/429/400-class. Anything that dies after a 2xx
        began => AmbiguousCompletion.
        """
        prov = self.provider_name
        try:
            with self._client(timeout_s) as client:
                with client.stream(method, url, json=body, headers=dict(headers)) as resp:
                    if resp.status_code >= 400:
                        resp.read()
                        raise classify_error(prov, resp.status_code, resp.text, resp.headers)
                    try:
                        if not stream:
                            resp.read()
                        return handler(resp)
                    except ProviderError:
                        raise
                    except httpx.TimeoutException as e:
                        raise AmbiguousCompletion(f"{prov}: timed out mid-response ({type(e).__name__})", provider=prov) from e
                    except httpx.TransportError as e:
                        raise AmbiguousCompletion(f"{prov}: connection dropped mid-response ({type(e).__name__})",
                                                  provider=prov) from e
        except ProviderError:
            raise
        except (httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            raise Timeout(f"{prov}: connect timeout", provider=prov, retry_safe=True, request_sent=False) from e
        except httpx.TimeoutException as e:
            raise Timeout(f"{prov}: {type(e).__name__} waiting for response", provider=prov, retry_safe=False) from e
        except httpx.ConnectError as e:
            raise Unreachable(f"{prov}: cannot connect to {self.endpoint or url}: {e}", provider=prov) from e
        except httpx.TransportError as e:
            raise AmbiguousCompletion(f"{prov}: transport error {type(e).__name__}", provider=prov) from e
        except httpx.InvalidURL as e:
            raise InvalidRequest(f"{prov}: invalid endpoint URL", provider=prov, request_sent=False) from e

    def _get_json(self, url: str, headers: Mapping[str, str], timeout_s: float | None = None) -> Any:
        def h(resp: httpx.Response) -> Any:
            try:
                return resp.json()
            except ValueError as e:
                raise InvalidRequest(f"{self.provider_name}: non-JSON response from {url}", provider=self.provider_name,
                                     status=resp.status_code) from e
        return self._send("GET", url, body=None, headers=headers, stream=False, timeout_s=timeout_s or 30.0, handler=h)

    # -- capability probing (generic; spends a few tokens - the caller reserves budget)
    PROBE_MAX_TOKENS = 256

    def probe_capabilities(self, model: str, *, test: tuple[str, ...] = ("basic", "tools", "json_schema", "images")) -> ProbeReport:
        caps = new_capabilities()
        total = Usage()
        detail: list[str] = []

        def add(u: Usage) -> None:
            total.input_tokens += u.input_tokens
            total.output_tokens += u.output_tokens
            total.cached_tokens += u.cached_tokens
            total.cache_write_tokens += u.cache_write_tokens
            if u.reported_cost_usd is not None:
                total.reported_cost_usd = (total.reported_cost_usd or 0.0) + u.reported_cost_usd
            if not u.known:
                total.known = False

        def run(name: str, req: Request) -> Response | None:
            try:
                r = self.complete(req)
                add(r.usage)
                return r
            except (AuthError, Unreachable, RateLimit, UsageLimit) as e:
                raise
            except Timeout as e:
                caps[name] = "error"
                detail.append(f"{name}: timeout")
            except InvalidRequest as e:
                caps[name] = "rejected"
                detail.append(f"{name}: rejected: {e}")
            except ProviderError as e:
                caps[name] = "error"
                detail.append(f"{name}: {e.kind}: {e}")
            return None

        def base_req(**kw: Any) -> Request:
            return Request(model=model, max_output_tokens=self.PROBE_MAX_TOKENS, **kw)

        try:
            if "basic" in test:
                r = run("streaming", base_req(messages=[Message.user("Reply with the single word: pong")]))
                if r is not None:
                    caps["streaming"] = "supported"
                    caps["usage_in_stream"] = "supported" if r.usage.known and (r.usage.output_tokens or r.usage.input_tokens) else "not_reported"
            if "tools" in test:
                tool = Tool("echo", "Echo a string back.", {"type": "object", "properties": {"value": {"type": "string"}},
                                                            "required": ["value"], "additionalProperties": False})
                r = run("tools", base_req(messages=[Message.user("Call the echo tool with value 'x'. Do not answer in text.")],
                                          tools=[tool]))
                if r is not None:
                    caps["tools"] = "supported" if r.tool_calls else "accepted_no_call"
            if "json_schema" in test:
                schema = JsonSchema("probe", {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                              "required": ["ok"], "additionalProperties": False})
                r = run("json_schema", base_req(messages=[Message.user("Return ok=true as JSON.")], json_schema=schema))
                if r is not None:
                    try:
                        v = json.loads(r.text)
                        caps["json_schema"] = "supported" if isinstance(v, dict) and isinstance(v.get("ok"), bool) else "accepted_invalid_output"
                    except ValueError:
                        caps["json_schema"] = "accepted_invalid_output"
            if "images" in test:
                msg = Message("user", [ImagePart.from_bytes(RED_PNG), TextPart("What colour is this image? One word.")])
                r = run("images", base_req(messages=[msg]))
                if r is not None:
                    caps["images"] = "supported" if "red" in r.text.lower() else "accepted_unverified"
        except AuthError as e:
            return ProbeReport("auth_failed", caps, total, str(e))
        except Unreachable as e:
            return ProbeReport("unreachable", caps, total, str(e))
        except (RateLimit, UsageLimit) as e:
            return ProbeReport("limited", caps, total, str(e))
        ok_any = any(caps[k] == "supported" for k in ("streaming", "tools", "json_schema", "images"))
        state = "ok" if ok_any or caps["streaming"] == "supported" else "error"
        return ProbeReport(state, caps, total, "; ".join(detail))


def with_model(request: Request, model: str) -> Request:
    return replace(request, model=model)
