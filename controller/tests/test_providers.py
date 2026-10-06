"""Protocol tests for every adapter. All traffic goes through httpx.MockTransport: nothing here reaches a network or
spends money, and passing these tests does NOT certify a live account (see docs/PROVIDERS.md; opt-in `pytest -m live`)."""
import json
import os
import stat
import sys

import httpx
import pytest

from rebuild_controller.providers import (AmbiguousCompletion, AuthError, ImagePart, InvalidRequest, JsonSchema, Message,
                                          MockProvider, ProviderError, ProviderUnavailable, RaisingProvider, RateLimit, Reasoning,
                                          Request, Response, TextPart, Timeout, Tool, ToolResultPart, ToolUsePart, Unreachable,
                                          Usage, UsageLimit)
from rebuild_controller.providers import subscription as sub
from rebuild_controller.providers.anthropic import AnthropicAdapter, thinking_style
from rebuild_controller.providers.base import CapabilityError, RED_PNG, classify_error, iter_sse
from rebuild_controller.providers.gemini import GeminiAdapter
from rebuild_controller.providers.mock import AICallAttempted
from rebuild_controller.providers.openai_responses import OpenAIResponsesAdapter
from rebuild_controller.providers.secrets import redact, register_secret

KEY = "test-key-NOT-REAL-0123456789abcdef"


# ----------------------------------------------------------------------------------------------- helpers
def sse(*events, event_names=True) -> bytes:
    out = []
    for ev in events:
        if ev == "[DONE]":
            out.append("data: [DONE]\n\n")
            continue
        name = ev.get("type") if isinstance(ev, dict) else None
        out.append((f"event: {name}\n" if (name and event_names) else "") + f"data: {json.dumps(ev)}\n\n")
    return "".join(out).encode()


def sse_response(*events, status=200, **kw) -> httpx.Response:
    return httpx.Response(status, content=sse(*events, **kw), headers={"content-type": "text/event-stream"})


class CutStream(httpx.SyncByteStream):
    """Yields some bytes, then the connection drops."""

    def __init__(self, data: bytes, exc: Exception):
        self.data, self.exc = data, exc

    def __iter__(self):
        yield self.data
        raise self.exc


def cut_response(data: bytes, exc: Exception | None = None) -> httpx.Response:
    return httpx.Response(200, stream=CutStream(data, exc or httpx.ReadError("connection reset")),
                          headers={"content-type": "text/event-stream"})


class Recorder:
    """Transport that records requests and replies from a list or a callable."""

    def __init__(self, reply):
        self.reply, self.requests = reply, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.reply(request) if callable(self.reply) else self.reply
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    @property
    def last_body(self) -> dict:
        return json.loads(self.requests[-1].content)


def json_response(obj, status=200, headers=None) -> httpx.Response:
    return httpx.Response(status, json=obj, headers=headers)


def user(text="hi") -> list[Message]:
    return [Message.user(text)]


ECHO = Tool("echo", "echo", {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]})


# =============================================================================================== OpenAI Responses
def responses_stream(text="Hello", usage=None, extra_items=None, status="completed", details=None):
    usage = usage or {"input_tokens": 12, "input_tokens_details": {"cached_tokens": 4}, "output_tokens": 7,
                      "output_tokens_details": {"reasoning_tokens": 3}, "total_tokens": 19}
    item = {"type": "message", "id": "msg_1", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    items = [item] + (extra_items or [])
    resp = {"id": "resp_1", "model": "gpt-test", "status": status, "output": items, "usage": usage}
    if details:
        resp["incomplete_details"] = details
    evs = [{"type": "response.created", "response": {"id": "resp_1"}},
           {"type": "response.output_text.delta", "delta": text[:2]}, {"type": "response.output_text.delta", "delta": text[2:]},
           {"type": "response.output_item.done", "output_index": 0, "item": item},
           {"type": "response.incomplete" if status == "incomplete" else "response.completed", "response": resp}]
    return sse_response(*evs)


def test_responses_streaming_text_and_usage_accounting():
    rec = Recorder(lambda r: responses_stream())
    a = OpenAIResponsesAdapter(api_key=KEY, transport=rec.transport)
    seen = []
    r = a.complete(Request(model="gpt-test", messages=user(), system="be brief", max_output_tokens=50), on_text=seen.append)
    assert r.text == "Hello" and seen == ["He", "llo"] and r.stop_reason == "end_turn"
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cached_tokens, r.usage.reasoning_tokens) == (12, 7, 4, 3)
    assert r.usage.known and r.request_id == "resp_1"
    req = rec.requests[0]
    assert str(req.url) == "https://api.openai.com/v1/responses" and req.headers["authorization"] == f"Bearer {KEY}"
    body = rec.last_body
    assert body["stream"] is True and body["store"] is False and body["max_output_tokens"] == 50
    assert body["instructions"] == "be brief"
    assert body["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]


def test_responses_request_shape_tools_schema_images_reasoning():
    rec = Recorder(lambda r: responses_stream())
    a = OpenAIResponsesAdapter(api_key=KEY, transport=rec.transport)
    msgs = [Message("user", [TextPart("look"), ImagePart.from_bytes(RED_PNG)])]
    a.complete(Request(model="m", messages=msgs, tools=[ECHO], tool_choice={"name": "echo"},
                       json_schema=JsonSchema("out", {"type": "object"}), reasoning=Reasoning(effort="low", summary="auto")))
    b = rec.last_body
    assert b["input"][0]["content"][1]["type"] == "input_image" and b["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,")
    assert b["tools"][0] == {"type": "function", "name": "echo", "description": "echo", "parameters": ECHO.parameters, "strict": False}
    assert b["tool_choice"] == {"type": "function", "name": "echo"}
    assert b["text"]["format"] == {"type": "json_schema", "name": "out", "schema": {"type": "object"}, "strict": True}
    assert b["reasoning"] == {"effort": "low", "summary": "auto"} and b["include"] == ["reasoning.encrypted_content"]


def test_responses_tool_call_roundtrip_preserves_output_items_verbatim():
    fc = {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "echo", "arguments": '{"value":"x"}', "status": "completed"}
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "thinking"}], "encrypted_content": "ENC"}
    rec = Recorder(lambda r: responses_stream(text="", extra_items=[reasoning, fc]))
    a = OpenAIResponsesAdapter(api_key=KEY, transport=rec.transport)
    r = a.complete(Request(model="m", messages=user(), tools=[ECHO]))
    assert r.stop_reason == "tool_use" and r.tool_calls[0].input == {"value": "x"} and r.tool_calls[0].id == "call_9"
    assert r.thinking == "thinking"
    assert fc in r.provider_blocks and reasoning in r.provider_blocks
    history = user() + [r.assistant_message(), Message("user", [ToolResultPart("call_9", "done")])]
    a.complete(Request(model="m", messages=history, tools=[ECHO]))
    sent = rec.last_body["input"]
    assert reasoning in sent and fc in sent  # replayed verbatim, incl. encrypted reasoning
    assert sent[-1] == {"type": "function_call_output", "call_id": "call_9", "output": "done"}


