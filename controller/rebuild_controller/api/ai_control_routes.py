"""R9 granular AI control (docs/AI_LADDER.md section 9): JeV advisor card, provider cooldowns, per-rung failure rules metadata and
the dry-run "test route" endpoint. Mounted from ``api/server.py`` with one line; error shape ``{error:{code,message,next_action}}``.

Rules themselves travel with the ladder (``PUT /ai/ladder/{task}`` entries carry ``rules``); this module adds no second store.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field


class JevSettings(BaseModel):
    enabled: bool | None = None
    monthly_cap_usd: float | None = None


class JevKey(BaseModel):
    key: str | None = Field(default=None, max_length=512)


class CooldownSettings(BaseModel):
    cooldown_minutes: dict[str, float] = Field(default_factory=dict)


class RouteTest(BaseModel):
    task: str = Field(min_length=1, max_length=64)
    case_id: str | None = Field(default=None, max_length=200)
    needs: list[str] = Field(default_factory=list)
    est_input_tokens: int = Field(default=8000, ge=0, le=10_000_000)
    max_output_tokens: int = Field(default=4096, ge=1, le=1_000_000)


def _err(code: str, message: str, status: int = 400, next_action: str | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "next_action": next_action})


def mount_ai_control_routes(app: FastAPI, studio: Any) -> None:
    r = APIRouter(prefix="/ai", tags=["ai-control"])
    adv = getattr(getattr(studio, "ai", None), "advisor", None)
    if adv is not None and hasattr(adv, "attach_secrets") and getattr(studio, "connections", None) is not None:
        adv.attach_secrets(studio.connections.secrets)          # one credential store for the whole app

    def conn():
        if studio.connections is None:
            raise _err("unavailable", "connection store unavailable", 503)
        return studio.connections

    def jev():
        a = getattr(getattr(studio, "ai", None), "advisor", None)
        if a is None or not hasattr(a, "status"):
            raise _err("unavailable", "the JeV advisor is not available in this build", 503)
        return a

    # ---------------------------------------------------------------- JeV advisor card
    @r.get("/jev")
    def jev_status():
        return jev().status()

    @r.put("/jev")
    def jev_put(body: JevSettings):
        try:
            return jev().update_settings(enabled=body.enabled, monthly_cap_usd=body.monthly_cap_usd)
        except ValueError as e:
            raise _err("jev_settings", str(e), 400, "use a monthly cap between $0 and $50") from None

    @r.put("/jev/key")
    def jev_key(body: JevKey):
        try:
            return jev().set_key(body.key)
        except ValueError as e:
            raise _err("jev_key", str(e), 400, "paste the key exactly as TypeSafe shows it") from None

    @r.post("/jev/key/import")
    def jev_key_import():
        from ..providers.jev import JeVError
        try:
            return jev().import_install_key()
        except JeVError as e:
            raise _err(f"jev_key_{e.code}", str(e), 404 if e.code == "not_found" else 400,
                       "enter the key in the JeV advisor card instead") from None

    @r.post("/jev/test")
    def jev_test():
        return jev().setup()

    # ---------------------------------------------------------------- provider cooldowns
    @r.get("/cooldowns")
    def cooldowns():
        c = conn()
        return {"settings": {"cooldown_minutes": c.cooldown_minutes()}, "cooldowns": c.cooldowns()}

    @r.delete("/cooldowns/{connection_id}")
    def cooldown_clear(connection_id: str):
        from ..providers.connections import ConnectionStoreError
        try:
            cleared = conn().clear_cooldown(connection_id)
        except ConnectionStoreError as e:
            raise _err("not_found", str(e), 404) from None
        return {"connection_id": connection_id, "cleared": cleared, "cooldowns": conn().cooldowns()}

    @r.put("/cooldowns/settings")
    def cooldown_settings(body: CooldownSettings):
        from ..providers.connections import DEFAULT_COOLDOWN_MINUTES
        cur = conn().cooldown_minutes()
        for k, v in body.cooldown_minutes.items():
            if k not in DEFAULT_COOLDOWN_MINUTES:
                raise _err("cooldown", f"unknown cooldown kind {k!r}; expected one of {tuple(DEFAULT_COOLDOWN_MINUTES)}")
            if not (0 <= float(v) <= 7 * 24 * 60):
                raise _err("cooldown", f"{k} cooldown must be between 0 and {7 * 24 * 60} minutes (0 = off)")
            cur[k] = float(v)
        conn().put_setting("cooldown_minutes", cur)
        return {"settings": {"cooldown_minutes": conn().cooldown_minutes()}, "cooldowns": conn().cooldowns()}

    # ---------------------------------------------------------------- rules metadata + dry run
    @r.get("/rules")
    def rules_meta():
        from ..providers.ladder import rules_meta as meta
        return meta()

    @r.post("/route/test")
    def route_test(body: RouteTest):
        from ..providers.connections import TASKS, normalize_task
        if studio.ai is None:
            raise _err("unavailable", "the AI client is not available in this build", 503)
        task = normalize_task(body.task)
        if task not in TASKS:
            raise _err("ladder", f"unknown task {body.task!r}; expected one of {TASKS}")
        budget = None
        if body.case_id:
            studio.cases.get_case(body.case_id)            # 404 for an unknown case
            budget = f"case:{body.case_id}"
        return studio.ai.dry_run(task, case_id=body.case_id, needs=body.needs, est_input_tokens=body.est_input_tokens,
                                 max_output_tokens=body.max_output_tokens, budget=budget)

    app.include_router(r)
