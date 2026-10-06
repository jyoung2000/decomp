"""Typed messages for the cua-driver MCP tool calls Hermes' computer-use backend makes.

Everything here is derived from the pinned Hermes source (see docs/HERMES.md for file:line citations), not from the
unreachable docs site, and not from a live driver capture on this host:

- transport: ``<cua-driver> mcp`` speaks MCP over stdio (newline-delimited JSON-RPC 2.0); the exact command is
  discoverable from ``<cua-driver> manifest`` -> ``mcp_invocation`` (tools/computer_use/cua_backend_driver.py:110-136).
- tool names and argument names: tools/computer_use/cua_backend_input.py, cua_backend_capture.py, cua_backend.py.
- result envelope ``{content[], structuredContent, isError}`` and the action verdict fields
  (``effect``/``verified``/``escalation``/``path``/``degraded``/``code``): cua_backend_parse.py:39-64, 158-188.

The module is pure (no I/O, no network). ``PROTOCOL_VERSION`` versions *our* typed view; bump it whenever a message
shape changes so recorded recipes and transcripts can be rejected or migrated instead of silently misread.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, Iterable

PROTOCOL_VERSION = "rebuild-studio.cua-driver-protocol/1"

# What the types below were derived from. ``runtime_contract_min`` is Hermes' own floor for the driver
# (cua_backend_driver.py:23); ``pinned_driver_version`` is pm/lock.json ``packages.cua-driver.version``.
PINNED_SOURCE = {
    "repo": "https://github.com/NousResearch/hermes-agent",
    "commit": "daefc2b735ea32a729b026e0116c1fc6bf5980d1",
    "pinned_driver_version": "0.21.0",
    "runtime_contract_min": (0, 20, 0),
    "fixture_contract_epoch": "cua-driver-0.9",
}

MCP_PROTOCOL_VERSION = "2024-11-05"  # the version we offer in `initialize`; the server answers with the one it speaks

# Mode labels used on every record the bridge produces. They are deliberately long and explicit: direct driver
# automation is NOT a Hermes agent session and must never be presented as one.
MODE_AGENT = "hermes_agent_session"
MODE_DIRECT = "cua_driver_direct"
MODES = (MODE_AGENT, MODE_DIRECT)

# The model-facing `computer_use` tool's action enum (tools/computer_use/schema.py:18-35).
A_CAPTURE, A_CLICK, A_DOUBLE_CLICK, A_RIGHT_CLICK, A_MIDDLE_CLICK = "capture", "click", "double_click", "right_click", "middle_click"
A_DRAG, A_SCROLL, A_TYPE, A_KEY, A_SET_VALUE = "drag", "scroll", "type", "key", "set_value"
A_WAIT, A_LIST_APPS, A_LIST_WINDOWS, A_FOCUS_APP = "wait", "list_apps", "list_windows", "focus_app"
COMPUTER_USE_ACTIONS: tuple[str, ...] = (
    A_CAPTURE, A_CLICK, A_DOUBLE_CLICK, A_RIGHT_CLICK, A_MIDDLE_CLICK, A_DRAG, A_SCROLL, A_TYPE, A_KEY, A_SET_VALUE,
    A_WAIT, A_LIST_APPS, A_LIST_WINDOWS, A_FOCUS_APP,
)
READ_ONLY_ACTIONS: tuple[str, ...] = (A_CAPTURE, A_WAIT, A_LIST_APPS, A_LIST_WINDOWS)
# Actions that mutate user-visible state (tools/computer_use/tool.py `destructive=True`: _input + focus_app).
MUTATING_ACTIONS: tuple[str, ...] = (
    A_CLICK, A_DOUBLE_CLICK, A_RIGHT_CLICK, A_MIDDLE_CLICK, A_DRAG, A_SCROLL, A_TYPE, A_KEY, A_SET_VALUE, A_FOCUS_APP,
)
INPUT_ACTIONS: tuple[str, ...] = tuple(a for a in MUTATING_ACTIONS if a != A_FOCUS_APP)


class ProtocolError(ValueError):
    """A message violates the typed contract (raised before anything is sent)."""


class UnsupportedAction(ProtocolError):
    """The action/tool is not part of the contract or the connected driver does not advertise it."""

    def __init__(self, action: str, reason: str):
        super().__init__(f"unsupported action {action!r}: {reason}")
        self.action = action
        self.reason = reason


# ---------------------------------------------------------------------------------------------------------------------
# Tool-call messages
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolCall:
    """One MCP ``tools/call``. ``arguments()`` drops ``None`` fields; ``validate()`` runs before anything is sent."""

    tool: ClassVar[str] = ""
    mutating: ClassVar[bool] = False

    def arguments(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if v is not None:
                out[f.name] = list(v) if isinstance(v, tuple) else v
        return out

    def validate(self) -> None:  # pragma: no cover - overridden where there is something to check
        return None

    def to_json(self) -> dict[str, Any]:
        return {"tool": self.tool, "arguments": self.arguments(), "mutating": self.mutating, "protocol": PROTOCOL_VERSION}


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise ProtocolError(msg)


def _addressing(element_index: Any, x: Any, y: Any, what: str) -> None:
    has_el = element_index is not None
    has_xy = x is not None and y is not None
    _need(has_el or has_xy, f"{what} requires element_index or x/y")
    _need(not (has_el and has_xy), f"{what} takes element_index or x/y, not both")
    if has_xy:
        _need(isinstance(x, int) and isinstance(y, int) and not isinstance(x, bool) and not isinstance(y, bool),
              f"{what} x/y must be integers")


_DELIVERY = (None, "background", "foreground")
_BUTTONS = ("left", "right", "middle")
_DIRECTIONS = ("up", "down", "left", "right")


@dataclass(frozen=True)
class StartSession(ToolCall):
    """Declare this run's identity (cua_backend.py:316-318). Non-fatal for Hermes; anonymous calls still work."""
    tool: ClassVar[str] = "start_session"
    session: str = ""

    def validate(self) -> None:
        _need(bool(self.session), "start_session requires a session id")


