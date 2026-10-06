"""Test doubles. ``MockProvider`` scripts responses/errors; ``RaisingProvider`` raises on ANY call and is used by the
knowledge-reuse demo to prove no AI call happens."""
from __future__ import annotations

from typing import Any, Callable, Union

from .base import (Message, ModelDiscovery, ModelInfo, OnText, ProbeReport, ProviderAdapter, ProviderError, Request,
                   Response, Usage, new_capabilities)

ScriptItem = Union[Response, ProviderError, str, Callable[[Request], Any]]


class AICallAttempted(AssertionError):
    """Raised by RaisingProvider: an AI call happened where none is allowed."""


class MockProvider(ProviderAdapter):
    provider_key = "mock"
    provider_name = "mock"

    def __init__(self, script: list[ScriptItem] | None = None, *, models: list[str] | None = None, discovery: bool = True,
                 capabilities: dict[str, Any] | None = None, provider_name: str = "mock", default_usage: Usage | None = None):
        super().__init__(endpoint="mock://")
        self.provider_name = provider_name
        self.script: list[ScriptItem] = list(script or [])
        self.calls: list[Request] = []
        self._models = list(models or [])
        self._discovery = discovery
        self._caps = capabilities
        self.default_usage = default_usage or Usage(input_tokens=10, output_tokens=5)

    def complete(self, request: Request, on_text: OnText | None = None) -> Response:
        self.calls.append(request)
        item: Any = self.script.pop(0) if self.script else "mock-ok"
        if callable(item) and not isinstance(item, (Response, ProviderError)):
            item = item(request)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            if on_text:
                on_text(item)
            return Response(provider=self.provider_name, model=request.model, text=item, tool_calls=[], stop_reason="end_turn",
                            usage=Usage(**self.default_usage.to_dict()), provider_key=self.provider_key)
        if on_text and item.text:
            on_text(item.text)
        return item

    def discover_models(self) -> ModelDiscovery:
        if not self._discovery:
            return ModelDiscovery(False, error="mock: discovery disabled")
        return ModelDiscovery(True, [ModelInfo(id=m, source="discovered") for m in self._models])

    def probe_capabilities(self, model: str, **kw: Any) -> ProbeReport:
        if self._caps is not None:
            caps = new_capabilities()
            caps.update(self._caps)
            return ProbeReport("ok", caps, Usage(), "mock capabilities")
        return super().probe_capabilities(model, **kw)


class RaisingProvider(ProviderAdapter):
    """Every method raises. Wire it behind every connection to assert an operation made zero AI calls."""
    provider_key = "raising"
    provider_name = "raising"

    def __init__(self) -> None:
        super().__init__(endpoint="raising://")
        self.attempts = 0

    def _boom(self, what: str):
        self.attempts += 1
        raise AICallAttempted(f"AI adapter used ({what}) in a context that must not call AI")

    def complete(self, request: Request, on_text: OnText | None = None) -> Response:
        self._boom("complete")

    def discover_models(self) -> ModelDiscovery:
        self._boom("discover_models")

    def probe_capabilities(self, model: str, **kw: Any) -> ProbeReport:
        self._boom("probe_capabilities")
