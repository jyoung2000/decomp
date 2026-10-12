"""R5 AI-assisted RE quality (docs/AI_LADDER.md section 10): model scoreboard, AI naming jobs and the review of model-proposed
annotations (accept / reject / confirm-by-test / roll back). Mounted from ``api/server.py`` with one line; error shape
``{error:{code,message,next_action}}``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field


class NamingReq(BaseModel):
    functions: list[str] | None = Field(default=None, max_length=50)
    limit: int = Field(default=8, ge=1, le=50)
    include_named: bool = False


class ReviewReq(BaseModel):
    kind: str = Field(pattern="^(function|global|local|comment)$")
    key: str = Field(min_length=1, max_length=200)
    local: str | None = Field(default=None, max_length=200)
    decision: str = Field(pattern="^(accept|reject|confirm)$")
    evidence: str | None = Field(default=None, max_length=300)


class RollbackReq(BaseModel):
    revision: int = Field(ge=0)          # 0 = before any annotation (undo of the first change)


class ImportReq(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)


def _err(code: str, message: str, status: int = 400, next_action: str | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "next_action": next_action})


def mount_ai_quality_routes(app: FastAPI, studio: Any) -> None:
    r = APIRouter(tags=["ai-quality"])

    def module(case_id: str, module_id: str) -> dict[str, Any]:
        studio.cases.get_case(case_id)                     # 404 for an unknown case (KeyError handler)
        m = studio.cases.get_module(module_id)
        if m.get("case_id") != case_id:
            raise KeyError(module_id)
        return m

    def backend():
        b = studio.re.backend()
        if not hasattr(b, "op_review_annotation"):
            raise _err("unavailable", "the analysis backend (rizin) is not available in this build", 503)
        return b

    def result(res):
        if not res.ok:
            msg = str(res.error)
            code = "annotation" if msg.startswith(("invalid target", "refused")) else "backend"
            raise _err(code, msg, 400 if code == "annotation" else 502,
                       "check the address / name in the list of proposals" if code == "annotation" else "see Tools > rizin")
        return {**res.data, "evidence_ids": res.evidence_ids}

    # ---------------------------------------------------------------- scoreboard
    @r.get("/ai/scoreboard")
    def scoreboard_view(limit: int = 50):
        from .. import scoreboard
        return {**scoreboard.summary(studio.db), "recent": scoreboard.rows(studio.db, limit=max(1, min(limit, 500)))}

    @r.post("/ai/scoreboard/import")
    def scoreboard_import(body: ImportReq):
        from .. import scoreboard
        return scoreboard.import_rows(studio.db, body.rows)

    # ---------------------------------------------------------------- annotations (model-proposed review)
    @r.get("/cases/{case_id}/modules/{module_id}/annotations")
    def annotations_view(case_id: str, module_id: str, history: int = 20):
        module(case_id, module_id)
        return result(backend().op_annotations(studio, case_id=case_id, module_id=module_id, history=max(0, min(history, 200))))

    @r.post("/cases/{case_id}/modules/{module_id}/annotations/review")
    def annotations_review(case_id: str, module_id: str, body: ReviewReq):
        module(case_id, module_id)
        author = "test" if body.decision == "confirm" else "user"
        return result(backend().op_review_annotation(studio, case_id=case_id, module_id=module_id, kind=body.kind, key=body.key,
                                                     decision=body.decision, local=body.local, evidence=body.evidence, author=author))

    @r.post("/cases/{case_id}/modules/{module_id}/annotations/rollback")
    def annotations_rollback(case_id: str, module_id: str, body: RollbackReq):
        module(case_id, module_id)
        return result(backend().op_rollback_annotations(studio, case_id=case_id, module_id=module_id, revision=body.revision))

    # ---------------------------------------------------------------- AI naming job
    @r.post("/cases/{case_id}/modules/{module_id}/ai/naming")
    def ai_naming(case_id: str, module_id: str, body: NamingReq):
        m = module(case_id, module_id)
        case = studio.cases.get_case(case_id)
        from ..implement import LoopPolicy, route_status
        rs = route_status(studio, LoopPolicy.from_case(case), "naming")
        if not rs["ok"]:
            raise _err(rs["code"] or "no_route", rs["message"], 409,
                       "set the project's AI policy and a 'Naming functions' ladder in Connections")
        j = studio.jobs.create(case_id, "ai_naming", f"AI naming in {m.get('rel_path')}",
                               {"module_id": module_id, "functions": body.functions, "limit": body.limit, "include_named": body.include_named},
                               max_attempts=1)
        return {"job": j.to_dict(), "route": rs["route"],
                "note": "Every name and comment is written as model-proposed; accept or reject each one under Annotations."}

    app.include_router(r)