@dataclass(frozen=True)
class EndSession(ToolCall):
    tool: ClassVar[str] = "end_session"
    session: str = ""


@dataclass(frozen=True)
class ListWindows(ToolCall):
    """Visible windows: result carries ``windows[] {app_name, pid, window_id, title, is_on_screen, z_index}``."""
    tool: ClassVar[str] = "list_windows"
    on_screen_only: bool | None = True
    session: str | None = None


@dataclass(frozen=True)
class ListApps(ToolCall):
    tool: ClassVar[str] = "list_apps"
    session: str | None = None


@dataclass(frozen=True)
class GetWindowState(ToolCall):
    """AX/UI tree + screenshot of one window (the only way to read the UI tree on current drivers)."""
    tool: ClassVar[str] = "get_window_state"
    pid: int = 0
    window_id: int = 0
    session: str | None = None
    max_elements: int | None = None
    screenshot_out_file: str | None = None

    def validate(self) -> None:
        _need(self.pid > 0 and self.window_id > 0, "get_window_state requires positive pid and window_id")


@dataclass(frozen=True)
class Screenshot(ToolCall):
    """Cheaper pixels-only capture; only on drivers whose tools/list advertises it (cua_backend_capture.py:265-268)."""
    tool: ClassVar[str] = "screenshot"
    window_id: int = 0
    format: str | None = "jpeg"
    quality: int | None = 85
    session: str | None = None

    def validate(self) -> None:
        _need(self.window_id > 0, "screenshot requires a positive window_id")


@dataclass(frozen=True)
class GetDesktopState(ToolCall):
    """Composited full-screen grab (``app='screen'`` lane); pixels only, no clickable elements."""
    tool: ClassVar[str] = "get_desktop_state"
    session: str | None = None


@dataclass(frozen=True)
class GetConfig(ToolCall):
    tool: ClassVar[str] = "get_config"
    session: str | None = None


@dataclass(frozen=True)
class SetConfig(ToolCall):
    tool: ClassVar[str] = "set_config"
    mutating: ClassVar[bool] = False  # driver-side config only; no desktop effect
    key: str = ""
    value: Any = None
    session: str | None = None


