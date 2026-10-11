"""Talk to the controller that serves a data folder (R11: one writer per data folder).

The CLI and ``rebuild-mcp`` never open the SQLite store of a folder whose controller is alive; they forward to it over
loopback HTTP with the bearer token from ``<data>/controller.json`` (REST for the CLI, ``/mcp`` for the MCP tools).
Standard library only; proxies are bypassed (loopback).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class RemoteError(Exception):
    def __init__(self, status: int, error: dict[str, Any]):
        self.status, self.error = status, error
        super().__init__(error.get("message") or f"HTTP {status}")


class ControllerLink:
    def __init__(self, port: int, token: str, pid: Optional[int] = None, timeout: float = 120.0):
        self.port, self.token, self.pid, self.timeout = int(port), token, pid, timeout

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def request(self, method: str, path: str, body: Any = None, params: Optional[dict[str, Any]] = None,
                timeout: Optional[float] = None, headers: Optional[dict[str, str]] = None) -> Any:
        url = self.base + path
        if params:
            q = {k: v for k, v in params.items() if v is not None}
            if q:
                url += "?" + urllib.parse.urlencode(q, doseq=True)
        data = None if body is None else json.dumps(body).encode("utf-8")
        h = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if data is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with _OPENER.open(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}").get("error") or {}
            except ValueError:
                err = {}
            raise RemoteError(e.code, err if isinstance(err, dict) else {"message": str(err)}) from None
        return json.loads(raw) if raw else None

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Any = None, **params: Any) -> Any:
        return self.request("POST", path, body=body if body is not None else {}, params=params)

    def mcp(self, method: str, params: dict[str, Any], rid: int = 1) -> dict[str, Any]:
        """One stateless JSON-RPC request to the controller's /mcp endpoint; returns ``result`` (raises on a JSON-RPC error)."""
        body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        out = self.request("POST", "/mcp", body=body, headers={"Accept": "application/json, text/event-stream"})
        if not isinstance(out, dict):
            raise RemoteError(502, {"message": "unexpected /mcp response"})
        if "error" in out:
            e = out["error"] or {}
            raise RemoteError(400, {"code": str(e.get("code")), "message": str(e.get("message"))})
        return out.get("result") or {}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.mcp("tools/call", {"name": name, "arguments": arguments or {}})


def live_controller(data_dir: Path | str, *, probe_timeout: float = 3.0) -> Optional[ControllerLink]:
    """The controller serving ``data_dir`` if its process is alive and it answers an authenticated /health, else None."""
    from .cli import pid_alive
    p = Path(data_dir) / "controller.json"
    try:
        info = json.loads(p.read_text("utf-8"))
        port, token, pid = int(info["port"]), str(info["token"]), int(info.get("pid") or 0)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    if not port or not token or (pid and not pid_alive(pid)):
        return None
    link = ControllerLink(port, token, pid)
    try:
        h = link.request("GET", "/health", timeout=probe_timeout)
    except (RemoteError, OSError, ValueError):
        return None
    return link if isinstance(h, dict) and h.get("ok") and "pid" in h else None
