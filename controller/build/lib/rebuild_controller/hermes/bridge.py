"""Hermes computer-use bridge (M14).

Two clearly separated modes, labelled on every record this module produces:

``hermes_agent_session``
    The user's own ``hermes`` CLI is launched non-interactively (``hermes chat -Q --format stream-json --query-file``)
    against a user-controlled, paired profile. Hermes' model decides the actions; we only scope, observe, cancel and
    record. Credentials stay in the user's Hermes profile; we never write or pass them.

``cua_driver_direct``
    We speak the cua-driver MCP protocol ourselves (deterministic recipes, no model). This is *not* a Hermes
    integration and must never be presented as one.

Nothing here opens a network socket: the only egress is whatever the user's Hermes profile does with its selected
model provider (agent mode) - direct mode has none (and sets ``CUA_DRIVER_RS_TELEMETRY_ENABLED=0`` for the driver).
The contract is derived from the pinned Hermes source; see docs/HERMES.md for file:line citations.
"""
from __future__ import annotations

import contextlib
import copy
import difflib
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..config import get_settings
from ..ids import new_id, now_iso, sha256_bytes
from ..jobs.runner import Cancelled, kill_tree
from . import protocol as P
from .protocol import (MODE_AGENT, MODE_DIRECT, MODES, ProtocolError, ToolResult, UnsupportedAction)

BRIDGE_SCHEMA = "rebuild-studio.hermes-bridge/1"
TASK_SCHEMA = "rebuild-studio.hermes-task/1"
RECIPE_SCHEMA = "rebuild-studio.hermes-recipe/1"
DEFAULT_MCP_SERVER_NAME = "rebuild_studio"
DRIVER_ENV_OVERRIDE = "HERMES_CUA_DRIVER_CMD"          # tools/computer_use/cua_backend_driver.py:20
TELEMETRY_ENV = "CUA_DRIVER_RS_TELEMETRY_ENABLED"      # cua_backend.py (_CUA_TELEMETRY_ENV_VAR), value "0" = off
INSTALL_PS1 = "iex (irm https://hermes-agent.nousresearch.com/install.ps1)"
INSTALL_SH = "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
# Files that mark a real Hermes home/profile (hermes_constants.py:318).
PROFILE_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")
_SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|passw(or)?d|credential|authorization|bearer)", re.I)
_SECRET_VALUE_RE = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}|Bearer\s+[A-Za-z0-9._-]{16,})")
_SAFE_FLAG_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")   # hermes_cli/main.py:431


class HermesBridgeError(Exception):
    """Maps onto the API error shape ``{"error": {code, message, affected?, next_action?}}`` (docs/API.md)."""

    def __init__(self, code: str, message: str, *, affected: str | None = None, next_action: str | None = None):
        super().__init__(message)
        self.code, self.message, self.affected, self.next_action = code, message, affected, next_action

    def to_error(self) -> dict[str, Any]:
        e: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.affected:
            e["affected"] = self.affected
        if self.next_action:
            e["next_action"] = self.next_action
        return {"error": e}


class ScopeViolation(ProtocolError):
    """An action would leave the task's declared scope (target window/process, allowed actions, max steps)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def diag(code: str, severity: str, message: str, *, next_action: str | None = None, **evidence: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"code": code, "severity": severity, "message": message}
    if next_action:
        d["next_action"] = next_action
    if evidence:
        d["evidence"] = evidence
    return d


def iso_from_ms(ms: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000.0)) + f".{int(ms % 1000):03d}Z"


# ---------------------------------------------------------------------------------------------------------------------
# Platform probe (injectable so Windows-only logic is unit-testable on Linux)
# ---------------------------------------------------------------------------------------------------------------------
class SystemProbe:
    """Facts about the machine the *bridge* runs on. The Windows branches use ctypes and are only reachable on
    Windows; they are UNTESTED on this Linux host (gate G3 in docs/HERMES.md)."""

    def system(self) -> str:
        return platform.system()

    def hostname(self) -> str:
        return os.environ.get("COMPUTERNAME") or platform.node() or "unknown"

    def display_env(self) -> dict[str, str]:
        return {k: os.environ[k] for k in ("DISPLAY", "WAYLAND_DISPLAY") if os.environ.get(k)}

    def windows_session(self) -> dict[str, Any] | None:
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        sid = wintypes.DWORD(0)
        ok = bool(k32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)))
        info: dict[str, Any] = {"session_id": int(sid.value) if ok else None}
        # Window station: an interactive desktop is WinSta0; services and OpenSSH sessions get a non-interactive one.
        u32.GetProcessWindowStation.restype = wintypes.HANDLE
        u32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                  ctypes.POINTER(wintypes.DWORD)]
        ws = u32.GetProcessWindowStation()
        buf = ctypes.create_unicode_buffer(256)
        need = wintypes.DWORD(0)
        if ws and u32.GetUserObjectInformationW(ws, 2, buf, ctypes.sizeof(buf), ctypes.byref(need)):  # UOI_NAME
            info["window_station"] = buf.value
        # Lock heuristic: while the workstation is locked the input desktop is Winlogon and OpenInputDesktop with
        # DESKTOP_SWITCHDESKTOP fails. A service/Session-0 caller fails the same way, so this is a heuristic only.
        u32.OpenInputDesktop.restype = wintypes.HANDLE
        u32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        h = u32.OpenInputDesktop(0, False, 0x0100)
        if h:
            u32.CloseDesktop.argtypes = [wintypes.HANDLE]
            u32.CloseDesktop(h)
            info["input_desktop_accessible"] = True
        else:
            info["input_desktop_accessible"] = False
            info["lock_probe_error"] = ctypes.get_last_error()
        return info

    def process_elevation(self, pid: int | None = None) -> dict[str, Any] | None:
        """``{"elevated": bool|None, "access_denied": bool}`` for *pid* (default: this process); Windows only."""
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        proc = k32.GetCurrentProcess() if pid is None else k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not proc:
            return {"elevated": None, "access_denied": ctypes.get_last_error() == 5}
        tok = wintypes.HANDLE()
        try:
            if not adv.OpenProcessToken(proc, 0x0008, ctypes.byref(tok)):  # TOKEN_QUERY
                return {"elevated": None, "access_denied": ctypes.get_last_error() == 5}
            elev, ret = wintypes.DWORD(0), wintypes.DWORD(0)
            if not adv.GetTokenInformation(tok, 20, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(ret)):  # TokenElevation
                return {"elevated": None, "access_denied": False}
            return {"elevated": bool(elev.value), "access_denied": False}
        finally:
            if tok:
                k32.CloseHandle(tok)
            if pid is not None:
                k32.CloseHandle(proc)


# ---------------------------------------------------------------------------------------------------------------------
# Hermes profile layout
# ---------------------------------------------------------------------------------------------------------------------
def default_hermes_home(env: dict[str, str], system: str) -> Path:
    """``HERMES_HOME`` -> platform default (hermes_constants.py:51-61, 111-121): ``%LOCALAPPDATA%\\hermes`` on native
    Windows, ``~/.hermes`` elsewhere (WSL2 included)."""
    explicit = (env.get("HERMES_HOME") or "").strip()
    suffix = env.get("HERMES_DATA_DIR_SUFFIX", "")
    if explicit:
        return Path(os.path.expandvars(os.path.expanduser(explicit)))
    home = Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())
    if system == "Windows":
        local = (env.get("LOCALAPPDATA") or "").strip()
        return (Path(local) if local else home / "AppData" / "Local") / ("hermes" + suffix)
    return home / (".hermes" + suffix)


def hermes_root_of(profile: Path) -> Path:
    """``<root>/profiles/<name>`` -> ``<root>``; anything else is its own root (hermes_constants.py:231)."""
    return profile.parent.parent if profile.parent.name == "profiles" else profile


def list_profiles(root: Path) -> list[str]:
    pdir = root / "profiles"
    try:
        return sorted(p.name for p in pdir.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def _looks_like_profile(path: Path) -> bool:
    return path.is_dir() and any((path / m).exists() for m in PROFILE_MARKERS)


# ---------------------------------------------------------------------------------------------------------------------
# config.yaml merge (mcp_servers) - text-preserving, verified, with backup + diff
# ---------------------------------------------------------------------------------------------------------------------
def _yaml():
    try:
        import yaml  # PyYAML: pulled in by uvicorn[standard]; Hermes itself uses ruamel (YAML 1.1 semantics)
        return yaml
    except ImportError as e:  # pragma: no cover
        raise HermesBridgeError("yaml_missing", "PyYAML is required to edit Hermes config.yaml",
                                next_action="pip install pyyaml") from e


def _dump_entry_block(name: str, entry: dict[str, Any], indent: str, nl: str) -> list[str]:
    text = _yaml().safe_dump({name: entry}, default_flow_style=False, sort_keys=False, allow_unicode=True, width=1000)
    return [indent + line + nl for line in text.splitlines()]


def merge_mcp_server(text: str, name: str, entry: dict[str, Any]) -> tuple[str, bool]:
    """Insert/replace ``mcp_servers.<name>`` in a Hermes ``config.yaml`` document.

    Returns ``(new_text, comments_preserved)``. Everything outside the one entry is preserved byte-for-byte whenever
    ``mcp_servers`` is absent, empty, or a block mapping (the normal cases); an exotic flow-style ``mcp_servers`` falls
    back to a full re-dump (comments lost, ``comments_preserved=False``). The result is always re-parsed and compared
    with the expected data structure; a mismatch raises instead of writing anything.
    """
    yaml = _yaml()
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as e:
        raise HermesBridgeError("config_unparseable", f"config.yaml is not valid YAML: {e}",
                                next_action="fix the file (or restore a backup) and retry; nothing was changed") from e
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise HermesBridgeError("config_shape", "config.yaml top level is not a mapping", next_action="fix config.yaml")
    servers = data.get("mcp_servers")
    if servers is not None and not isinstance(servers, dict):
        raise HermesBridgeError("config_shape", "mcp_servers in config.yaml is not a mapping",
                                next_action="fix mcp_servers (it must map server names to settings)")
    if isinstance(servers, dict) and servers.get(name) == entry:
        return text, True
    expected = copy.deepcopy(data)
    expected.setdefault("mcp_servers", {})
    if expected["mcp_servers"] is None:
        expected["mcp_servers"] = {}
    expected["mcp_servers"][name] = entry

    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    key_re = re.compile(r"^mcp_servers\s*:(.*)$")
    idx = next((i for i, l in enumerate(lines) if key_re.match(l.rstrip("\r\n"))), None)
    new_lines: list[str] | None = None
    if idx is None:
        base = list(lines)
        if base and not base[-1].endswith(("\n", "\r")):
            base[-1] += nl
        new_lines = base + ([nl] if base and base[-1].strip() else []) + ["mcp_servers:" + nl] + _dump_entry_block(name, entry, "  ", nl)
    else:
        rest = key_re.match(lines[idx].rstrip("\r\n")).group(1).split("#", 1)[0].strip()  # type: ignore[union-attr]
        # extent of the block: following lines until the next top-level (col-0, non-comment) content line
        end = idx + 1
        while end < len(lines):
            l = lines[end]
            if l.strip() and not l.startswith((" ", "\t", "#")):
                break
            end += 1
        if rest in ("", "{}", "null", "~"):
            body = lines[idx + 1:end]
            has_children = any(l.strip() and not l.lstrip().startswith("#") for l in body)
            if rest == "" and has_children:
                child = next(l for l in body if l.strip() and not l.lstrip().startswith("#"))
                indent = child[: len(child) - len(child.lstrip())]
                # replace an existing child block, else append after the last content line of the block
                cre = re.compile(r"^" + re.escape(indent) + r"(?:" + re.escape(name) + r"|'" + re.escape(name) + r"'|\"" + re.escape(name) + r"\")\s*:")
                cs = next((j for j, l in enumerate(body) if cre.match(l)), None)
                if cs is not None:
                    ce = cs + 1
                    while ce < len(body):
                        l = body[ce]
                        if l.strip() and not l.lstrip().startswith("#") and (len(l) - len(l.lstrip())) <= len(indent):
                            break
                        ce += 1
                    # keep trailing blank/comment lines that belong to what follows
                    while ce > cs + 1 and (not body[ce - 1].strip() or body[ce - 1].lstrip().startswith("#")):
                        ce -= 1
                    body = body[:cs] + _dump_entry_block(name, entry, indent, nl) + body[ce:]
                else:
                    last = max(j for j, l in enumerate(body) if l.strip() and not l.lstrip().startswith("#"))
                    if not body[last].endswith(("\n", "\r")):
                        body[last] += nl
                    body = body[: last + 1] + _dump_entry_block(name, entry, indent, nl) + body[last + 1:]
            else:
                body = _dump_entry_block(name, entry, "  ", nl) + body
            head = lines[idx] if rest == "" else "mcp_servers:" + nl
            if not head.endswith(("\n", "\r")):
                head += nl
            new_lines = lines[:idx] + [head] + body + lines[end:]
    if new_lines is not None:
        new_text = "".join(new_lines)
        try:
            ok = yaml.safe_load(new_text) == expected
        except yaml.YAMLError:
            ok = False
        if ok:
            return new_text, True
    # Fallback: full re-dump (still verified below).
    comments_preserved = False
    new_text = yaml.safe_dump(expected, default_flow_style=False, sort_keys=False, allow_unicode=True, width=1000)
    if yaml.safe_load(new_text) != expected:  # pragma: no cover - defensive
        raise HermesBridgeError("merge_verification_failed", "could not produce a verified config merge; nothing was changed",
                                next_action="add the mcp_servers entry manually (see docs/HERMES.md)")
    return new_text, comments_preserved


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


# ---------------------------------------------------------------------------------------------------------------------
# Scope, task, records
# ---------------------------------------------------------------------------------------------------------------------
_FULL_SCREEN = {"screen", "fullscreen", "full screen", "all", "desktop"}


def _norm_proc(name: str) -> str:
    n = name.strip().lower()
    return n[:-4] if n.endswith(".exe") else n


@dataclass
class Scope:
    """What a task may touch. Enforced pre-send in direct mode; enforced by kill-on-observe in agent mode."""
    allowed_actions: tuple[str, ...] = P.READ_ONLY_ACTIONS
    window_title_contains: str | None = None
    window_title_regex: str | None = None
    process_names: tuple[str, ...] = ()
    max_steps: int = 30
    reverify_target: bool = True

    @property
    def has_target(self) -> bool:
        return bool(self.window_title_contains or self.window_title_regex or self.process_names)

    def title_ok(self, title: str) -> bool:
        if self.window_title_contains and self.window_title_contains.lower() not in (title or "").lower():
            return False
        if self.window_title_regex and not re.search(self.window_title_regex, title or ""):
            return False
        return True

    def process_ok(self, app: str) -> bool:
        return not self.process_names or _norm_proc(app or "") in {_norm_proc(p) for p in self.process_names}

    def window_ok(self, w: P.WindowInfo) -> bool:
        return self.title_ok(w.title) and self.process_ok(w.app_name)

    def to_json(self) -> dict[str, Any]:
        return {"allowed_actions": list(self.allowed_actions), "window_title_contains": self.window_title_contains,
                "window_title_regex": self.window_title_regex, "process_names": list(self.process_names),
                "max_steps": self.max_steps, "reverify_target": self.reverify_target}


DEFAULT_TASK_ACTIONS = P.READ_ONLY_ACTIONS + (P.A_CLICK, P.A_DOUBLE_CLICK, P.A_TYPE, P.A_KEY, P.A_SCROLL, P.A_SET_VALUE, P.A_FOCUS_APP)


@dataclass
class HermesTask:
    """A scoped task. ``model``/``provider`` map to Hermes' documented ``-m``/``--provider`` flags only; the account
    (credentials) is whichever Hermes profile the user paired - nothing credential-like may appear in a task."""
    goal: str = ""
    task_id: str = field(default_factory=lambda: new_id("htask"))
    mode: str = MODE_AGENT
    target_window_title: str | None = None
    target_window_regex: str | None = None
    target_processes: tuple[str, ...] = ()
    allowed_actions: tuple[str, ...] = DEFAULT_TASK_ACTIONS
    max_steps: int = 30
    timeout_s: float | None = None
    model: str | None = None
    provider: str | None = None
    toolsets: tuple[str, ...] = ("computer_use",)
    ignore_rules: bool = False
    strict_tools: bool = False
    recipe: dict[str, Any] | None = None          # direct mode only
    metadata: dict[str, Any] = field(default_factory=dict)

    def scope(self) -> Scope:
        return Scope(tuple(self.allowed_actions), self.target_window_title, self.target_window_regex,
                     tuple(self.target_processes), self.max_steps)

    def validate(self) -> None:
        if self.mode not in MODES:
            raise HermesBridgeError("bad_mode", f"mode must be one of {MODES}", affected="mode")
        bad = [a for a in self.allowed_actions if a not in P.COMPUTER_USE_ACTIONS]
        if bad:
            raise UnsupportedAction(",".join(bad), f"not in the computer_use action enum {P.COMPUTER_USE_ACTIONS}")
        if not (1 <= int(self.max_steps) <= 500):
            raise HermesBridgeError("bad_task", "max_steps must be 1..500", affected="max_steps")
        if self.mode == MODE_AGENT and not (self.goal or "").strip():
            raise HermesBridgeError("bad_task", "goal must not be empty", affected="goal")
        if len(self.goal) > 20000:
            raise HermesBridgeError("bad_task", "goal is too long (20000 chars max)", affected="goal")
        if self.mode == MODE_DIRECT and not self.recipe:
            raise HermesBridgeError("bad_task", "direct mode runs a recorded recipe, not a free-form goal; pass recipe=",
                                    affected="recipe", next_action="use mode='hermes_agent_session' for goal-driven tasks")
        for k in ("model", "provider"):
            v = getattr(self, k)
            if v is not None and not _SAFE_FLAG_VALUE.match(v):
                raise HermesBridgeError("bad_task", f"{k} contains characters that are not valid in a Hermes model/provider id", affected=k)
        for ts in self.toolsets:
            if not _SAFE_FLAG_VALUE.match(ts):
                raise HermesBridgeError("bad_task", "invalid toolset name", affected="toolsets")
        if self.target_window_regex:
            try:
                re.compile(self.target_window_regex)
            except re.error as e:
                raise HermesBridgeError("bad_task", f"invalid target_window_regex: {e}", affected="target_window_regex") from e
        _assert_no_credentials(self.to_file_dict(), what="task")

    def to_file_dict(self) -> dict[str, Any]:
        return {"schema": TASK_SCHEMA, "task_id": self.task_id, "mode": self.mode, "goal": self.goal,
                "scope": self.scope().to_json(), "model": self.model, "provider": self.provider,
                "toolsets": list(self.toolsets), "ignore_rules": self.ignore_rules, "timeout_s": self.timeout_s,
                "metadata": self.metadata, "credentials": "none: credentials stay in the user's Hermes profile"}


def _assert_no_credentials(obj: Any, *, what: str, _path: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "credentials":
                continue
            if _SECRET_KEY_RE.search(str(k)) and v not in (None, "", False):
                raise HermesBridgeError("credential_in_task", f"{what} field {_path}{k!r} looks like a credential; never put credentials in a task",
                                        affected=f"{_path}{k}", next_action="configure the credential in your Hermes profile (hermes auth / hermes setup)")
            _assert_no_credentials(v, what=what, _path=f"{_path}{k}.")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _assert_no_credentials(v, what=what, _path=f"{_path}{i}.")
    elif isinstance(obj, str) and _SECRET_VALUE_RE.search(obj):
        raise HermesBridgeError("credential_in_task", f"{what} text at {_path.rstrip('.')} contains something that looks like a secret",
                                affected=_path.rstrip("."), next_action="remove it; credentials belong in your Hermes profile")


def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Typed text may be a password: records keep length + hash, never the text."""
    out = dict(args)
    for key in ("text", "value"):
        if isinstance(out.get(key), str):
            s = out.pop(key)
            out[f"{key}_len"] = len(s)
            out[f"{key}_sha256"] = hashlib.sha256(s.encode("utf-8")).hexdigest()
    return out