@dataclass(frozen=True)
class Click(ToolCall):
    tool: ClassVar[str] = "click"
    mutating: ClassVar[bool] = True
    pid: int = 0
    window_id: int = 0
    element_index: int | None = None
    element_token: str | None = None
    x: int | None = None
    y: int | None = None
    button: str | None = "left"
    modifier: tuple[str, ...] | None = None
    delivery_mode: str | None = None
    session: str | None = None

    def validate(self) -> None:
        _need(self.pid > 0 and self.window_id > 0, "click requires a captured target (pid, window_id)")
        _addressing(self.element_index, self.x, self.y, "click")
        _need(self.button in _BUTTONS, f"click button must be one of {_BUTTONS}")
        _need(self.delivery_mode in _DELIVERY, "delivery_mode must be background|foreground")


@dataclass(frozen=True)
class DoubleClick(Click):
    tool: ClassVar[str] = "double_click"


@dataclass(frozen=True)
class TypeText(ToolCall):
    tool: ClassVar[str] = "type_text"
    mutating: ClassVar[bool] = True
    pid: int = 0
    window_id: int = 0
    text: str = ""
    delivery_mode: str | None = None
    session: str | None = None

    def validate(self) -> None:
        _need(self.pid > 0 and self.window_id > 0, "type_text requires a captured target (pid, window_id)")
        _need(isinstance(self.text, str) and self.text != "", "type_text requires non-empty text")
        _need(self.delivery_mode in _DELIVERY, "delivery_mode must be background|foreground")


@dataclass(frozen=True)
class PressKey(ToolCall):
    """A single key without modifiers (``keys='return'``); combos go through :class:`Hotkey`."""
    tool: ClassVar[str] = "press_key"
    mutating: ClassVar[bool] = True
    pid: int = 0
    window_id: int = 0
    key: str = ""
    delivery_mode: str | None = None
    session: str | None = None

    def validate(self) -> None:
        _need(self.pid > 0 and self.window_id > 0, "press_key requires a captured target (pid, window_id)")
        _need(bool(self.key), "press_key requires a key")


@dataclass(frozen=True)
class Hotkey(ToolCall):
    """Modifier(s) + key, e.g. ``["ctrl", "s"]`` (cua_backend_input.py:153-162). Needs at least one modifier."""
    tool: ClassVar[str] = "hotkey"
    mutating: ClassVar[bool] = True
    pid: int = 0
    window_id: int = 0
    keys: tuple[str, ...] = ()
    delivery_mode: str | None = None
    session: str | None = None

    def validate(self) -> None:
        _need(self.pid > 0 and self.window_id > 0, "hotkey requires a captured target (pid, window_id)")
        _need(len(self.keys) >= 2, "hotkey requires at least one modifier and one key")


@dataclass(frozen=True)
class Scroll(ToolCall):
    tool: ClassVar[str] = "scroll"
    mutating: ClassVar[bool] = True
    pid: int = 0
    window_id: int = 0
    direction: str = "down"
    amount: int = 3
    element_index: int | None = None
    x: int | None = None
    y: int | None = None
    delivery_mode: str | None = None
    session: str | None = None

    def validate(self) -> None:
        _need(self.pid > 0 and self.window_id > 0, "scroll requires a captured target (pid, window_id)")
        _need(self.direction in _DIRECTIONS, f"scroll direction must be one of {_DIRECTIONS}")
        _need(1 <= self.amount <= 50, "scroll amount must be 1..50")


@dataclass(frozen=True)
class Drag(ToolCall):
    tool: ClassVar[str] = "drag"
    mutating: ClassVar[bool] = True
    pid: int = 0
    window_id: int = 0
    from_element: int | None = None
    to_element: int | None = None
    from_x: int | None = None
    from_y: int | None = None
    to_x: int | None = None
    to_y: int | None = None
    button: str | None = "left"
    delivery_mode: str | None = None
    session: str | None = None

    def validate(self) -> None:
        _need(self.pid > 0 and self.window_id > 0, "drag requires a captured target (pid, window_id)")
        el = self.from_element is not None and self.to_element is not None
        xy = None not in (self.from_x, self.from_y, self.to_x, self.to_y)
        _need(el or xy, "drag requires from_element/to_element or from/to coordinates")
        _need(self.button in _BUTTONS, f"drag button must be one of {_BUTTONS}")


