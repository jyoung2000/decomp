"""Gemini generateContent REST adapter (streamGenerateContent?alt=sse).

docs unreachable on 2026-10-06 (ai.google.dev); verify. Implemented from the stable, publicly known REST shapes:
* auth header ``x-goog-api-key`` (never a ``?key=`` query parameter, so keys cannot leak into URLs/logs);
* ``contents[].parts[]`` text / inlineData / functionCall / functionResponse; ``systemInstruction``;
* ``tools[].functionDeclarations`` with ``parametersJsonSchema`` (set ``schema_field="parameters"`` for the older
  OpenAPI-subset field), ``toolConfig.functionCallingConfig``;
* ``generationConfig``: maxOutputTokens, temperature, responseMimeType + ``responseJsonSchema``, ``thinkingConfig``;
* usage from ``usageMetadata`` (prompt, candidates + thoughts billed as output, cachedContentTokenCount).
Model parts are preserved verbatim (thought signatures) and replayed to Gemini unchanged.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Mapping

import httpx

from .base import (AmbiguousCompletion, CapabilityError, ImagePart, InvalidRequest, ModelDiscovery, ModelInfo, OnText,
                   ProviderAdapter, ProviderError, Request, Response, TextPart, ToolCall, ToolResultPart, ToolUsePart,
                   Usage, iter_sse)

DEFAULT_ENDPOINT = "https://generativelanguage.googleapis.com"
_FINISH = {"STOP": "end_turn", "MAX_TOKENS": "max_tokens", "SAFETY": "content_filter", "RECITATION": "content_filter",
           "BLOCKLIST": "content_filter", "PROHIBITED_CONTENT": "content_filter", "SPII": "content_filter",
           "IMAGE_SAFETY": "content_filter"}


def _bare(model: str) -> str:
    return model[len("models/"):] if model.startswith("models/") else model


class GeminiAdapter(ProviderAdapter):
    provider_key = "gemini"
    provider_name = "gemini"

    def __init__(self, *, endpoint: str | None = None, api_key: str | None = None, transport: httpx.BaseTransport | None = None,
                 timeout_s: float = 180.0, extra_headers: Mapping[str, str] | None = None,
                 api_version: str = "v1beta", schema_field: str = "parametersJsonSchema"):
        super().__init__(endpoint=endpoint or DEFAULT_ENDPOINT, api_key=api_key, transport=transport, timeout_s=timeout_s,
                         extra_headers=extra_headers)
        self.api_version = api_version
        self.schema_field = schema_field

    def _headers(self) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self._api_key:
            h["x-goog-api-key"] = self._api_key
        h.update(self.extra_headers)
        return h

    # ------------------------------------------------------------------ request building
    def _contents(self, request: Request) -> list[dict]:
        names: dict[str, str] = {}
        for m in request.messages:
            for p in m.parts():
                if isinstance(p, ToolUsePart):
                    names[p.id] = p.name
        out: list[dict] = []
        for m in request.messages:
            if m.role == "assistant" and m.provider == self.provider_key and m.provider_blocks is not None:
                out.append({"role": "model", "parts": copy.deepcopy(m.provider_blocks)})
                continue
            parts: list[dict] = []
            for p in m.parts():
                if isinstance(p, TextPart):
                    parts.append({"text": p.text})
                elif isinstance(p, ImagePart):
                    if p.data is None:
                        raise CapabilityError("gemini adapter needs inline image data (URL images are not sent)", provider="gemini")
                    parts.append({"inlineData": {"mimeType": p.media_type, "data": p.data}})
                elif isinstance(p, ToolUsePart):
                    fc: dict[str, Any] = {"name": p.name, "args": p.input}
                    if not p.id.startswith("gemini-call-"):   # synthesized ids are local-only; never sent
                        fc["id"] = p.id
                    parts.append({"functionCall": fc})
                elif isinstance(p, ToolResultPart):
                    name = p.name or names.get(p.tool_use_id)
                    if not name:
                        raise CapabilityError("gemini functionResponse needs the function name (set ToolResultPart.name)", provider="gemini")
                    key = "error" if p.is_error else "output"
                    fr: dict[str, Any] = {"name": name, "response": {key: p.content}}
                    if not p.tool_use_id.startswith("gemini-call-"):
                        fr["id"] = p.tool_use_id
                    parts.append({"functionResponse": fr})
            if parts:
                out.append({"role": "model" if m.role == "assistant" else "user", "parts": parts})
        return out

    def build_body(self, request: Request) -> dict:
        body: dict[str, Any] = {"contents": self._contents(request)}
        if request.system:
            body["systemInstruction"] = {"parts": [{"text": request.system}]}
        if request.tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t.name, "description": t.description, self.schema_field: t.parameters} for t in request.tools]}]
            tc = request.tool_choice
            if tc is not None:
                if tc == "auto":
                    cfg: dict[str, Any] = {"mode": "AUTO"}
                elif tc == "none":
                    cfg = {"mode": "NONE"}
                elif tc == "required":
                    cfg = {"mode": "ANY"}
                else:
                    cfg = {"mode": "ANY", "allowedFunctionNames": [tc["name"]]}
                body["toolConfig"] = {"functionCallingConfig": cfg}
        gen: dict[str, Any] = {"maxOutputTokens": request.max_output_tokens}
        if request.temperature is not None:
            gen["temperature"] = request.temperature
        if request.json_schema is not None:
            gen["responseMimeType"] = "application/json"
            gen["responseJsonSchema"] = request.json_schema.schema
        r = request.reasoning
        if r is not None:
            th: dict[str, Any] = {}
            if r.budget_tokens is not None:
                th["thinkingBudget"] = r.budget_tokens
            if r.effort:
                th["thinkingLevel"] = r.effort
            if r.summary:
                th["includeThoughts"] = True
            if th:
                gen["thinkingConfig"] = th
        body["generationConfig"] = gen
        return body

    # ------------------------------------------------------------------ complete
    def complete(self, request: Request, on_text: OnText | None = None) -> Response:
        if not request.model:
            raise CapabilityError("no model id given (models are never inferred)", provider="gemini")
        stream = request.stream
        model = _bare(request.model)
        method = "streamGenerateContent?alt=sse" if stream else "generateContent"
        url = f"{self.endpoint}/{self.api_version}/models/{model}:{method}"
        body = self.build_body(request)

        def handler(resp: httpx.Response) -> Response:
            ctype = resp.headers.get("content-type", "")
            if stream and "json" not in ctype:
                return self._parse_stream(resp, on_text, request.model)
            resp.read()
            data = resp.json()
            chunks = data if isinstance(data, list) else [data]
            r = self._fold(chunks, request.model, on_text)
            return r

        return self._send("POST", url, body=body, headers=self._headers(), stream=stream, timeout_s=request.timeout_s,
                          handler=handler)

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def _usage(u: Mapping[str, Any] | None) -> Usage:
        if not u:
            return Usage(known=False)
        thoughts = int(u.get("thoughtsTokenCount") or 0)
        return Usage(input_tokens=int(u.get("promptTokenCount") or 0),
                     output_tokens=int(u.get("candidatesTokenCount") or 0) + thoughts,
                     cached_tokens=int(u.get("cachedContentTokenCount") or 0), reasoning_tokens=thoughts)

    @staticmethod
    def _merge_part(parts: list[dict], new: dict) -> None:
        """Append a streamed part, merging adjacent plain text fragments (same thought flag, no signature on the first)."""
        if parts and "text" in new and "text" in parts[-1] and "functionCall" not in new:
            last = parts[-1]
            if bool(last.get("thought")) == bool(new.get("thought")) and "thoughtSignature" not in last:
                last["text"] += new["text"]
                if "thoughtSignature" in new:
                    last["thoughtSignature"] = new["thoughtSignature"]
                return
        parts.append(dict(new))

    def _fold(self, chunks: list[Mapping[str, Any]], req_model: str, on_text: OnText | None) -> Response:
        parts: list[dict] = []
        usage = Usage(known=False)
        finish: str | None = None
        rid = model = None
        blocked: str | None = None
        for c in chunks:
            if c.get("error"):
                e = c["error"]
                raise AmbiguousCompletion(f"gemini stream error {e.get('status', '')}: {e.get('message', '')}", provider="gemini")
            rid = c.get("responseId", rid)
            model = c.get("modelVersion", model)
            if c.get("usageMetadata"):
                usage = self._usage(c["usageMetadata"])
            pf = c.get("promptFeedback") or {}
            if pf.get("blockReason"):
                blocked = str(pf["blockReason"])
            for cand in c.get("candidates") or []:
                for p in (cand.get("content") or {}).get("parts") or []:
                    self._merge_part(parts, p)
                    if on_text and p.get("text") and not p.get("thought"):
                        on_text(p["text"])
                if cand.get("finishReason"):
                    finish = cand["finishReason"]
        if finish is None and blocked is None:
            raise AmbiguousCompletion("gemini: stream ended without finishReason", provider="gemini",
                                      partial_usage=usage if usage.known else None)
        text = "".join(p["text"] for p in parts if "text" in p and not p.get("thought"))
        thinking = "".join(p["text"] for p in parts if "text" in p and p.get("thought"))
        calls, notes = [], []
        for i, p in enumerate(parts):
            fc = p.get("functionCall")
            if fc:
                calls.append(ToolCall(fc.get("id") or f"gemini-call-{i}", fc.get("name", ""), fc.get("args") or {}))
        if blocked:
            reason = "refusal"
            notes.append(f"prompt blocked: {blocked}")
        elif calls and finish == "STOP":
            reason = "tool_use"
        else:
            reason = _FINISH.get(finish or "", f"other:{finish}")
        if not usage.known:
            notes.append("endpoint did not report usage")
        return Response(provider="gemini", model=model or req_model, text=text, tool_calls=calls, stop_reason=reason,
                        usage=usage, request_id=rid, provider_blocks=parts or None, thinking=thinking, notes=notes,
                        provider_key=self.provider_key)

    def _parse_stream(self, resp: httpx.Response, on_text: OnText | None, req_model: str) -> Response:
        chunks: list[dict] = []
        for _ev, data in iter_sse(resp):
            try:
                chunks.append(json.loads(data))
            except ValueError:
                continue
        return self._fold(chunks, req_model, on_text)

    # ------------------------------------------------------------------ discovery
    def discover_models(self) -> ModelDiscovery:
        models: list[ModelInfo] = []
        token: str | None = None
        for _ in range(20):
            url = f"{self.endpoint}/{self.api_version}/models?pageSize=1000" + (f"&pageToken={token}" if token else "")
            try:
                data = self._get_json(url, self._headers())
            except InvalidRequest as e:
                if e.status in (404, 405, 501):
                    return ModelDiscovery(False, error=f"models endpoint unavailable (HTTP {e.status})", status=e.status)
                raise
            for r in data.get("models") or []:
                name = r.get("name")
                methods = r.get("supportedGenerationMethods") or []
                if not name or ("generateContent" not in methods and methods):
                    continue
                models.append(ModelInfo(id=_bare(str(name)), display_name=r.get("displayName"), source="discovered",
                                        context_window=r.get("inputTokenLimit") if isinstance(r.get("inputTokenLimit"), int) else None,
                                        max_output=r.get("outputTokenLimit") if isinstance(r.get("outputTokenLimit"), int) else None))
            token = data.get("nextPageToken")
            if not token:
                break
        return ModelDiscovery(True, models)
