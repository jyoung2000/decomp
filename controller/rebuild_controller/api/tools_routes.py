"""Guided tool setup endpoints (docs/API.md "Tools"): list, install, install-from-file, cancel, remove.

Error shape matches the rest of the API: ``{error:{code,message,affected,next_action}}``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..tool_setup import ToolSetup, ToolSetupError


class InstallFromFile(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


def _raise(e: ToolSetupError) -> HTTPException:
    d = e.to_dict()
    return HTTPException(status_code=e.status, detail={"code": d["code"], "message": d["message"], "affected": d["affected"],
                                                       "next_action": d["next_action"], "retryable": d["retryable"], "url": d["url"]})


def build_router(setup: ToolSetup) -> APIRouter:
    r = APIRouter(prefix="/tools/setup", tags=["tools"])

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except ToolSetupError as e:
            raise _raise(e) from e

    @r.get("")
    def tools_setup() -> dict[str, Any]:
        return guard(setup.snapshot)

    @r.post("/{name}/install")
    def tools_install(name: str):
        return guard(setup.install, name)

    @r.post("/{name}/install-from-file")
    def tools_install_from_file(name: str, body: InstallFromFile):
        return guard(setup.install_from_file, name, body.path)

    @r.post("/{name}/cancel")
    def tools_cancel(name: str):
        return guard(setup.cancel, name)

    @r.delete("/{name}")
    def tools_remove(name: str):
        return guard(setup.remove, name)

    return r


def mount_tools_routes(app: FastAPI, studio: Any) -> ToolSetup:
    setup = getattr(studio, "tool_setup", None) or ToolSetup(studio.settings, studio.events)   # one installer for the whole app
    app.state.tool_setup = setup
    app.include_router(build_router(setup))
    return setup