@dataclass(frozen=True)
class SetValue(ToolCall):
    tool: ClassVar[str] = "set_value"
    mutating: ClassVar[bool] = True
    pid: int = 0
    window_id: int = 0
    element_index: int | None = None
    element_token: str | None = None
    value: str = ""
    session: str | None = None

    def validate(self) -> None:
        _need(self.pid > 0 and self.window_id > 0, "set_value requires a captured target (pid, window_id)")
        _need(self.element_index is not None, "set_value requires element_index")


@dataclass(frozen=True)
class BringToFront(ToolCall):
    """Standalone focus tool; strict live schema with no ``session`` property (cua_backend.py:382-387)."""
    tool: ClassVar[str] = "bring_to_front"
    mutating: ClassVar[bool] = True
    pid: int = 0
    window_id: int | None = None

    def validate(self) -> None:
        _need(self.pid > 0, "bring_to_front requires a positive pid")


TOOL_MESSAGES: dict[str, type[ToolCall]] = {c.tool: c for c in (
    StartSession, EndSession, ListWindows, ListApps, GetWindowState, Screenshot, GetDesktopState, GetConfig, SetConfig,
    Click, DoubleClick, TypeText, PressKey, Hotkey, Scroll, Drag, SetValue, BringToFront)}
MUTATING_TOOLS = frozenset(t for t, c in TOOL_MESSAGES.items() if c.mutating)


# ---------------------------------------------------------------------------------------------------------------------
# Hard blocks (parity with Hermes' tool-level blocks: tools/computer_use/tool.py:39-66). Applied in direct mode, where
# there is no Hermes tool layer in between.
# ---------------------------------------------------------------------------------------------------------------------
_MODIFIERS = {"cmd", "command", "shift", "option", "alt", "ctrl", "control", "fn"}
_KEY_ALIASES = {"command": "cmd", "alt": "option", "control": "ctrl", "windows": "win", "super": "win", "meta": "win"}
_BLOCKED_COMBOS = (
    frozenset({"cmd", "shift", "backspace"}), frozenset({"cmd", "option", "backspace"}), frozenset({"cmd", "ctrl", "q"}),
    frozenset({"cmd", "shift", "q"}), frozenset({"cmd", "option", "shift", "q"}), frozenset({"win", "l"}),
    frozenset({"ctrl", "option", "delete"}), frozenset({"ctrl", "option", "del"}), frozenset({"option", "f4"}),
)
_BLOCKED_TYPE = tuple(re.compile(p, re.I) for p in (
    r"curl\s+[^|]*\|\s*bash", r"curl\s+[^|]*\|\s*sh", r"wget\s+[^|]*\|\s*bash", r"\bsudo\s+rm\s+-[rf]",
    r"\brm\s+-rf\s+/\s*$", r":\s*\(\)\s*\{\s*:\|:\s*&\s*\}"))


def parse_key_combo(keys: str) -> tuple[str | None, list[str]]:
    """``'ctrl+s'`` -> ``('s', ['ctrl'])`` (cua_backend_parse.py:136-146: last non-modifier wins)."""
    mods: list[str] = []
    key = None
    for part in (p.strip().lower() for p in re.split(r"[+\-]", keys) if p.strip()):
        norm = _KEY_ALIASES.get(part, part)
        if norm in _MODIFIERS or part in _MODIFIERS:
            mods.append(norm)
        else:
            key = part
    return key, mods


def reject_unsafe(action: str, args: dict[str, Any]) -> str | None:
    """Reason string if the input is hard-blocked, else None."""
    if action == A_TYPE:
        for pat in _BLOCKED_TYPE:
            if pat.search(str(args.get("text", ""))):
                return f"blocked pattern in typed text: {pat.pattern!r}"
    if action == A_KEY:
        canon = frozenset(_KEY_ALIASES.get(p, p) for p in (q.strip().lower() for q in re.split(r"\s*[+\-]\s*", str(args.get("keys", "")))) if p)
        for b in _BLOCKED_COMBOS:
            if b.issubset(canon):
                return f"blocked key combo: {sorted(b)}"
    return None