@dataclass
class ActionRecord:
    seq: int
    mode: str
    action: str
    args: dict[str, Any]
    result: dict[str, Any]
    pre_screenshot_sha: str | None
    post_screenshot_sha: str | None
    postcondition_ok: bool | None
    ts: str
    source: str = ""                     # "driver" | "hermes_stream"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = {"seq": self.seq, "mode": self.mode, "source": self.source, "action": self.action, "args": self.args,
             "pre_screenshot_sha": self.pre_screenshot_sha, "result": self.result,
             "post_screenshot_sha": self.post_screenshot_sha, "postcondition_ok": self.postcondition_ok, "ts": self.ts}
        d.update(self.extra)
        return d


@dataclass
class TaskResult:
    task_id: str
    mode: str
    status: str                           # completed|failed|cancelled|disconnected|timeout|scope_violation|postcondition_failed
    records: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    exit_code: int | None = None
    final_text: str = ""
    error: str | None = None
    session_id: str | None = None
    tokens: dict[str, Any] | None = None
    evidence_ids: list[str] = field(default_factory=list)
    task_file: str | None = None
    command: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["schema"] = BRIDGE_SCHEMA
        d["integration"] = integration_note(self.mode)
        return d


def integration_note(mode: str) -> str:
    if mode == MODE_DIRECT:
        return ("cua-driver direct automation: Rebuild Studio drove the cua-driver MCP server itself with a recorded recipe. "
                "No Hermes agent, no model, and NOT a Hermes integration.")
    return ("Hermes agent session: the user's Hermes CLI ran a model-driven session against the paired profile; "
            "the model provider selected in that profile received any screenshots.")


# ---------------------------------------------------------------------------------------------------------------------
# Failure-text -> diagnostics (shared by both modes)
# ---------------------------------------------------------------------------------------------------------------------
_DIAG_PATTERNS: list[tuple[re.Pattern[str], str, str, str, str]] = [
    (re.compile(r"access is denied|uipi|integrity level|\belevated\b|run as administrator", re.I), "uipi_elevated_target", "error",
     "The target window belongs to a more privileged (elevated) process; Windows blocks input from a lower-integrity process (UIPI).",
     "Run the target app unelevated, or start Hermes/cua-driver elevated too. Never elevate silently."),
    (re.compile(r"no on-screen window|returned no windows|no windows? (found|match)|no display is set", re.I), "no_windows_found", "error",
     "Window discovery found nothing: wrong desktop, locked screen, Session 0, or the target app is not running.",
     "Run `hermes computer-use doctor` on the machine that owns the desktop; unlock the screen; start the target app."),
    (re.compile(r"desktop session is locked|workstation is locked|\bLOCKED\b"), "desktop_locked", "error",
     "The desktop is locked; locked sessions hide windows and freeze renderers.", "Unlock the desktop and retry."),
    (re.compile(r"unknown action|unsupported_action|not in the action enum|foreground_unsupported", re.I), "unsupported_action", "error",
     "The action is not supported by this Hermes/cua-driver version.", "Use an action from the computer_use enum, or update Hermes/cua-driver."),
    (re.compile(r"requires approval but no interactive|BLOCKED: computer_use", re.I), "approval_blocked", "error",
     "Hermes blocked a desktop action because nobody can approve it in a non-interactive run.",
     "Opt in deliberately in YOUR profile: approvals.single_query_mode: approve, or computer_use.permission_mode: bounded with a reviewed capability manifest. Rebuild Studio never changes this for you."),
    (re.compile(r"0 interactable element|\"total_elements\":\s*0\b|'total_elements':\s*0\b|degraded.{0,6}true", re.I), "empty_accessibility_tree", "warning",
     "The accessibility (UI) tree is empty or degraded; element-index actions will not work.",
     "Act by pixel coordinates, or target an app that exposes UI Automation; check `hermes computer-use doctor`."),
    (re.compile(r"cua-driver is not installed|cua-driver is not ready", re.I), "driver_missing", "error",
     "cua-driver is missing or does not meet Hermes' runtime contract.", "Run `hermes computer-use install`."),
    (re.compile(r"human_has_control", re.I), "human_has_control", "warning",
     "A human holds the screen lease; the agent was refused.", "Release the screen and retry."),
]


def diagnose_text(text: str) -> list[dict[str, Any]]:
    out, seen = [], set()
    for rx, code, sev, msg, nxt in _DIAG_PATTERNS:
        if code not in seen and rx.search(text or ""):
            seen.add(code)
            out.append(diag(code, sev, msg, next_action=nxt))
    return out


