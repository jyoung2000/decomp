"""Local AI on this PC (docs/AI_LADDER.md section 8): detection, 'use detected models', model search / download / registration.

Error shape matches the rest of the API: ``{error:{code,message,affected,next_action}}`` (plus ``retryable``, ``url``).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..local_models import ModelLibrary
from ..providers.local_ai import LocalAI
from ..tool_setup import ToolSetupError


class UseDetected(BaseModel):
    apply: bool = False
    preset: str | None = Field(default=None, pattern="^(all_local|local_first)$")


class LocalSettings(BaseModel):
    models_dir: str | None = Field(default=None, max_length=4096)
    num_ctx_cap: int | None = None


class HfToken(BaseModel):
    token: str | None = Field(default=None, max_length=300)


class FolderCheck(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    need_bytes: int = 0


class DownloadReq(BaseModel):
    repo: str = Field(min_length=3, max_length=200)
    path: str = Field(min_length=1, max_length=500)
    dest_dir: str | None = Field(default=None, max_length=4096)
    accept_license: bool = False
    register_model: bool = Field(default=True, alias="register")


class PullReq(BaseModel):
    name: str = Field(min_length=1, max_length=200)


def _raise(e: ToolSetupError) -> HTTPException:
    d = e.to_dict()
    return HTTPException(status_code=e.status, detail={"code": d["code"], "message": d["message"], "affected": d["affected"],
                                                       "next_action": d["next_action"], "retryable": d["retryable"], "url": d["url"]})


def build_router(local: LocalAI, lib: ModelLibrary) -> APIRouter:
    r = APIRouter(prefix="/ai/local", tags=["local-ai"])

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except ToolSetupError as e:
            raise _raise(e) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"code": "local_ai", "message": str(e), "affected": None,
                                                         "next_action": None}) from e

    @r.get("")
    def local_snapshot(max_age: float | None = None):
        return guard(local.snapshot, max_age_s=max_age)

    @r.post("/detect")
    def local_detect():
        return guard(local.detect)

    @r.post("/use")
    def local_use(body: UseDetected):
        return guard(local.use_detected, apply=body.apply, preset_name=body.preset)

    @r.get("/settings")
    def local_settings():
        return lib.config()

    @r.put("/settings")
    def local_settings_put(body: LocalSettings):
        if body.num_ctx_cap is not None:
            guard(local.set_num_ctx_cap, int(body.num_ctx_cap))
        if body.models_dir is not None:
            guard(lib.set_models_dir, body.models_dir)
        return lib.config()

    @r.put("/hf-token")
    def local_hf_token(body: HfToken):
        return guard(lib.set_hf_token, body.token or None)

    @r.post("/check-folder")
    def local_check_folder(body: FolderCheck):
        return guard(lib.check_folder, body.path, body.need_bytes)

    @r.get("/search")
    def local_search(q: str, limit: int = 20):
        return guard(lib.search, q, limit)

    @r.get("/files")
    def local_files(repo: str):
        return guard(lib.files, repo)

    @r.post("/downloads")
    def local_download(body: DownloadReq):
        return guard(lib.start_download, body.repo, body.path, dest_dir=body.dest_dir, accept_license=body.accept_license,
                     register=body.register_model)

    @r.get("/jobs")
    def local_jobs():
        return lib.jobs()

    @r.get("/jobs/{job_id}")
    def local_job(job_id: str):
        return guard(lib.job, job_id)

    @r.post("/jobs/{job_id}/cancel")
    def local_job_cancel(job_id: str):
        return guard(lib.cancel, job_id)

    @r.get("/models")
    def local_models():
        return lib.downloads()

    @r.post("/models/{rec_id}/register")
    def local_model_register(rec_id: str):
        return guard(lib.register, rec_id)

    @r.delete("/models/{rec_id}")
    def local_model_remove(rec_id: str, unregister: int = 0):
        return guard(lib.remove, rec_id, unregister=bool(unregister))

    @r.post("/ollama/pull")
    def local_ollama_pull(body: PullReq):
        return guard(lib.start_pull, body.name)

    return r


def mount_local_ai_routes(app: FastAPI, studio: Any, *, local: LocalAI | None = None, lib: ModelLibrary | None = None) -> tuple[LocalAI, ModelLibrary]:
    data_dir = studio.settings.data_dir
    local = local or LocalAI(studio.connections, studio.events, state_path=data_dir / "local_ai.json")
    secrets = getattr(studio.connections, "secrets", None) if studio.connections is not None else None
    lib = lib or ModelLibrary(local, data_dir=data_dir, events=studio.events, secrets=secrets)
    app.state.local_ai = local
    app.state.model_library = lib
    app.include_router(build_router(local, lib))
    return local, lib