def build_call(action: str, args: dict[str, Any], *, pid: int | None, window_id: int | None, session: str | None,
               element_token: str | None = None) -> ToolCall:
    """Translate one Hermes-level ``computer_use`` action into the driver tool call Hermes would make.

    Raises :class:`UnsupportedAction` for anything outside the action enum and for ``capture``/``wait``/``focus_app``,
    which are composite or local and are handled by the session rather than being a single driver call.
    """
    if action not in COMPUTER_USE_ACTIONS:
        raise UnsupportedAction(action, f"not in the computer_use action enum {COMPUTER_USE_ACTIONS}")
    blocked = reject_unsafe(action, args)
    if blocked:
        raise UnsupportedAction(action, blocked)
    p, w = int(pid or 0), int(window_id or 0)
    delivery = args.get("delivery_mode")
    el = args.get("element")
    coord = args.get("coordinate")
    try:
        x, y = (int(coord[0]), int(coord[1])) if isinstance(coord, (list, tuple)) and len(coord) == 2 and coord[0] is not None and coord[1] is not None else (None, None)
    except (TypeError, ValueError) as e:
        raise ProtocolError(f"coordinate must be two integers, got {coord!r}") from e
    if action in (A_CLICK, A_DOUBLE_CLICK, A_RIGHT_CLICK, A_MIDDLE_CLICK):
        # Hermes picks the tool by click count only and passes `button` through (cua_backend_input.py:95-111).
        button = {"right_click": "right", "middle_click": "middle"}.get(action, args.get("button") or "left")
        cls = DoubleClick if action == A_DOUBLE_CLICK else Click
        mods = tuple(args["modifiers"]) if args.get("modifiers") else None
        return cls(pid=p, window_id=w, element_index=el, element_token=element_token if el is not None else None,
                   x=None if el is not None else x, y=None if el is not None else y, button=button, modifier=mods,
                   delivery_mode=delivery, session=session)
    if action == A_TYPE:
        return TypeText(pid=p, window_id=w, text=str(args.get("text", "")), delivery_mode=delivery, session=session)
    if action == A_KEY:
        key, mods = parse_key_combo(str(args.get("keys", "")))
        if not key:
            raise UnsupportedAction(action, f"could not parse a key from {args.get('keys')!r}")
        if mods:
            return Hotkey(pid=p, window_id=w, keys=tuple(mods + [key]), delivery_mode=delivery, session=session)
        return PressKey(pid=p, window_id=w, key=key, delivery_mode=delivery, session=session)
    if action == A_SCROLL:
        return Scroll(pid=p, window_id=w, direction=str(args.get("direction", "down")),
                      amount=max(1, min(50, int(args.get("amount", 3)))), element_index=el, x=x, y=y,
                      delivery_mode=delivery, session=session)
    if action == A_DRAG:
        fc, tc = args.get("from_coordinate"), args.get("to_coordinate")
        return Drag(pid=p, window_id=w, from_element=args.get("from_element"), to_element=args.get("to_element"),
                    from_x=fc[0] if fc else None, from_y=fc[1] if fc else None, to_x=tc[0] if tc else None,
                    to_y=tc[1] if tc else None, button=args.get("button") or "left", delivery_mode=delivery, session=session)
    if action == A_SET_VALUE:
        if args.get("value") is None:
            raise UnsupportedAction(action, "set_value requires `value`")
        return SetValue(pid=p, window_id=w, element_index=el, element_token=element_token, value=str(args["value"]), session=session)
    if action == A_LIST_APPS:
        return ListApps(session=session)
    if action == A_LIST_WINDOWS:
        return ListWindows(on_screen_only=True, session=session)
    raise UnsupportedAction(action, "composite/local action; handled by the session, not a single driver call")