def _merge_diags(into: list[dict[str, Any]], more: Iterable[dict[str, Any]]) -> None:
    have = {d["code"] for d in into}
    for d in more:
        if d["code"] not in have:
            into.append(d)
            have.add(d["code"])


def sanitized_driver_env(base: dict[str, str]) -> dict[str, str]:
    """Env for the (third-party) cua-driver binary: provider secrets stripped, telemetry off (Hermes does the same:
    cua_backend.py:137-184)."""
    env = {k: v for k, v in base.items() if not _SECRET_KEY_RE.search(k)}
    env[TELEMETRY_ENV] = "0"
    return env


# ---------------------------------------------------------------------------------------------------------------------
# Direct cua-driver MCP client
# ---------------------------------------------------------------------------------------------------------------------
class DriverTransportError(Exception):
    pass


@dataclass
class CaptureState:
    window: P.WindowInfo
    sha: str | None
    elements: list[P.Element]
    tree: str
    title: str
    empty: bool
    degraded: bool


class DirectDriverSession:
    """Minimal stdio MCP client for ``cua-driver mcp``. Mode label ``cua_driver_direct`` on every record.

    Fails closed like Hermes: a mutating call whose transport broke or timed out is NEVER replayed (the action may
    have landed); the session is marked suspect, restarted before the next call, and the sticky target is dropped so
    a fresh capture is required before any further input.
    """

    mode = MODE_DIRECT

    def __init__(self, command: list[str], *, env: dict[str, str] | None = None, scope: Scope | None = None,
                 session_id: str | None = None, call_timeout: float = 30.0, checkpoint: Callable[[], None] | None = None,
                 max_screenshot_bytes: int = 64 * 1024 * 1024):
        self.command = list(command)
        self.env = sanitized_driver_env(env if env is not None else dict(os.environ))
        self.scope = scope or Scope(allowed_actions=tuple(P.COMPUTER_USE_ACTIONS), max_steps=500)
        self.session_id = session_id or f"rebuild-studio-{uuid.uuid4().hex[:12]}"
        self.call_timeout = call_timeout
        self.checkpoint = checkpoint
        self.max_screenshot_bytes = max_screenshot_bytes
        self.records: list[ActionRecord] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.screenshots: dict[str, bytes] = {}
        self.tools: dict[str, dict[str, Any]] = {}
        self.capability_version = ""
        self.server_info: dict[str, Any] = {}
        self.sent_calls: list[dict[str, Any]] = []     # (tool, arguments) actually sent, for audit
        self._proc: subprocess.Popen | None = None
        self._q: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
        self._id = 0
        self._lock = threading.RLock()
        self._suspect = False
        self._target: P.WindowInfo | None = None
        self._state: CaptureState | None = None
        self._last_sha: str | None = None
        self._steps = 0
        self._seq = 0
        self._stderr_tail: list[str] = []
        self._shot_bytes = 0

    # -- lifecycle ----------------------------------------------------------------------------------------------
    def __enter__(self) -> "DirectDriverSession":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def start(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            kw: dict[str, Any] = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "env": self.env}
            if os.name == "nt":
                kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            else:
                kw["start_new_session"] = True
            try:
                self._proc = subprocess.Popen(self.command, **kw)
            except OSError as e:
                raise HermesBridgeError("driver_spawn_failed", f"could not start cua-driver: {e}", affected=self.command[0],
                                        next_action="run `hermes computer-use install` / `hermes computer-use doctor`") from e
            self._q = queue.Queue()
            threading.Thread(target=self._pump_stdout, args=(self._proc, self._q), daemon=True).start()
            threading.Thread(target=self._pump_stderr, args=(self._proc,), daemon=True).start()
            self._suspect = False
            init = self._rpc("initialize", P.initialize_params(), timeout=self.call_timeout)
            self.server_info = init.get("serverInfo", {}) if isinstance(init, dict) else {}
            self._notify("notifications/initialized")
            self._load_tools()
            # Declared identity; non-fatal (Hermes continues anonymously if the driver rejects it).
            with contextlib.suppress(Exception):
                self._send_tool(P.StartSession(session=self.session_id), audit=False)

    def _pump_stdout(self, proc: subprocess.Popen, q: "queue.Queue[dict[str, Any] | None]") -> None:
        try:
            for line in iter(proc.stdout.readline, b""):  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    q.put(json.loads(line.decode("utf-8", "replace")))
                except ValueError:
                    continue
        finally:
            q.put(None)

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        with contextlib.suppress(Exception):
            for line in iter(proc.stderr.readline, b""):  # type: ignore[union-attr]
                self._stderr_tail = (self._stderr_tail + [line.decode("utf-8", "replace").rstrip()])[-20:]

    def _write(self, msg: dict[str, Any]) -> None:
        if self._proc is None or self._proc.poll() is not None or self._proc.stdin is None:
            raise DriverTransportError("cua-driver is not running")
        try:
            self._proc.stdin.write(P.encode(msg))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise DriverTransportError(f"write to cua-driver failed: {e}") from e

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write(P.rpc_notification(method, params))

    def _rpc(self, method: str, params: dict[str, Any] | None, *, timeout: float) -> Any:
        self._id += 1
        mid = self._id
        self._write(P.rpc_request(mid, method, params))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"cua-driver MCP call {method} timed out after {timeout:.0f}s")
            try:
                msg = self._q.get(timeout=min(left, 0.25))
            except queue.Empty:
                if self.checkpoint:
                    self.checkpoint()
                continue
            if msg is None:
                raise DriverTransportError("cua-driver closed the connection: " + " | ".join(self._stderr_tail[-3:]))
            if msg.get("id") != mid:
                continue  # notification or stale response
            if "error" in msg:
                return {"__jsonrpc_error__": msg["error"]}
            return msg.get("result")

    def _load_tools(self) -> None:
        tools: dict[str, dict[str, Any]] = {}
        cursor = None
        for _ in range(10):
            res = self._rpc("tools/list", {"cursor": cursor} if cursor else {}, timeout=self.call_timeout)
            if not isinstance(res, dict) or "__jsonrpc_error__" in res:
                break
            for t in res.get("tools") or []:
                if isinstance(t, dict) and isinstance(t.get("name"), str):
                    tools[t["name"]] = t.get("inputSchema") or {}
            if isinstance(res.get("capability_version"), str):
                self.capability_version = res["capability_version"]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = tools

    def close(self) -> None:
        with self._lock:
            p, self._proc = self._proc, None
            if p is None:
                return
            if p.poll() is None:
                with contextlib.suppress(Exception):
                    self._id += 1
                    p.stdin.write(P.encode(P.rpc_request(self._id, "tools/call", P.tools_call_params(P.EndSession(session=self.session_id)))))  # best effort
                    p.stdin.flush()
                with contextlib.suppress(Exception):
                    p.stdin.close()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                kill_tree(p)
            with contextlib.suppress(Exception):
                p.wait(timeout=5)

    # -- low-level calls ----------------------------------------------------------------------------------------
    def supports_input_property(self, tool: str, prop: str) -> bool:
        props = (self.tools.get(tool) or {}).get("properties")
        return isinstance(props, dict) and prop in props

    def _send_tool(self, msg: P.ToolCall, *, audit: bool = True) -> ToolResult:
        """Send one validated call. Never raises for driver-side failures (they become error ToolResults)."""
        if self._suspect:
            self.close()
            self._target, self._state = None, None
            self.start()
        self._precheck(msg)
        if audit:
            self.sent_calls.append({"tool": msg.tool, "arguments": msg.arguments()})
        try:
            res = self._rpc("tools/call", P.tools_call_params(msg), timeout=self.call_timeout)
        except TimeoutError as e:
            self._suspect = True
            return self._unknown_outcome(msg, "timeout_outcome_unknown", str(e))
        except DriverTransportError as e:
            self._suspect = True
            return self._unknown_outcome(msg, "transport_outcome_unknown", str(e))
        if isinstance(res, dict) and "__jsonrpc_error__" in res:
            err = res["__jsonrpc_error__"]
            return ToolResult(msg.tool, True, data=str(err.get("message", err)) if isinstance(err, dict) else str(err),
                              structured={"ok": False, "code": "jsonrpc_error", "detail": err})
        return ToolResult.from_mcp(msg.tool, res or {})

    def _precheck(self, msg: P.ToolCall) -> None:
        """Everything that can be decided without talking to the driver. Raises before anything is sent."""
        msg.validate()
        if self.tools and msg.tool not in self.tools and msg.tool not in ("start_session", "end_session"):
            raise UnsupportedAction(msg.tool, "the connected cua-driver does not advertise this tool")
        if getattr(msg, "delivery_mode", None) == "foreground" and self.tools and not self.supports_input_property(msg.tool, "delivery_mode"):
            raise UnsupportedAction(msg.tool, "foreground_unsupported: the live action schema does not accept delivery_mode")

    def _unknown_outcome(self, msg: P.ToolCall, code: str, detail: str) -> ToolResult:
        self._target, self._state = None, None     # fail closed: a fresh capture is required before any further input
        text = (f"cua-driver call {msg.tool} failed ({code}); the action outcome is unknown and was NOT replayed. "
                "Take fresh state before deciding whether to act again.")
        _merge_diags(self.diagnostics, [diag(code, "error", text, next_action="re-capture, then decide; the bridge never replays an uncertain input")])
        return ToolResult(msg.tool, True, data=text, structured={"ok": False, "code": code, "message": text, "detail": detail, "next_step": "fresh_state"})

    def _tick(self) -> None:
        if self.checkpoint:
            self.checkpoint()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _keep_shot(self, data: bytes | None) -> str | None:
        if not data:
            return None
        sha = sha256_bytes(data)
        if sha not in self.screenshots and self._shot_bytes + len(data) <= self.max_screenshot_bytes:
            self.screenshots[sha] = data
            self._shot_bytes += len(data)
        return sha

    # -- reads --------------------------------------------------------------------------------------------------
    def list_windows(self) -> list[P.WindowInfo]:
        self._tick()
        res = self._send_tool(P.ListWindows(on_screen_only=True, session=self.session_id))
        if res.is_error:
            _merge_diags(self.diagnostics, diagnose_text(res.error_text()))
            return []
        return P.parse_windows(res)

    def select_target(self, *, title_contains: str | None = None, process: str | None = None) -> P.WindowInfo:
        """Pick the frontmost on-screen window inside the scope. Never picks a window outside it."""
        wins = [w for w in self.list_windows() if w.on_screen and self.scope.window_ok(w)]
        if title_contains:
            wins = [w for w in wins if title_contains.lower() in w.title.lower()]
        if process:
            wins = [w for w in wins if _norm_proc(w.app_name) == _norm_proc(process)]
        if not wins:
            _merge_diags(self.diagnostics, [diag("no_windows_found", "error", "no on-screen window matches the task scope",
                                                 next_action="start the target app on the interactive desktop; check `hermes computer-use doctor`",
                                                 scope=self.scope.to_json())])
            raise ScopeViolation("no_target_window", "no on-screen window matches the task scope")
        wins.sort(key=lambda w: -float(w.z_index))
        self._target, self._state = wins[0], None
        return self._target

    def capture(self) -> CaptureState:
        """``get_window_state`` of the sticky target: screenshot sha + UI tree (read-only, not a counted step)."""
        self._tick()
        if self._target is None:
            self.select_target()
        w = self._target
        assert w is not None
        res = self._send_tool(P.GetWindowState(pid=w.pid, window_id=w.window_id, session=self.session_id))
        if res.is_error:
            _merge_diags(self.diagnostics, diagnose_text(res.error_text()))
            self._target, self._state = None, None
            raise ScopeViolation("capture_failed", f"get_window_state failed: {res.message or res.error_text()[:200]}")
        sha = self._keep_shot(res.image_bytes())
        els = P.parse_elements(res)
        tree = P.tree_text(res)
        degraded = bool(res.verdict.degraded) or P.window_state_is_empty(res) or not els
        if degraded:
            _merge_diags(self.diagnostics, [d for d in diagnose_text("0 interactable element(s)") if d["code"] == "empty_accessibility_tree"])
        title = (res.structured or {}).get("window_title") or w.title
        st = CaptureState(w, sha, els, tree, str(title or ""), P.window_state_is_empty(res), degraded)
        self._state, self._last_sha = st, sha
        return st

    # -- actions ------------------------------------------------------------------------------------------------
    def _check_scope(self, action: str, args: dict[str, Any]) -> None:
        if action not in self.scope.allowed_actions:
            raise UnsupportedAction(action, f"not permitted by the task scope {list(self.scope.allowed_actions)}")
        if action not in P.COMPUTER_USE_ACTIONS:
            raise UnsupportedAction(action, "not in the computer_use action enum")
        app = args.get("app")
        if isinstance(app, str) and app.strip():
            low = app.strip().lower()
            if self.scope.has_target and low in _FULL_SCREEN:
                raise ScopeViolation("target_scope", f"app={app!r} captures beyond the scoped target window")
            if self.scope.process_names and low not in _FULL_SCREEN and not self.scope.process_ok(app):
                raise ScopeViolation("target_scope", f"app={app!r} is not an allowed process {list(self.scope.process_names)}")

    def act(self, action: str, args: dict[str, Any] | None = None, *, expect: list[dict[str, Any]] | None = None,
            settle_s: float = 0.0, expect_timeout_s: float = 0.0) -> ActionRecord:
        """Perform one Hermes-level ``computer_use`` action directly against the driver and record it.

        Raises (before anything is sent): :class:`UnsupportedAction` for actions outside the contract/scope/driver,
        :class:`ScopeViolation` for max steps or a target outside the scope.
        """
        args = dict(args or {})
        self._tick()
        self._check_scope(action, args)
        mutating = action in P.MUTATING_ACTIONS
        if mutating:
            if self._steps + 1 > self.scope.max_steps:
                raise ScopeViolation("max_steps_exceeded", f"task scope allows {self.scope.max_steps} mutating steps")
        pre = self._last_sha
        ts = now_iso()
        seq = self._next_seq()
        redacted = _redact_args(args)

        def finish(result: dict[str, Any], post: str | None, ok: bool | None, extra: dict[str, Any] | None = None) -> ActionRecord:
            rec = ActionRecord(seq, MODE_DIRECT, action, redacted, result, pre, post, ok, ts, "driver", extra or {})
            self.records.append(rec)
            return rec

        if action == P.A_WAIT:
            time.sleep(max(0.0, min(float(args.get("seconds", 1.0)), 30.0)))
            return finish({"ok": True, "message": "waited"}, None, None)
        if action == P.A_CAPTURE:
            st = self.capture()
            return finish({"ok": True, "elements": len(st.elements), "degraded": st.degraded, "window_title": st.title}, st.sha, None)
        if action in (P.A_LIST_APPS, P.A_LIST_WINDOWS):
            call = P.build_call(action, args, pid=None, window_id=None, session=self.session_id)
            res = self._send_tool(call)
            return finish({"ok": res.ok, "message": res.message[:300]}, None, None)
        if action == P.A_FOCUS_APP:
            app = args.get("app")
            if not app:
                raise UnsupportedAction(action, "focus_app requires `app`")
            self._check_scope(action, {"app": app})
            self.select_target(process=str(app))
            return finish({"ok": True, "message": f"targeted pid {self._target.pid}"}, None, None)  # type: ignore[union-attr]
        # input actions need an established, in-scope target
        if self._target is None:
            raise ScopeViolation("no_target", "no active target - capture() first")
        token = None
        if args.get("element") is not None and self._state is not None:
            token = next((e.token for e in self._state.elements if e.index == args["element"]), None)
        call = P.build_call(action, args, pid=self._target.pid, window_id=self._target.window_id, session=self.session_id, element_token=token)
        if token and self.tools and not self.supports_input_property(call.tool, "element_token"):
            call = P.build_call(action, args, pid=self._target.pid, window_id=self._target.window_id, session=self.session_id)
        self._precheck(call)                       # static rejection first: nothing is sent (not even a read) for a bad action
        if self.scope.reverify_target:
            self._reverify_target()
            call = P.build_call(action, args, pid=self._target.pid, window_id=self._target.window_id, session=self.session_id,
                                element_token=token if (token and (not self.tools or self.supports_input_property(call.tool, "element_token"))) else None)
        self._steps += 1
        res = self._send_tool(call)
        verdict = res.verdict
        if not res.ok:
            _merge_diags(self.diagnostics, diagnose_text(res.error_text()))
        post_sha, post_ok = None, verdict.postcondition()
        if verdict.code in ("timeout_outcome_unknown", "transport_outcome_unknown") or (res.structured or {}).get("code") in ("timeout_outcome_unknown", "transport_outcome_unknown"):
            post_ok = None          # the input may or may not have landed: unknown, never "failed" and never replayed
        extra: dict[str, Any] = {"tool": call.tool}
        if res.ok and not self._suspect:
            if settle_s:
                time.sleep(settle_s)
            deadline = time.monotonic() + max(0.0, expect_timeout_s)
            while True:
                try:
                    st = self.capture()
                except ScopeViolation as e:
                    extra["post_capture_error"] = e.code
                    break
                post_sha = st.sha
                if not expect:
                    break
                ok, details = evaluate_expect(expect, st, pre, st.sha, verdict)
                post_ok = ok
                extra["expect"] = details
                if ok or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
        rec_result = {"ok": res.ok, "message": res.message[:300], "verdict": verdict.to_json(), "tool": call.tool}
        return finish(rec_result, post_sha, post_ok, extra)

    def _reverify_target(self) -> None:
        if self._target is None:
            return
        tgt = self._target
        wins = {w.window_id: w for w in self.list_windows()}
        w = wins.get(tgt.window_id) if self._target is not None else None
        if self._target is None or w is None or w.pid != tgt.pid:
            self._target, self._state = None, None
            raise ScopeViolation("target_gone", "the target window is no longer present")
        if not self.scope.window_ok(w):
            self._target, self._state = None, None
            raise ScopeViolation("target_scope", f"target window changed outside the scope (now {w.title!r})")
        self._target = w


