"""`rebuildctl` against a running controller (R11: the CLI never opens the store of a folder whose controller is alive).

``RemoteStudio`` implements exactly the part of StudioServices that the CLI commands use, over the controller's loopback
REST API (docs/API.md) with the token from ``<data>/controller.json``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..remote import ControllerLink, RemoteError


def _call(fn: Any, *a: Any, **kw: Any) -> Any:
    try:
        return fn(*a, **kw)
    except RemoteError as e:
        msg = str(e.error.get("message") or e)
        if e.status == 404:
            raise KeyError(msg) from None
        if e.status in (400, 409, 422):
            raise ValueError(msg) from None
        from .main import CliError
        raise CliError(f"the running Rebuild Studio app answered HTTP {e.status}: {msg}", 3) from None
    except OSError as e:
        from .main import CliError
        raise CliError(f"the running Rebuild Studio app did not answer ({e}); try again", 3) from None


class _JobView:
    def __init__(self, d: dict[str, Any]):
        self._d = d
        self.job_id, self.state = d.get("job_id"), d.get("state")

    def to_dict(self) -> dict[str, Any]:
        return dict(self._d)


class _Settings:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)


class _Cases:
    def __init__(self, link: ControllerLink):
        self.link = link

    def get_case(self, case_id: str) -> dict[str, Any]:
        return _call(self.link.get, f"/cases/{case_id}")

    def list_evidence(self, case_id: str, kind: Optional[str] = None, module_id: Optional[str] = None) -> list[dict[str, Any]]:
        return _call(self.link.get, f"/cases/{case_id}/evidence", kind=kind, module_id=module_id)


class _Jobs:
    def __init__(self, link: ControllerLink):
        self.link = link

    def list(self, case_id: Optional[str] = None, states: Optional[list[Any]] = None) -> list[_JobView]:
        if case_id:
            rows = _call(self.link.get, f"/cases/{case_id}/jobs")
        else:
            rows = []
            for c in _call(self.link.get, "/cases", include_re=1):
                rows += _call(self.link.get, f"/cases/{c['case_id']}/jobs")
        want = {getattr(s, "value", s) for s in states} if states else None
        return [_JobView(r) for r in rows if want is None or r.get("state") in want]


class _Plan:
    def __init__(self, link: ControllerLink):
        self.link = link

    def progress(self, case_id: str) -> Any:
        return _call(self.link.get, f"/cases/{case_id}/plan").get("progress")

    def export(self, case_id: str) -> dict[str, Any]:
        p = _call(self.link.get, f"/cases/{case_id}/plan")
        revs = _call(self.link.get, f"/cases/{case_id}/plan/revisions")
        return {"case_id": case_id, "revision": p.get("revision"), "exported_at": datetime.now(timezone.utc).isoformat(),
                "items": p.get("items", []), "revisions": revs, "progress": p.get("progress")}


class _Ledger:
    def __init__(self, link: ControllerLink):
        self.link = link

    def summary(self, case_id: str) -> Any:
        return (_call(self.link.get, f"/cases/{case_id}").get("counts") or {}).get("features")


class RemoteStudio:
    """Forwarding stand-in for StudioServices. ``remote`` lets commands mention that the app did the work."""

    remote = True

    def __init__(self, link: ControllerLink, data_dir: Path):
        self.link = link
        self.settings = _Settings(data_dir)
        self.cases, self.jobs, self.plan, self.ledger = _Cases(link), _Jobs(link), _Plan(link), _Ledger(link)

    def doctor(self, smoke: bool = False, verify: bool = False) -> dict[str, Any]:
        if verify:
            from .main import CliError
            raise CliError("`doctor --verify` runs the fixture regressions in this process and needs the data folder to itself; "
                           "the Rebuild Studio app is running. Close the app and run it again, or use the app's Tools page.", 3)
        return _call(self.link.get, "/doctor", smoke=int(bool(smoke)))

    def create_case(self, **kwargs: Any) -> dict[str, Any]:
        return _call(self.link.post, "/cases", kwargs)

    def start_rebuild(self, case_id: str) -> Any:
        return _call(self.link.post, f"/cases/{case_id}/start")

    def cancel(self, job_id: Optional[str] = None, case_id: Optional[str] = None) -> list[str]:
        if job_id:
            return list(_call(self.link.post, f"/jobs/{job_id}/cancel").get("cancelled") or [])
        if case_id:
            return list(_call(self.link.post, f"/cases/{case_id}/cancel").get("cancelled") or [])
        return []

    def resume(self, job_id: Optional[str] = None, case_id: Optional[str] = None) -> list[str]:
        if job_id:
            return [_call(self.link.post, f"/jobs/{job_id}/resume")["job_id"]]
        if case_id:
            return list(_call(self.link.post, f"/cases/{case_id}/resume").get("resumed") or [])
        return []

    def search_evidence(self, case_id: str, query: str, kinds: Optional[list[str]] = None, limit: int = 50) -> list[dict[str, Any]]:
        return _call(self.link.get, f"/cases/{case_id}/evidence/search", q=query, kinds=",".join(kinds) if kinds else None, limit=limit)

    def get_evidence(self, evidence_id: str, max_bytes: Optional[int] = None) -> dict[str, Any]:
        return _call(self.link.get, f"/evidence/{evidence_id}", max_bytes=max_bytes)

    def stop(self) -> None:
        pass