# ---------------------------------------------------------------------------------------------------------------------
# JSON-RPC / MCP framing (stdio: one JSON object per line)
# ---------------------------------------------------------------------------------------------------------------------
def rpc_request(msg_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        out["params"] = params
    return out


def rpc_notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        out["params"] = params
    return out


def initialize_params(client_name: str = "rebuild-studio", client_version: str = "0") -> dict[str, Any]:
    return {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": client_name, "version": client_version}}


def tools_call_params(call: ToolCall) -> dict[str, Any]:
    return {"name": call.tool, "arguments": call.arguments()}


def encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class ToolResult:
    """Flattened MCP ``CallToolResult`` (mirrors Hermes' ``_extract_tool_result``)."""

    tool: str
    is_error: bool
    data: Any = None                       # joined text parts (parsed as JSON when it looks like JSON)
    images: list[str] = field(default_factory=list)   # base64 image parts
    image_mime_types: list[str] = field(default_factory=list)
    structured: dict[str, Any] | None = None

    @classmethod
    def from_mcp(cls, tool: str, result: dict[str, Any]) -> "ToolResult":
        texts: list[str] = []
        images: list[str] = []
        mimes: list[str] = []
        for part in result.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                texts.append(part.get("text") or "")
            elif part.get("type") == "image" and part.get("data"):
                images.append(part["data"])
                mimes.append(part.get("mimeType") or "")
        data: Any = None
        if texts:
            joined = "\n".join(t for t in texts if t)
            try:
                data = json.loads(joined) if joined.strip().startswith(("{", "[")) else joined
            except json.JSONDecodeError:
                data = joined
        sc = result.get("structuredContent")
        return cls(tool=tool, is_error=result.get("isError") is True, data=data, images=images, image_mime_types=mimes,
                   structured=sc if isinstance(sc, dict) else None)

    @property
    def ok(self) -> bool:
        """Transport/tool success only - NOT the semantic verdict (read :attr:`verdict`)."""
        return not self.is_error

    @property
    def message(self) -> str:
        if isinstance(self.data, dict) and self.data.get("message"):
            return str(self.data["message"])
        if isinstance(self.data, str):
            return self.data
        if self.structured and self.structured.get("message"):
            return str(self.structured["message"])
        return ""

    def _field(self, key: str) -> Any:
        if self.structured and key in self.structured:
            return self.structured[key]
        if isinstance(self.data, dict):
            return self.data.get(key)
        return None

    @property
    def verdict(self) -> "ActionVerdict":
        def typed(v: Any, t: type) -> Any:
            return v if isinstance(v, t) else None
        return ActionVerdict(
            effect=typed(self._field("effect"), str), verified=typed(self._field("verified"), bool),
            escalation=typed(self._field("escalation"), dict), path=typed(self._field("path"), str),
            degraded=typed(self._field("degraded"), bool),
            code=typed(self._field("code") or self._field("reason_code"), str))

    def image_bytes(self) -> bytes | None:
        """Decoded first screenshot, from an image part or ``structuredContent.screenshot_png_b64``."""
        import base64
        b64 = self.images[0] if self.images and self.images[0] else None
        if not b64 and self.structured:
            b64 = self.structured.get("screenshot_png_b64") or self.structured.get("png_b64")
        if not b64:
            return None
        try:
            return base64.b64decode(b64, validate=False)
        except Exception:
            return None

    def error_text(self) -> str:
        parts = []
        for v in (self.data, self.structured):
            if v is None:
                continue
            parts.append(v if isinstance(v, str) else json.dumps(v, sort_keys=True, default=str))
        return "\n".join(parts)


@dataclass
class ActionVerdict:
    """cua-driver's structured action verdict (cua_backend_parse.py:39-64). ``effect`` is the semantic result."""
    effect: str | None = None       # "confirmed" | "unverifiable" | "suspected_noop"
    verified: bool | None = None
    escalation: dict[str, Any] | None = None
    path: str | None = None
    degraded: bool | None = None
    code: str | None = None

    def postcondition(self) -> bool | None:
        """True only for a confirmed effect, False for a suspected no-op / refusal, None when unproven."""
        if self.effect == "confirmed" or self.verified is True:
            return True
        if self.effect == "suspected_noop" or self.code:
            return False
        return None

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class WindowInfo:
    app_name: str
    pid: int
    window_id: int
    title: str = ""
    on_screen: bool = True
    z_index: float = 0

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _pos_int(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        return None
    try:
        n = int(v)
    except ValueError:
        return None
    return n if n > 0 else None


def parse_windows(result: ToolResult) -> list[WindowInfo]:
    """``list_windows`` payloads across result shapes (cua_backend_parse.py:225-261); unusable rows are dropped."""
    candidates: list[tuple[Any, tuple[str, ...]]] = [
        (result.structured, ("windows",)), (result.data, ("windows", "_legacy_windows"))]
    raw: list[Any] = []
    for container, keys in candidates:
        if isinstance(container, dict):
            for k in keys:
                v = container.get(k)
                if isinstance(v, list) and v:
                    raw = v
                    break
        if raw:
            break
    out: list[WindowInfo] = []
    for w in raw:
        if not isinstance(w, dict):
            continue
        pid, wid = _pos_int(w.get("pid")), _pos_int(w.get("window_id"))
        if pid is None or wid is None:
            continue
        z = w.get("z_index")
        out.append(WindowInfo(app_name=w.get("app_name") if isinstance(w.get("app_name"), str) else "", pid=pid,
                              window_id=wid, title=w.get("title") if isinstance(w.get("title"), str) else "",
                              on_screen=w.get("is_on_screen") is not False,
                              z_index=z if isinstance(z, (int, float)) and not isinstance(z, bool) else 0))
    return out


@dataclass
class Element:
    index: int
    role: str = ""
    label: str = ""
    frame: tuple[int, int, int, int] = (0, 0, 0, 0)
    token: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"index": self.index, "role": self.role, "label": self.label, "frame": list(self.frame), "token": self.token}


_ELEMENT_LINE_RE = re.compile(
    r'^\s*(?:-\s+)?\[(\d+)\]\s+(\w+)(?:\s*=\s*"([^"]*)"|\s+"([^"]*)"|\s+\((?!\d+\))([^)]*)\))?'
    r'(?:\s+(?:\(\d+\)\s+)?id=([^\s\[\]]+))?', re.M)


def parse_elements(result: ToolResult) -> list[Element]:
    """UI tree from ``get_window_state``: ``structuredContent.elements`` first (real frames + tokens), else the
    markdown tree (``[N] Role "label"`` lines; no bounds) - same precedence as Hermes (cua_backend_capture.py:285-303)."""
    sc = (result.structured or {}).get("elements")
    out: list[Element] = []
    if isinstance(sc, list) and sc:
        for raw in sc:
            idx = raw.get("element_index") if isinstance(raw, dict) else None
            if not isinstance(idx, int):
                continue
            frame = raw.get("frame")
            box = (0, 0, 0, 0)
            if isinstance(frame, dict) and frame:
                try:
                    box = tuple(int(frame.get(k, 0)) for k in ("x", "y", "w", "h"))  # type: ignore[assignment]
                except (TypeError, ValueError):
                    pass
            tok = raw.get("element_token")
            out.append(Element(idx, raw.get("role") if isinstance(raw.get("role"), str) else "",
                               raw.get("label") if isinstance(raw.get("label"), str) else "", box,
                               tok if isinstance(tok, str) and tok else None))
        return out
    tree = tree_text(result)
    for m in _ELEMENT_LINE_RE.finditer(tree):
        out.append(Element(int(m.group(1)), m.group(2), m.group(3) or m.group(4) or m.group(5) or m.group(6) or ""))
    return out


def tree_text(result: ToolResult) -> str:
    """Markdown AX tree text of a ``get_window_state`` result (``tree_markdown`` or the text part)."""
    sc = result.structured or {}
    if isinstance(sc.get("tree_markdown"), str):
        return sc["tree_markdown"]
    if isinstance(result.data, str):
        return result.data
    if isinstance(result.data, dict):
        t = result.data.get("tree_markdown")
        if isinstance(t, str):
            return t
    return ""


def window_state_is_empty(result: ToolResult) -> bool:
    """No screenshot and no parseable tree (cua_backend_capture.py:100-106) - Hermes treats this as a degenerate result."""
    sc = result.structured or {}
    return not (result.images or sc.get("elements") or sc.get("screenshot_png_b64") or tree_text(result).strip())


def iter_tool_names(tools_list_result: dict[str, Any]) -> Iterable[str]:
    for t in tools_list_result.get("tools") or []:
        if isinstance(t, dict) and isinstance(t.get("name"), str):
            yield t["name"]
