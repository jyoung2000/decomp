"""Anthropic Messages API adapter (raw HTTP via httpx; no SDK dependency, by project design so transports are injectable).

Field choices follow the claude-api skill as read on 2026-10-06 (model ids/pricing/params) and the streaming docs at
platform.claude.com:
* ``anthropic-version: 2023-06-01``; ``x-api-key``; optional ``anthropic-beta`` list (``betas``).
* Thinking: models in the always-on family (Fable 5.x, Mythos 5.x, Opus 5.5) get no ``thinking`` field unless a
  summary is requested (``{"type":"adaptive","display":"summarized"}``); adaptive-capable models get
  ``{"type":"adaptive"}``; Haiku 4.5 and older take ``budget_tokens``. Unknown model ids get NO thinking field unless
  ``Reasoning.mode`` is set explicitly (nothing is guessed). ``budget_tokens`` is never sent to models that reject it.
* Effort: ``output_config.effort``. JSON schema: ``output_config.format = {"type":"json_schema","schema":...}``.
* Forced ``tool_choice`` (any/tool) is refused client-side for models documented to 400 on it.
* Sampling params (temperature) are dropped (with a note) for models documented to reject them.
* Prompt caching: explicit ``cache_control`` breakpoint on the last system block (and last tool), no beta header needed
  at the time of writing; TTL 5m default, ``cache_ttl="1h"`` supported.
* Content blocks returned by the API (text, thinking + signature, redacted_thinking, tool_use, server tool blocks) are
  preserved verbatim in ``Response.provider_blocks`` and replayed unchanged on later turns.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Mapping

import httpx

from .base import (AmbiguousCompletion, AuthError, CapabilityError, ImagePart, InvalidRequest, Message, ModelDiscovery,
                   ModelInfo, OnText, ProviderAdapter, ProviderError, ProviderUnavailable, RateLimit, Request, Response,
                   TextPart, ToolCall, ToolResultPart, ToolUsePart, Usage, UsageLimit, iter_sse, parse_json_args,
                   ContextWindowExceeded, CreditsExhausted, ModelUnavailable)

DEFAULT_ENDPOINT = "https://api.anthropic.com"
API_VERSION = "2023-06-01"

_ALWAYS_ON = re.compile(r"^claude-(fable-5|mythos-5|opus-5-5)")                       # thinking cannot be disabled/budgeted
_ADAPTIVE = re.compile(r"^claude-(opus-5$|opus-4-[678]|sonnet-5|sonnet-4-6)")          # adaptive thinking available
_BUDGET = re.compile(r"^claude-(haiku-4-5|haiku-3|sonnet-4-5|sonnet-4$|sonnet-4-2|opus-4-5|opus-4-1|opus-4$|opus-4-2)")
_NO_FORCED_TOOL = re.compile(r"^claude-(fable-5-1|mythos-5-1|opus-5-5|sonnet-5-5)")
_NO_SAMPLING = re.compile(r"^claude-(fable-5|mythos-5|opus-5|opus-4-[78]|sonnet-5)")

_STOP = {"end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens", "stop_sequence": "end_turn",
         "refusal": "refusal", "pause_turn": "other:pause_turn"}


def thinking_style(model: str) -> str:
    if _ALWAYS_ON.match(model):
        return "always_on"
    if _ADAPTIVE.match(model):
        return "adaptive"
    if _BUDGET.match(model):
        return "budget"
    return "unknown"


class AnthropicAdapter(ProviderAdapter):
    provider_key = "anthropic"
    provider_name = "anthropic"

    def __init__(self, *, endpoint: str | None = None, api_key: str | None = None, transport: httpx.BaseTransport | None = None,
                 timeout_s: float = 300.0, extra_headers: Mapping[str, str] | None = None, betas: list[str] | None = None,
                 api_version: str = API_VERSION):
        super().__init__(endpoint=endpoint or DEFAULT_ENDPOINT, api_key=api_key, transport=transport, timeout_s=timeout_s,
                         extra_headers=extra_headers)
        self.betas = list(betas or [])
        self.api_version = api_version

    def _headers(self) -> dict[str, str]:
        h = {"content-type": "application/json", "anthropic-version": self.api_version}
        if self._api_key:
            h["x-api-key"] = self._api_key
        if self.betas:
            h["anthropic-beta"] = ",".join(self.betas)
        h.update(self.extra_headers)
        return h

    # ------------------------------------------------------------------ request building
    @staticmethod
    def _image_block(p: ImagePart) -> dict:
        if p.url:
            return {"type": "image", "source": {"type": "url", "url": p.url}}
        return {"type": "image", "source": {"type": "base64", "media_type": p.media_type, "data": p.data}}

    def _messages(self, request: Request) -> list[dict]:
        out: list[dict] = []
        for m in request.messages:
            if m.role == "assistant" and m.provider == self.provider_key and m.provider_blocks is not None:
                out.append({"role": "assistant", "content": copy.deepcopy(m.provider_blocks)})
                continue
            blocks: list[dict] = []
            for p in m.parts():
                if isinstance(p, TextPart):
                    if p.text:
                        blocks.append({"type": "text", "text": p.text})
                elif isinstance(p, ImagePart):
                    blocks.append(self._image_block(p))
                elif isinstance(p, ToolUsePart):
                    blocks.append({"type": "tool_use", "id": p.id, "name": p.name, "input": p.input})
                elif isinstance(p, ToolResultPart):
                    b: dict[str, Any] = {"type": "tool_result", "tool_use_id": p.tool_use_id, "content": p.content}
                    if p.is_error:
                        b["is_error"] = True
                    blocks.append(b)
            if blocks:
                # tool_result blocks must lead the user message
                blocks.sort(key=lambda b: 0 if b["type"] == "tool_result" else 1)
                out.append({"role": m.role, "content": blocks})
        return out

    def build_body(self, request: Request, stream: bool, notes: list[str] | None = None) -> dict:
        notes = notes if notes is not None else []
        model = request.model
        body: dict[str, Any] = {"model": model, "max_tokens": request.max_output_tokens, "messages": self._messages(request)}
        if stream:
            body["stream"] = True
        cc: dict[str, Any] = {"type": "ephemeral"}
        if request.cache and request.cache_ttl == "1h":
            cc["ttl"] = "1h"
        if request.system:
            block: dict[str, Any] = {"type": "text", "text": request.system}
            if request.cache:
                block["cache_control"] = cc
            body["system"] = [block]
        if request.tools:
            tools = [{"name": t.name, "description": t.description, "input_schema": t.parameters,
                      **({"strict": True} if t.strict else {})} for t in request.tools]
            if request.cache:
                tools[-1]["cache_control"] = cc
            body["tools"] = tools
            tc = request.tool_choice
            if isinstance(tc, str):
                if tc in ("auto", "none"):
                    body["tool_choice"] = {"type": tc}
                elif tc == "required":
                    if _NO_FORCED_TOOL.match(model):
                        raise CapabilityError(f"{model} rejects forced tool_choice; use 'auto' and instruct the model", provider="anthropic")
                    body["tool_choice"] = {"type": "any"}
            elif isinstance(tc, dict):
                if _NO_FORCED_TOOL.match(model):
                    raise CapabilityError(f"{model} rejects forced tool_choice; use 'auto' and instruct the model", provider="anthropic")
                body["tool_choice"] = {"type": "tool", "name": tc["name"]}
        elif request.cache and not request.system and body["messages"]:
            last = body["messages"][-1]["content"][-1]
            last["cache_control"] = cc
        out_cfg: dict[str, Any] = {}
        r = request.reasoning
        if r is not None:
            style = r.mode or thinking_style(model)
            if style in ("always_on", "adaptive"):
                if r.summary or style == "adaptive" and not _ALWAYS_ON.match(model):
                    th: dict[str, Any] = {"type": "adaptive"}
                    if r.summary:
                        th["display"] = "summarized"
                    body["thinking"] = th
                if r.budget_tokens:
                    notes.append("budget_tokens ignored: this model uses adaptive thinking")
            elif style in ("budget",):
                if r.budget_tokens:
                    if r.budget_tokens < 1024 or r.budget_tokens >= request.max_output_tokens:
                        raise CapabilityError("budget_tokens must be >=1024 and < max_output_tokens", provider="anthropic")
                    body["thinking"] = {"type": "enabled", "budget_tokens": r.budget_tokens}
            else:
                notes.append("unknown model family: no thinking parameter sent (set Reasoning.mode to force)")
            if r.effort:
                out_cfg["effort"] = r.effort
        if request.json_schema is not None:
            out_cfg["format"] = {"type": "json_schema", "schema": request.json_schema.schema}
        if out_cfg:
            body["output_config"] = out_cfg
        if request.temperature is not None:
            if _NO_SAMPLING.match(model):
                notes.append("temperature dropped: model rejects sampling parameters")
            else:
                body["temperature"] = request.temperature
        return body

    # ------------------------------------------------------------------ complete
    def complete(self, request: Request, on_text: OnText | None = None) -> Response:
        if not request.model:
            raise CapabilityError("no model id given (models are never inferred)", provider="anthropic")
        notes: list[str] = []
        stream = request.stream
        body = self.build_body(request, stream, notes)

        def handler(resp: httpx.Response) -> Response:
            ctype = resp.headers.get("content-type", "")
            if stream and "json" not in ctype:
                r = self._parse_stream(resp, on_text)
            else:
                resp.read()
                r = self._finish(resp.json(), None)
                if on_text and r.text:
                    on_text(r.text)
            r.notes = notes + r.notes
            r.usage.cache_write_ttl = request.cache_ttl
            return r

        return self._send("POST", f"{self.endpoint}/v1/messages", body=body, headers=self._headers(), stream=stream,
                          timeout_s=request.timeout_s, handler=handler)

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def _usage(u: Mapping[str, Any] | None, prev: Usage | None = None) -> Usage:
        """Normalise Anthropic usage. input_tokens (API) is the UNCACHED remainder; total prompt = it + cache read + write."""
        if not u:
            return prev or Usage(known=False)
        base = prev or Usage()
        cr = int(u["cache_read_input_tokens"] or 0) if "cache_read_input_tokens" in u else base.cached_tokens
        cw = int(u["cache_creation_input_tokens"] or 0) if "cache_creation_input_tokens" in u else base.cache_write_tokens
        if "input_tokens" in u:
            total_in = int(u["input_tokens"] or 0) + cr + cw
        else:
            total_in = base.input_tokens
        out = int(u["output_tokens"] or 0) if "output_tokens" in u else base.output_tokens
        return Usage(input_tokens=total_in, output_tokens=out, cached_tokens=cr, cache_write_tokens=cw)

    def _finish(self, msg: Mapping[str, Any], blocks_override: list[dict] | None) -> Response:
        blocks = blocks_override if blocks_override is not None else list(msg.get("content") or [])
        text, thinking, calls, notes = [], [], [], []
        for b in blocks:
            t = b.get("type")
            if t == "text":
                text.append(b.get("text", ""))
            elif t == "thinking":
                thinking.append(b.get("thinking", ""))
            elif t == "tool_use":
                calls.append(ToolCall(b.get("id", ""), b.get("name", ""), b.get("input") or {}))
                if b.get("_invalid_json"):
                    notes.append(f"tool call {b.get('name')} had invalid JSON arguments")
        stop = msg.get("stop_reason")
        reason = _STOP.get(stop, f"other:{stop}") if stop else "other:missing"
        return Response(provider="anthropic", model=msg.get("model", ""), text="".join(text), tool_calls=calls,
                        stop_reason=reason, usage=self._usage(msg.get("usage")), request_id=msg.get("id"),
                        provider_blocks=[{k: v for k, v in b.items() if k != "_invalid_json"} for b in blocks],
                        thinking="".join(thinking), notes=notes, provider_key=self.provider_key)

    def _stream_error(self, err: Mapping[str, Any], usage: Usage | None) -> ProviderError:
        et, em = str(err.get("type", "")), str(err.get("message", ""))
        txt = f"anthropic stream error {et}: {em}"
        if et == "authentication_error" or et == "permission_error":
            return AuthError(txt, provider="anthropic")
        if et == "rate_limit_error":
            return RateLimit(txt, provider="anthropic")
        if et == "not_found_error" and re.search(r"(?i)model", em):
            return ModelUnavailable(txt, provider="anthropic")
        if et == "invalid_request_error":
            if re.search(r"(?i)credit balance", em):
                return CreditsExhausted(txt, provider="anthropic")
            if re.search(r"(?i)usage limit", em):
                return UsageLimit(txt, provider="anthropic")
            if re.search(r"(?i)prompt is too long|context window|too many tokens", em):
                return ContextWindowExceeded(txt, provider="anthropic", request_sent=True)
            return InvalidRequest(txt, provider="anthropic")
        # overloaded_error / api_error mid-stream: tokens may already have been generated and billed
        return AmbiguousCompletion(txt, provider="anthropic", partial_usage=usage)

    def _parse_stream(self, resp: httpx.Response, on_text: OnText | None) -> Response:
        msg: dict[str, Any] = {}
        blocks: dict[int, dict] = {}
        json_buf: dict[int, list[str]] = {}
        usage = Usage(known=False)
        stopped = False
        for ev, data in iter_sse(resp):
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            t = obj.get("type") or ev
            if t == "message_start":
                msg = dict(obj.get("message") or {})
                usage = self._usage(msg.get("usage"), None)
            elif t == "content_block_start":
                idx = int(obj.get("index", 0))
                blocks[idx] = dict(obj.get("content_block") or {})
                json_buf[idx] = []
            elif t == "content_block_delta":
                idx = int(obj.get("index", 0))
                b = blocks.setdefault(idx, {})
                d = obj.get("delta") or {}
                dt = d.get("type")
                if dt == "text_delta":
                    b["text"] = b.get("text", "") + d.get("text", "")
                    if on_text and d.get("text"):
                        on_text(d["text"])
                elif dt == "thinking_delta":
                    b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
                elif dt == "signature_delta":
                    b["signature"] = d.get("signature", "")
                elif dt == "input_json_delta":
                    json_buf.setdefault(idx, []).append(d.get("partial_json", ""))
                elif dt == "citations_delta":
                    b.setdefault("citations", []).append(d.get("citation"))
            elif t == "content_block_stop":
                idx = int(obj.get("index", 0))
                b = blocks.get(idx)
                if b is not None and b.get("type") in ("tool_use", "server_tool_use", "mcp_tool_use"):
                    raw = "".join(json_buf.get(idx, []))
                    if raw.strip():
                        val, ok = parse_json_args(raw)
                        b["input"] = val
                        if not ok:
                            b["_invalid_json"] = True
                    else:
                        b.setdefault("input", {})
            elif t == "message_delta":
                d = obj.get("delta") or {}
                for k, v in d.items():
                    msg[k] = v
                usage = self._usage(obj.get("usage"), usage)  # cumulative
            elif t == "message_stop":
                stopped = True
            elif t == "error":
                raise self._stream_error(obj.get("error") or {}, usage if usage.known else None)
            # ping and unknown events are ignored by design
        if not stopped:
            raise AmbiguousCompletion("anthropic: stream ended without message_stop", provider="anthropic",
                                      partial_usage=usage if usage.known else None)
        msg["usage"] = None
        r = self._finish(msg, [blocks[i] for i in sorted(blocks)])
        r.usage = usage
        return r

    # ------------------------------------------------------------------ discovery
    def discover_models(self) -> ModelDiscovery:
        models: list[ModelInfo] = []
        after: str | None = None
        for _ in range(20):  # bounded pagination
            url = f"{self.endpoint}/v1/models?limit=1000" + (f"&after_id={after}" if after else "")
            try:
                data = self._get_json(url, self._headers())
            except InvalidRequest as e:
                if e.status in (404, 405, 501):
                    return ModelDiscovery(False, error=f"models endpoint unavailable (HTTP {e.status})", status=e.status)
                raise
            for r in data.get("data") or []:
                if isinstance(r, dict) and r.get("id"):
                    mi = r.get("max_input_tokens")
                    mo = r.get("max_tokens")
                    models.append(ModelInfo(id=str(r["id"]), display_name=r.get("display_name"), source="discovered",
                                            context_window=mi if isinstance(mi, int) else None,
                                            max_output=mo if isinstance(mo, int) else None,
                                            meta={"capabilities": r["capabilities"]} if isinstance(r.get("capabilities"), dict) else {}))
            if data.get("has_more") and data.get("last_id"):
                after = data["last_id"]
            else:
                break
        return ModelDiscovery(True, models)