def test_responses_incomplete_maps_to_max_tokens():
    rec = Recorder(lambda r: responses_stream(status="incomplete", details={"reason": "max_output_tokens"}))
    r = OpenAIResponsesAdapter(api_key=KEY, transport=rec.transport).complete(Request(model="m", messages=user()))
    assert r.stop_reason == "max_tokens"


def test_responses_non_stream_request_parses_json_body():
    body = {"id": "r", "model": "m", "status": "completed", "usage": {"input_tokens": 3, "output_tokens": 2},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "plain"}]}]}
    rec = Recorder(lambda r: json_response(body))
    r = OpenAIResponsesAdapter(api_key=KEY, transport=rec.transport).complete(Request(model="m", messages=user(), stream=False))
    assert r.text == "plain" and r.usage.output_tokens == 2 and rec.last_body["stream"] is False


def test_responses_stream_failure_events_are_typed():
    def failed(code, msg):
        return sse_response({"type": "response.failed", "response": {"error": {"code": code, "message": msg}}})
    a = lambda reply: OpenAIResponsesAdapter(api_key=KEY, transport=Recorder(reply).transport)
    with pytest.raises(UsageLimit):
        a(failed("insufficient_quota", "You exceeded your current quota")).complete(Request(model="m", messages=user()))
    with pytest.raises(RateLimit):
        a(failed("rate_limit_exceeded", "slow down")).complete(Request(model="m", messages=user()))
    with pytest.raises(AmbiguousCompletion):
        a(failed("server_error", "boom")).complete(Request(model="m", messages=user()))


