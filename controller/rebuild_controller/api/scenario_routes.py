"""User-declared scenario endpoints (command-line programs).

  GET    /cases/{id}/scenarios            list + per-scenario status + counts + consent summary
  POST   /cases/{id}/scenarios            create
  GET    /cases/{id}/scenarios/{sid}      one scenario
  PUT    /cases/{id}/scenarios/{sid}      replace the definition (a recorded baseline is NOT edited; status becomes "changed")
  DELETE /cases/{id}/scenarios/{sid}
  POST   /cases/{id}/scenarios/record     run the ORIGINAL (needs recorded consent, else 409) and freeze a NEW baseline revision

Errors use the standard ``{error:{code,message,affected,next_action}}`` body.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..user_scenarios import ScenarioError, UserScenarioStore, clean_definition, record, scenario_statuses


class StepBody(BaseModel):
    args: list[str] = Field(default_factory=list)
    stdin: str = ""


class ScenarioBody(BaseModel):
    title: str = ""
    feature_id: str | None = None
    steps: list[StepBody] = Field(default_factory=list)
    compare: dict[str, bool] = Field(default_factory=dict)       # exit_code | output | files
    normalize: dict[str, bool] = Field(default_factory=dict)     # line_endings | trailing_spaces | trim | ignore_timestamps
    timeout: float | None = None


class RecordBody(BaseModel):
    scenario_ids: list[str] | None = None
    program: str | None = Field(default=None, max_length=1024)


def _http(e: ScenarioError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"code": e.code, "message": str(e), "affected": e.affected, "next_action": e.next_action})


def mount_scenario_routes(app: FastAPI, studio: Any) -> None:
    store = UserScenarioStore(studio.db)

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except ScenarioError as e:
            raise _http(e) from e

    def definition(case_id: str, body: ScenarioBody) -> dict[str, Any]:
        d = clean_definition(body.model_dump())
        if d["feature_id"]:
            try:
                f = studio.ledger.get(d["feature_id"])
                if f["case_id"] != case_id:
                    raise KeyError(d["feature_id"])
            except KeyError:
                raise ScenarioError("scenario_feature", f"The feature {d['feature_id']!r} does not exist in this project.", affected="feature_id",
                                    next_action="Pick one of the project's features, or leave it empty.") from None
        return d

    def view(case_id: str) -> dict[str, Any]:
        out = scenario_statuses(studio, case_id, store.list(case_id))
        out["consent"] = studio.cases.original_execution_consent(case_id)
        return out

    @app.get("/cases/{case_id}/scenarios")
    def scenarios_list(case_id: str):
        studio.cases.get_case(case_id)
        return view(case_id)

    @app.post("/cases/{case_id}/scenarios")
    def scenarios_create(case_id: str, body: ScenarioBody):
        studio.cases.get_case(case_id)
        return guard(lambda: store.create(case_id, definition(case_id, body)))

    @app.post("/cases/{case_id}/scenarios/record")
    def scenarios_record(case_id: str, body: RecordBody | None = None):
        b = body or RecordBody()
        studio.cases.get_case(case_id)
        res = guard(record, studio, case_id, store, scenario_ids=b.scenario_ids, program=b.program)
        return {**res, **view(case_id)}

    @app.get("/cases/{case_id}/scenarios/{scenario_id}")
    def scenarios_get(case_id: str, scenario_id: str):
        studio.cases.get_case(case_id)
        guard(store.get, case_id, scenario_id)
        return next(s for s in view(case_id)["scenarios"] if s["scenario_id"] == scenario_id)

    @app.put("/cases/{case_id}/scenarios/{scenario_id}")
    def scenarios_put(case_id: str, scenario_id: str, body: ScenarioBody):
        studio.cases.get_case(case_id)
        guard(store.get, case_id, scenario_id)
        guard(lambda: store.update(case_id, scenario_id, definition(case_id, body)))
        return next(s for s in view(case_id)["scenarios"] if s["scenario_id"] == scenario_id)

    @app.delete("/cases/{case_id}/scenarios/{scenario_id}")
    def scenarios_delete(case_id: str, scenario_id: str):
        studio.cases.get_case(case_id)
        guard(store.delete, case_id, scenario_id)
        return {"deleted": scenario_id}
