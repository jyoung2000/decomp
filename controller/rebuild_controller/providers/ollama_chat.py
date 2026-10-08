"""Native Ollama chat adapter (``POST {root}/api/chat``) - used for every connection whose server is Ollama.

Why not the OpenAI-compatible ``/v1/chat/completions``? That endpoint has no way to set the context size, so Ollama runs the model
with the server's default context (often 4096-16384 tokens) and SILENTLY drops the start of a longer prompt: measured on this PC
with Ollama 0.35 / qwen2.5:3b, a 28,947-token prompt was cut to 8,194 tokens and the model answered wrongly, with HTTP 200.

This adapter therefore always sends
* ``options.num_ctx`` sized to the request (a generous token estimate rounded up to a power-of-two bucket so the model is not
  reloaded on every call), bounded by the model's trained context length and a VRAM-safe cap (default 32k, adjustable per
  connection as ``limits.num_ctx_cap``);
* ``truncate: false`` so a prompt that still does not fit is REFUSED (HTTP 400 ``exceed_context_size_error``) instead of cut.
  When the server reports the exact prompt size and it fits under the bound, the request is re-sent once with a larger
  ``num_ctx`` (retry-safe: the server processed nothing). Servers older than the ``truncate`` flag ignore it; for them a reply
  whose ``prompt_eval_count`` matches Ollama's truncation pattern is flagged in ``Response.notes``.

The context actually used is reported in ``Response.meta`` (``num_ctx``, ``context_window``, ``prompt_tokens``) and recorded by the
router on every ``ai_calls`` row (``effective_context``).
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Mapping

import httpx

from .base import (AmbiguousCompletion, CapabilityError, ContextWindowExceeded, ImagePart, InvalidRequest, ModelDiscovery, ModelInfo,
                   OnText, ProviderAdapter, ProviderError, Request, Response, TextPart, ToolCall, ToolResultPart, ToolUsePart, Usage,
                   classify_error)
from .ollama import ollama_root
from .pricing import estimate_request_tokens

DEFAULT_NUM_CTX_CAP = 32768     # VRAM-safe default; a user can raise it per connection (limits.num_ctx_cap)
MIN_NUM_CTX = 8192
CHARS_PER_TOKEN_CONSERVATIVE = 2.0   # measured worst case on identifier-heavy text was 1.83 chars/token; code is usually ~3
_EXCEED_RE = re.compile(r"(?i)exceed_context_size|exceeds the available context size")


def estimate_tokens(request: Request) -> int:
    """Generous prompt-size estimate (never used for billing): the larger of the pricing estimate and chars/2."""
    chars = len(request.system or "")
    for m in request.messages:
        for p in m.parts():
            if isinstance(p, TextPart):
                chars += len(p.text)
            elif isinstance(p, ToolResultPart):
                chars += len(p.content or "")
            elif isinstance(p, ToolUsePart):
                chars += len(json.dumps(p.input, default=str)) + len(p.name)
    for t in request.tools:
        chars += len(t.name) + len(t.description) + len(json.dumps(t.parameters, default=str))
    return max(estimate_request_tokens(request), int(chars / CHARS_PER_TOKEN_CONSERVATIVE) + 64)


def bucket(n: int) -> int:
    b = MIN_NUM_CTX
    while b < n:
        b *= 2
    return b


def size_num_ctx(needed: int, model_context: int | None, cap: int) -> tuple[int, int]:
    """(num_ctx, bound). ``bound`` = min(model context, cap); raises nothing - the caller compares ``needed`` with ``bound``."""
    bound = int(cap)
    if model_context and model_context > 0:
        bound = min(bound, int(model_context))
    return min(bucket(needed), bound), bound


class OllamaChatAdapter(ProviderAdapter):
    provider_key = "ollama:chat"

    def __init__(self, *, endpoint: str, provider_name: str = "local", transport: httpx.BaseTransport | None = None,
                 timeout_s: float = 300.0, model_context: Mapping[str, int | None] | None = None,
                 num_ctx_cap: int | None = None, api_key: str | None = None):
        root = ollama_root(endpoint)
        if root is None:
            raise ValueError("an Ollama connection needs an http(s) endpoint")
        super().__init__(endpoint=root, api_key=api_key, transport=transport, timeout_s=timeout_s)
        self.provider_name = provider_name
        self.model_context = dict(model_context or {})
        self.num_ctx_cap = int(num_ctx_cap or DEFAULT_NUM_CTX_CAP)

    def _headers(self) -> dict[str, str]:
        h = {"content-type": "application/json", "accept": "application/x-ndjson, application/json"}
        if self._api_key:
            h["authorization"] = f"Bearer {self._api_key}"
        return h

    # ------------------------------------------------------------------ request building
    def _messages(self, request: Request) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
        if request.system:
            msgs.append({"role": "system", "content": request.system})
        tool_names: dict[str, str] = {}
        for m in request.messages:
            if m.role == "assistant" and m.provider == self.provider_key and m.provider_blocks is not None:
                msgs.extend(copy.deepcopy(m.provider_blocks))
                for b in m.provider_blocks:
                    for tc in b.get("tool_calls") or []:
                        fn = tc.get("function") or {}
                        if tc.get("id"):
                            tool_names[str(tc["id"])] = str(fn.get("name") or "")
                continue
            if m.role == "assistant":
                text = "".join(p.text for p in m.parts() if isinstance(p, TextPart))
                calls = []
                for p in m.parts():
                    if isinstance(p, ToolUsePart):
                        tool_names[p.id] = p.name
                        calls.append({"id": p.id, "function": {"name": p.name, "arguments": p.input}})
                msg: dict[str, Any] = {"role": "assistant", "content": text}
                if calls:
                    msg["tool_calls"] = calls
                msgs.append(msg)
                continue
            texts: list[str] = []
            images: list[str] = []
            for p in m.parts():
                if isinstance(p, ToolResultPart):
                    msgs.append({"role": "tool", "content": ("ERROR: " + p.content) if p.is_error else p.content,
                                 "tool_name": p.name or tool_names.get(p.tool_use_id, "")})
                elif isinstance(p, ImagePart):
                    if p.data is None:
                        raise CapabilityError("Ollama needs images inline (base64), not by URL", provider=self.provider_name)
                    images.append(p.data)
                elif isinstance(p, TextPart):
                    texts.append(p.text)
            if texts or images:
                um: dict[str, Any] = {"role": "user", "content": "\n".join(texts)}
                if images:
                    um["images"] = images
                msgs.append(um)
        return msgs

    def _body(self, request: Request, num_ctx: int) -> dict[str, Any]:
        opts: dict[str, Any] = {"num_ctx": int(num_ctx), "num_predict": int(request.max_output_tokens)}
        if request.temperature is not None:
            opts["temperature"] = request.temperature
        body: dict[str, Any] = {"model": request.model, "messages": self._messages(request), "stream": bool(request.stream),
                                "options": opts, "truncate": False}
        if request.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.parameters}} for t in request.tools]
        if request.json_schema is not None:
            body["format"] = request.json_schema.schema
        elif (request.metadata or {}).get("output_format") == "json_file_map":
            # Grammar-constrained JSON: a 14B model wrote correct Rust but left one quote unescaped, so the whole answer was unusable.
            body["format"] = {"type": "object", "additionalProperties": {"type": "string"}}
        if request.reasoning is not None and (request.reasoning.effort or request.reasoning.budget_tokens):
            body["think"] = True
        else:
            # Unrequested hidden reasoning spent gemma4:12b's whole 16000-token budget and returned no answer text.
            body["think"] = False
        return body

    # ------------------------------------------------------------------ complete
    def complete(self, request: Request, on_text: OnText | None = None) -> Response:
        if not request.model:
            raise CapabilityError("no model id given (models are never inferred)", provider=self.provider_name)
        est = estimate_tokens(request)
        needed = est + int(request.max_output_tokens) + 256
        mctx = self.model_context.get(request.model)
        num_ctx, bound = size_num_ctx(needed, mctx, self.num_ctx_cap)
        if needed > bound:
            raise ContextWindowExceeded(self._too_big(est, request.max_output_tokens, bound, mctx), provider=self.provider_name,
                                        request_sent=False)
        try:
            return self._complete_once(request, on_text, num_ctx, est, mctx)
        except InvalidRequest as e:
            n_prompt = getattr(e, "n_prompt_tokens", None)
            if not isinstance(n_prompt, int):
                raise
            need2 = n_prompt + int(request.max_output_tokens) + 64
            if need2 > bound:
                raise ContextWindowExceeded(self._too_big(n_prompt, request.max_output_tokens, bound, mctx, exact=True),
                                            provider=self.provider_name, status=e.status, request_sent=False) from e
            num2 = min(bucket(need2), bound)
            if num2 <= num_ctx:
                raise
            return self._complete_once(request, on_text, num2, n_prompt, mctx)

    def _too_big(self, tokens: int, max_out: int, bound: int, mctx: int | None, exact: bool = False) -> str:
        why = (f"the prompt ({'exactly' if exact else 'about'} {tokens} tokens) plus {max_out} output tokens does not fit "
               f"the {bound}-token context Rebuild Studio allows for this model")
        if mctx and bound < mctx:
            why += (f" (the model supports {mctx}; raise 'Max context' for this Ollama connection if your GPU/RAM can hold it)")
        return why + "; nothing was sent, so nothing was silently cut"

    def _complete_once(self, request: Request, on_text: OnText | None, num_ctx: int, est: int, mctx: int | None) -> Response:
        body = self._body(request, num_ctx)
        # Always stream from Ollama, even when the caller wants one final answer: the HTTP read timeout then bounds the gap
        # between tokens instead of the whole generation. A 12-16B model on a consumer GPU can need far longer than any
        # fixed total timeout to write a program, and cutting it off mid-answer wasted every local attempt.
        body["stream"] = True
        stream = True
        prov = self.provider_name

        def handler(resp: httpx.Response) -> Response:
            ctype = resp.headers.get("content-type", "")
            if "ndjson" in ctype or "json" not in ctype:
                r = self._parse_stream(resp, on_text)
            else:
                resp.read()
                r = self._parse_obj(resp.json())
                if on_text and r.text:
                    on_text(r.text)
            if not r.model:
                r.model = request.model
            r.provider_key = self.provider_key
            r.meta.update({"num_ctx": num_ctx, "context_window": mctx, "estimated_prompt_tokens": est, "server": "ollama",
                           "endpoint": "/api/chat"})
            pt = r.meta.get("prompt_tokens")
            if isinstance(pt, int) and est > num_ctx and pt <= num_ctx // 2 + 16:
                r.notes.append(f"the server evaluated only {pt} prompt tokens with num_ctx={num_ctx}: it may have truncated the "
                               f"prompt (Ollama older than the 'truncate' flag)")
            return r

        try:
            return self._send("POST", f"{self.endpoint}/api/chat", body=body, headers=self._headers(), stream=stream,
                              timeout_s=request.timeout_s, handler=handler)
        except InvalidRequest as e:
            raw = str(e)
            if _EXCEED_RE.search(raw):
                m = re.search(r"n_prompt_tokens\D{0,6}(\d+)", raw) or re.search(r"request \((\d+) tokens\)", raw)
                err = InvalidRequest(f"{prov}: the prompt does not fit num_ctx={num_ctx}: {raw[:200]}", provider=prov,
                                     status=e.status, request_sent=False)
                err.n_prompt_tokens = int(m.group(1)) if m else None    # type: ignore[attr-defined]
                raise err from e
            raise

    # ------------------------------------------------------------------ parsing
    def _error(self, err: Any) -> ProviderError:
        msg = err if isinstance(err, str) else json.dumps(err)
        return classify_error(self.provider_name, 400, json.dumps({"error": msg}))

    def _finish(self, text: str, thinking: str, calls_raw: list[dict], final: Mapping[str, Any], model: str) -> Response:
        calls, wire = [], []
        for i, c in enumerate(calls_raw):
            fn = c.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"_raw_arguments": args}
            if not isinstance(args, dict):
                args = {"_value": args}
            cid = str(c.get("id") or f"call_{i}")
            calls.append(ToolCall(cid, str(fn.get("name") or ""), args))
            wire.append({"id": cid, "function": {"name": str(fn.get("name") or ""), "arguments": args}})
        pe = final.get("prompt_eval_count")
        ev = final.get("eval_count")
        cached = final.get("prompt_eval_cached_count")
        known = isinstance(pe, int) or isinstance(ev, int)
        usage = Usage(input_tokens=int(pe or 0) + int(cached or 0), output_tokens=int(ev or 0), cached_tokens=int(cached or 0), known=known)
        dr = final.get("done_reason")
        if calls:
            stop = "tool_use"
        else:
            stop = {"stop": "end_turn", "length": "max_tokens", None: "end_turn", "": "end_turn"}.get(dr, f"other:{dr}")
        msg: dict[str, Any] = {"role": "assistant", "content": text}
        if wire:
            msg["tool_calls"] = wire
        notes = [] if known else ["endpoint did not report usage"]
        r = Response(provider=self.provider_name, model=model, text=text, tool_calls=calls, stop_reason=stop, usage=usage,
                     provider_blocks=[msg], thinking=thinking, notes=notes)
        if isinstance(pe, int):
            r.meta["prompt_tokens"] = int(pe) + int(cached or 0)
        return r

    def _parse_obj(self, data: Mapping[str, Any]) -> Response:
        if data.get("error"):
            raise self._error(data["error"])
        m = data.get("message") or {}
        return self._finish(str(m.get("content") or ""), str(m.get("thinking") or ""), list(m.get("tool_calls") or []), data,
                            str(data.get("model") or ""))

    def _parse_stream(self, resp: httpx.Response, on_text: OnText | None) -> Response:
        text: list[str] = []
        thinking: list[str] = []
        calls: list[dict] = []
        final: Mapping[str, Any] | None = None
        model = ""
        for line in resp.iter_lines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("error"):
                raise self._error(obj["error"])
            model = obj.get("model") or model
            m = obj.get("message") or {}
            if m.get("content"):
                text.append(m["content"])
                if on_text:
                    on_text(m["content"])
            if m.get("thinking"):
                thinking.append(m["thinking"])
            calls.extend(m.get("tool_calls") or [])
            if obj.get("done"):
                final = obj
                break
        if final is None:
            raise AmbiguousCompletion(f"{self.provider_name}: Ollama stream ended without done=true", provider=self.provider_name)
        return self._finish("".join(text), "".join(thinking), calls, final, model)

    # ------------------------------------------------------------------ discovery
    def discover_models(self) -> ModelDiscovery:
        try:
            data = self._get_json(f"{self.endpoint}/api/tags", self._headers())
        except InvalidRequest as e:
            return ModelDiscovery(False, error=f"{self.endpoint}/api/tags not available (HTTP {e.status})", status=e.status)
        rows = data.get("models") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return ModelDiscovery(False, error="unexpected /api/tags response shape")
        out: list[ModelInfo] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            name = r.get("name") or r.get("model")
            if not name:
                continue
            d = r.get("details") or {}
            cw = d.get("context_length") if isinstance(d.get("context_length"), int) else None
            out.append(ModelInfo(id=str(name), source="discovered", context_window=cw))
        return ModelDiscovery(True, out)