# ---------------------------------------------------------------------------------------------------------------------
# Recipes (deterministic replay with postconditions - no model calls)
# ---------------------------------------------------------------------------------------------------------------------
EXPECT_TYPES = ("element_present", "element_absent", "tree_contains", "window_title_contains", "screenshot_changed",
                "screenshot_sha", "effect_confirmed")


def _elem_matches(e: P.Element, spec: dict[str, Any]) -> bool:
    if spec.get("role") and e.role.casefold() != str(spec["role"]).casefold():
        return False
    if spec.get("label") is not None and e.label.casefold() != str(spec["label"]).casefold():
        return False
    if spec.get("label_contains") and str(spec["label_contains"]).casefold() not in e.label.casefold():
        return False
    return True


def evaluate_expect(expect: list[dict[str, Any]], st: CaptureState, pre_sha: str | None, post_sha: str | None,
                    verdict: P.ActionVerdict | None) -> tuple[bool, list[dict[str, Any]]]:
    details, all_ok = [], True
    for ex in expect:
        t = ex.get("type")
        if t == "element_present":
            ok = any(_elem_matches(e, ex) for e in st.elements)
        elif t == "element_absent":
            ok = not any(_elem_matches(e, ex) for e in st.elements)
        elif t == "tree_contains":
            ok = str(ex.get("text", "")).casefold() in st.tree.casefold() or any(str(ex.get("text", "")).casefold() in e.label.casefold() for e in st.elements)
        elif t == "window_title_contains":
            ok = str(ex.get("text", "")).casefold() in st.title.casefold()
        elif t == "screenshot_changed":
            ok = post_sha is not None and post_sha != pre_sha
        elif t == "screenshot_sha":
            ok = post_sha is not None and post_sha == ex.get("sha")
        elif t == "effect_confirmed":
            ok = bool(verdict and verdict.postcondition() is True)
        else:
            ok = False
        details.append({"type": t, "ok": ok})
        all_ok = all_ok and ok
    return all_ok, details


