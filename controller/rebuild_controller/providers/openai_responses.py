"""OpenAI Responses API adapter (via httpx), also serving OpenRouter and local OpenAI-compatible endpoints.

Dialects
--------
* ``responses``  POST {endpoint}/responses   (OpenAI). Stateless: ``store:false``; reasoning items replayed verbatim.
* ``chat``       POST {endpoint}/chat/completions (OpenRouter default, LM Studio, Ollama, llama.cpp, vLLM ...).

Full compatibility is NOT assumed for non-OpenAI endpoints: tools / JSON schema / image / usage-in-stream support is
established by ``probe_capabilities`` and recorded on the connection. Missing usage in a stream is reported as
``Usage.known=False`` (the router then settles the reservation conservatively for metered connections).

docs unreachable on 2026-10-06; verify: this module was written from the stable, publicly known request/stream shapes.
See docs/PROVIDERS.md.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Mapping

import httpx

from .base import (_CONTEXT_RE, _CREDITS_RE, _MODEL_RE, AmbiguousCompletion, AuthError, CapabilityError, ContextWindowExceeded,
                   CreditsExhausted, ImagePart, InvalidRequest, Message, ModelDiscovery, ModelInfo, ModelUnavailable, OnText,
                   ProviderAdapter, ProviderError, ProviderUnavailable, RateLimit, Request, Response, TextPart, ToolCall,
                   ToolResultPart, ToolUsePart, Unreachable, Usage, UsageLimit, classify_error, iter_sse, parse_json_args)

DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
DIALECTS = ("responses", "chat")


def _stop_reason_responses(final: Mapping[str, Any], has_tools: bool) -> str:
    status = final.get("status")
    if status == "incomplete":
        why = (final.get("incomplete_details") or {}).get("reason")
        return {"max_output_tokens": "max_tokens", "content_filter": "content_filter"}.get(why, f"other:{why}")
    if has_tools:
        return "tool_use"
    return "end_turn" if status in (None, "completed") else f"other:{status}"


def _stop_reason_chat(fr: str | None, has_tools: bool) -> str:
    if has_tools and fr in (None, "stop", "tool_calls", "function_call"):
        return "tool_use"
    return {"stop": "end_turn", "length": "max_tokens", "content_filter": "content_filter", None: "end_turn"}.get(fr, f"other:{fr}")


class OpenAIResponsesAdapter(ProviderAdapter):
    """provider_name: openai | openrouter | local (or any label the connection uses)."""

    def __init__(self, *, endpoint: str | None = None, api_key: str | None = None, dialect: str = "responses",
                 provider_name: str = "openai", transport: httpx.BaseTransport | None = None, timeout_s: float = 120.0,
                 extra_headers: Mapping[str, str] | None = None, store: bool = False, request_cost: bool | None = None):
        if dialect not in DIALECTS:
            raise ValueError(f"unknown dialect {dialect!r}; expected one of {DIALECTS}")
        ep = endpoint or DEFAULT_ENDPOINTS.get(provider_name)
        if not ep:
            raise ValueError(f"provider '{provider_name}' requires an explicit endpoint")
        super().__init__(endpoint=ep, api_key=api_key, transport=transport, timeout_s=timeout_s, extra_headers=extra_headers)
        self.dialect = dialect
        self.provider_name = provider_name
        self.provider_key = f"openai:{dialect}"
        self.store = store
        # OpenRouter can include the charged cost in usage; ask for it there only.
        self.request_cost = (provider_name == "openrouter") if request_cost is None else request_cost

    # ------------------------------------------------------------------ headers/urls
    def _headers(self) -> dict[str, str]:
        h = {"content-type": "application/json", "accept": "text/event-stream, application/json"}
        if self._api_key:
            h["authorization"] = f"Bearer {self._api_key}"
        h.update(self.extra_headers)
        return h

    # ------------------------------------------------------------------ request building: responses
    @staticmethod
    def _user_content_resp(parts: list) -> list[dict]:
        out = []
        for p in parts:
            if isinstance(p, TextPart):
                out.append({"type": "input_text", "text": p.text})
            elif isinstance(p, ImagePart):
                out.append({"type": "input_image", "image_url": p.url or p.data_url()})
        return out

    def _input_items(self, request: Request) -> list[dict]:
        items: list[dict] = []
        for m in request.messages:
            if m.role == "assistant" and m.provider == self.provider_key and m.provider_blocks is not None:
                items.extend(copy.deepcopy(m.provider_blocks))
                continue
            pending: list = []

            def flush() -> None:
                nonlocal pending
                if pending:
                    items.append({"role": m.role, "content": self._user_content_resp(pending) if m.role == "user"
                                  else [{"type": "output_text", "text": p.text} for p in pending if isinstance(p, TextPart)]})
                    pending = []

            for p in m.parts():
                if isinstance(p, ToolResultPart):
                    flush()
                    items.append({"type": "function_call_output", "call_id": p.tool_use_id,
                                  "output": ("ERROR: " + p.content) if p.is_error else p.content})
                elif isinstance(p, ToolUsePart):
                    flush()
                    items.append({"type": "function_call", "call_id": p.id, "name": p.name, "arguments": json.dumps(p.input)})
                else:
                    pending.append(p)
            flush()
        return items

    def _tool_choice_resp(self, tc: Any) -> Any:
        if tc is None:
            return None
        if isinstance(tc, str):
            return tc
        return {"type": "function", "name": tc["name"]}

    def _body_responses(self, request: Request, stream: bool) -> dict:
        body: dict[str, Any] = {"model": request.model, "input": self._input_items(request), "stream": stream,
                                "store": self.store, "max_output_tokens": request.max_output_tokens}
        if request.system:
            body["instructions"] = request.system
        if request.tools:
            body["tools"] = [{"type": "function", "name": t.name, "description": t.description,
                              "parameters": t.parameters, "strict": t.strict} for t in request.tools]
            tc = self._tool_choice_resp(request.tool_choice)
            if tc is not None:
                body["tool_choice"] = tc
        if request.json_schema is not None:
            body["text"] = {"format": {"type": "json_schema", "name": request.json_schema.name,
                                       "schema": request.json_schema.schema, "strict": request.json_schema.strict}}
        if request.reasoning is not None:
            r: dict[str, Any] = {}
            if request.reasoning.effort:
                r["effort"] = request.reasoning.effort
            if request.reasoning.summary:
                r["summary"] = request.reasoning.summary
            if r:
                body["reasoning"] = r
                if not self.store:
                    body["include"] = ["reasoning.encrypted_content"]   # lets stateless replays keep reasoning items
        if request.temperature is not None:
            body["temperature"] = request.temperature
        return body

    # ------------------------------------------------------------------ request building: chat
    def _chat_messages(self, request: Request) -> list[dict]:
        msgs: list[dict] = []
        if request.system:
            msgs.append({"role": "system", "content": request.system})
        for m in request.messages:
            if m.role == "assistant" and m.provider == self.provider_key and m.provider_blocks is not None:
                msgs.extend(copy.deepcopy(m.provider_blocks))
                continue
            if m.role == "assistant":
                text = "".join(p.text for p in m.parts() if isinstance(p, TextPart))
                calls = [{"id": p.id, "type": "function", "function": {"name": p.name, "arguments": json.dumps(p.input)}}
                         for p in m.parts() if isinstance(p, ToolUsePart)]
                msg: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                msgs.append(msg)
                continue
            content: list[dict] = []
            for p in m.parts():
                if isinstance(p, ToolResultPart):
                    msgs.append({"role": "tool", "tool_call_id": p.tool_use_id,
                                 "content": ("ERROR: " + p.content) if p.is_error else p.content})
                elif isinstance(p, ImagePart):
                    content.append({"type": "image_url", "image_url": {"url": p.url or p.data_url()}})
                elif isinstance(p, TextPart):
                    content.append({"type": "text", "text": p.text})
            if content:
                if all(c["type"] == "text" for c in content):
                    msgs.append({"role": "user", "content": "\n".join(c["text"] for c in content)})
                else:
                    msgs.append({"role": "user", "content": content})
        return msgs

    def _body_chat(self, request: Request, stream: bool) -> dict:
        body: dict[str, Any] = {"model": request.model, "messages": self._chat_messages(request), "stream": stream}
        body["max_completion_tokens" if self.provider_name == "openai" else "max_tokens"] = request.max_output_tokens
        if stream:
            body["stream_options"] = {"include_usage": True}
        if request.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.parameters}} for t in request.tools]
            tc = request.tool_choice
            if tc is not None:
                body["tool_choice"] = tc if isinstance(tc, str) else {"type": "function", "function": {"name": tc["name"]}}
        if request.json_schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": request.json_schema.name, "schema": request.json_schema.schema, "strict": request.json_schema.strict}}
        if request.reasoning is not None and request.reasoning.effort:
            if self.provider_name == "openrouter":
                body["reasoning"] = {"effort": request.reasoning.effort}
            else:
                body["reasoning_effort"] = request.reasoning.effort
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if self.request_cost:
            body["usage"] = {"include": True}
        return body

    # ------------------------------------------------------------------ complete
    def complete(self, request: Request, on_text: OnText | None = None) -> Response:
        if not request.model:
            raise CapabilityError("no model id given (models are never inferred)", provider=self.provider_name)
        stream = request.stream
        if self.dialect == "responses":
            url, body = f"{self.endpoint}/responses", self._body_responses(request, stream)
            parse_stream, parse_json = self._parse_stream_responses, self._parse_json_responses
        else:
            url, body = f"{self.endpoint}/chat/completions", self._body_chat(request, stream)
            parse_stream, parse_json = self._parse_stream_chat, self._parse_json_chat

        def handler(resp: httpx.Response) -> Response:
            ctype = resp.headers.get("content-type", "")
            if stream and "json" not in ctype:
                r = parse_stream(resp, on_text)
            else:  # server ignored stream=true (some local servers) or non-stream request
                resp.read()
                r = parse_json(resp.json())
                if on_text and r.text:
                    on_text(r.text)
            r.provider_key = self.provider_key
            if not r.model:
                r.model = request.model
            return r

        return self._send("POST", url, body=body, headers=self._headers(), stream=stream, timeout_s=request.timeout_s,
                          handler=handler)

    # ------------------------------------------------------------------ parse: responses
    @staticmethod
    def _usage_responses(u: Mapping[str, Any] | None) -> Usage:
        if not u:
            return Usage(known=False)
        return Usage(input_tokens=int(u.get("input_tokens") or 0), output_tokens=int(u.get("output_tokens") or 0),
                     cached_tokens=int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0),
                     reasoning_tokens=int((u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0))

    def _finish_responses(self, final: Mapping[str, Any], streamed_text: str) -> Response:
        items = list(final.get("output") or [])
        text_parts, calls, notes, thinking = [], [], [], []
        for it in items:
            t = it.get("type")
            if t == "message":
                for c in it.get("content") or []:
                    if c.get("type") == "output_text":
                        text_parts.append(c.get("text", ""))
                    elif c.get("type") == "refusal":
                        notes.append("refusal: " + str(c.get("refusal", ""))[:200])
            elif t == "function_call":
                args, ok = parse_json_args(it.get("arguments"))
                if not ok:
                    notes.append(f"tool call {it.get('name')} had invalid JSON arguments")
                calls.append(ToolCall(it.get("call_id") or it.get("id", ""), it.get("name", ""), args))
            elif t == "reasoning":
                for s in it.get("summary") or []:
                    thinking.append(s.get("text", ""))
        text = "".join(text_parts) or streamed_text
        return Response(provider=self.provider_name, model=final.get("model", ""), text=text, tool_calls=calls,
                        stop_reason=_stop_reason_responses(final, bool(calls)), usage=self._usage_responses(final.get("usage")),
                        request_id=final.get("id"), provider_blocks=items or None, thinking="\n".join(thinking), notes=notes)

    def _parse_json_responses(self, data: Mapping[str, Any]) -> Response:
        if data.get("error"):
            raise self._event_error(data["error"], data.get("usage"))
        return self._finish_responses(data, "")

    def _event_error(self, err: Mapping[str, Any] | str | None, usage: Mapping[str, Any] | None) -> ProviderError:
        e = err if isinstance(err, Mapping) else {"message": str(err)}
        code, msg = str(e.get("code") or e.get("type") or ""), str(e.get("message") or "error event")
        pu = self._usage_responses(usage) if usage else None
        txt = f"{self.provider_name} stream error {code}: {msg}"
        low = (code + " " + msg).lower()
        if _CREDITS_RE.search(low) or "quota" in low and "exceed" in low:
            return CreditsExhausted(txt, provider=self.provider_name)
        if _MODEL_RE.search(low):
            return ModelUnavailable(txt, provider=self.provider_name)
        if _CONTEXT_RE.search(low):
            return ContextWindowExceeded(txt, provider=self.provider_name, request_sent=True)
        if "rate_limit" in low:
            return RateLimit(txt, provider=self.provider_name)
        if "invalid_api_key" in low or "authentication" in low:
            return AuthError(txt, provider=self.provider_name)
        return AmbiguousCompletion(txt, provider=self.provider_name, partial_usage=pu)

    def _parse_stream_responses(self, resp: httpx.Response, on_text: OnText | None) -> Response:
        deltas: list[str] = []
        items: dict[int, dict] = {}
        final: Mapping[str, Any] | None = None
        for ev, data in iter_sse(resp):
            if data.strip() == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            t = obj.get("type") or ev
            if t == "response.output_text.delta":
                d = obj.get("delta", "")
                deltas.append(d)
                if on_text and d:
                    on_text(d)
            elif t == "response.output_item.done":
                items[int(obj.get("output_index", len(items)))] = obj.get("item", {})
            elif t in ("response.completed", "response.incomplete"):
                final = obj.get("response", {})
            elif t == "response.failed":
                r = obj.get("response", {})
                raise self._event_error(r.get("error"), r.get("usage"))
            elif t == "error":
                raise self._event_error(obj.get("error") or obj, None)
        if final is None:
            raise AmbiguousCompletion(f"{self.provider_name}: stream ended without a terminal response event",
                                      provider=self.provider_name)
        if not final.get("output") and items:
            final = {**final, "output": [items[k] for k in sorted(items)]}
        return self._finish_responses(final, "".join(deltas))

    # ------------------------------------------------------------------ parse: chat
    @staticmethod
    def _usage_chat(u: Mapping[str, Any] | None) -> Usage:
        if not u:
            return Usage(known=False)
        cost = u.get("cost")
        return Usage(input_tokens=int(u.get("prompt_tokens") or 0), output_tokens=int(u.get("completion_tokens") or 0),
                     cached_tokens=int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
                     reasoning_tokens=int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
                     reported_cost_usd=float(cost) if isinstance(cost, (int, float)) else None)

    def _finish_chat(self, text: str, thinking: str, calls_raw: dict[int, dict], finish: str | None, usage: Usage,
                     rid: str | None, model: str) -> Response:
        calls, notes, wire = [], [], []
        for idx in sorted(calls_raw):
            c = calls_raw[idx]
            args, ok = parse_json_args(c.get("arguments"))
            if not ok:
                notes.append(f"tool call {c.get('name')} had invalid JSON arguments")
            calls.append(ToolCall(c.get("id") or f"call_{idx}", c.get("name", ""), args))
            wire.append({"id": c.get("id") or f"call_{idx}", "type": "function",
                         "function": {"name": c.get("name", ""), "arguments": c.get("arguments") or ""}})
        msg: dict[str, Any] = {"role": "assistant", "content": text or None}
        if wire:
            msg["tool_calls"] = wire
        if not usage.known:
            notes.append("endpoint did not report usage")
        return Response(provider=self.provider_name, model=model, text=text, tool_calls=calls,
                        stop_reason=_stop_reason_chat(finish, bool(calls)), usage=usage, request_id=rid,
                        provider_blocks=[msg], thinking=thinking, notes=notes)

    def _parse_json_chat(self, data: Mapping[str, Any]) -> Response:
        if data.get("error"):
            raise self._event_error(data["error"], None)
        ch = (data.get("choices") or [{}])[0]
        m = ch.get("message") or {}
        calls = {i: {"id": c.get("id"), "name": (c.get("function") or {}).get("name"),
                     "arguments": (c.get("function") or {}).get("arguments")} for i, c in enumerate(m.get("tool_calls") or [])}
        return self._finish_chat(m.get("content") or "", m.get("reasoning_content") or m.get("reasoning") or "", calls,
                                 ch.get("finish_reason"), self._usage_chat(data.get("usage")), data.get("id"), data.get("model", ""))

    def _parse_stream_chat(self, resp: httpx.Response, on_text: OnText | None) -> Response:
        text: list[str] = []
        thinking: list[str] = []
        calls: dict[int, dict] = {}
        finish: str | None = None
        usage = Usage(known=False)
        rid = model = None
        done = False
        for _ev, data in iter_sse(resp):
            if data.strip() == "[DONE]":
                done = True
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if obj.get("error"):
                raise self._event_error(obj["error"], None)
            rid = obj.get("id", rid)
            model = obj.get("model", model)
            if obj.get("usage"):
                usage = self._usage_chat(obj["usage"])
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    text.append(d["content"])
                    if on_text:
                        on_text(d["content"])
                rc = d.get("reasoning_content") or d.get("reasoning")
                if isinstance(rc, str) and rc:
                    thinking.append(rc)
                for tc in d.get("tool_calls") or []:
                    slot = calls.setdefault(int(tc.get("index", 0)), {"id": None, "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
        if not done and finish is None:
            raise AmbiguousCompletion(f"{self.provider_name}: stream ended without finish_reason or [DONE]",
                                      provider=self.provider_name,
                                      partial_usage=usage if usage.known else None)
        return self._finish_chat("".join(text), "".join(thinking), calls, finish, usage, rid, model or "")

    # ------------------------------------------------------------------ discovery
    def discover_models(self) -> ModelDiscovery:
        try:
            data = self._get_json(f"{self.endpoint}/models", self._headers())
        except InvalidRequest as e:
            if e.status in (404, 405, 501):
                return ModelDiscovery(False, error=f"{self.endpoint}/models not available (HTTP {e.status})", status=e.status)
            raise
        rows = data.get("data") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return ModelDiscovery(False, error="unexpected /models response shape")
        models: list[ModelInfo] = []
        for r in rows:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            price = None
            pr = r.get("pricing")
            if isinstance(pr, dict):
                try:
                    price = {"input_per_mtok": float(pr["prompt"]) * 1e6, "output_per_mtok": float(pr["completion"]) * 1e6}
                except (KeyError, TypeError, ValueError):
                    price = None
            models.append(ModelInfo(id=str(r["id"]), display_name=r.get("name"), source="discovered",
                                    context_window=r.get("context_length") if isinstance(r.get("context_length"), int) else None,
                                    price=price))
        return ModelDiscovery(True, models)
