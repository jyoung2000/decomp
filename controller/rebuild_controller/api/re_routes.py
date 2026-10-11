"""R6 RE workbench in the app, piece 1: the Functions list of a project module.

Backed by the same workbench session and ``op_functions`` operation as the MCP ``list_functions`` tool, shaped by the same
helper (``re_workbench.shape_functions``), so the app and MCP cannot diverge. Mounted from ``api/server.py`` with one line;
error shape ``{error:{code,message,next_action}}``.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query


def _err(code: str, message: str, status: int = 400, next_action: str | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "next_action": next_action})


def mount_re_routes(app: FastAPI, studio: Any) -> None:
    from ..re_workbench import NATIVE_PROFILES, WorkbenchError, shape_functions

    r = APIRouter(tags=["re"])

    def native_module(case_id: str, module_id: str) -> dict[str, Any]:
        studio.cases.get_case(case_id)                     # 404 for an unknown case (KeyError handler)
        m = studio.cases.get_module(module_id)
        if m.get("case_id") != case_id:
            raise KeyError(module_id)
        if m.get("profile") not in NATIVE_PROFILES:
            raise _err("not_native", f"{m.get('rel_path')} is not native code (it was detected as {m.get('profile') or 'unknown'}), "
                                     "so it has no machine-code functions to list.", 409,
                       "Pick a native module (.exe, .dll, .so or Mach-O) in the module picker; managed and script code is "
                       "shown under Overview and Plan.")
        return m

    def analysed(case_id: str, module_id: str) -> bool:
        return any(studio.cases.list_evidence(case_id, kind=k, module_id=module_id) for k in ("native.functions", "native.analyze"))

    @r.get("/cases/{case_id}/modules/{module_id}/functions")
    def functions(case_id: str, module_id: str, contains: str | None = Query(default=None, max_length=120),
                  sort: Literal["addr", "size", "name"] = "addr", offset: int = Query(default=0, ge=0, le=10_000_000),
                  limit: int = Query(default=100, ge=1, le=500), analyze: bool = False):
        m = native_module(case_id, module_id)
        if contains is not None and any(ord(c) < 32 for c in contains):
            raise _err("validation", "the search text contains control characters", 400, "type plain text and retry")
        b = studio.re.backend()
        tool = b.tool() if hasattr(b, "tool") else None
        if tool is None or not getattr(tool, "version", None):
            raise _err("engine_missing", "The analysis engine (rizin) is not installed, so functions cannot be listed.", 503,
                       "Install rizin on the Tools page, then come back to this tab.")
        if not analyze and not analysed(case_id, module_id):
            raise _err("not_analyzed", f"{m.get('rel_path')} has not been analysed yet.", 409,
                       "Choose 'Analyse now' (about a minute for a small program, longer for large ones), or start the "
                       "project's analysis from Overview.")
        try:
            sess = studio.re.open(case_id=case_id, module_id=module_id)
            res, _s = studio.re.call(sess["session_id"], "functions")
        except WorkbenchError as e:
            raise _err(e.code, e.message, 400, e.next_action or None) from None
        if not res.ok:
            err = str(res.error or "")
            low = err.lower()
            if "not available" in low:
                raise _err("engine_missing", "The analysis engine (rizin) is not available.", 503, "Check rizin on the Tools page.")
            if low.startswith("timeout"):
                raise _err("timeout", "The analysis took too long and was stopped.", 504,
                           "Retry; if it keeps happening, lower the analysis depth in the project settings.")
            if "changed on disk" in low:
                raise _err("module_changed", err[:500], 409, "Run the project's analysis again so the inventory matches the file.")
            raise _err("backend_error", f"The analysis engine reported an error: {err[:500]}", 502, "See Tools > rizin, then retry.")
        page = shape_functions(list((res.data or {}).get("functions") or []), contains, sort, offset, limit)
        return {**page, "session_id": sess["session_id"], "module_id": module_id, "case_id": case_id, "sort": sort,
                "contains": contains or "", "truncated": bool(res.truncated), "cached": bool((res.data or {}).get("cached")),
                "evidence_ids": list(res.evidence_ids or [])}

    app.include_router(r)
