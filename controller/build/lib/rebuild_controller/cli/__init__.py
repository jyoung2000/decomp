"""`rebuildctl` command line interface (see main.py) plus small helpers shared with the MCP server."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def pid_alive(pid: int) -> bool:
    """True if a process with this pid exists. Never signals the process (os.kill(pid, 0) is NOT safe on Windows)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def controller_status(data_dir: Path | str) -> dict[str, Any]:
    """Inspect `<data_dir>/controller.json` written by the sidecar. The per-launch token is never returned."""
    p = Path(data_dir) / "controller.json"
    try:
        info = json.loads(p.read_text("utf-8"))
        pid = int(info.get("pid", 0))
        port = info.get("port")
    except (OSError, ValueError, TypeError, AttributeError):
        return {"running": False, "pid": None, "port": None}
    return {"running": pid_alive(pid), "pid": pid, "port": port}