# =============================================================================================== chat dialect (OpenRouter / local)
def chat_chunks(text="ok", usage=True, tool=None, finish="stop", with_done=True, cost=None):
    chunks = [{"id": "c1", "model": "local-m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}]
    for piece in (text[:1], text[1:]):
        if piece:
            chunks.append({"id": "c1", "choices": [{"index": 0, "delta": {"content": piece}}]})
    if tool:
        chunks.append({"id": "c1", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "type": "function", "function": {"name": "echo", "arguments": ""}}]}}]})
        for frag in ('{"val', 'ue":"x"}'):
            chunks.append({"id": "c1", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": frag}}]}}]})
    chunks.append({"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else finish}]})
    if usage:
        u = {"prompt_tokens": 20, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 8},
             "completion_tokens_details": {"reasoning_tokens": 1}}
        if cost is not None:
            u["cost"] = cost
        chunks.append({"id": "c1", "choices": [], "usage": u})
    if with_done:
        chunks.append("[DONE]")
    return sse_response(*chunks, event_names=False)


def test_chat_dialect_streaming_usage_and_request_shape():
    rec = Recorder(lambda r: chat_chunks("ok"))
    a = OpenAIResponsesAdapter(endpoint="http://localhost:1234/v1", provider_name="local", dialect="chat", transport=rec.transport)
    r = a.complete(Request(model="local-m", messages=user(), system="sys", max_output_tokens=33))
    assert r.text == "ok" and r.usage.input_tokens == 20 and r.usage.cached_tokens == 8 and r.usage.output_tokens == 5 and r.usage.known
    req = rec.requests[0]
    assert str(req.url) == "http://localhost:1234/v1/chat/completions" and "authorization" not in req.headers  # no key => no header
    b = rec.last_body
    assert b["stream_options"] == {"include_usage": True} and b["max_tokens"] == 33 and "max_completion_tokens" not in b
    assert b["messages"][0] == {"role": "system", "content": "sys"} and b["messages"][1] == {"role": "user", "content": "hi"}


def test_chat_dialect_tool_calls_accumulate_and_are_replayed():
    rec = Recorder(lambda r: chat_chunks("", tool=True))
    a = OpenAIResponsesAdapter(endpoint="http://localhost:11434/v1", provider_name="local", dialect="chat", transport=rec.transport)
    r = a.complete(Request(model="m", messages=user(), tools=[ECHO], tool_choice="auto"))
    assert r.stop_reason == "tool_use" and r.tool_calls[0].name == "echo" and r.tool_calls[0].input == {"value": "x"}
    assert rec.last_body["tools"][0]["function"]["name"] == "echo" and rec.last_body["tool_choice"] == "auto"
    a.complete(Request(model="m", messages=user() + [r.assistant_message(), Message("user", [ToolResultPart("call_a", "r", is_error=True)])], tools=[ECHO]))
    msgs = rec.last_body["messages"]
    assert msgs[1]["tool_calls"][0]["function"]["arguments"] == '{"value":"x"}'
    assert msgs[2] == {"role": "tool", "tool_call_id": "call_a", "content": "ERROR: r"}


def test_chat_dialect_missing_usage_is_reported_unknown_not_zero():
    rec = Recorder(lambda r: chat_chunks("ok", usage=False))
    r = OpenAIResponsesAdapter(endpoint="http://localhost:1234/v1", provider_name="local", dialect="chat", transport=rec.transport) \
        .complete(Request(model="m", messages=user()))
    assert r.usage.known is False and "did not report usage" in " ".join(r.notes)


def test_chat_dialect_openrouter_requests_and_reads_cost_and_images():
    rec = Recorder(lambda r: chat_chunks("ok", cost=0.00123))
    a = OpenAIResponsesAdapter(provider_name="openrouter", dialect="chat", api_key=KEY, transport=rec.transport)
    r = a.complete(Request(model="vendor/m", messages=[Message("user", [TextPart("x"), ImagePart.from_bytes(RED_PNG)])],
                           reasoning=Reasoning(effort="high")))
    assert str(rec.requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    b = rec.last_body
    assert b["usage"] == {"include": True} and b["reasoning"] == {"effort": "high"}
    assert b["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert r.usage.reported_cost_usd == pytest.approx(0.00123)


def test_chat_dialect_openai_uses_max_completion_tokens_and_reasoning_effort():
    rec = Recorder(lambda r: chat_chunks("ok"))
    OpenAIResponsesAdapter(provider_name="openai", dialect="chat", api_key=KEY, transport=rec.transport) \
        .complete(Request(model="m", messages=user(), max_output_tokens=9, reasoning=Reasoning(effort="low")))
    b = rec.last_body
    assert b["max_completion_tokens"] == 9 and b["reasoning_effort"] == "low" and "max_tokens" not in b


def test_chat_dialect_server_that_ignores_stream_returns_json():
    body = {"id": "x", "model": "m", "choices": [{"message": {"content": "json path"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3}}
    rec = Recorder(lambda r: json_response(body))
    r = OpenAIResponsesAdapter(endpoint="http://localhost:1/v1", provider_name="local", dialect="chat", transport=rec.transport) \
        .complete(Request(model="m", messages=user()))
    assert r.text == "json path" and r.usage.input_tokens == 4


def test_chat_stream_without_terminal_marker_is_ambiguous_with_partial_usage():
    def truncated(r):  # content arrives, then neither finish_reason nor [DONE]
        data = sse({"id": "c", "choices": [{"index": 0, "delta": {"content": "par"}}]}, event_names=False)
        return httpx.Response(200, content=data, headers={"content-type": "text/event-stream"})
    a = OpenAIResponsesAdapter(endpoint="http://localhost:1/v1", provider_name="local", dialect="chat", transport=Recorder(truncated).transport)
    with pytest.raises(AmbiguousCompletion):
        a.complete(Request(model="m", messages=user()))


def test_invalid_dialect_and_missing_endpoint_rejected():
    with pytest.raises(ValueError):
        OpenAIResponsesAdapter(dialect="soap")
    with pytest.raises(ValueError):
        OpenAIResponsesAdapter(provider_name="local")  # local endpoints are never defaulted


# =============================================================================================== Anthropic
def anthropic_stream(blocks=None, usage_start=None, usage_end=None, stop="end_turn", model="claude-sonnet-5-5", error=None, no_stop=False):
    usage_start = usage_start or {"input_tokens": 25, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 100, "output_tokens": 1}
    usage_end = usage_end or {"output_tokens": 42}
    evs = [{"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
                                                 "usage": usage_start}}]
    for i, b in enumerate(blocks or [("text", "Hi there")]):
        kind = b[0]
        if kind == "text":
            evs += [{"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                    {"type": "ping"},
                    {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b[1][:3]}},
                    {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b[1][3:]}}]
        elif kind == "thinking":
            evs += [{"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": ""}},
                    {"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": b[1]}},
                    {"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": "SIG=="}}]
        elif kind == "redacted":
            evs += [{"type": "content_block_start", "index": i, "content_block": {"type": "redacted_thinking", "data": "OPAQUE"}}]
        elif kind == "tool":
            evs += [{"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "echo", "input": {}}},
                    {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": ""}},
                    {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": '{"value": "'}},
                    {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": 'x"}'}}]
        elif kind == "badtool":
            evs += [{"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": "toolu_2", "name": "echo", "input": {}}},
                    {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": '{"value": "tru'}}]
        evs.append({"type": "content_block_stop", "index": i})
    if error:
        evs.append({"type": "error", "error": error})
    else:
        evs.append({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": usage_end})
        if not no_stop:
            evs.append({"type": "message_stop"})
    return sse_response(*evs)


def test_anthropic_streaming_text_usage_and_headers():
    rec = Recorder(lambda r: anthropic_stream())
    a = AnthropicAdapter(api_key=KEY, transport=rec.transport, betas=["some-beta-1", "some-beta-2"])
    seen = []
    r = a.complete(Request(model="claude-sonnet-5-5", messages=user(), system="sys", max_output_tokens=500), on_text=seen.append)
    assert r.text == "Hi there" and seen == ["Hi ", "there"] and r.stop_reason == "end_turn" and r.request_id == "msg_1"
    # total prompt = uncached + cache write + cache read; output is the cumulative count from message_delta
    assert (r.usage.input_tokens, r.usage.cached_tokens, r.usage.cache_write_tokens, r.usage.output_tokens) == (135, 100, 10, 42)
    h = rec.requests[0].headers
    assert str(rec.requests[0].url) == "https://api.anthropic.com/v1/messages"
    assert h["x-api-key"] == KEY and h["anthropic-version"] == "2023-06-01" and h["anthropic-beta"] == "some-beta-1,some-beta-2"
    b = rec.last_body
    assert b["stream"] is True and b["max_tokens"] == 500 and b["system"] == [{"type": "text", "text": "sys"}]
    assert b["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_anthropic_content_blocks_preserved_verbatim_and_replayed():
    rec = Recorder(lambda r: anthropic_stream([("thinking", "hmm"), ("redacted",), ("text", "answer"), ("tool", )], stop="tool_use"))
    a = AnthropicAdapter(api_key=KEY, transport=rec.transport)
    r = a.complete(Request(model="claude-opus-5-5", messages=user(), tools=[ECHO]))
    assert r.stop_reason == "tool_use" and r.tool_calls[0].input == {"value": "x"} and r.thinking == "hmm"
    assert r.provider_blocks == [{"type": "thinking", "thinking": "hmm", "signature": "SIG=="},
                                 {"type": "redacted_thinking", "data": "OPAQUE"},
                                 {"type": "text", "text": "answer"},
                                 {"type": "tool_use", "id": "toolu_1", "name": "echo", "input": {"value": "x"}}]
    history = user() + [r.assistant_message(), Message("user", [TextPart("also"), ToolResultPart("toolu_1", "res")])]
    a.complete(Request(model="claude-opus-5-5", messages=history, tools=[ECHO]))
    sent = rec.last_body["messages"]
    assert sent[1] == {"role": "assistant", "content": r.provider_blocks}
    # tool_result blocks lead the user message (API requirement)
    assert [b["type"] for b in sent[2]["content"]] == ["tool_result", "text"]


def test_anthropic_foreign_provider_blocks_degrade_to_typed_content():
    msg = Message("assistant", [TextPart("t"), ToolUsePart("c1", "echo", {"value": "v"})], provider="openai:chat", provider_blocks=[{"role": "assistant"}])
    rec = Recorder(lambda r: anthropic_stream())
    AnthropicAdapter(api_key=KEY, transport=rec.transport).complete(Request(model="claude-sonnet-5-5", messages=user() + [msg, Message.user("go")]))
    sent = rec.last_body["messages"][1]
    assert sent == {"role": "assistant", "content": [{"type": "text", "text": "t"}, {"type": "tool_use", "id": "c1", "name": "echo", "input": {"value": "v"}}]}


def test_anthropic_invalid_tool_json_is_surfaced():
    rec = Recorder(lambda r: anthropic_stream([("badtool",)], stop="tool_use"))
    r = AnthropicAdapter(api_key=KEY, transport=rec.transport).complete(Request(model="claude-sonnet-5-5", messages=user(), tools=[ECHO]))
    assert r.tool_calls[0].input == {"_raw_arguments": '{"value": "tru'} and any("invalid JSON" in n for n in r.notes)
    assert "_invalid_json" not in json.dumps(r.provider_blocks)


@pytest.mark.parametrize("model,style", [("claude-fable-5-1", "always_on"), ("claude-opus-5-5", "always_on"), ("claude-opus-4-8", "adaptive"),
                                          ("claude-sonnet-5-5", "adaptive"), ("claude-haiku-4-5", "budget"),
                                          ("claude-haiku-4-5-20251001", "budget"), ("some-new-model", "unknown")])
def test_anthropic_thinking_style_table(model, style):
    assert thinking_style(model) == style


def _body(model, **kw):
    rec = Recorder(lambda r: anthropic_stream(model=model))
    AnthropicAdapter(api_key=KEY, transport=rec.transport).complete(Request(model=model, messages=user(), max_output_tokens=4000, **kw))
    return rec.last_body


def test_anthropic_thinking_effort_schema_and_sampling_rules():
    # adaptive family: adaptive thinking + output_config.effort + format; temperature dropped on models that reject it
    b = _body("claude-sonnet-5-5", reasoning=Reasoning(effort="medium", summary="auto"), temperature=0.2,
              json_schema=JsonSchema("x", {"type": "object"}))
    assert b["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert b["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert "temperature" not in b
    # always-on family: no thinking field unless a summary is requested; budget never sent
    b = _body("claude-opus-5-5", reasoning=Reasoning(effort="high", budget_tokens=2000))
    assert "thinking" not in b and b["output_config"] == {"effort": "high"}
    # budget family
    b = _body("claude-haiku-4-5", reasoning=Reasoning(budget_tokens=2048), temperature=0.3)
    assert b["thinking"] == {"type": "enabled", "budget_tokens": 2048} and b["temperature"] == 0.3
    # unknown models: nothing is guessed
    b = _body("claude-brand-new", reasoning=Reasoning(effort="low"))
    assert "thinking" not in b and b["output_config"] == {"effort": "low"}
    b = _body("claude-brand-new", reasoning=Reasoning(mode="adaptive"))
    assert b["thinking"] == {"type": "adaptive"}
    with pytest.raises(CapabilityError):
        _body("claude-haiku-4-5", reasoning=Reasoning(budget_tokens=100))


def test_anthropic_forced_tool_choice_refused_where_documented_to_400():
    a = AnthropicAdapter(api_key=KEY, transport=Recorder(lambda r: anthropic_stream()).transport)
    with pytest.raises(CapabilityError):
        a.complete(Request(model="claude-opus-5-5", messages=user(), tools=[ECHO], tool_choice={"name": "echo"}))
    with pytest.raises(CapabilityError):
        a.complete(Request(model="claude-sonnet-5-5", messages=user(), tools=[ECHO], tool_choice="required"))
    rec = Recorder(lambda r: anthropic_stream(model="claude-opus-4-8"))
    AnthropicAdapter(api_key=KEY, transport=rec.transport).complete(Request(model="claude-opus-4-8", messages=user(), tools=[ECHO], tool_choice={"name": "echo"}))
    assert rec.last_body["tool_choice"] == {"type": "tool", "name": "echo"}


def test_anthropic_prompt_caching_markers_and_ttl():
    b = _body("claude-sonnet-5-5", system="stable prefix", tools=[ECHO], cache=True, cache_ttl="1h")
    assert b["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"} and b["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    b = _body("claude-sonnet-5-5", system="s", cache=True)
    assert b["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in _body("claude-sonnet-5-5", system="s")["system"][0]


def test_anthropic_images_url_and_base64():
    rec = Recorder(lambda r: anthropic_stream())
    msgs = [Message("user", [ImagePart.from_bytes(RED_PNG), ImagePart(url="https://x.test/a.png"), TextPart("q")])]
    AnthropicAdapter(api_key=KEY, transport=rec.transport).complete(Request(model="claude-sonnet-5-5", messages=msgs))
    c = rec.last_body["messages"][0]["content"]
    assert c[0]["source"]["type"] == "base64" and c[0]["source"]["media_type"] == "image/png" and c[1]["source"] == {"type": "url", "url": "https://x.test/a.png"}


def test_anthropic_midstream_overloaded_is_ambiguous_with_partial_usage():
    rec = Recorder(lambda r: anthropic_stream(error={"type": "overloaded_error", "message": "Overloaded"}))
    with pytest.raises(AmbiguousCompletion) as ei:
        AnthropicAdapter(api_key=KEY, transport=rec.transport).complete(Request(model="claude-sonnet-5-5", messages=user()))
    assert ei.value.partial_usage is not None and ei.value.partial_usage.input_tokens == 135 and not ei.value.retry_safe


def test_anthropic_stream_without_message_stop_is_ambiguous():
    rec = Recorder(lambda r: anthropic_stream(no_stop=True))
    with pytest.raises(AmbiguousCompletion):
        AnthropicAdapter(api_key=KEY, transport=rec.transport).complete(Request(model="claude-sonnet-5-5", messages=user()))


def test_anthropic_connection_drop_mid_stream_is_ambiguous():
    full = sse({"type": "message_start", "message": {"id": "m", "model": "x", "usage": {"input_tokens": 5, "output_tokens": 1}}},
               {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    rec = Recorder(lambda r: cut_response(full))
    with pytest.raises(AmbiguousCompletion) as ei:
        AnthropicAdapter(api_key=KEY, transport=rec.transport).complete(Request(model="claude-sonnet-5-5", messages=user()))
    assert not ei.value.retry_safe and "dropped" in str(ei.value)


def test_anthropic_unknown_stream_events_ignored():
    evs = [{"type": "message_start", "message": {"id": "m", "model": "x", "usage": {"input_tokens": 1, "output_tokens": 1}}},
           {"type": "future_event", "x": 1},
           {"type": "message_delta", "delta": {"stop_reason": "refusal"}, "usage": {"output_tokens": 2}}, {"type": "message_stop"}]
    r = AnthropicAdapter(api_key=KEY, transport=Recorder(lambda q: sse_response(*evs)).transport).complete(Request(model="claude-sonnet-5-5", messages=user()))
    assert r.stop_reason == "refusal" and r.text == ""


def test_anthropic_model_discovery_paginates_and_never_invents():
    pages = {None: {"data": [{"id": "claude-a", "display_name": "A", "max_input_tokens": 1000000, "max_tokens": 128000,
                              "capabilities": {"thinking": {"supported": True}}}], "has_more": True, "last_id": "claude-a"},
             "claude-a": {"data": [{"id": "claude-b", "display_name": "B"}], "has_more": False, "last_id": "claude-b"}}

    def reply(req):
        return json_response(pages[req.url.params.get("after_id")])
    rec = Recorder(reply)
    d = AnthropicAdapter(api_key=KEY, transport=rec.transport).discover_models()
    assert d.supported and [m.id for m in d.models] == ["claude-a", "claude-b"] and d.models[0].context_window == 1000000
    assert d.models[0].max_output == 128000 and all(m.source == "discovered" for m in d.models)
    assert len(rec.requests) == 2


def test_discovery_unsupported_endpoint_reports_not_empty_list():
    rec = Recorder(lambda r: json_response({"error": {"message": "no such route"}}, status=404))
    d = OpenAIResponsesAdapter(endpoint="http://localhost:9/v1", provider_name="local", dialect="chat", transport=rec.transport).discover_models()
    assert d.supported is False and d.models == [] and d.status == 404 and "not available" in d.error


def test_openai_style_discovery_parses_models_and_openrouter_pricing():
    rec = Recorder(lambda r: json_response({"data": [{"id": "vendor/a", "name": "A", "context_length": 8000,
                                                      "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
                                                     {"id": "vendor/b", "pricing": {"prompt": "x"}}, {"nope": 1}]}))
    d = OpenAIResponsesAdapter(provider_name="openrouter", dialect="chat", api_key=KEY, transport=rec.transport).discover_models()
    assert [m.id for m in d.models] == ["vendor/a", "vendor/b"]
    assert d.models[0].price == {"input_per_mtok": pytest.approx(1.0), "output_per_mtok": pytest.approx(2.0)} and d.models[1].price is None
    assert d.models[0].context_window == 8000


# =============================================================================================== Gemini
def gemini_chunks(parts_list, usage=None, finish="STOP", block=None):
    chunks = []
    for i, parts in enumerate(parts_list):
        c = {"candidates": [{"content": {"role": "model", "parts": parts}, "index": 0}], "modelVersion": "gemini-test", "responseId": "r1"}
        if i == len(parts_list) - 1:
            if finish:
                c["candidates"][0]["finishReason"] = finish
            c["usageMetadata"] = usage or {"promptTokenCount": 30, "candidatesTokenCount": 8, "thoughtsTokenCount": 12,
                                           "cachedContentTokenCount": 5, "totalTokenCount": 50}
        chunks.append(c)
    if block:
        chunks = [{"promptFeedback": {"blockReason": block}, "usageMetadata": {"promptTokenCount": 3}}]
    return sse_response(*chunks, event_names=False)


def test_gemini_streaming_text_usage_headers_and_url():
    rec = Recorder(lambda r: gemini_chunks([[{"text": "Hel"}], [{"text": "lo"}]]))
    a = GeminiAdapter(api_key=KEY, transport=rec.transport)
    seen = []
    r = a.complete(Request(model="models/gemini-test", messages=user(), system="sys", max_output_tokens=77, temperature=0.4), on_text=seen.append)
    assert r.text == "Hello" and seen == ["Hel", "lo"] and r.stop_reason == "end_turn" and r.request_id == "r1"
    # thoughts are billed as output
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cached_tokens, r.usage.reasoning_tokens) == (30, 20, 5, 12)
    req = rec.requests[0]
    assert req.url.path == "/v1beta/models/gemini-test:streamGenerateContent" and req.url.params["alt"] == "sse"
    assert req.headers["x-goog-api-key"] == KEY and KEY not in str(req.url)
    b = rec.last_body
    assert b["systemInstruction"] == {"parts": [{"text": "sys"}]} and b["generationConfig"] == {"maxOutputTokens": 77, "temperature": 0.4}
    assert b["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_gemini_tools_schema_images_reasoning_request_shape():
    rec = Recorder(lambda r: gemini_chunks([[{"text": "x"}]]))
    msgs = [Message("user", [TextPart("see"), ImagePart.from_bytes(RED_PNG)])]
    GeminiAdapter(api_key=KEY, transport=rec.transport).complete(
        Request(model="gemini-test", messages=msgs, tools=[ECHO], tool_choice={"name": "echo"}, json_schema=JsonSchema("o", {"type": "object"}),
                reasoning=Reasoning(budget_tokens=1024, summary="auto")))
    b = rec.last_body
    assert b["contents"][0]["parts"][1]["inlineData"]["mimeType"] == "image/png"
    assert b["tools"] == [{"functionDeclarations": [{"name": "echo", "description": "echo", "parametersJsonSchema": ECHO.parameters}]}]
    assert b["toolConfig"] == {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["echo"]}}
    g = b["generationConfig"]
    assert g["responseMimeType"] == "application/json" and g["responseJsonSchema"] == {"type": "object"}
    assert g["thinkingConfig"] == {"thinkingBudget": 1024, "includeThoughts": True}


def test_gemini_function_calls_and_verbatim_part_replay_with_thought_signature():
    parts = [{"text": "plan", "thought": True}, {"functionCall": {"name": "echo", "args": {"value": "x"}}, "thoughtSignature": "SIGNATURE"}]
    rec = Recorder(lambda r: gemini_chunks([parts]))
    a = GeminiAdapter(api_key=KEY, transport=rec.transport)
    r = a.complete(Request(model="gemini-test", messages=user(), tools=[ECHO]))
    assert r.stop_reason == "tool_use" and r.tool_calls[0].input == {"value": "x"} and r.thinking == "plan" and r.text == ""
    assert r.provider_blocks == parts
    cid = r.tool_calls[0].id
    a.complete(Request(model="gemini-test", messages=user() + [r.assistant_message(), Message("user", [ToolResultPart(cid, "out")])], tools=[ECHO]))
    contents = rec.last_body["contents"]
    assert contents[1] == {"role": "model", "parts": parts}  # signature survives
    assert contents[2]["parts"][0]["functionResponse"] == {"name": "echo", "response": {"output": "out"}}  # synthesized id never sent


def test_gemini_prompt_block_is_refusal_and_missing_finish_is_ambiguous():
    r = GeminiAdapter(api_key=KEY, transport=Recorder(lambda q: gemini_chunks([], block="SAFETY")).transport).complete(Request(model="g", messages=user()))
    assert r.stop_reason == "refusal" and any("SAFETY" in n for n in r.notes)
    with pytest.raises(AmbiguousCompletion):
        GeminiAdapter(api_key=KEY, transport=Recorder(lambda q: gemini_chunks([[{"text": "x"}]], finish=None)).transport).complete(Request(model="g", messages=user()))


def test_gemini_max_tokens_mapping_and_non_stream():
    body = {"candidates": [{"content": {"parts": [{"text": "cut"}]}, "finishReason": "MAX_TOKENS"}],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3}}
    rec = Recorder(lambda r: json_response(body))
    r = GeminiAdapter(api_key=KEY, transport=rec.transport).complete(Request(model="g", messages=user(), stream=False))
    assert r.stop_reason == "max_tokens" and rec.requests[0].url.path.endswith(":generateContent") and r.usage.output_tokens == 3


def test_gemini_discovery_filters_non_generative_and_strips_prefix():
    rec = Recorder(lambda r: json_response({"models": [
        {"name": "models/gemini-a", "displayName": "A", "supportedGenerationMethods": ["generateContent"], "inputTokenLimit": 1000, "outputTokenLimit": 100},
        {"name": "models/embed-x", "supportedGenerationMethods": ["embedContent"]}]}))
    d = GeminiAdapter(api_key=KEY, transport=rec.transport).discover_models()
    assert [m.id for m in d.models] == ["gemini-a"] and d.models[0].context_window == 1000 and d.models[0].max_output == 100


# =============================================================================================== typed errors (all adapters)
ADAPTERS = {
    "responses": lambda t: OpenAIResponsesAdapter(api_key=KEY, transport=t),
    "chat": lambda t: OpenAIResponsesAdapter(endpoint="http://localhost:1/v1", provider_name="local", dialect="chat", transport=t),
    "anthropic": lambda t: AnthropicAdapter(api_key=KEY, transport=t),
    "gemini": lambda t: GeminiAdapter(api_key=KEY, transport=t),
}


@pytest.mark.parametrize("name", list(ADAPTERS))
def test_auth_failure_is_typed_and_retry_safe(name):
    rec = Recorder(lambda r: json_response({"error": {"message": f"Incorrect API key provided: {KEY}", "type": "invalid_request_error"}}, status=401))
    register_secret(KEY)
    with pytest.raises(AuthError) as ei:
        ADAPTERS[name](rec.transport).complete(Request(model="m", messages=user()))
    assert ei.value.status == 401 and ei.value.retry_safe and KEY not in str(ei.value)  # error text is redacted


def test_gemini_invalid_key_arrives_as_400_and_is_still_auth_error():
    rec = Recorder(lambda r: json_response({"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT",
                                                       "details": [{"reason": "API_KEY_INVALID"}]}}, status=400))
    with pytest.raises(AuthError):
        ADAPTERS["gemini"](rec.transport).complete(Request(model="g", messages=user()))


@pytest.mark.parametrize("name", list(ADAPTERS))
def test_rate_limit_carries_retry_after(name):
    rec = Recorder(lambda r: json_response({"error": {"message": "slow down", "type": "rate_limit_error"}}, status=429, headers={"retry-after": "7"}))
    with pytest.raises(RateLimit) as ei:
        ADAPTERS[name](rec.transport).complete(Request(model="m", messages=user()))
    assert ei.value.retry_after == 7.0 and ei.value.retry_safe


@pytest.mark.parametrize("status,body", [
    (429, {"error": {"message": "You exceeded your current quota, please check your plan and billing details.", "type": "insufficient_quota", "code": "insufficient_quota"}}),
    (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low to access the Anthropic API."}}),
    (429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota"}}),
    (402, {"error": {"message": "Payment required"}}),
])
def test_usage_limit_is_distinguished_from_rate_limit(status, body):
    e = classify_error("x", status, json.dumps(body))
    assert isinstance(e, UsageLimit) and not isinstance(e, RateLimit)


def test_classify_error_other_statuses():
    assert isinstance(classify_error("x", 400, '{"error":{"message":"bad field"}}'), InvalidRequest)
    assert isinstance(classify_error("x", 404, "not found"), InvalidRequest)
    assert isinstance(classify_error("x", 529, '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'), ProviderUnavailable)
    e = classify_error("x", 500, "oops")
    assert isinstance(e, ProviderUnavailable) and not e.retry_safe
    assert isinstance(classify_error("x", 504, ""), Timeout) and not classify_error("x", 504, "").retry_safe
    assert classify_error("x", 429, "{}", {"retry-after-ms": "1500"}).retry_after == 1.5


@pytest.mark.parametrize("name", list(ADAPTERS))
def test_read_timeout_is_typed_not_retry_safe_and_not_retried_by_adapter(name):
    calls = []

    def boom(req):
        calls.append(1)
        return httpx.ReadTimeout("slow", request=req)
    with pytest.raises(Timeout) as ei:
        ADAPTERS[name](Recorder(boom).transport).complete(Request(model="m", messages=user()))
    assert not ei.value.retry_safe and ei.value.request_sent and len(calls) == 1  # adapters never loop on timeouts


def test_connect_failures_are_retry_safe_and_unsent():
    with pytest.raises(Timeout) as ei:
        ADAPTERS["anthropic"](Recorder(lambda r: httpx.ConnectTimeout("t", request=r)).transport).complete(Request(model="m", messages=user()))
    assert ei.value.retry_safe and not ei.value.request_sent
    with pytest.raises(Unreachable) as ei2:
        ADAPTERS["anthropic"](Recorder(lambda r: httpx.ConnectError("refused", request=r)).transport).complete(Request(model="m", messages=user()))
    assert ei2.value.retry_safe and not ei2.value.request_sent


def test_server_error_is_unavailable_not_retry_safe():
    with pytest.raises(ProviderUnavailable) as ei:
        ADAPTERS["responses"](Recorder(lambda r: httpx.Response(503, text="down")).transport).complete(Request(model="m", messages=user()))
    assert not ei.value.retry_safe


def test_missing_model_is_refused_before_any_request():
    rec = Recorder(lambda r: pytest.fail("must not send"))
    for name in ADAPTERS:
        with pytest.raises(CapabilityError):
            ADAPTERS[name](rec.transport).complete(Request(model="", messages=user()))
    assert rec.requests == []


def test_sse_parser_handles_comments_multiline_data_and_crlf():
    raw = b": keepalive\r\nevent: a\r\ndata: {\"x\":\r\ndata: 1}\r\n\r\ndata: tail"
    resp = httpx.Response(200, content=raw)
    assert list(iter_sse(resp)) == [("a", '{"x":\n1}'), (None, "tail")]


# =============================================================================================== capability probing
def probing_handler(*, tools=True, schema=True, images=True, see_red=True):
    """A fake OpenAI-compatible chat endpoint whose features can be switched off."""
    def reply(req):
        if req.url.path.endswith("/responses"):  # chat-completions-only server
            return json_response({"error": {"message": "Unknown endpoint"}}, status=404)
        b = json.loads(req.content)
        if "tools" in b:
            if not tools:
                return json_response({"error": {"message": "tools are not supported by this model"}}, status=400)
            return chat_chunks("", tool=True)
        if "response_format" in b:
            if not schema:
                return json_response({"error": {"message": "response_format unsupported"}}, status=400)
            return chat_chunks('{"ok": true}')
        content = b["messages"][-1]["content"]
        if isinstance(content, list):
            if not images:
                return json_response({"error": {"message": "images unsupported"}}, status=400)
            return chat_chunks("It is red." if see_red else "A cat.")
        return chat_chunks("pong")
    return reply


def test_probe_records_what_the_endpoint_actually_does():
    a = OpenAIResponsesAdapter(endpoint="http://localhost:1/v1", provider_name="local", dialect="chat",
                               transport=Recorder(probing_handler()).transport)
    rep = a.probe_capabilities("m")
    assert rep.state == "ok" and rep.capabilities == {"streaming": "supported", "usage_in_stream": "supported", "tools": "supported",
                                                      "json_schema": "supported", "images": "supported", "discovery": "untested"}
    assert rep.usage.input_tokens > 0 and rep.usage.known


def test_probe_does_not_assume_full_compatibility():
    a = OpenAIResponsesAdapter(endpoint="http://localhost:1/v1", provider_name="local", dialect="chat",
                               transport=Recorder(probing_handler(tools=False, schema=False, see_red=False)).transport)
    rep = a.probe_capabilities("m")
    assert rep.state == "ok"
    assert rep.capabilities["tools"] == "rejected" and rep.capabilities["json_schema"] == "rejected"
    assert rep.capabilities["images"] == "accepted_unverified" and rep.capabilities["streaming"] == "supported"
    assert "tools" in rep.detail


def test_probe_state_mapping_for_auth_unreachable_limited():
    def mk(reply):
        return OpenAIResponsesAdapter(endpoint="http://localhost:1/v1", provider_name="local", dialect="chat", transport=Recorder(reply).transport)
    assert mk(lambda r: json_response({"error": {"message": "bad key"}}, status=401)).probe_capabilities("m").state == "auth_failed"
    assert mk(lambda r: httpx.ConnectError("refused", request=r)).probe_capabilities("m").state == "unreachable"
    assert mk(lambda r: json_response({"error": {"message": "x"}}, status=429)).probe_capabilities("m").state == "limited"
    assert mk(lambda r: json_response({"error": {"message": "no such model"}}, status=404)).probe_capabilities("m").state == "error"


def test_probe_without_usage_reports_not_reported():
    def reply(req):
        return chat_chunks("pong", usage=False)
    rep = OpenAIResponsesAdapter(endpoint="http://localhost:1/v1", provider_name="local", dialect="chat", transport=Recorder(reply).transport) \
        .probe_capabilities("m", test=("basic",))
    assert rep.capabilities["usage_in_stream"] == "not_reported" and rep.usage.known is False


# =============================================================================================== mocks
def test_mock_provider_scripts_and_records():
    m = MockProvider(["first", RateLimit("slow"), lambda req: f"echo:{req.model}"], models=["a", "b"])
    assert m.complete(Request(model="x", messages=user())).text == "first"
    with pytest.raises(RateLimit):
        m.complete(Request(model="x", messages=user()))
    assert m.complete(Request(model="y", messages=user())).text == "echo:y"
    assert m.complete(Request(model="z", messages=user())).text == "mock-ok" and len(m.calls) == 4
    assert [x.id for x in m.discover_models().models] == ["a", "b"]
    assert MockProvider(discovery=False).discover_models().supported is False


def test_raising_provider_raises_on_every_entry_point():
    r = RaisingProvider()
    with pytest.raises(AICallAttempted):
        r.complete(Request(model="m", messages=user()))
    with pytest.raises(AICallAttempted):
        r.discover_models()
    with pytest.raises(AICallAttempted):
        r.probe_capabilities("m")
    assert r.attempts == 3 and issubclass(AICallAttempted, AssertionError)


# =============================================================================================== subscription handoff
def test_handoff_modes_are_capability_gated_and_dated():
    modes = {m.key: m for m in sub.all_modes()}
    assert set(modes) == {"openai_siwc", "claude_agent_sdk", "gemini_cli"}
    assert modes["claude_agent_sdk"].supported is False and "not allow" in modes["claude_agent_sdk"].access_method
    for m in modes.values():
        assert m.checked_on == "2026-10-06" and m.limits and m.access_method and m.sources
        assert m.cli in ("codex", "claude", "gemini")
    assert modes["openai_siwc"].verification_gaps and modes["gemini_cli"].verification_gaps
    d = modes["openai_siwc"].to_dict()
    assert d["supported"] is True and d["argv"] == ["exec", "-"]
    with pytest.raises(KeyError):
        sub.get_mode("nope")


class FakeRunner:
    def __init__(self, rc=0, out="done", err="", timed_out=False):
        self.rc, self.out, self.err, self.timed_out, self.calls = rc, out, err, timed_out, []

    def __call__(self, argv, *, input, cwd, env, timeout):
        self.calls.append({"argv": list(argv), "input": input, "cwd": cwd, "env": dict(env), "timeout": timeout})
        return self.rc, self.out, self.err, self.timed_out


def test_launch_external_passes_task_on_stdin_with_isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-leak-key-123456")
    monkeypatch.setenv("JEV_API_KEY", "jev-should-not-leak-123456")
    task = tmp_path / "task.md"
    task.write_text("rebuild the thing $(rm -rf /) `x`")
    runner = FakeRunner(out="finished")
    res = sub.launch_external("openai_siwc", task, cwd=tmp_path, runner=runner, which=lambda n: f"/usr/bin/{n}")
    assert res.launched and res.returncode == 0 and res.stdout == "finished" and not res.usage_limit
    call = runner.calls[0]
    assert call["argv"] == ["/usr/bin/codex", "exec", "-"]
    assert call["input"] == "rebuild the thing $(rm -rf /) `x`"        # task text only ever on stdin
    assert not any("rebuild" in a for a in call["argv"])
    assert "OPENAI_API_KEY" not in call["env"] and "JEV_API_KEY" not in call["env"] and "PATH" in call["env"]


def test_launch_external_refuses_when_cli_or_file_missing(tmp_path):
    task = tmp_path / "t.md"
    task.write_text("x")
    r = sub.launch_external("gemini_cli", task, runner=FakeRunner(), which=lambda n: None)
    assert not r.launched and "not found on PATH" in r.reason
    r = sub.launch_external("gemini_cli", tmp_path / "absent.md", runner=FakeRunner(), which=lambda n: "/bin/gemini")
    assert not r.launched and "task file not found" in r.reason
    assert not sub.launch_external("gemini_cli", None, runner=FakeRunner(), which=lambda n: "/bin/gemini").launched
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * (sub.MAX_TASK_BYTES + 1))
    assert "larger than" in sub.launch_external("gemini_cli", big, runner=FakeRunner(), which=lambda n: "/bin/gemini").reason


def test_launch_external_refuses_unsupported_mode_unless_vendor_approved(tmp_path):
    task = tmp_path / "t.md"
    task.write_text("x")
    runner = FakeRunner()
    r = sub.launch_external("claude_agent_sdk", task, runner=runner, which=lambda n: "/bin/claude")
    assert not r.launched and "not a supported mode" in r.reason and runner.calls == []
    ok = sub.launch_external("claude_agent_sdk", task, runner=runner, which=lambda n: "/bin/claude", vendor_approved=True)
    assert ok.launched and runner.calls[0]["argv"][:2] == ["/bin/claude", "-p"]


def test_subscription_usage_limit_surfaces_as_typed_error(tmp_path):
    task = tmp_path / "t.md"
    task.write_text("x")
    runner = FakeRunner(rc=1, err="Error: You've hit your usage limit. Try again later.")
    res = sub.launch_external("openai_siwc", task, runner=runner, which=lambda n: "/bin/codex")
    assert res.launched and res.usage_limit and res.returncode == 1
    with pytest.raises(UsageLimit) as ei:
        res.raise_for_usage_limit()
    assert not isinstance(ei.value, RateLimit)
    # a clean exit that merely mentions limits is not an error; a plain failure is not a limit
    assert not sub.launch_external("openai_siwc", task, runner=FakeRunner(rc=0, out="discussed rate limit design"), which=lambda n: "/b").usage_limit
    assert not sub.launch_external("openai_siwc", task, runner=FakeRunner(rc=2, err="segfault"), which=lambda n: "/b").usage_limit
    sub.launch_external("openai_siwc", task, runner=FakeRunner(), which=lambda n: "/b").raise_for_usage_limit()  # no-op


def test_launch_external_redacts_secrets_in_cli_output(tmp_path):
    task = tmp_path / "t.md"
    task.write_text("x")
    register_secret("printed-by-cli-secret-9999")
    res = sub.launch_external("gemini_cli", task, runner=FakeRunner(out="token printed-by-cli-secret-9999 ok"), which=lambda n: "/b")
    assert "printed-by-cli-secret-9999" not in res.stdout


@pytest.mark.skipif(os.name != "posix", reason="process groups")
def test_default_runner_runs_a_real_process_and_enforces_timeout(tmp_path):
    rc, out, err, to = sub._default_runner([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input="abc", cwd=None,
                                           env=sub.isolated_env(), timeout=20)
    assert (rc, out.strip(), to) == (0, "ABC", False)
    rc, out, err, to = sub._default_runner([sys.executable, "-c", "import time; time.sleep(30)"], input="", cwd=None,
                                           env=sub.isolated_env(), timeout=0.5)
    assert to is True and rc != 0


# =============================================================================================== opt-in live checks
@pytest.fixture
def live_opt_in(request):
    """Live tests run ONLY when explicitly selected with `-m live` (and keys are present): a stray env key never spends money."""
    if "live" not in (request.config.getoption("-m") or ""):
        pytest.skip("live tests are opt-in: run `pytest -m live` with provider keys in the environment")


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="live: set ANTHROPIC_API_KEY and run `pytest -m live`")
def test_live_anthropic_minimal_call(live_opt_in):
    a = AnthropicAdapter(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.environ.get("LIVE_ANTHROPIC_MODEL")
    if not model:
        pytest.skip("set LIVE_ANTHROPIC_MODEL explicitly (no model is guessed)")
    r = a.complete(Request(model=model, messages=user("Reply with: ok"), max_output_tokens=16))
    assert r.usage.known and r.usage.output_tokens > 0


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY") or not os.environ.get("LIVE_OPENAI_MODEL"), reason="live: set OPENAI_API_KEY and LIVE_OPENAI_MODEL")
def test_live_openai_minimal_call(live_opt_in):
    a = OpenAIResponsesAdapter(api_key=os.environ["OPENAI_API_KEY"])
    r = a.complete(Request(model=os.environ["LIVE_OPENAI_MODEL"], messages=user("Reply with: ok"), max_output_tokens=32))
    assert r.usage.known


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY") or not os.environ.get("LIVE_GEMINI_MODEL"), reason="live: set GEMINI_API_KEY and LIVE_GEMINI_MODEL")
def test_live_gemini_minimal_call(live_opt_in):
    a = GeminiAdapter(api_key=os.environ["GEMINI_API_KEY"])
    r = a.complete(Request(model=os.environ["LIVE_GEMINI_MODEL"], messages=user("Reply with: ok"), max_output_tokens=32))
    assert r.usage.known
