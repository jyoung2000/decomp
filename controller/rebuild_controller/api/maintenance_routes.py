"""Data-safety endpoints (R11).

GET  /maintenance/backups   kept backups (newest first), where they live, the rotation policy and the last recovery
POST /maintenance/backup    take a verified backup now -> the new backup (409 when the copy fails its integrity check)

Error shape matches the rest of the API: ``{error:{code,message,affected,next_action}}``.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException

from ..store import safety


def mount_maintenance_routes(app: FastAPI, studio: Any) -> None:
    data_dir = studio.settings.data_dir

    def _last_recovery() -> dict[str, Any] | None:
        try:
            lines = (data_dir / "recovery-log.jsonl").read_text("utf-8").strip().splitlines()
            return json.loads(lines[-1]) if lines else None
        except (OSError, ValueError, IndexError):
            return None

    @app.get("/maintenance/backups")
    def backups() -> dict[str, Any]:
        rows = safety.list_backups(data_dir)
        return {"backups": [{k: b[k] for k in ("name", "taken_at", "reason", "projects", "size", "integrity")} for b in rows],
                "last_backup": rows[0]["taken_at"] if rows else None,
                "backups_dir": str(safety.backups_dir(data_dir)), "damaged_dir": str(data_dir / safety.DAMAGED_DIR),
                "policy": {"keep_last": safety.KEEP_LAST, "keep_daily_days": safety.KEEP_DAYS,
                           "periodic_hours": safety.PERIODIC_SECONDS / 3600,
                           "when": ["every app start", "every 6 hours while running", "before a database upgrade", "on request"]},
                "last_recovery": _last_recovery()}

    @app.post("/maintenance/backup")
    def backup_now() -> dict[str, Any]:
        try:
            b = safety.backup_store(studio.settings.db_path, "manual")
        except safety.BackupFailed as e:
            raise HTTPException(status_code=409, detail={"code": "backup_failed", "message": str(e), "affected": "project database",
                                                         "next_action": "restart Rebuild Studio; it checks the database at start"})
        try:
            studio.events.emit("store.backup", {"name": b["name"], "reason": "manual", "projects": b.get("projects")})
        except Exception:  # noqa: BLE001 - the backup itself succeeded
            pass
        return {k: b[k] for k in ("name", "taken_at", "reason", "projects", "size", "integrity")}
