"""Dependency health endpoints (R10): what is installed, what projects need, one-click install of what is missing.

GET  /health/dependencies                 report (cached a few seconds; ?refresh=1, ?smoke=1 runs each tool, ?network=1 tests download servers)
GET  /health/dependencies/settings        {auto_install, asked}
PUT  /health/dependencies/settings        {auto_install} (asked once on first run; turning it on installs what is needed now)
POST /health/dependencies/install         {items?, case_id?, repair?, include_optional?} -> install queue snapshot
GET  /health/dependencies/install         install queue snapshot
POST /health/dependencies/install/cancel  cancel the running queue
POST /health/dependencies/install/retry   retry failed / cancelled / interrupted items
POST /health/services/ollama/start        start an Ollama the user already installed (never installs it)
GET  /cases/{case_id}/preflight           what this project still needs before Start

Error shape matches the rest of the API: ``{error:{code,message,affected,next_action}}``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..dependency_health import DependencyHealth
from ..tool_setup import ToolSetupError
from .tools_routes import _raise


class InstallReq(BaseModel):
    items: list[str] | None = Field(default=None, max_length=50)
    case_id: str | None = Field(default=None, max_length=200)
    repair: bool = False
    include_optional: bool = False


class SettingsPut(BaseModel):
    auto_install: bool


def build_router(dh: DependencyHealth) -> APIRouter:
    r = APIRouter(tags=["dependencies"])

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except ToolSetupError as e:
            raise _raise(e) from e

    @r.get("/health/dependencies")
    def deps(refresh: int = 0, smoke: int = 0, network: int = 0) -> dict[str, Any]:
        return guard(dh.report, refresh=bool(refresh), smoke=bool(smoke), network=bool(network))

    @r.get("/health/dependencies/settings")
    def deps_settings() -> dict[str, Any]:
        return dh.get_settings()

    @r.put("/health/dependencies/settings")
    def deps_settings_put(body: SettingsPut) -> dict[str, Any]:
        return guard(dh.put_settings, auto_install=body.auto_install)

    @r.get("/health/dependencies/install")
    def deps_queue() -> dict[str, Any]:
        return dh.queue.snapshot()

    @r.post("/health/dependencies/install")
    def deps_install(body: InstallReq | None = None) -> dict[str, Any]:
        b = body or InstallReq()
        return guard(dh.install, items=b.items, case_id=b.case_id, repair=b.repair, include_optional=b.include_optional)

    @r.post("/health/dependencies/install/cancel")
    def deps_cancel() -> dict[str, Any]:
        return guard(dh.queue.cancel)

    @r.post("/health/dependencies/install/retry")
    def deps_retry() -> dict[str, Any]:
        return guard(dh.queue.retry)

    @r.post("/health/services/ollama/start")
    def ollama_start() -> dict[str, Any]:
        return guard(dh.start_ollama)

    @r.get("/cases/{case_id}/preflight")
    def preflight(case_id: str) -> dict[str, Any]:
        try:
            return guard(dh.preflight, case_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"Project {case_id} was not found.",
                                                         "affected": case_id, "next_action": "Refresh the project list."}) from e

    return r


def mount_health_routes(app: FastAPI, studio: Any) -> DependencyHealth:
    dh = studio.dependency_health
    app.state.dependency_health = dh
    app.include_router(build_router(dh))
    return dh
