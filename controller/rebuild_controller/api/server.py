"""Loopback HTTP + WebSocket API for the desktop UI (contract: docs/API.md).

Security: per-launch bearer token written to <data_dir>/controller.json (0600); Origin allow-list; loopback bind only.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..ids import now_iso
from ..services import StudioServices

ALLOWED_ORIGIN_PREFIXES = ("http://localhost", "http://127.0.0.1", "tauri://localhost", "https://tauri.localhost", "http://tauri.localhost")


def _err(code: str, message: str, status: int = 400, affected: str | None = None, next_action: str | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "affected": affected, "next_action": next_action})


class CaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_root: str
    output_root: str
    target_language: str = Field(pattern="^(rust|rust_bevy|web|auto)$")
    output_type: str = Field(pattern="^(exe|installer|portable|web|pwa)$")
    ai_policy: dict[str, Any] = Field(default_factory=lambda: {"mode": "no_ai"})
    launch_profile: dict[str, Any] = Field(default_factory=lambda: {"execute_original": False})
    settings: dict[str, Any] = Field(default_factory=dict)


class FeedbackCreate(BaseModel):
    target_kind: str
    target_id: str
    candidate_id: str | None = None
    classification: str = Field(pattern="^(bug|change|question|acceptance)$")
    priority: str = Field(pattern="^(low|medium|high|critical)$")
    comment: str = Field(min_length=1, max_length=20000)
    expected: str = ""
    actual: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class Triage(BaseModel):
    status: str = "triaged"
    note: str = ""
    create_work: bool = False


class ConnectionCreate(BaseModel):
    provider: str
    label: str
    endpoint: str = ""
    auth_mode: str = "api_key"
    api_key: str | None = None
    models: list[str] = Field(default_factory=list)


class RouteSet(BaseModel):
    primary_connection: str
    primary_model: str
    fallbacks: list[dict[str, str]] = Field(default_factory=list)
    allow_unlisted: bool = False


def create_app(studio: StudioServices, token: str) -> FastAPI:
    app = FastAPI(title="Rebuild Studio controller", version=__version__, docs_url=None, redoc_url=None)
    started_at = now_iso()
    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}
    ws_clients: set[asyncio.Queue] = set()

    def fanout(ev: dict[str, Any]) -> None:
        loop = loop_holder.get("loop")
        if not loop:
            return
        for q in list(ws_clients):
            loop.call_soon_threadsafe(q.put_nowait, ev)
    studio.events.subscribe(fanout)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and not origin.startswith(ALLOWED_ORIGIN_PREFIXES):
            return JSONResponse({"error": {"code": "origin", "message": "origin not allowed"}}, status_code=403)
        if request.url.path not in ("/health",):
            hdr = request.headers.get("authorization", "")
            if not secrets.compare_digest(hdr, f"Bearer {token}"):
                return JSONResponse({"error": {"code": "auth", "message": "missing or invalid token"}}, status_code=401)
        return await call_next(request)

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        d = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
        return JSONResponse({"error": d}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def any_exc(request: Request, exc: Exception):
        name = type(exc).__name__
        code = {KeyError: 404, ValueError: 400, PermissionError: 403}.get(type(exc), 500)
        next_action = None
        if name == "ApprovalRequired":
            code, next_action = 409, "approve unknown pricing for this model or set an explicit price on the connection"
        elif name in ("BudgetExhausted", "BudgetRequired", "DuplicateReservation"):
            code, next_action = 402 if name == "BudgetExhausted" else 400, "raise or configure the per-job budget"
        elif name in ("NoRoute", "AllCandidatesFailed"):
            code, next_action = 424, "configure a connection and route for this task"
        elif name == "HermesBridgeError":
            return JSONResponse({"error": exc.to_error()}, status_code=400)
        elif name == "PathPolicyError":
            code = 400
        return JSONResponse({"error": {"code": name, "message": str(exc)[:2000], "next_action": next_action}}, status_code=code)

    @app.on_event("startup")
    async def _startup():
        loop_holder["loop"] = asyncio.get_running_loop()

    # ---------------------------------------------------------------- health / events
    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__, "pid": os.getpid(), "heartbeat_seconds": studio.settings.limits.lease_timeout_seconds,
                "latest_seq": studio.events.latest_seq(), "started_at": started_at, "optional_errors": getattr(studio, "optional_errors", {})}

    @app.get("/events")
    def events(since: int = 0, case_id: str | None = None):
        return studio.events.events_since(since, case_id=case_id)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, token_q: str = Query(default="", alias="token"), since: int = 0):
        origin = websocket.headers.get("origin")
        if (origin and not origin.startswith(ALLOWED_ORIGIN_PREFIXES)) or not secrets.compare_digest(token_q, token):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        q: asyncio.Queue = asyncio.Queue()
        ws_clients.add(q)
        try:
            last = since
            for ev in studio.events.events_since(since):
                await websocket.send_text(json.dumps(ev, default=str)); last = ev["seq"]
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=studio.settings.limits.lease_timeout_seconds)
                except asyncio.TimeoutError:
                    await websocket.send_text(json.dumps({"seq": last, "ts": now_iso(), "kind": "controller.heartbeat", "payload": {"idle": True}, "case_id": None, "job_id": None}))
                    continue
                if ev["seq"] <= last:
                    continue
                last = ev["seq"]
                await websocket.send_text(json.dumps(ev, default=str))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            ws_clients.discard(q)

    # ---------------------------------------------------------------- doctor / cases
    @app.get("/doctor")
    def doctor(smoke: int = 0):
        return studio.doctor(smoke=bool(smoke))

    @app.get("/cases")
    def cases():
        return studio.cases.list_cases()

    @app.post("/cases")
    def create_case(body: CaseCreate):
        try:
            return studio.create_case(**body.model_dump())
        except Exception as e:
            raise _err("case", str(e), 400, affected="project folders", next_action="choose non-overlapping, existing source and a writable output folder")

    @app.get("/cases/{case_id}")
    def get_case(case_id: str):
        c = studio.cases.get_case(case_id)
        c["counts"] = {"jobs": studio.jobs.counts(case_id), "features": studio.ledger.summary(case_id), "candidates": len(studio.candidates.list(case_id)),
                       "previews": len(studio.previews.list(case_id)), "open_feedback": sum(1 for f in studio.feedback.list(case_id) if f["status"] not in ("resolved",))}
        c["stalled_jobs"] = [j.job_id for j in studio.jobs.stalled() if j.case_id == case_id]
        return c

    @app.post("/cases/{case_id}/start")
    def start(case_id: str):
        return studio.start_rebuild(case_id)

    @app.post("/cases/{case_id}/pause")
    def pause(case_id: str):
        from ..jobs import JobState
        n = 0
        for j in studio.jobs.list(case_id, [JobState.QUEUED]):
            studio.db.update("jobs", "job_id", j.job_id, {"state": "blocked", "blocker": "paused by user"}); n += 1
        studio.cases.set_case_status(case_id, "paused")
        return {"paused": n}

    @app.post("/cases/{case_id}/resume")
    def resume(case_id: str):
        from ..jobs import JobState
        for j in studio.jobs.list(case_id, [JobState.BLOCKED]):
            if j.blocker == "paused by user":
                studio.jobs.resume(j.job_id)
        return {"resumed": studio.resume(case_id=case_id)}

    @app.post("/cases/{case_id}/cancel")
    def cancel(case_id: str):
        return {"cancelled": studio.cancel(case_id=case_id)}

    @app.post("/cases/{case_id}/deliver")
    def deliver(case_id: str, body: dict[str, Any] | None = None):
        return studio.deliver(case_id, (body or {}).get("candidate_id"))

    @app.get("/cases/{case_id}/jobs")
    def jobs(case_id: str):
        return [j.to_dict() for j in studio.jobs.list(case_id)]

    @app.post("/jobs/{job_id}/cancel")
    def job_cancel(job_id: str):
        return {"cancelled": studio.cancel(job_id=job_id)}

    @app.post("/jobs/{job_id}/resume")
    def job_resume(job_id: str):
        return studio.jobs.resume(job_id).to_dict()

    @app.get("/cases/{case_id}/modules")
    def modules(case_id: str):
        return studio.cases.modules(case_id)

    @app.get("/cases/{case_id}/evidence")
    def evidence(case_id: str, kind: str | None = None, module_id: str | None = None):
        return studio.cases.list_evidence(case_id, kind=kind, module_id=module_id)

    @app.get("/cases/{case_id}/evidence/search")
    def evidence_search(case_id: str, q: str, kinds: str | None = None, limit: int = 50):
        return studio.search_evidence(case_id, q, kinds.split(",") if kinds else None, min(limit, 200))

    @app.get("/evidence/{evidence_id}")
    def evidence_get(evidence_id: str, max_bytes: int | None = None):
        cap = 16 * 1024 * 1024
        return studio.get_evidence(evidence_id, min(max_bytes or studio.settings.limits.max_context_bytes, cap))

    @app.get("/cases/{case_id}/features")
    def features(case_id: str):
        return studio.ledger.list(case_id)

    # ---------------------------------------------------------------- plan
    @app.get("/cases/{case_id}/plan")
    def plan(case_id: str):
        studio.plan.refresh_statuses(case_id)
        items = studio.plan.items(case_id)
        prog = studio.plan.progress(case_id)
        return {"revision": studio.plan.current_revision(case_id), "items": items, "progress": prog, "eta": prog["eta"],
                "unknown_scope": [i for i in items if i["kind"] in ("discovery", "deferred", "unsupported")]}

    @app.get("/cases/{case_id}/plan/revisions")
    def plan_revisions(case_id: str):
        return studio.plan.revisions(case_id)

    @app.get("/cases/{case_id}/plan/revisions/{rev}")
    def plan_revision(case_id: str, rev: int):
        return studio.plan.revision_snapshot(case_id, rev)

    @app.post("/cases/{case_id}/plan/prioritize")
    def plan_prioritize(case_id: str, body: dict[str, str]):
        return studio.plan.prioritize(case_id, body["item_id"])

    @app.post("/cases/{case_id}/plan/change")
    def plan_change(case_id: str, body: dict[str, str]):
        return studio.plan.request_change(case_id, body["item_id"], body.get("request", ""), body.get("reason", "user request"))

    @app.get("/cases/{case_id}/plan/export")
    def plan_export(case_id: str):
        from ..export.report import export_plan
        case = studio.cases.get_case(case_id)
        return export_plan(studio, case_id, Path(case["output_root"]) / "reports")

    # ---------------------------------------------------------------- candidates / comparisons / previews
    @app.get("/cases/{case_id}/candidates")
    def candidates(case_id: str):
        return studio.candidates.list(case_id)

    @app.get("/candidates/{candidate_id}")
    def candidate(candidate_id: str):
        c = studio.candidates.get(candidate_id)
        c["manifest"] = studio.candidates.manifest(candidate_id)
        return c

    @app.get("/cases/{case_id}/comparisons")
    def comparisons(case_id: str, candidate_id: str | None = None, feature_id: str | None = None):
        return studio.verifier.comparisons(case_id, candidate_id, feature_id)

    @app.get("/cases/{case_id}/previews")
    def previews(case_id: str):
        return studio.previews.list(case_id)

    @app.post("/previews/{preview_id}/open")
    def preview_open(preview_id: str):
        return studio.previews.open(preview_id)

    @app.post("/previews/{preview_id}/stop")
    def preview_stop(preview_id: str, body: dict[str, str] | None = None):
        p = studio.previews.get(preview_id)
        ids = [body["instance_id"]] if body and body.get("instance_id") else p["running"]
        return {"stopped": [i for i in ids if studio.previews.stop(i)]}

    # ---------------------------------------------------------------- feedback
    @app.get("/cases/{case_id}/feedback")
    def feedback_list(case_id: str):
        return studio.feedback.list(case_id)

    @app.post("/cases/{case_id}/feedback")
    def feedback_create(case_id: str, body: FeedbackCreate):
        return studio.feedback.create(case_id, **body.model_dump())

    @app.get("/feedback/{feedback_id}")
    def feedback_get(feedback_id: str):
        return studio.feedback.get(feedback_id)

    @app.post("/feedback/{feedback_id}/triage")
    def feedback_triage(feedback_id: str, body: Triage):
        return studio.feedback.triage(feedback_id, status=body.status, note=body.note, create_work=body.create_work)

    @app.post("/feedback/{feedback_id}/reopen")
    def feedback_reopen(feedback_id: str, body: dict[str, str] | None = None):
        return studio.feedback.reopen(feedback_id, (body or {}).get("note", ""))

    @app.post("/features/{feature_id}/review")
    def feature_review(feature_id: str, body: dict[str, Any]):
        return studio.ledger.set_user_review(feature_id, body.get("review"))

    # ---------------------------------------------------------------- connections / routes / budgets / ai
    def _conn():
        if studio.connections is None:
            raise _err("unavailable", "connections service unavailable: " + studio.optional_errors.get("connections", ""), 503)
        return studio.connections

    @app.get("/connections")
    def connections():
        return _conn().list()

    @app.post("/connections")
    def connection_create(body: ConnectionCreate):
        return _conn().create(**body.model_dump())

    @app.post("/connections/{connection_id}/probe")
    def connection_probe(connection_id: str, body: dict[str, Any] | None = None):
        body = body or {}
        return _conn().probe(connection_id, model=body.get("model"), approve_unknown_pricing=bool(body.get("approve_unknown_pricing")))

    @app.delete("/connections/{connection_id}")
    def connection_delete(connection_id: str):
        _conn().delete(connection_id)
        return {"deleted": connection_id}

    @app.get("/routes")
    def routes():
        return _conn().get_routes()

    @app.put("/routes/{task}")
    def route_set(task: str, body: RouteSet):
        return _conn().set_route(task, body.primary_connection, body.primary_model, body.fallbacks, allow_unlisted=body.allow_unlisted)

    @app.get("/budgets")
    def budgets():
        if studio.budgets is None:
            raise _err("unavailable", "budget service unavailable", 503)
        return studio.budgets.snapshot()

    @app.get("/ai/calls")
    def ai_calls(case_id: str | None = None):
        sql, params = "SELECT * FROM ai_calls", ()
        if case_id:
            sql += " WHERE case_id=?"; params = (case_id,)
        return studio.db.query(sql + " ORDER BY created_at DESC LIMIT 500", params)

    @app.get("/subscriptions")
    def subscriptions():
        try:
            from ..providers.subscription import all_modes, cli_available
            return [{**m.__dict__, "cli_available": cli_available(m)} for m in all_modes()]
        except Exception as e:
            return {"error": str(e)}

    # ---------------------------------------------------------------- knowledge
    @app.get("/knowledge")
    def knowledge(kind: str | None = None, state: str | None = None):
        return studio.knowledge.list(kind=kind, state=state)

    @app.get("/knowledge/{knowledge_id}")
    def knowledge_get(knowledge_id: str):
        k = studio.knowledge.get(knowledge_id); k["body"] = studio.knowledge.body(knowledge_id); return k

    @app.post("/knowledge/{knowledge_id}/validate")
    def knowledge_validate(knowledge_id: str):
        return studio.knowledge.validate(knowledge_id)

    @app.post("/knowledge/{knowledge_id}/rollback")
    def knowledge_rollback(knowledge_id: str):
        return studio.knowledge.rollback(knowledge_id) or {"rolled_back": knowledge_id, "restored": None}

    # ---------------------------------------------------------------- settings / hermes
    @app.get("/settings")
    def settings_get():
        s = studio.settings
        return {"data_dir": str(s.data_dir), "tools_dir": str(s.tools_dir), "limits": s.limits.__dict__, "previews_running": studio.previews.running()}

    @app.put("/settings")
    def settings_put(body: dict[str, Any]):
        lim = body.get("limits", {})
        for k, v in lim.items():
            if hasattr(studio.settings.limits, k) and isinstance(v, (int, float)):
                setattr(studio.settings.limits, k, type(getattr(studio.settings.limits, k))(v))
        return settings_get()

    @app.get("/hermes/status")
    def hermes_status():
        try:
            from ..hermes.bridge import HermesBridge
            return HermesBridge(studio.settings.data_dir).status()
        except Exception as e:
            return {"available": False, "error": f"{type(e).__name__}: {e}"}

    @app.post("/hermes/pair")
    def hermes_pair(body: dict[str, Any] | None = None):
        from ..hermes.bridge import HermesBridge
        return HermesBridge(studio.settings.data_dir).pair((body or {}).get("profile_path"))

    @app.post("/hermes/register_mcp")
    def hermes_register(body: dict[str, Any] | None = None):
        from ..hermes.bridge import HermesBridge
        return HermesBridge(studio.settings.data_dir).register_mcp(dry_run=bool((body or {}).get("dry_run", True)))

    return app


def write_controller_info(data_dir: Path, port: int, token: str) -> Path:
    p = data_dir / "controller.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"port": port, "token": token, "pid": os.getpid(), "started_at": now_iso()}))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)
    return p


def serve(port: int = 0, data_dir: str | None = None, token: str | None = None, *, host: str = "127.0.0.1") -> None:
    import socket
    import uvicorn
    from ..config import Settings, set_settings
    settings = Settings(data_dir=Path(data_dir)) if data_dir else Settings()
    set_settings(settings)
    settings.ensure_dirs()
    token = token or os.environ.get("REBUILD_STUDIO_TOKEN") or secrets.token_urlsafe(32)
    if port == 0:
        with socket.socket() as s:
            s.bind((host, 0)); port = s.getsockname()[1]
    studio = StudioServices(settings, start_runner=True)
    app = create_app(studio, token)
    write_controller_info(settings.data_dir, port, token)
    print(json.dumps({"port": port, "controller_json": str(settings.data_dir / "controller.json")}), flush=True)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning", ws_ping_interval=10, ws_ping_timeout=20)
    finally:
        studio.stop()