@dataclass
class Recipe:
    """A recorded deterministic action sequence. Locators (role/label) are resolved against a fresh UI tree before each
    input; element indexes are never stored because they change between snapshots."""
    name: str
    steps: list[dict[str, Any]]
    target: dict[str, Any] = field(default_factory=dict)       # {window_title_contains, window_title_regex, process_names}
    allow_unchecked: bool = False
    schema: str = RECIPE_SCHEMA
    protocol: str = P.PROTOCOL_VERSION

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Recipe":
        if d.get("schema") != RECIPE_SCHEMA:
            raise HermesBridgeError("bad_recipe", f"recipe schema must be {RECIPE_SCHEMA!r}", affected="schema")
        if d.get("protocol") != P.PROTOCOL_VERSION:
            raise HermesBridgeError("bad_recipe", f"recipe was recorded with protocol {d.get('protocol')!r}; this bridge speaks {P.PROTOCOL_VERSION!r}",
                                    affected="protocol", next_action="re-record the recipe")
        r = cls(name=str(d.get("name") or "recipe"), steps=list(d.get("steps") or []), target=dict(d.get("target") or {}),
                allow_unchecked=bool(d.get("allow_unchecked", False)))
        r.validate()
        return r

    def validate(self) -> None:
        if not self.steps:
            raise HermesBridgeError("bad_recipe", "recipe has no steps", affected="steps")
        for i, s in enumerate(self.steps):
            a = s.get("action")
            if a not in P.COMPUTER_USE_ACTIONS:
                raise UnsupportedAction(str(a), f"recipe step {i}: not in the computer_use action enum")
            for ex in s.get("expect") or []:
                if ex.get("type") not in EXPECT_TYPES:
                    raise HermesBridgeError("bad_recipe", f"recipe step {i}: unknown expect type {ex.get('type')!r}", affected=f"steps[{i}].expect")
            if a in P.MUTATING_ACTIONS and a != P.A_FOCUS_APP and not s.get("expect") and not self.allow_unchecked:
                raise HermesBridgeError("bad_recipe", f"recipe step {i} ({a}) has no postcondition (expect)", affected=f"steps[{i}]",
                                        next_action="add an expect list, or set allow_unchecked on the recipe")
        _assert_no_credentials({k: v for k, v in self.to_dict().items() if k != "steps"}, what="recipe")

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "protocol": self.protocol, "name": self.name, "target": self.target,
                "allow_unchecked": self.allow_unchecked, "steps": self.steps}

    def sha256(self) -> str:
        return sha256_bytes(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def scope(self, allowed: tuple[str, ...] | None = None) -> Scope:
        acts = allowed or tuple(sorted({s["action"] for s in self.steps} | {P.A_CAPTURE}))
        return Scope(acts, self.target.get("window_title_contains"), self.target.get("window_title_regex"),
                     tuple(self.target.get("process_names") or ()), max(1, sum(1 for s in self.steps if s["action"] in P.MUTATING_ACTIONS)))


def run_recipe(session: DirectDriverSession, recipe: Recipe) -> tuple[str, str | None]:
    """Execute *recipe* on an open direct session. Returns ``(status, error)``. Stops at the first failed locator or
    postcondition; an input is never re-sent."""
    session.select_target()
    session.act(P.A_CAPTURE)
    for i, step in enumerate(recipe.steps):
        action = step["action"]
        args = dict(step.get("args") or {})
        if action in P.INPUT_ACTIONS and (step.get("locator") or step.get("target")):
            loc = step.get("locator") or step.get("target")
            if "x" in loc and "y" in loc:
                args["coordinate"] = [int(loc["x"]), int(loc["y"])]
            else:
                st = session._state
                matches = [e for e in (st.elements if st else []) if _elem_matches(e, loc)]
                nth = loc.get("nth")
                if not matches:
                    return "postcondition_failed", f"step {i} ({action}): locator {loc} matched no element; nothing was sent"
                if nth is None and len(matches) > 1:
                    return "postcondition_failed", f"step {i} ({action}): locator {loc} is ambiguous ({len(matches)} matches); add nth"
                args["element"] = matches[int(nth or 0)].index
        rec = session.act(action, args, expect=step.get("expect") or None, settle_s=float(step.get("settle_s", 0)),
                          expect_timeout_s=float(step.get("expect_timeout_s", 0)))
        rec.extra["step"] = i
        if not rec.result.get("ok", True):
            return "failed", f"step {i} ({action}) failed: {rec.result.get('message') or rec.result.get('verdict')}"
        if step.get("expect") and rec.postcondition_ok is not True:
            return "postcondition_failed", f"step {i} ({action}) postcondition not met: {rec.extra.get('expect')}"
    return "completed", None


# ---------------------------------------------------------------------------------------------------------------------
# Agent-session stream parsing (hermes chat --format stream-json)
# ---------------------------------------------------------------------------------------------------------------------
_TOOL_OUTPUT_CAP = 5000   # hermes_cli/stream_json.py:15 - outputs are truncated by Hermes itself


def _grab(rx: str, text: str) -> str | None:
    m = re.search(rx, text)
    return m.group(1) if m else None


def extract_result_fields(output: str) -> dict[str, Any]:
    """Fields of a ``computer_use`` tool_result ``output``. JSON when the result was text-only; for multimodal captures
    Hermes str()s a dict and truncates at 5000 chars, so fall back to regex extraction and report what is missing."""
    output = output or ""
    with contextlib.suppress(ValueError):
        obj = json.loads(output)
        if isinstance(obj, dict):
            meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
            obj.setdefault("screenshot_path", meta.get("screenshot_path"))
            return obj
    f: dict[str, Any] = {}
    for key in ("screenshot_path", "effect", "code", "app", "window_title"):
        v = _grab(r"""['"]%s['"]\s*:\s*['"]([^'"]*)['"]""" % key, output)
        if v is not None:
            f[key] = v
    ok = _grab(r"""['"]ok['"]\s*:\s*(True|False|true|false)""", output)
    if ok is not None:
        f["ok"] = ok.lower() == "true"
    n = _grab(r"""['"]total_elements['"]\s*:\s*(\d+)""", output)
    if n is not None:
        f["total_elements"] = int(n)
    f["_truncated_repr"] = True
    return f


class StreamParser:
    """Turns Hermes' JSONL events into scoped, hash-chained action records and detects scope violations."""

    def __init__(self, task: HermesTask, profile_dir: Path, started_ms: float, *, max_raw_events: int = 2000):
        self.task = task
        self.scope = task.scope()
        self.profile_dir = profile_dir
        self.started_ms = started_ms
        self.init: dict[str, Any] = {}
        self.text: list[str] = []
        self.result: dict[str, Any] | None = None
        self.violation: dict[str, Any] | None = None
        self.other_tools: Counter[str] = Counter()
        self.diagnostics: list[dict[str, Any]] = []
        self.raw_events: list[dict[str, Any]] = []
        self.bad_lines = 0
        self.steps = 0
        self._raw: list[dict[str, Any]] = []         # per computer_use call, filled live, finalized later
        self._pending: dict[str, dict[str, Any]] = {}
        self._max_raw = max_raw_events

    def _violate(self, code: str, message: str, seq: int | None = None) -> None:
        if self.violation is None:
            self.violation = {"code": code, "message": message, "seq": seq}
            self.diagnostics.append(diag("scope_violation", "error", message, next_action="task was terminated; review the task scope", violation_code=code))

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            self.bad_lines += 1
            return
        if not isinstance(ev, dict):
            self.bad_lines += 1
            return
        if len(self.raw_events) < self._max_raw:
            self.raw_events.append(self._redact_event(ev))
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            self.init = {k: ev.get(k) for k in ("model", "session_id")}
        elif t == "text":
            if sum(map(len, self.text)) < 200_000:
                self.text.append(str(ev.get("text", "")))
        elif t == "tool_use":
            self._on_tool_use(ev)
        elif t == "tool_result":
            self._on_tool_result(ev)
        elif t == "result":
            self.result = ev

    @staticmethod
    def _redact_event(ev: dict[str, Any]) -> dict[str, Any]:
        e = dict(ev)
        if isinstance(e.get("input"), dict):
            e["input"] = _redact_args(e["input"])
        if isinstance(e.get("output"), str) and len(e["output"]) > 600:
            e["output"] = e["output"][:600] + "...[trimmed in evidence]"
        return e

    def _on_tool_use(self, ev: dict[str, Any]) -> None:
        name = str(ev.get("name") or "unknown")
        inp = ev.get("input") if isinstance(ev.get("input"), dict) else {}
        if name != "computer_use":
            self.other_tools[name] += 1
            if not name.startswith("mcp_rebuild_studio_"):
                msg = f"the session called tool {name!r}, which is outside the computer-use scope"
                if self.task.strict_tools:
                    self._violate("tool_outside_scope", msg)
                else:
                    _merge_diags(self.diagnostics, [diag("tool_outside_scope", "warning", msg)])
            return
        self.steps += 1
        action = str(inp.get("action") or "").strip().lower()
        seq = self.steps
        if action not in P.COMPUTER_USE_ACTIONS:
            self._violate("unsupported_action", f"unknown computer_use action {action!r}", seq)
            _merge_diags(self.diagnostics, [diag("unsupported_action", "error", f"unknown computer_use action {action!r}")])
        elif action not in self.scope.allowed_actions:
            self._violate("action_not_allowed", f"action {action!r} is not in the task's allowed actions", seq)
        if self.steps > self.scope.max_steps:
            self._violate("max_steps_exceeded", f"more than {self.scope.max_steps} computer_use actions", seq)
        app = inp.get("app")
        if isinstance(app, str) and app.strip():
            low = app.strip().lower()
            if self.scope.has_target and low in _FULL_SCREEN:
                self._violate("target_scope", f"app={app!r} captures beyond the scoped target", seq)
            elif self.scope.process_names and low not in _FULL_SCREEN and not self.scope.process_ok(app):
                self._violate("target_scope", f"app={app!r} is not an allowed process", seq)
        raw = {"seq": seq, "action": action, "args": _redact_args(inp), "ts_ms": ev.get("timestamp") or self.started_ms,
               "result_ms": None, "fields": {}, "output": None, "is_error": None, "duration_ms": None}
        self._raw.append(raw)
        self._pending[str(ev.get("tool_call_id") or f"#{len(self._raw)}")] = raw

    def _on_tool_result(self, ev: dict[str, Any]) -> None:
        if ev.get("name") != "computer_use":
            return
        key = str(ev.get("tool_call_id") or "")
        raw = self._pending.pop(key, None) if key in self._pending else None
        if raw is None and self._pending:  # no id on the wire: FIFO
            raw = self._pending.pop(next(iter(self._pending)))
        if raw is None:
            return
        out = str(ev.get("output") or "")
        fields = extract_result_fields(out)
        raw.update(output=out[:_TOOL_OUTPUT_CAP], fields=fields, is_error=bool(ev.get("is_error")),
                   duration_ms=ev.get("duration_ms"), result_ms=ev.get("timestamp"))
        raw["has_image"] = "data:image" in out or bool(fields.get("screenshot_path"))
        if raw["is_error"] or fields.get("ok") is False or "error" in fields:
            _merge_diags(self.diagnostics, diagnose_text(out))
        if fields.get("total_elements") == 0 or "0 interactable element" in out:
            _merge_diags(self.diagnostics, [d for d in diagnose_text("0 interactable element(s)")])
        _merge_diags(self.diagnostics, [d for d in diagnose_text(out) if d["code"] in ("desktop_locked", "no_windows_found", "unsupported_action", "approval_blocked", "uipi_elevated_target", "human_has_control")])
        # target mismatch seen in a parseable capture result
        title, app = fields.get("window_title"), fields.get("app")
        if raw["action"] == P.A_CAPTURE:
            if self.scope.window_title_contains or self.scope.window_title_regex:
                if isinstance(title, str) and title and not self.scope.title_ok(title):
                    self._violate("target_window_mismatch", f"captured window {title!r} is outside the scoped target", raw["seq"])
            if isinstance(app, str) and app and self.scope.process_names and not self.scope.process_ok(app):
                self._violate("target_window_mismatch", f"captured app {app!r} is outside the scoped processes", raw["seq"])

    # -- finalization ---------------------------------------------------------------------------------------------
    def _inside_profile(self, p: Path) -> bool:
        try:
            p.resolve().relative_to(self.profile_dir.resolve())
            return p.is_file()
        except (ValueError, OSError):
            return False

    def _scan_cache(self) -> list[Path]:
        found: list[Path] = []
        for sub in ("cache/images", "image_cache"):   # tools/computer_use/tool.py:823-829 (get_hermes_dir legacy fallback)
            d = self.profile_dir / sub
            with contextlib.suppress(OSError):
                for f in d.glob("computer_use_*.*"):
                    if f.is_file() and f.stat().st_mtime * 1000 >= self.started_ms - 2000:
                        found.append(f)
        return sorted(found, key=lambda f: f.stat().st_mtime)

    def finalize(self) -> tuple[list[ActionRecord], dict[str, bytes]]:
        shots: dict[str, bytes] = {}
        claimed: set[str] = set()
        for raw in self._raw:
            p = raw["fields"].get("screenshot_path")
            if isinstance(p, str) and p:
                claimed.add(str(Path(p)))
        spare = [f for f in self._scan_cache() if str(f) not in claimed]
        chain: list[ActionRecord] = []
        last_sha: str | None = None
        own: list[str | None] = []
        sources: list[str | None] = []
        for raw in self._raw:
            path = raw["fields"].get("screenshot_path")
            source = None
            fpath: Path | None = Path(path) if isinstance(path, str) and path else None
            if fpath is None and raw.get("has_image") and spare:
                fpath, source = spare.pop(0), "cache_scan_ordered"
            elif fpath is not None:
                source = "tool_result_path"
            sha = None
            if fpath is not None and self._inside_profile(fpath):
                with contextlib.suppress(OSError):
                    if fpath.stat().st_size <= 32 * 1024 * 1024:
                        data = fpath.read_bytes()
                        sha = sha256_bytes(data)
                        shots[sha] = data
            own.append(sha)
            sources.append(source if sha else None)
        for i, raw in enumerate(self._raw):
            f = raw["fields"]
            verdict = f.get("verdict") if isinstance(f.get("verdict"), dict) else {}
            effect = f.get("effect") if isinstance(f.get("effect"), str) else None
            verified = f.get("verified") if isinstance(f.get("verified"), bool) else None
            v = P.ActionVerdict(effect=effect, verified=verified, code=f.get("code") if isinstance(f.get("code"), str) else None)
            ok_field = f.get("ok")
            ok = (not raw["is_error"]) and ok_field is not False if raw["output"] is not None else None
            post_ok = v.postcondition() if raw["output"] is not None else None
            if raw["action"] in (P.A_CAPTURE, P.A_WAIT, P.A_LIST_APPS, P.A_LIST_WINDOWS):
                post_ok = None
            pre = last_sha
            post = own[i]
            extra: dict[str, Any] = {"tool_call": "computer_use"}
            if sources[i]:
                extra["screenshot_source"] = sources[i]
            if post is None and i + 1 < len(self._raw) and own[i + 1] and self._raw[i + 1]["action"] == P.A_CAPTURE:
                post = own[i + 1]
                extra["post_screenshot_inferred"] = True
            if raw["duration_ms"] is not None:
                extra["duration_ms"] = raw["duration_ms"]
            if f.get("_truncated_repr"):
                extra["result_truncated"] = True
            result = {"ok": ok, "effect": effect, "code": v.code, "verdict": verdict or None,
                      "message": (raw["output"] or "")[:300] if raw["output"] is not None else "no tool_result received"}
            chain.append(ActionRecord(raw["seq"], MODE_AGENT, raw["action"], raw["args"], result, pre, post, post_ok,
                                      iso_from_ms(float(raw["ts_ms"])), "hermes_stream", extra))
            if own[i]:
                last_sha = own[i]
        return chain, shots

    @property
    def final_text(self) -> str:
        if self.result and self.result.get("text"):
            return str(self.result["text"])
        return "".join(self.text)


# ---------------------------------------------------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------------------------------------------------
class HermesBridge:
    def __init__(self, data_dir: Path | str | None = None, *, hermes_command: str | None = None,
                 env: dict[str, str] | None = None, probe: SystemProbe | None = None, expected_host: str | None = None,
                 mcp_command: list[str] | None = None):
        self.data_dir = Path(data_dir) if data_dir else get_settings().data_dir
        self.hermes_command = hermes_command
        self.env = dict(os.environ if env is None else env)
        self.probe = probe or SystemProbe()
        self.expected_host = expected_host
        self.mcp_command = mcp_command
        self._active: dict[str, threading.Event] = {}
        self._active_lock = threading.Lock()

    # -- paths ------------------------------------------------------------------------------------------------------
    @property
    def state_dir(self) -> Path:
        return self.data_dir / "hermes"

    @property
    def pairing_path(self) -> Path:
        return self.state_dir / "pairing.json"

    def _which(self, name: str) -> str | None:
        return shutil.which(name, path=self.env.get("PATH"))

    # -- detection ----------------------------------------------------------------------------------------------------
    def detect_installation(self) -> dict[str, Any]:
        system = self.probe.system()
        exe = self.hermes_command or self._which("hermes")
        home = default_hermes_home(self.env, system)
        root = hermes_root_of(home)
        out: dict[str, Any] = {
            "state": "missing", "hermes_path": None, "version": None, "release_date": None, "platform": system,
            "hermes_home": str(home), "profile_dir": str(home), "profile_exists": home.is_dir(),
            "config_path": str(home / "config.yaml"), "config_exists": (home / "config.yaml").is_file(),
            "profiles": list_profiles(root), "next_action": None,
        }
        if not exe:
            out["next_action"] = (f"Install Hermes yourself (Rebuild Studio never runs the installer): in PowerShell `{INSTALL_PS1}`"
                                  if system == "Windows" else f"Install Hermes yourself (Rebuild Studio never runs the installer): `{INSTALL_SH}`") \
                + "; then run `hermes setup` to create a profile and choose a model provider."
            return out
        out["hermes_path"] = exe
        try:
            cp = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=30, stdin=subprocess.DEVNULL, env=self.env)
            text = (cp.stdout or cp.stderr or "").strip()
            m = re.search(r"Hermes Agent v(\S+)(?:\s*\(([^)]*)\))?", text)   # hermes_cli/_startup_fast.py:202
            if cp.returncode != 0 or not m:
                out.update(state="broken", error=f"`hermes --version` gave exit {cp.returncode}: {text[:200]!r}",
                           next_action="run `hermes doctor` and reinstall if it fails")
                return out
            out.update(state="installed", version=m.group(1), release_date=m.group(2))
        except (OSError, subprocess.SubprocessError) as e:
            out.update(state="broken", error=str(e), next_action="run `hermes doctor` and reinstall if it fails")
            return out
        if not out["config_exists"]:
            out["next_action"] = "Run `hermes setup` to create a profile (config.yaml) and pick a model provider."
        return out

    def detect_driver(self, *, via_hermes: bool = True) -> dict[str, Any]:
        """Locate cua-driver and check Hermes' runtime contract (min 0.20.0, ``manifest`` verb, mcp/serve/stop args)."""
        out: dict[str, Any] = {"available": False, "path": None, "source": None, "version": None, "contract_ready": False,
                               "contract_reason": "cua-driver is not installed", "mcp_invocation": None, "subcommands": [],
                               "next_action": "Run `hermes computer-use install` (or install cua-driver from github.com/trycua/cua releases and set HERMES_CUA_DRIVER_CMD)."}
        cand: list[tuple[str, str]] = []
        ov = (self.env.get(DRIVER_ENV_OVERRIDE) or "").strip()
        if ov:
            exp = os.path.expanduser(ov)
            cand.append((shutil.which(exp, path=self.env.get("PATH")) or exp, DRIVER_ENV_OVERRIDE))
        w = self._which("cua-driver")
        if w:
            cand.append((w, "PATH"))
        home = Path(self.env.get("HOME") or self.env.get("USERPROFILE") or Path.home())
        for rel in (".cua-driver/packages/current/cua-driver", ".cua-driver/packages/current/cua-driver.exe"):
            if (home / rel).is_file():
                cand.append((str(home / rel), "~/.cua-driver/packages/current"))
        if not cand and via_hermes:
            hx = self.hermes_command or self._which("hermes")
            if hx:
                with contextlib.suppress(Exception):
                    cp = subprocess.run([hx, "computer-use", "status"], capture_output=True, text=True, encoding="utf-8",
                                        errors="replace", timeout=30, stdin=subprocess.DEVNULL, env=self.env)
                    m = re.search(r"cua-driver: installed at (.*?)(?: \[custom binary from HERMES_CUA_DRIVER_CMD\])?(?: \([^()]*\))?\s*$",
                                  cp.stdout or "", re.M)
                    if m and Path(m.group(1)).exists():
                        cand.append((m.group(1), "hermes computer-use status"))
        if not cand:
            return out
        path, source = cand[0]
        out.update(path=path, source=source, available=Path(path).exists())
        if not out["available"]:
            out["contract_reason"] = f"{path} does not exist"
            return out
        try:
            cp = subprocess.run([path, "manifest"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=30 if self.probe.system() == "Windows" else 10, stdin=subprocess.DEVNULL,
                                env=sanitized_driver_env(self.env))
            manifest = json.loads((cp.stdout or "").strip()) if cp.returncode == 0 and (cp.stdout or "").strip() else None
        except (OSError, subprocess.SubprocessError, ValueError):
            manifest = None
        if not isinstance(manifest, dict):
            out["contract_reason"] = "driver manifest is missing or invalid (Hermes requires a driver with the `manifest` verb)"
            with contextlib.suppress(Exception):
                cp = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL, env=sanitized_driver_env(self.env))
                m = re.search(r"(\d+\.\d+\.\d+(?:[-+][\w.]+)?)", (cp.stdout or "") + (cp.stderr or ""))
                out["version"] = m.group(1) if m else None
            out["next_action"] = "Update cua-driver: `hermes computer-use install --upgrade`"
            return out
        out["version"] = str(manifest.get("binary_version") or "") or None
        inv = manifest.get("mcp_invocation") if isinstance(manifest.get("mcp_invocation"), dict) else None
        out["mcp_invocation"] = inv
        subs = {c["name"]: sorted(a["name"] for a in c.get("args") or [] if isinstance(a, dict) and isinstance(a.get("name"), str))
                for c in manifest.get("subcommands") or [] if isinstance(c, dict) and isinstance(c.get("name"), str)}
        out["subcommands"] = sorted(subs)
        reason = self._contract_reason(manifest, subs)
        out.update(contract_ready=not reason, contract_reason=reason)
        out["next_action"] = None if not reason else "Update cua-driver: `hermes computer-use install --upgrade`"
        return out

    @staticmethod
    def _contract_reason(manifest: dict[str, Any], subs: dict[str, list[str]]) -> str:
        """Mirror of cua_backend_driver.py:138-158 (_manifest_contract_reason)."""
        m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", str(manifest.get("binary_version") or "").strip())
        if not m:
            return "driver manifest does not report a semantic version"
        if tuple(int(x) for x in m.groups()) < P.PINNED_SOURCE["runtime_contract_min"]:  # type: ignore[operator]
            return "Hermes computer use requires cua-driver 0.20.0 or newer"
        inv = manifest.get("mcp_invocation")
        if not (isinstance(inv, dict) and isinstance(inv.get("args"), list) and all(isinstance(a, str) for a in inv["args"])):
            return "driver manifest does not provide an MCP launch command"
        need = {"mcp": {"--socket", "--grant"}, "serve": {"--socket", "--permission-mode", "--capability-manifest", "--approve-capability-manifest", "--embedded"}, "stop": {"--socket"}}
        missing = [f"{c} {a}" for c, req in need.items() for a in sorted(req - set(subs.get(c, [])))]
        return ("driver manifest is missing: " + ", ".join(missing)) if missing else ""

    # -- pairing --------------------------------------------------------------------------------------------------------
    def pairing(self) -> dict[str, Any] | None:
        try:
            d = json.loads(self.pairing_path.read_text("utf-8"))
            return d if isinstance(d, dict) and d.get("profile_path") else None
        except (OSError, ValueError):
            return None

    def pair(self, profile_path: str | os.PathLike | None = None) -> dict[str, Any]:
        """Record the user-controlled Hermes profile to use. Never creates or modifies a profile."""
        inst = self.detect_installation()
        if inst["state"] == "missing":
            raise HermesBridgeError("hermes_missing", "Hermes is not installed on this machine", affected="hermes",
                                    next_action=inst["next_action"])
        if profile_path is None:
            path = Path(inst["profile_dir"])
        else:
            path = Path(os.path.expandvars(os.path.expanduser(str(profile_path))))
            if not path.is_absolute():
                raise HermesBridgeError("bad_profile_path", "profile_path must be absolute", affected="profile_path")
        try:
            path = path.resolve(strict=True)
        except OSError as e:
            raise HermesBridgeError("profile_not_found", f"Hermes profile directory not found: {path}", affected=str(path),
                                    next_action="run `hermes setup` (or `hermes profile create <name>`) yourself, then pair again") from e
        if not path.is_dir():
            raise HermesBridgeError("bad_profile_path", f"{path} is not a directory", affected=str(path))
        if _is_inside(path, self.data_dir):
            raise HermesBridgeError("bad_profile_path", "the profile must not live inside Rebuild Studio's own data directory", affected=str(path))
        if not _looks_like_profile(path):
            raise HermesBridgeError("not_a_hermes_profile", f"{path} has none of {PROFILE_MARKERS}; it does not look like a Hermes profile",
                                    affected=str(path), next_action="run `hermes setup` first, or pass the directory `hermes config path` reports")
        rec = {"schema": BRIDGE_SCHEMA, "profile_path": str(path), "profile_name": path.name if path.parent.name == "profiles" else "default",
               "hermes_path": inst["hermes_path"], "hermes_version": inst["version"], "host": self.probe.hostname(),
               "platform": self.probe.system(), "paired_at": now_iso(), "config_exists": (path / "config.yaml").is_file()}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.pairing_path, json.dumps(rec, indent=1).encode("utf-8"))
        return rec

    # -- MCP registration --------------------------------------------------------------------------------------------------
    def mcp_entry(self) -> dict[str, Any]:
        cmd = self.mcp_command or ([w] if (w := self._which("rebuild-mcp")) else [sys.executable, "-m", "rebuild_controller.mcp.server"])
        # Hermes only forwards env keys listed here (native-mcp.md:169), so the data dir must be explicit. No secrets.
        return {"command": cmd[0], "args": list(cmd[1:]), "env": {"REBUILD_STUDIO_DATA": str(self.data_dir)},
                "timeout": 120, "connect_timeout": 60}

    def register_mcp(self, dry_run: bool = True, *, name: str = DEFAULT_MCP_SERVER_NAME) -> dict[str, Any]:
        """Merge Rebuild Studio's MCP server into the paired profile's ``config.yaml`` ``mcp_servers``.

        Preserves everything else; writes a timestamped backup first; returns a unified diff. Idempotent: a second run
        changes nothing and creates no backup. ``dry_run=True`` (default) never touches the file.
        """
        pr = self.pairing()
        if not pr:
            raise HermesBridgeError("not_paired", "no Hermes profile is paired", next_action="POST /hermes/pair first (or call pair())")
        profile = Path(pr["profile_path"])
        cfg = profile / "config.yaml"
        if not cfg.is_file():
            raise HermesBridgeError("profile_not_initialized", f"{cfg} does not exist", affected=str(cfg),
                                    next_action="run `hermes setup` in that profile; Rebuild Studio will not create config.yaml for you")
        raw = cfg.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        old = raw[3:].decode("utf-8") if bom else raw.decode("utf-8")
        entry = self.mcp_entry()
        new, preserved = merge_mcp_server(old, name, entry)
        changed = new != old
        diff = "".join(l if l.endswith("\n") else l + "\n" for l in difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile=f"{cfg.name} (current)", tofile=f"{cfg.name} (with {name})", lineterm="")) if changed else ""
        out: dict[str, Any] = {"dry_run": dry_run, "changed": changed, "already_registered": not changed, "profile_path": str(profile),
                               "config_path": str(cfg), "server_name": name, "entry": entry, "diff": diff,
                               "comments_preserved": preserved, "backup_path": None, "written": False}
        if dry_run or not changed:
            return out
        backup = cfg.with_name(f"{cfg.name}.rebuild-studio-backup-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:4]}")
        shutil.copy2(cfg, backup)
        if backup.read_bytes() != raw:  # pragma: no cover - defensive
            raise HermesBridgeError("backup_failed", "backup does not match the original; config left untouched", affected=str(backup))
        _atomic_write(cfg, (b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8"))
        out.update(backup_path=str(backup), written=True)
        return out

    # -- status ---------------------------------------------------------------------------------------------------------
    def status(self, *, target_pid: int | None = None, probe_tools: bool = False) -> dict[str, Any]:
        probe = self.probe
        system = probe.system()
        host = probe.hostname()
        diags: list[dict[str, Any]] = []
        inst = self.detect_installation()
        pr = self.pairing()
        driver = self.detect_driver(via_hermes=inst["state"] == "installed")
        session: dict[str, Any] = {"kind": "windows" if system == "Windows" else "posix", "interactive": None, "session_id": None,
                                   "is_session_0": False, "desktop_locked": None, "display": probe.display_env(), "has_display": bool(probe.display_env())}
        if system == "Windows":
            ws = probe.windows_session() or {}
            sid = ws.get("session_id")
            session.update({k: v for k, v in ws.items() if k in ("session_id", "window_station", "input_desktop_accessible", "lock_probe_error")})
            session["is_session_0"] = sid == 0
            station_ok = ws.get("window_station", "WinSta0") == "WinSta0"
            session["interactive"] = bool(sid) and station_ok
            if sid == 0:
                diags.append(diag("session_0", "error", "The bridge runs in Windows Session 0 (service/SSH): there is no interactive desktop to drive.",
                                  next_action="Start Rebuild Studio from the logged-in desktop session; for SSH use the cua-driver autostart logon task (`computer_use.autostart: true` in your Hermes profile) - see docs/HERMES.md gate G3."))
            elif not station_ok:
                diags.append(diag("no_interactive_windows_session", "error", f"Window station {ws.get('window_station')!r} is not the interactive WinSta0.",
                                  next_action="Run from the interactive desktop."))
            if ws.get("input_desktop_accessible") is False and sid != 0:
                session["desktop_locked"] = True
                diags.append(diag("desktop_locked", "error", "The input desktop is not accessible: the workstation is probably locked (heuristic: OpenInputDesktop failed).",
                                  next_action="Unlock the desktop and retry.", win32_error=ws.get("lock_probe_error")))
            elif ws.get("input_desktop_accessible") is True:
                session["desktop_locked"] = False
            if target_pid is not None:
                mine, theirs = probe.process_elevation(None) or {}, probe.process_elevation(target_pid) or {}
                if theirs.get("access_denied") or (theirs.get("elevated") and not mine.get("elevated")):
                    diags.append(diag("uipi_elevated_target", "error",
                                      "The target process is elevated (or its token is unreadable) while this process is not; Windows UIPI will block synthetic input.",
                                      next_action="Run the target app unelevated, or run Hermes/cua-driver elevated as well.", target_pid=target_pid))
        else:
            session["interactive"] = False
            diags.append(diag("no_interactive_windows_session", "warning",
                              f"no interactive Windows session: the bridge host is {system}, so Windows desktop automation cannot run here "
                              "(cua-driver also supports Linux/macOS, but the Windows gates in docs/HERMES.md are untested on this host).",
                              next_action="Run the controller on the Windows machine that owns the desktop."))
            if system == "Linux" and not session["has_display"]:
                diags.append(diag("no_display", "warning", "Neither DISPLAY nor WAYLAND_DISPLAY is set; cua-driver has no display to reach.",
                                  next_action="Run inside a desktop session (or provide a display)."))
        # machine identity
        paired_host = (pr or {}).get("host")
        machine = {"hostname": host, "paired_host": paired_host, "expected_host": self.expected_host, "match": None}
        for other, what in ((paired_host, "the host the profile was paired on"), (self.expected_host, "the host Hermes is declared to run on")):
            if other and other.lower() != host.lower():
                machine["match"] = False
                diags.append(diag("wrong_machine", "error", f"This bridge runs on {host!r} but {what} is {other!r}; a Hermes session started here would use the wrong machine's profile and desktop.",
                                  next_action="Run Rebuild Studio on the same machine as Hermes (or re-pair here).", bridge_host=host, hermes_host=other))
                break
        else:
            machine["match"] = True if (paired_host or self.expected_host) else None
        if inst["state"] == "missing":
            diags.append(diag("hermes_missing", "warning", "Hermes is not installed; agent-session mode is unavailable (direct cua-driver mode may still work).", next_action=inst["next_action"]))
        elif inst["state"] == "broken":
            diags.append(diag("hermes_broken", "error", inst.get("error", "hermes failed"), next_action=inst["next_action"]))
        elif not pr:
            diags.append(diag("profile_not_paired", "info", "No Hermes profile is paired yet.", next_action="POST /hermes/pair"))
        if pr and not Path(pr["profile_path"]).is_dir():
            diags.append(diag("profile_missing", "error", f"The paired profile {pr['profile_path']} no longer exists.", next_action="pair again"))
        if not driver["available"]:
            diags.append(diag("driver_missing", "warning", "cua-driver was not found; computer use is unavailable.", next_action=driver["next_action"]))
        elif not driver["contract_ready"]:
            diags.append(diag("driver_contract_unmet", "error", driver["contract_reason"], next_action=driver["next_action"]))
        if pr and (Path(pr["profile_path"]) / "config.yaml").is_file():
            diags.extend(self._approval_diagnostics(Path(pr["profile_path"]) / "config.yaml"))
        caps: dict[str, Any] = {"actions": list(P.COMPUTER_USE_ACTIONS), "mutating_actions": list(P.MUTATING_ACTIONS),
                                "driver_tools": sorted(P.TOOL_MESSAGES), "live_tools": None, "capability_version": None,
                                "protocol": P.PROTOCOL_VERSION, "subcommands": driver["subcommands"]}
        if probe_tools and driver["available"] and driver["contract_ready"]:
            try:
                with self.direct_driver(Scope(allowed_actions=P.READ_ONLY_ACTIONS)) as s:
                    caps.update(live_tools=sorted(s.tools), capability_version=s.capability_version)
            except Exception as e:  # probing must never crash status
                diags.append(diag("driver_probe_failed", "error", f"could not open a cua-driver MCP session: {e}"))
        agent_ready = inst["state"] == "installed" and bool(pr) and bool(machine["match"] is not False) and driver["contract_ready"]
        direct_ready = driver["available"] and driver["contract_ready"] and machine["match"] is not False
        return {"schema": BRIDGE_SCHEMA, "host": host, "platform": system, "machine": machine, "session": session, "hermes": inst,
                "pairing": pr, "driver": driver, "driver_version": driver["version"], "capabilities": caps,
                "modes": {MODE_AGENT: {"ready": agent_ready, "description": integration_note(MODE_AGENT)},
                          MODE_DIRECT: {"ready": direct_ready, "description": integration_note(MODE_DIRECT)}},
                "diagnostics": diags}

    def _approval_diagnostics(self, cfg: Path) -> list[dict[str, Any]]:
        try:
            data = _yaml().safe_load(cfg.read_text("utf-8")) or {}
        except Exception:
            return [diag("config_unreadable", "warning", f"{cfg} could not be parsed; approval policy unknown.")]
        ap = data.get("approvals") if isinstance(data, dict) and isinstance(data.get("approvals"), dict) else {}
        cu = data.get("computer_use") if isinstance(data, dict) and isinstance(data.get("computer_use"), dict) else {}
        if cu.get("permission_mode") == "bounded":
            return []
        if (ap.get("single_query_mode") or "deny") != "approve":
            return [diag("approval_blocks_unattended", "warning",
                         "In non-interactive (`chat -q`) runs Hermes defaults to approvals.single_query_mode: deny, which blocks every desktop input action. "
                         "Agent-session tasks will only be able to capture until you opt in.",
                         next_action="In YOUR profile set approvals.single_query_mode: approve (trusting the task scope) or computer_use.permission_mode: bounded with a reviewed capability_manifest. Rebuild Studio never edits this and never passes --yolo.")]
        return []

    # -- direct driver --------------------------------------------------------------------------------------------------
    def direct_driver(self, scope: Scope | None = None, *, checkpoint: Callable[[], None] | None = None,
                      call_timeout: float = 30.0) -> DirectDriverSession:
        """Open a ``cua_driver_direct`` session (not started; use as a context manager). NOT a Hermes integration."""
        driver = self.detect_driver(via_hermes=False)
        if not driver["available"]:
            raise HermesBridgeError("driver_missing", "cua-driver was not found", affected="cua-driver", next_action=driver["next_action"])
        if not driver["contract_ready"]:
            raise HermesBridgeError("driver_contract_unmet", driver["contract_reason"], affected=driver["path"], next_action=driver["next_action"])
        inv = driver.get("mcp_invocation") or {}
        cmd0 = inv.get("command") if isinstance(inv.get("command"), str) and (os.sep in inv["command"] or (os.altsep and os.altsep in inv["command"])) else driver["path"]
        argv = [cmd0, *(inv.get("args") if isinstance(inv.get("args"), list) else ["mcp"])]
        return DirectDriverSession(argv, env=self.env, scope=scope, checkpoint=checkpoint, call_timeout=call_timeout)

    def replay(self, recipe: Recipe | dict[str, Any], *, ctx: Any = None, cases: Any = None, case_id: str | None = None,
               connected: Callable[[], bool] | None = None, call_timeout: float = 30.0) -> TaskResult:
        """Replay a recorded recipe through the cua-driver (mode ``cua_driver_direct``). Zero model calls."""
        rec = recipe if isinstance(recipe, Recipe) else Recipe.from_dict(recipe)
        rec.validate()
        task_id = new_id("hrep")
        started = now_iso()
        status, error = "failed", None
        gone = {"v": False}

        def checkpoint() -> None:
            if ctx is not None:
                ctx.heartbeat()
            if connected is not None and not connected():
                gone["v"] = True
                raise Cancelled("client disconnected")

        sess = self.direct_driver(rec.scope(), checkpoint=checkpoint, call_timeout=call_timeout)
        try:
            with sess:
                status, error = run_recipe(sess, rec)
        except Cancelled as e:
            sess.close()
            if gone["v"]:
                status, error = "disconnected", str(e)
            else:
                raise
        except (ScopeViolation, UnsupportedAction) as e:
            sess.close()
            status, error = ("scope_violation" if isinstance(e, ScopeViolation) else "unsupported_action"), str(e)
        finally:
            sess.close()
        res = TaskResult(task_id=task_id, mode=MODE_DIRECT, status=status, records=[r.to_json() for r in sess.records],
                         diagnostics=sess.diagnostics, error=error, started_at=started, finished_at=now_iso(),
                         command=sess.command, extra={"recipe_sha256": rec.sha256(), "recipe_name": rec.name, "model_calls": 0,
                                                      "driver_calls_sent": len(sess.sent_calls), "capability_version": sess.capability_version})
        if cases is not None and case_id:
            self._store_evidence(cases, case_id, res, sess.screenshots, inputs={"recipe": rec.sha256()})
        return res

    # -- agent session ------------------------------------------------------------------------------------------------
    def _profile_cli_args(self, profile: Path) -> tuple[list[str], dict[str, str]]:
        """``-p <name>`` + ``HERMES_HOME`` so Hermes cannot silently switch to a sticky ``active_profile``
        (hermes_cli/main.py:600-607)."""
        name = profile.name if profile.parent.name == "profiles" and _PROFILE_NAME_RE.match(profile.name) else "default"
        return ["-p", name], {"HERMES_HOME": str(profile)}

    def _write_task_files(self, task: HermesTask) -> tuple[Path, Path, str]:
        d = self.state_dir / "tasks" / task.task_id
        d.mkdir(parents=True, exist_ok=True)
        tf = d / "task.json"
        body = json.dumps(task.to_file_dict(), indent=1, sort_keys=True)
        tf.write_text(body, "utf-8")
        sc = task.scope()
        lines = [task.goal.strip(), "", "---", "Task scope (the controller enforces this and will terminate the session on a violation):",
                 f"- Allowed computer_use actions: {', '.join(sc.allowed_actions)}", f"- Maximum actions: {sc.max_steps}"]
        if sc.window_title_contains:
            lines.append(f"- Only operate on a window whose title contains: {sc.window_title_contains!r}")
        if sc.window_title_regex:
            lines.append(f"- Only operate on a window whose title matches: {sc.window_title_regex!r}")
        if sc.process_names:
            lines.append(f"- Only operate on these processes/apps: {', '.join(sc.process_names)}")
        if sc.has_target:
            lines.append("- Never capture the full screen or the desktop (app='screen' / 'desktop' are out of scope).")
        lines += ["- Capture first, verify each action's effect, and never repeat an input whose effect was confirmed.",
                  "- Do not use any tool other than computer_use and the Rebuild Studio MCP tools."]
        pf = d / "prompt.md"
        pf.write_text("\n".join(lines) + "\n", "utf-8")
        return tf, pf, sha256_bytes(body.encode("utf-8"))

    def cancel(self, task_id: str) -> bool:
        """Request cancellation of a running task; the process tree is killed by the run loop."""
        with self._active_lock:
            ev = self._active.get(task_id)
        if ev is None:
            return False
        ev.set()
        return True

    def run_task(self, task: HermesTask, *, ctx: Any = None, cases: Any = None, case_id: str | None = None,
                 connected: Callable[[], bool] | None = None) -> TaskResult:
        """Run *task*. Agent mode launches the user's Hermes CLI non-interactively; direct mode replays ``task.recipe``.

        ``ctx`` is a :class:`StageContext` (or any object with ``heartbeat()``/``log()``): its cancellation kills the
        Hermes process tree. ``connected`` returning False terminates the run cleanly (status ``disconnected``).
        """
        task.validate()
        if task.mode == MODE_DIRECT:
            res = self.replay(Recipe.from_dict(task.recipe or {}), ctx=ctx, cases=cases, case_id=case_id, connected=connected)
            res.task_id = task.task_id
            return res
        inst = self.detect_installation()
        if inst["state"] != "installed":
            raise HermesBridgeError("hermes_missing" if inst["state"] == "missing" else "hermes_broken",
                                    inst.get("error") or "Hermes is not installed", affected="hermes", next_action=inst["next_action"])
        pr = self.pairing()
        if not pr:
            raise HermesBridgeError("not_paired", "no Hermes profile is paired", next_action="pair a profile first")
        host = self.probe.hostname()
        for other in (pr.get("host"), self.expected_host):
            if other and other.lower() != host.lower():
                raise HermesBridgeError("wrong_machine", f"bridge host {host!r} differs from the Hermes host {other!r}", affected=host,
                                        next_action="run Rebuild Studio on the machine that owns the Hermes profile and desktop")
        profile = Path(pr["profile_path"])
        if not profile.is_dir():
            raise HermesBridgeError("profile_missing", f"paired profile {profile} no longer exists", affected=str(profile), next_action="pair again")
        tf, pf, task_sha = self._write_task_files(task)
        pargs, penv = self._profile_cli_args(profile)
        cmd = [inst["hermes_path"], *pargs, "chat", "-Q", "--format", "stream-json", "--query-file", str(pf), "--source", "tool",
               "-t", ",".join(task.toolsets), "--max-turns", str(task.max_steps)]
        timeout = float(task.timeout_s or get_settings().limits.max_stage_seconds)
        cmd += ["--run-budget", str(int(max(30, timeout * 0.9)))]
        if task.model:
            cmd += ["-m", task.model]
        if task.provider:
            cmd += ["--provider", task.provider]
        if task.ignore_rules:
            cmd += ["--ignore-rules"]
        env = dict(self.env)
        env.update(penv)  # the ONLY thing we add: which profile. No credentials are ever injected.
        cancel_ev = threading.Event()
        with self._active_lock:
            self._active[task.task_id] = cancel_ev
        started_ms = time.time() * 1000
        log_path = profile / "logs" / "agent.log"
        log_off = log_path.stat().st_size if log_path.exists() else 0
        parser = StreamParser(task, profile, started_ms)
        started = now_iso()
        try:
            outcome = self._stream(cmd, env, profile, parser, cancel_ev, ctx, connected, timeout)
        finally:
            with self._active_lock:
                self._active.pop(task.task_id, None)
        records, shots = parser.finalize()
        diags = list(parser.diagnostics)
        status = outcome["status"]
        if parser.violation:
            status = "scope_violation"
        elif status == "exited":
            rc = outcome["exit_code"]
            res_ev = parser.result
            if res_ev is None:
                status, outcome["error"] = "failed", "Hermes exited without a terminal `result` record (truncated or crashed session)"
            elif rc != 0 or res_ev.get("exit_code") not in (0, None):
                status = "failed"
                outcome["error"] = outcome.get("error") or str(res_ev.get("error") or f"Hermes exited with {rc}")
            else:
                status = "completed"
        res = TaskResult(task_id=task.task_id, mode=MODE_AGENT, status=status, records=[r.to_json() for r in records], diagnostics=diags,
                         exit_code=outcome.get("exit_code"), final_text=parser.final_text[:20000], error=outcome.get("error") or (parser.violation or {}).get("message"),
                         session_id=outcome.get("session_id") or parser.init.get("session_id"), tokens=(parser.result or {}).get("tokens"),
                         task_file=str(tf), command=cmd, started_at=started, finished_at=now_iso(),
                         extra={"task_sha256": task_sha, "profile_path": str(profile), "model": parser.init.get("model"),
                                "violation": parser.violation, "other_tool_calls": dict(parser.other_tools),
                                "stderr_tail": outcome.get("stderr_tail", ""), "output_truncated": outcome.get("truncated", False),
                                "bad_stream_lines": parser.bad_lines, "hermes_log": str(log_path),
                                "hermes_log_excerpt": _read_new(log_path, log_off), "env_added": sorted(penv)})
        if status in ("failed", "timeout") and res.extra["stderr_tail"]:
            _merge_diags(res.diagnostics, diagnose_text(res.extra["stderr_tail"] + "\n" + str(res.error or "")))
        if cases is not None and case_id:
            self._store_evidence(cases, case_id, res, shots, inputs={"task": task_sha, "started": started}, raw_events=parser.raw_events)
        if ctx is not None:
            with contextlib.suppress(Exception):
                ctx.log(f"hermes task {task.task_id} [{MODE_AGENT}] {status}: {len(records)} desktop actions", mode=MODE_AGENT, status=status)
        return res

    def _stream(self, cmd: list[str], env: dict[str, str], profile: Path, parser: StreamParser, cancel_ev: threading.Event,
                ctx: Any, connected: Callable[[], bool] | None, timeout: float) -> dict[str, Any]:
        kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "env": env, "cwd": str(profile)}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kw["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, **kw)
        except OSError as e:
            raise HermesBridgeError("hermes_spawn_failed", f"could not start hermes: {e}", affected=cmd[0], next_action="run `hermes doctor`") from e
        procs = getattr(ctx, "_procs", None)
        if isinstance(procs, list):
            procs.append(proc)   # so StageContext.kill_all()/cancel also kills us
        cap = get_settings().limits.max_subprocess_output_bytes
        q: "queue.Queue[tuple[str, bytes | None]]" = queue.Queue()
        stderr_chunks: list[bytes] = []

        def pump(stream: Any, name: str) -> None:
            try:
                for line in iter(lambda: stream.readline(1 << 20), b""):
                    q.put((name, line))
            finally:
                q.put((name, None))

        threads = [threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
                   threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True)]
        for t in threads:
            t.start()
        done = {"out": False, "err": False}
        total = 0
        status: str | None = None
        error: str | None = None
        truncated = False
        start = time.monotonic()
        last_hb = 0.0
        cancelled_exc: Cancelled | None = None
        exit_seen = 0.0
        try:
            while True:
                try:
                    name, line = q.get(timeout=0.1)
                    if line is None:
                        done[name] = True
                    else:
                        total += len(line)
                        if name == "out":
                            if total <= cap:
                                parser.feed(line.decode("utf-8", "replace"))
                            else:
                                truncated = True
                        elif sum(map(len, stderr_chunks)) < 256 * 1024:
                            stderr_chunks.append(line)
                except queue.Empty:
                    pass
                if parser.violation:
                    status = "scope_violation"
                elif cancel_ev.is_set():
                    status = "cancelled"
                elif connected is not None and not connected():
                    status = "disconnected"
                elif truncated:
                    status, error = "failed", "Hermes output exceeded the configured cap"
                elif time.monotonic() - start > timeout:
                    status, error = "timeout", f"Hermes did not finish within {timeout:.0f}s"
                elif ctx is not None and time.monotonic() - last_hb > 0.5:
                    last_hb = time.monotonic()
                    try:
                        ctx.heartbeat()
                    except Cancelled as e:
                        status, cancelled_exc = "cancelled", e
                if status:
                    kill_tree(proc)
                    break
                if proc.poll() is not None:
                    exit_seen = exit_seen or time.monotonic()
                    # a grandchild may keep the pipes open after Hermes exits: do not wait for EOF forever
                    if all(done.values()) or time.monotonic() - exit_seen > 1.0:
                        break
            # drain whatever is left (bounded) so the last events are not lost
            drain_until = time.monotonic() + 1.0
            while not all(done.values()) and time.monotonic() < drain_until:
                try:
                    name, line = q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    done[name] = True
                elif name == "out" and not status:
                    parser.feed(line.decode("utf-8", "replace"))
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                kill_tree(proc)
                with contextlib.suppress(Exception):
                    proc.wait(timeout=10)
            elif os.name != "nt":  # Hermes is gone; make sure nothing it spawned (driver, helpers) outlives it
                with contextlib.suppress(Exception):
                    os.killpg(proc.pid, 15)
            if isinstance(procs, list) and proc in procs:
                procs.remove(proc)
        stderr_text = b"".join(stderr_chunks).decode("utf-8", "replace")
        sid = _grab(r"session_id:\s*(\S+)", stderr_text)   # hermes_cli/stream_json.py:95 - session id lives on stderr
        if cancelled_exc is not None:
            raise cancelled_exc
        return {"status": status or "exited", "exit_code": proc.returncode, "error": error, "stderr_tail": stderr_text[-4000:],
                "session_id": sid, "truncated": truncated}

    # -- evidence ---------------------------------------------------------------------------------------------------
    def _store_evidence(self, cases: Any, case_id: str, res: TaskResult, shots: dict[str, bytes], *, inputs: dict[str, Any],
                        raw_events: list[dict[str, Any]] | None = None) -> None:
        body = {"schema": BRIDGE_SCHEMA, "mode": res.mode, "integration": integration_note(res.mode), "task_id": res.task_id,
                "status": res.status, "error": res.error, "started_at": res.started_at, "finished_at": res.finished_at,
                "records": res.records, "diagnostics": res.diagnostics, "extra": res.extra, "session_id": res.session_id,
                "tokens": res.tokens, "final_text": res.final_text}
        if raw_events is not None:
            body["raw_events"] = raw_events
        ev = cases.add_evidence(case_id, "hermes_action_log", f"Hermes {res.mode} task {res.task_id}: {res.status}", body=body,
                                meta={"mode": res.mode, "status": res.status, "actions": len(res.records), "task_id": res.task_id},
                                inputs={**inputs, "task_id": res.task_id}, producer="hermes-bridge")
        res.evidence_ids.append(ev["evidence_id"])
        for sha, data in shots.items():
            sev = cases.add_evidence(case_id, "hermes_screenshot", f"Hermes screenshot {sha[:12]} ({res.mode})", body_bytes=data,
                                     meta={"sha256": sha, "mode": res.mode, "bytes": len(data), "task_id": res.task_id},
                                     inputs={"sha256": sha}, producer="hermes-bridge")
            res.evidence_ids.append(sev["evidence_id"])

    def close(self) -> None:
        with self._active_lock:
            for ev in self._active.values():
                ev.set()


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _read_new(path: Path, offset: int, limit: int = 32 * 1024) -> str:
    try:
        size = path.stat().st_size
        if size <= offset:
            return ""
        with open(path, "rb") as f:
            f.seek(max(offset, size - limit))
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


__all__ = [
    "HermesBridge", "HermesBridgeError", "HermesTask", "TaskResult", "ActionRecord", "Scope", "ScopeViolation", "Recipe",
    "DirectDriverSession", "SystemProbe", "StreamParser", "merge_mcp_server", "diagnose_text", "run_recipe", "evaluate_expect",
    "default_hermes_home", "sanitized_driver_env", "MODE_AGENT", "MODE_DIRECT", "BRIDGE_SCHEMA", "DEFAULT_TASK_ACTIONS",

]
