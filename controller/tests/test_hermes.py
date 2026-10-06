"""Hermes bridge tests (M14): recorded-protocol tests against a fake `hermes` executable and a fake cua-driver.

The fake `hermes` replays JSONL scenarios in the shape `hermes chat --format stream-json` emits
(hermes_cli/stream_json.py). The fake cua-driver answers `manifest`, `--version` and `mcp` (stdio JSON-RPC) from a
transcript. Both transcripts are hand-built from the contract in the pinned Hermes source (docs/HERMES.md), NOT captured
from a live Windows driver - the Windows-live gate is G1..G7 in docs/HERMES.md.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import struct
import sys
import textwrap
import threading
import time
import zlib
from pathlib import Path

import pytest

from rebuild_controller.hermes import (MODE_AGENT, MODE_DIRECT, HermesBridge, HermesBridgeError, HermesTask, Recipe, Scope,
                                       ScopeViolation, SystemProbe, UnsupportedAction, diagnose_text, merge_mcp_server)
from rebuild_controller.hermes import protocol as P

PY = sys.executable


@pytest.fixture(autouse=True)
def _settings(settings):  # run_task reads limits from the global settings
    return settings


# ---------------------------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------------------------
def png(r: int, g: int, b: int) -> bytes:
    def chunk(t: bytes, d: bytes) -> bytes:
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"\x00" + bytes([r, g, b])
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill(pid, 0) would TERMINATE the process on Windows; query the exit code instead
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # zombies count as dead
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def wait_for(path: Path, timeout: float = 10) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.05)
    return False


class FakeProbe(SystemProbe):
    def __init__(self, system="Windows", host="WIN-BOX", session=None, display=None, elevation=None):
        self._s, self._h, self._session, self._display, self._elev = system, host, session, display or {}, elevation or {}

    def system(self): return self._s
    def hostname(self): return self._h
    def display_env(self): return dict(self._display)
    def windows_session(self): return self._session
    def process_elevation(self, pid=None): return self._elev.get(pid)


FAKE_HERMES = textwrap.dedent('''\
    #!{py}
    import json, os, subprocess, sys, time
    args = sys.argv[1:]
    inv = os.environ.get("FAKE_HERMES_INVOKED")
    if inv:
        open(inv, "a").write(json.dumps(args) + "\\n")
    if "--version" in args[:1]:
        if os.environ.get("FAKE_HERMES_BROKEN"):
            sys.stderr.write("boom\\n"); sys.exit(1)
        print("Hermes Agent v0.0.0-fake (2026-10-06)"); sys.exit(0)
    if args[-2:] == ["computer-use", "status"]:
        print("cua-driver: installed at " + os.environ.get("FAKE_DRIVER_PATH", "") + " (0.21.0)"); sys.exit(0)
    dump = os.environ.get("FAKE_HERMES_DUMP")
    if dump:
        json.dump({{"argv": args, "env": dict(os.environ), "cwd": os.getcwd()}}, open(dump, "w"))
    pidfile = os.environ.get("FAKE_HERMES_PIDFILE")
    if pidfile:
        open(pidfile, "w").write(str(os.getpid()))
    home = os.environ.get("HERMES_HOME", "")
    scen = json.load(open(os.environ["FAKE_HERMES_SCENARIO"]))
    for step in scen["steps"]:
        if "sleep" in step:
            time.sleep(step["sleep"]); continue
        if "write_file" in step:
            p = step["write_file"].replace("{{PROFILE}}", home)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(bytes.fromhex(step["hex"])); continue
        if "append_log" in step:
            p = os.path.join(home, "logs"); os.makedirs(p, exist_ok=True)
            open(os.path.join(p, "agent.log"), "a").write(step["append_log"] + "\\n"); continue
        if "spawn_child" in step:
            c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
            open(step["spawn_child"], "w").write(str(c.pid)); continue
        if "marker" in step:
            open(step["marker"], "w").write("x"); continue
        if "stderr" in step:
            sys.stderr.write(step["stderr"].replace("{{PROFILE}}", home)); sys.stderr.flush(); continue
        if "raw" in step:
            print(step["raw"], flush=True); continue
        ev = dict(step["event"])
        ev.setdefault("timestamp", int(time.time() * 1000))
        for k in ("output",):
            if isinstance(ev.get(k), str):
                ev[k] = ev[k].replace("{{PROFILE}}", home)
        print(json.dumps(ev), flush=True)
    sys.exit(scen.get("exit_code", 0))
''')

FAKE_DRIVER = textwrap.dedent('''\
    #!{py}
    import json, os, sys, time
    T = json.load(open(os.environ["FAKE_DRIVER_TRANSCRIPT"]))
    LOG = os.environ.get("FAKE_DRIVER_LOG")
    def log(o):
        if LOG:
            open(LOG, "a").write(json.dumps(o) + "\\n")
    a = sys.argv[1:]
    if os.environ.get("FAKE_DRIVER_ENVDUMP"):
        json.dump(dict(os.environ), open(os.environ["FAKE_DRIVER_ENVDUMP"], "w"))
    if a[:1] == ["manifest"]:
        print(json.dumps(T["manifest"])); sys.exit(0)
    if a[:1] == ["--version"]:
        print("cua-driver " + T["manifest"]["binary_version"]); sys.exit(0)
    if a[:1] != ["mcp"]:
        sys.stderr.write("usage: cua-driver mcp\\n"); sys.exit(2)
    state = [dict(e, _left=e.get("repeat", 1)) for e in T["exchanges"]]
    def sub(m, args):
        return all(args.get(k) == v for k, v in m.items())
    for line in sys.stdin:
        msg = json.loads(line)
        log({{"method": msg.get("method"), "params": msg.get("params")}})
        mid = msg.get("id")
        if mid is None:
            continue
        m = msg["method"]
        if m == "initialize":
            res = {{"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {{"tools": {{}}}}, "serverInfo": T["serverInfo"]}}
        elif m == "tools/list":
            res = {{"tools": T["tools"], "capability_version": T["capability_version"]}}
        elif m == "tools/call":
            name, args = msg["params"]["name"], msg["params"].get("arguments") or {{}}
            res = None
            for e in state:
                if e["tool"] == name and sub(e.get("match", {{}}), args) and (e["_left"] == "inf" or e["_left"] > 0):
                    if e.get("hang"):
                        time.sleep(3600)
                    if e["_left"] != "inf":
                        e["_left"] -= 1
                    res = e["result"]; break
            if res is None:
                res = {{"content": [{{"type": "text", "text": "no recorded exchange for " + name}}], "isError": True}}
        else:
            res = {{}}
        sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": res}}) + "\\n"); sys.stdout.flush()
''')


def _tool(name: str, **props: dict) -> dict:
    return {"name": name, "inputSchema": {"type": "object", "properties": props}}


def transcript(driver_path: str, *, states: list[dict] | None = None, extra_exchanges: list[dict] | None = None,
               tools: list[dict] | None = None) -> dict:
    """A recorded-style cua-driver transcript for a Notepad-like window (shapes per docs/HERMES.md section 3)."""
    win = {"app_name": "Notepad", "pid": 4242, "window_id": 7001, "title": "Untitled - Notepad", "is_on_screen": True, "z_index": 2}
    other = {"app_name": "Calculator", "pid": 5000, "window_id": 7002, "title": "Calculator", "is_on_screen": True, "z_index": 1}

    def state(img: bytes, elements: list[tuple[int, str, str]], title="Untitled - Notepad") -> dict:
        els = [{"element_index": i, "role": r, "label": l, "frame": {"x": 1, "y": 2, "w": 30, "h": 10}, "element_token": f"s1:{i}"} for i, r, l in elements]
        return {"content": [{"type": "text", "text": f"{len(els)} elements\n" + "\n".join(f'- [{i}] {r} "{l}"' for i, r, l in elements)},
                            {"type": "image", "data": base64.b64encode(img).decode(), "mimeType": "image/png"}],
                "structuredContent": {"elements": els, "window_title": title, "element_count": len(els)}}

    s0 = state(png(10, 10, 10), [(1, "Edit", "Text editor"), (2, "Button", "Save")])
    s1 = state(png(20, 20, 20), [(1, "Edit", "Text editor"), (2, "Button", "Save"), (3, "Text", "Saved")])
    s2 = state(png(30, 30, 30), [(1, "Edit", "Text editor"), (2, "Button", "Save"), (3, "Text", "Saved"), (4, "Text", "abc")])
    seq = states or [s0, s1, s2]
    ex = [
        {"tool": "start_session", "repeat": "inf", "result": {"content": [{"type": "text", "text": "ok"}]}},
        {"tool": "end_session", "repeat": "inf", "result": {"content": [{"type": "text", "text": "ok"}]}},
        {"tool": "list_windows", "repeat": "inf", "result": {"content": [{"type": "text", "text": "2 windows"}], "structuredContent": {"windows": [win, other]}}},
        *({"tool": "get_window_state", "result": s} for s in seq),
        {"tool": "click", "repeat": "inf", "result": {"content": [{"type": "text", "text": "clicked"}], "structuredContent": {"ok": True, "effect": "confirmed", "path": "uia_invoke"}}},
        {"tool": "type_text", "repeat": "inf", "result": {"content": [{"type": "text", "text": "typed"}], "structuredContent": {"ok": True, "effect": "unverifiable"}}},
        *(extra_exchanges or []),
    ]
    return {
        "_comment": "hand-built from the pinned Hermes source contract; not a live capture",
        "manifest": {"binary_version": "0.21.0", "mcp_invocation": {"command": driver_path, "args": ["mcp"]},
                     "subcommands": [{"name": "mcp", "args": [{"name": "--socket"}, {"name": "--grant"}]},
                                     {"name": "serve", "args": [{"name": a} for a in ("--socket", "--permission-mode", "--capability-manifest", "--approve-capability-manifest", "--embedded")]},
                                     {"name": "stop", "args": [{"name": "--socket"}]}]},
        "serverInfo": {"name": "cua-driver", "version": "0.21.0"}, "capability_version": "1",
        "tools": tools or [_tool("start_session", session={}), _tool("end_session", session={}), _tool("list_windows", on_screen_only={}, session={}),
                           _tool("get_window_state", pid={}, window_id={}, session={}, max_elements={}),
                           _tool("click", pid={}, window_id={}, element_index={}, element_token={}, x={}, y={}, button={}, modifier={}, delivery_mode={}, session={}),
                           _tool("type_text", pid={}, window_id={}, text={}, delivery_mode={}, session={})],
        "exchanges": ex,
    }


def _install_fake(bindir: Path, name: str, template: str) -> Path:
    """Install a fake executable. POSIX: a shebang script. Windows (no shebang/exec bit): a .py script plus a .cmd launcher
    that runs it with sys.executable, which is what PATH/PATHEXT lookup and CreateProcess can actually execute."""
    if os.name != "nt":
        p = bindir / name
        p.write_text(template.format(py=PY))
        p.chmod(0o755)
        return p
    (bindir / f"{name}.py").write_text(template.format(py=PY))
    cmd = bindir / f"{name}.cmd"
    cmd.write_text(f'@"{PY}" "%~dp0{name}.py" %*' + chr(13) + chr(10), newline="")
    return cmd


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A tmp PATH holding a fake `hermes` and `cua-driver`, a Hermes profile, and a bridge wired to them."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    hermes = _install_fake(bindir, "hermes", FAKE_HERMES)
    driver = _install_fake(bindir, "cua-driver", FAKE_DRIVER)
    home = tmp_path / "home"
    profile = home / ".hermes"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model:\n  default: fake/model\n")
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    scen = ctl / "scenario.json"
    drv_t = ctl / "driver.json"
    drv_t.write_text(json.dumps(transcript(str(driver))))
    sysenv = {k: os.environ[k] for k in ("SystemRoot", "COMSPEC", "PATHEXT", "TEMP", "TMP") if k in os.environ} if os.name == "nt" else {}
    sysdirs = os.pathsep.join([os.environ.get("SystemRoot", r"C:\Windows") + r"\System32"]) if os.name == "nt" else f"/usr/bin{os.pathsep}/bin"
    env = {**sysenv, "PATH": f"{bindir}{os.pathsep}{sysdirs}", "HOME": str(home), "USERPROFILE": str(home), "FAKE_HERMES_SCENARIO": str(scen),
           "FAKE_HERMES_INVOKED": str(ctl / "invoked.jsonl"), "FAKE_HERMES_DUMP": str(ctl / "dump.json"),
           "FAKE_HERMES_PIDFILE": str(ctl / "hermes.pid"), "FAKE_DRIVER_TRANSCRIPT": str(drv_t), "FAKE_DRIVER_LOG": str(ctl / "driver.log"),
           "FAKE_DRIVER_ENVDUMP": str(ctl / "driver.env"), "FAKE_DRIVER_PATH": str(driver)}

    class W:
        pass
    w = W()
    w.tmp, w.bindir, w.profile, w.ctl, w.env, w.driver, w.drv_t, w.scen = tmp_path, bindir, profile, ctl, env, driver, drv_t, scen
    w.bridge = HermesBridge(tmp_path / "data", env=env, probe=FakeProbe("Linux", "linux-host"), mcp_command=["rebuild-mcp"])
    w.invoked = lambda: (ctl / "invoked.jsonl").read_text().splitlines() if (ctl / "invoked.jsonl").exists() else []
    w.chat_calls = lambda: [json.loads(l) for l in w.invoked() if '"chat"' in l]
    w.driver_calls = lambda: [json.loads(l) for l in (ctl / "driver.log").read_text().splitlines()] if (ctl / "driver.log").exists() else []
    w.set_scenario = lambda steps, **kw: scen.write_text(json.dumps({"steps": steps, **kw}))
    w.set_driver = lambda **kw: drv_t.write_text(json.dumps(transcript(str(driver), **kw)))
    return w


def E(type_: str, **kw) -> dict:
    return {"event": {"type": type_, **kw}}


def result_event(text="done", code=0, **kw) -> dict:
    return E("result", session_id="sess_1", exit_code=code, text=text, tokens={"input": 10, "output": 5, "total": 15}, duration_ms=5, **kw)


def happy_scenario(shot1: bytes, shot2: bytes) -> list[dict]:
    return [
        E("system", subtype="init", model="fake/model", session_id="sess_1"),
        E("tool_use", name="computer_use", tool_call_id="c1", input={"action": "capture", "mode": "som", "app": "Notepad"}),
        {"write_file": "{PROFILE}/cache/images/computer_use_aaa.png", "hex": shot1.hex()},
        E("tool_result", name="computer_use", tool_call_id="c1", duration_ms=12, is_error=False,
          output=json.dumps({"mode": "som", "width": 1, "height": 1, "app": "Notepad", "window_title": "Untitled - Notepad",
                             "elements": [{"index": 1}], "total_elements": 2, "screenshot_path": "{PROFILE}/cache/images/computer_use_aaa.png"})),
        E("tool_use", name="computer_use", tool_call_id="c2", input={"action": "click", "element": 2}),
        E("tool_result", name="computer_use", tool_call_id="c2", duration_ms=7, is_error=False,
          output=json.dumps({"ok": True, "action": "click", "effect": "confirmed", "verdict": {"decision": "done"}})),
        E("tool_use", name="computer_use", tool_call_id="c3", input={"action": "type", "text": "hunter2-secret-text"}),
        E("tool_result", name="computer_use", tool_call_id="c3", is_error=False,
          output=json.dumps({"ok": True, "action": "type", "effect": "unverifiable", "verdict": {"decision": "verify_fresh_state"}})),
        E("tool_use", name="computer_use", tool_call_id="c4", input={"action": "capture", "mode": "som", "app": "Notepad"}),
        {"write_file": "{PROFILE}/cache/images/computer_use_bbb.png", "hex": shot2.hex()},
        # multimodal capture: Hermes str()s a dict and truncates at 5000 chars -> no parseable screenshot_path
        E("tool_result", name="computer_use", tool_call_id="c4", is_error=False,
          output="{'_multimodal': True, 'content': [{'type': 'text', 'text': 'Notepad'}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBORw0KGgo"),
        E("text", text="All "), E("text", text="done."),
        {"append_log": "INFO hermes session sess_1 finished"},
        {"stderr": "\nsession_id: sess_1\n"},
        result_event("All done."),
    ]


def pair(w) -> dict:
    return w.bridge.pair(w.profile)


# ---------------------------------------------------------------------------------------------------------------------
# detection / pairing / MCP registration
# ---------------------------------------------------------------------------------------------------------------------
def test_detection_missing_gives_clear_next_action(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    b = HermesBridge(tmp_path / "d", env={"PATH": str(empty), "HOME": str(tmp_path / "h")}, probe=FakeProbe("Linux"))
    inst = b.detect_installation()
    assert inst["state"] == "missing" and inst["hermes_path"] is None
    assert "install.sh" in inst["next_action"] and "hermes setup" in inst["next_action"] and "never runs the installer" in inst["next_action"]
    win = HermesBridge(tmp_path / "d", env={"PATH": str(empty), "HOME": str(tmp_path / "h"), "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}, probe=FakeProbe("Windows"))
    wi = win.detect_installation()
    assert "install.ps1" in wi["next_action"] and wi["hermes_home"].endswith("hermes")
    with pytest.raises(HermesBridgeError) as e:
        b.pair()
    assert e.value.code == "hermes_missing" and "install" in e.value.to_error()["error"]["next_action"]
    with pytest.raises(HermesBridgeError) as e:
        b.run_task(HermesTask(goal="x"))
    assert e.value.code == "hermes_missing"


def test_detection_installed_and_broken(world):
    inst = world.bridge.detect_installation()
    assert inst["state"] == "installed" and inst["version"] == "0.0.0-fake" and inst["release_date"] == "2026-10-06"
    assert Path(inst["hermes_path"]).stem == "hermes" and inst["profile_dir"] == str(world.profile) and inst["config_exists"]
    broken = HermesBridge(world.tmp / "d2", env={**world.env, "FAKE_HERMES_BROKEN": "1"}, probe=FakeProbe("Linux"))
    b = broken.detect_installation()
    assert b["state"] == "broken" and "hermes doctor" in b["next_action"]


def test_pair_validates_and_records_user_profile(world):
    other = world.tmp / "elsewhere"
    other.mkdir()
    with pytest.raises(HermesBridgeError) as e:
        world.bridge.pair(other)
    assert e.value.code == "not_a_hermes_profile"
    with pytest.raises(HermesBridgeError) as e:
        world.bridge.pair(world.tmp / "nope")
    assert e.value.code == "profile_not_found" and "hermes setup" in e.value.next_action
    with pytest.raises(HermesBridgeError) as e:
        world.bridge.pair("relative/dir")
    assert e.value.code == "bad_profile_path"
    inside = world.bridge.data_dir / "x"
    inside.mkdir(parents=True)
    (inside / "config.yaml").write_text("a: 1\n")
    with pytest.raises(HermesBridgeError) as e:
        world.bridge.pair(inside)
    assert e.value.code == "bad_profile_path"
    rec = world.bridge.pair()  # default profile from HOME
    assert rec["profile_path"] == str(world.profile.resolve()) and rec["host"] == "linux-host" and rec["hermes_version"] == "0.0.0-fake"
    assert world.bridge.pairing()["profile_path"] == rec["profile_path"]
    assert (world.profile / "config.yaml").read_text() == "model:\n  default: fake/model\n"  # pairing never edits the profile


CONFIG = textwrap.dedent('''\
    # my hermes config - hand edited
    model:
      default: anthropic/claude-opus-4.6   # keep this
      provider: auto

    mcp_servers:
      # servers I added by hand
      time:
        command: uvx
        args: ["mcp-server-time"]
      github:
        command: npx
        args: ["-y", "@modelcontextprotocol/server-github"]
        env:
          GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_keepme"

    approvals:
      mode: smart   # trailing comment
''')


def test_register_mcp_dry_run_diff_write_backup_idempotent(world):
    cfg = world.profile / "config.yaml"
    cfg.write_text(CONFIG)
    with pytest.raises(HermesBridgeError) as e:
        world.bridge.register_mcp()
    assert e.value.code == "not_paired"
    pair(world)
    import yaml
    before = yaml.safe_load(CONFIG)
    dry = world.bridge.register_mcp(dry_run=True)
    assert dry["changed"] and dry["dry_run"] and not dry["written"] and dry["backup_path"] is None
    assert cfg.read_text() == CONFIG and not list(world.profile.glob("*backup*"))
    assert "+  rebuild_studio:" in dry["diff"] and "+    command: rebuild-mcp" in dry["diff"]
    assert not [l for l in dry["diff"].splitlines() if l.startswith("-") and not l.startswith("---")]  # pure addition
    real = world.bridge.register_mcp(dry_run=False)
    assert real["written"] and real["comments_preserved"]
    backups = list(world.profile.glob("config.yaml.rebuild-studio-backup-*"))
    assert len(backups) == 1 and backups[0].read_text() == CONFIG and real["backup_path"] == str(backups[0])
    new_text = cfg.read_text()
    after = yaml.safe_load(new_text)
    for k in ("# my hermes config - hand edited", "# keep this", "# servers I added by hand", "# trailing comment", "ghp_keepme"):
        assert k in new_text
    assert {k: v for k, v in after.items() if k != "mcp_servers"} == {k: v for k, v in before.items() if k != "mcp_servers"}
    assert after["mcp_servers"]["time"] == before["mcp_servers"]["time"] and after["mcp_servers"]["github"] == before["mcp_servers"]["github"]
    entry = after["mcp_servers"]["rebuild_studio"]
    assert entry["command"] == "rebuild-mcp" and entry["env"] == {"REBUILD_STUDIO_DATA": str(world.bridge.data_dir)}
    assert not any("key" in k.lower() or "token" in k.lower() for k in entry["env"])
    # idempotent: no change, no new backup, file untouched
    again = world.bridge.register_mcp(dry_run=False)
    assert not again["changed"] and again["already_registered"] and again["diff"] == "" and again["backup_path"] is None
    assert cfg.read_text() == new_text and len(list(world.profile.glob("config.yaml.rebuild-studio-backup-*"))) == 1
    # a stale entry is replaced in place, not duplicated
    world.bridge.mcp_command = ["other-cmd", "--flag"]
    upd = world.bridge.register_mcp(dry_run=False)
    assert upd["changed"] and yaml.safe_load(cfg.read_text())["mcp_servers"]["rebuild_studio"]["command"] == "other-cmd"
    assert cfg.read_text().count("rebuild_studio:") == 1 and "# my hermes config" in cfg.read_text()


@pytest.mark.parametrize("text,preserved", [
    ("model: x\n", True),
    ("model: x\nmcp_servers: {}\nafter: 1\n", True),
    ("mcp_servers:  # servers\n\nz: 1\n", True),
    ("mcp_servers: {a: {command: c}}\nz: 1\n", False),
    ("", True),
    ("model: x\r\nmcp_servers:\r\n  a:\r\n    command: c\r\nz: 1\r\n", True),
])
def test_merge_variants(text, preserved):
    import yaml
    entry = {"command": "rebuild-mcp", "args": ["--x"], "timeout": 120}
    new, kept = merge_mcp_server(text, "rebuild_studio", entry)
    before = yaml.safe_load(text) or {}
    after = yaml.safe_load(new)
    assert after["mcp_servers"]["rebuild_studio"] == entry and kept is preserved
    assert {k: v for k, v in after.items() if k != "mcp_servers"} == {k: v for k, v in before.items() if k != "mcp_servers"}
    for k, v in (before.get("mcp_servers") or {}).items():
        assert after["mcp_servers"][k] == v
    if "\r\n" in text:
        assert "\r\n" in new and "\n" not in new.replace("\r\n", "")
    assert merge_mcp_server(new, "rebuild_studio", entry)[0] == new


def test_register_mcp_refuses_bad_config_without_touching_it(world):
    pair(world)
    cfg = world.profile / "config.yaml"
    cfg.write_text("a: [unclosed\n")
    with pytest.raises(HermesBridgeError) as e:
        world.bridge.register_mcp(dry_run=False)
    assert e.value.code == "config_unparseable" and cfg.read_text() == "a: [unclosed\n" and not list(world.profile.glob("*backup*"))
    cfg.write_text("mcp_servers: [1, 2]\n")
    with pytest.raises(HermesBridgeError) as e:
        world.bridge.register_mcp(dry_run=False)
    assert e.value.code == "config_shape"
    cfg.unlink()
    with pytest.raises(HermesBridgeError) as e:
        world.bridge.register_mcp(dry_run=False)
    assert e.value.code == "profile_not_initialized" and not cfg.exists()


# ---------------------------------------------------------------------------------------------------------------------
# status / diagnostics
# ---------------------------------------------------------------------------------------------------------------------
def codes(st) -> set[str]:
    return {d["code"] for d in st["diagnostics"]}


def test_status_on_this_linux_host_is_a_diagnostic_not_a_crash(tmp_path):
    b = HermesBridge(tmp_path / "d", env={"PATH": "", "HOME": str(tmp_path)})  # real SystemProbe, nothing installed
    st = b.status()
    assert st["platform"] == platform_system() and st["host"]
    d = {x["code"]: x for x in st["diagnostics"]}
    if platform_system() != "Windows":
        assert "no interactive Windows session" in d["no_interactive_windows_session"]["message"]
        assert st["session"]["interactive"] is False
    assert "hermes_missing" in d and "driver_missing" in d
    assert st["modes"][MODE_AGENT]["ready"] is False and st["modes"][MODE_DIRECT]["ready"] is False
    assert MODE_DIRECT in st["modes"] and "NOT a Hermes integration" in st["modes"][MODE_DIRECT]["description"]
    json.dumps(st)  # JSON-serialisable for GET /hermes/status


def platform_system() -> str:
    import platform
    return platform.system()


def test_status_ready_with_fakes_reports_identity_driver_capabilities(world):
    pair(world)
    st = world.bridge.status(probe_tools=True)
    assert st["host"] == "linux-host" and st["machine"]["match"] is True
    assert st["driver"]["available"] and st["driver"]["contract_ready"] and st["driver_version"] == "0.21.0"
    assert st["driver"]["source"] == "PATH" and set(st["driver"]["subcommands"]) == {"mcp", "serve", "stop"}
    assert st["capabilities"]["live_tools"] == sorted(t["name"] for t in transcript("x")["tools"]) and st["capabilities"]["capability_version"] == "1"
    assert set(P.COMPUTER_USE_ACTIONS) == set(st["capabilities"]["actions"])
    assert st["modes"][MODE_AGENT]["ready"] and st["modes"][MODE_DIRECT]["ready"]
    assert st["hermes"]["version"] == "0.0.0-fake"
    # unattended approvals default: surfaced, never silently changed
    assert "approval_blocks_unattended" in codes(st)
    assert "single_query_mode" not in (world.profile / "config.yaml").read_text()


def test_driver_contract_unmet_is_reported(world):
    t = transcript(str(world.driver))
    t["manifest"]["binary_version"] = "0.9.0"
    world.drv_t.write_text(json.dumps(t))
    st = world.bridge.status()
    assert "driver_contract_unmet" in codes(st) and "0.20.0" in st["driver"]["contract_reason"]
    t["manifest"]["binary_version"] = "0.21.0"
    t["manifest"]["subcommands"] = [{"name": "mcp", "args": []}]
    world.drv_t.write_text(json.dumps(t))
    assert "serve --capability-manifest" in world.bridge.status()["driver"]["contract_reason"]
    with pytest.raises(HermesBridgeError) as e:
        world.bridge.direct_driver()
    assert e.value.code == "driver_contract_unmet"


def test_windows_session_diagnostics_via_injected_probe(world):
    """The Windows-only checks (Session 0, locked desktop, UIPI) run through an injected probe: the logic is tested here,
    the real ctypes calls are Windows gate G3."""
    def status(session, elevation=None, target=None):
        b = HermesBridge(world.bridge.data_dir, env=world.env, probe=FakeProbe("Windows", "WIN-BOX", session, elevation=elevation))
        return b.status(target_pid=target)

    s0 = status({"session_id": 0, "window_station": "Service-0x0-3e7$", "input_desktop_accessible": False})
    assert "session_0" in codes(s0) and s0["session"]["is_session_0"] and s0["session"]["interactive"] is False
    assert "desktop_locked" not in codes(s0)           # Session 0 is its own diagnosis, not "locked"
    locked = status({"session_id": 1, "window_station": "WinSta0", "input_desktop_accessible": False, "lock_probe_error": 5})
    assert "desktop_locked" in codes(locked) and locked["session"]["desktop_locked"] is True and "session_0" not in codes(locked)
    ok = status({"session_id": 1, "window_station": "WinSta0", "input_desktop_accessible": True})
    assert not ({"session_0", "desktop_locked", "uipi_elevated_target", "no_interactive_windows_session"} & codes(ok))
    assert ok["session"]["interactive"] is True and ok["session"]["session_id"] == 1 and ok["platform"] == "Windows"
    elev = {None: {"elevated": False, "access_denied": False}, 4242: {"elevated": True, "access_denied": False},
            4243: {"elevated": None, "access_denied": True}, 4244: {"elevated": False, "access_denied": False}}
    sess = {"session_id": 1, "window_station": "WinSta0", "input_desktop_accessible": True}
    assert "uipi_elevated_target" in codes(status(sess, elev, 4242))     # elevated target, unelevated bridge
    assert "uipi_elevated_target" in codes(status(sess, elev, 4243))     # token unreadable (access denied) is treated the same
    assert "uipi_elevated_target" not in codes(status(sess, elev, 4244))
    assert "uipi_elevated_target" not in codes(status(sess, elev))
    real = SystemProbe()
    if os.name != "nt":                                                   # the ctypes branches are unreachable off Windows
        assert real.windows_session() is None and real.process_elevation(1) is None


def test_wrong_machine_diagnostic_and_run_refused(world):
    pair(world)  # paired on "linux-host"
    other = HermesBridge(world.bridge.data_dir, env=world.env, probe=FakeProbe("Linux", "some-other-host"))
    st = other.status()
    assert "wrong_machine" in codes(st) and st["machine"]["match"] is False
    d = next(x for x in st["diagnostics"] if x["code"] == "wrong_machine")
    assert d["evidence"] == {"bridge_host": "some-other-host", "hermes_host": "linux-host"}
    assert st["modes"][MODE_AGENT]["ready"] is False
    world.set_scenario([result_event()])
    with pytest.raises(HermesBridgeError) as e:
        other.run_task(HermesTask(goal="do it"))
    assert e.value.code == "wrong_machine" and not world.chat_calls()
    declared = HermesBridge(world.bridge.data_dir / "x", env=world.env, probe=FakeProbe("Linux", "linux-host"), expected_host="hermes-box.lan")
    assert "wrong_machine" in codes(declared.status())


def test_diagnose_text_patterns():
    assert {d["code"] for d in diagnose_text("Access is denied (UIPI)")} == {"uipi_elevated_target"}
    assert "no_windows_found" in {d["code"] for d in diagnose_text("No on-screen window found for app 'X'")}
    assert "unsupported_action" in {d["code"] for d in diagnose_text('{"error": "unknown action \'hotkey\'"}')}
    assert "approval_blocked" in {d["code"] for d in diagnose_text("BLOCKED: computer_use `click` requires approval but no interactive user or gateway is present")}
    assert "desktop_locked" in {d["code"] for d in diagnose_text("the desktop session is LOCKED (loginctl LockedHint=yes)")}


# ---------------------------------------------------------------------------------------------------------------------
# agent session: run_task
# ---------------------------------------------------------------------------------------------------------------------
def test_run_task_records_actions_results_evidence_and_mode(world, cases, src_out):
    pair(world)
    s1, s2 = png(1, 2, 3), png(9, 8, 7)
    world.set_scenario(happy_scenario(s1, s2))
    src, out = src_out
    cid = cases.create_case(name="t", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe")["case_id"]
    task = HermesTask(goal="Open Save in Notepad", task_id="t-happy", target_window_title="Notepad", target_processes=("notepad.exe",),
                      allowed_actions=("capture", "click", "type"), max_steps=10, model="fake/model", provider="openrouter")
    res = world.bridge.run_task(task, cases=cases, case_id=cid)
    assert res.status == "completed" and res.ok and res.mode == MODE_AGENT and res.exit_code == 0
    assert res.session_id == "sess_1" and res.final_text == "All done." and res.tokens["total"] == 15
    d = res.to_dict()
    assert d["mode"] == MODE_AGENT and "Hermes agent session" in d["integration"] and "NOT a Hermes" not in d["integration"]
    r = res.records
    assert [x["action"] for x in r] == ["capture", "click", "type", "capture"]
    assert all(x["mode"] == MODE_AGENT and x["source"] == "hermes_stream" for x in r)
    import hashlib
    h1, h2 = hashlib.sha256(s1).hexdigest(), hashlib.sha256(s2).hexdigest()
    assert r[0]["pre_screenshot_sha"] is None and r[0]["post_screenshot_sha"] == h1 and r[0]["result"]["ok"] is True
    assert r[1]["pre_screenshot_sha"] == h1 and r[1]["result"]["effect"] == "confirmed" and r[1]["postcondition_ok"] is True
    assert r[2]["result"]["effect"] == "unverifiable" and r[2]["postcondition_ok"] is None
    assert r[2]["post_screenshot_sha"] == h2 and r[2]["post_screenshot_inferred"] is True     # back-filled from the next capture
    assert r[3]["post_screenshot_sha"] == h2 and r[3]["screenshot_source"] == "cache_scan_ordered" and r[3].get("result_truncated") is True
    assert all(x["ts"].endswith("Z") for x in r)
    # typed text never lands in records or evidence
    blob = json.dumps(res.to_dict())
    assert "hunter2-secret-text" not in blob and r[2]["args"]["text_len"] == len("hunter2-secret-text")
    assert "hermes session sess_1 finished" in res.extra["hermes_log_excerpt"]
    # the CLI contract we rely on
    argv = json.loads((world.ctl / "dump.json").read_text())
    a = argv["argv"]
    assert a[:4] == ["-p", "default", "chat", "-Q"] and "--format" in a and a[a.index("--format") + 1] == "stream-json"
    assert "--query-file" in a and a[a.index("--max-turns") + 1] == "10" and a[a.index("-m") + 1] == "fake/model"
    assert a[a.index("--provider") + 1] == "openrouter" and a[a.index("-t") + 1] == "computer_use" and a[a.index("--source") + 1] == "tool"
    assert "--yolo" not in a and "--ignore-user-config" not in a
    assert argv["env"]["HERMES_HOME"] == str(world.profile.resolve()) and res.extra["env_added"] == ["HERMES_HOME"]
    tf = json.loads(Path(res.task_file).read_text())
    assert tf["scope"]["allowed_actions"] == ["capture", "click", "type"] and tf["scope"]["max_steps"] == 10
    assert tf["scope"]["process_names"] == ["notepad.exe"] and tf["scope"]["window_title_contains"] == "Notepad"
    assert tf["model"] == "fake/model" and "api_key" not in json.dumps(tf).lower()
    prompt = Path(res.task_file).with_name("prompt.md").read_text()
    assert "Open Save in Notepad" in prompt and "notepad.exe" in prompt and "Maximum actions: 10" in prompt
    # evidence
    assert len(res.evidence_ids) == 3
    log_ev = cases.list_evidence(cid, kind="hermes_action_log")[0]
    body = cases.evidence_body(log_ev["evidence_id"])
    assert body["mode"] == MODE_AGENT and len(body["records"]) == 4 and body["status"] == "completed"
    assert "hunter2-secret-text" not in json.dumps(body)
    shots = cases.list_evidence(cid, kind="hermes_screenshot")
    assert {s["meta"]["sha256"] for s in shots} == {h1, h2} and all(s["meta"]["mode"] == MODE_AGENT for s in shots)
    assert cases.blobs.get_bytes(shots[0]["blob_sha"]) in (s1, s2)


def test_named_profile_uses_dash_p_and_never_sticky_profile(world):
    named = world.tmp / "root" / "profiles" / "coder"
    named.mkdir(parents=True)
    (named / "config.yaml").write_text("a: 1\n")
    world.bridge.pair(named)
    world.set_scenario([E("system", subtype="init", model="m", session_id="s"), result_event()])
    res = world.bridge.run_task(HermesTask(goal="x"))
    assert res.status == "completed"
    a = json.loads((world.ctl / "dump.json").read_text())
    assert a["argv"][:2] == ["-p", "coder"] and a["env"]["HERMES_HOME"] == str(named.resolve())


def test_run_task_rejects_bad_tasks_before_launching(world):
    pair(world)
    world.set_scenario([result_event()])
    for bad, code in [
        (HermesTask(goal="x", allowed_actions=("click", "teleport")), None),
        (HermesTask(goal="   "), "bad_task"),
        (HermesTask(goal="x", max_steps=0), "bad_task"),
        (HermesTask(goal="x", model="--yolo"), "bad_task"),
        (HermesTask(goal="x", provider="a b"), "bad_task"),
        (HermesTask(goal="use key sk-abcdefghijklmnopqrstuvwx to log in"), "credential_in_task"),
        (HermesTask(goal="x", metadata={"api_key": "hunter2"}), "credential_in_task"),
        (HermesTask(goal="x", target_window_regex="("), "bad_task"),
        (HermesTask(goal="x", mode=MODE_DIRECT), "bad_task"),
        (HermesTask(goal="x", mode="made_up"), "bad_mode"),
    ]:
        if code is None:
            with pytest.raises(UnsupportedAction):
                world.bridge.run_task(bad)
        else:
            with pytest.raises(HermesBridgeError) as e:
                world.bridge.run_task(bad)
            assert e.value.code == code
    assert not world.chat_calls(), "hermes must not have been launched for any rejected task"


def test_nonzero_exit_and_truncated_stream_are_failures_with_diagnostics(world):
    pair(world)
    world.set_scenario([E("system", subtype="init", model="m", session_id="s"),
                        E("tool_use", name="computer_use", tool_call_id="c1", input={"action": "click", "element": 1}),
                        E("tool_result", name="computer_use", tool_call_id="c1", is_error=True,
                          output=json.dumps({"error": "BLOCKED: computer_use `click` requires approval but no interactive user or gateway is present to approve it.", "action": "click"})),
                        result_event("", code=1, error="blocked")], exit_code=1)
    res = world.bridge.run_task(HermesTask(goal="x"))
    assert res.status == "failed" and res.exit_code == 1 and "approval_blocked" in {d["code"] for d in res.diagnostics}
    assert res.records[0]["result"]["ok"] is False and res.records[0]["postcondition_ok"] is None
    world.set_scenario([E("system", subtype="init", model="m", session_id="s"), {"raw": "not json at all"}])
    res = world.bridge.run_task(HermesTask(goal="x"))
    assert res.status == "failed" and "terminal `result` record" in res.error and res.extra["bad_stream_lines"] == 1


def test_scope_violation_terminates_session_early(world):
    pair(world)
    marker = world.ctl / "after_violation"
    world.set_scenario([E("system", subtype="init", model="m", session_id="s"),
                        E("tool_use", name="computer_use", tool_call_id="c1", input={"action": "drag", "from_element": 1, "to_element": 2}),
                        {"sleep": 8}, {"marker": str(marker)}, result_event()])
    t0 = time.monotonic()
    res = world.bridge.run_task(HermesTask(goal="x", allowed_actions=("capture", "click")))
    assert time.monotonic() - t0 < 6 and res.status == "scope_violation" and not marker.exists()
    assert res.extra["violation"]["code"] == "action_not_allowed" and "scope_violation" in {d["code"] for d in res.diagnostics}
    assert not alive(int((world.ctl / "hermes.pid").read_text()))
    # full-screen capture is outside a window-scoped task; max_steps is enforced too
    world.set_scenario([E("tool_use", name="computer_use", tool_call_id="c1", input={"action": "capture", "app": "screen"}), {"sleep": 8}, result_event()])
    res = world.bridge.run_task(HermesTask(goal="x", target_window_title="Notepad"))
    assert res.status == "scope_violation" and res.extra["violation"]["code"] == "target_scope"
    steps = [E("tool_use", name="computer_use", tool_call_id=f"c{i}", input={"action": "capture"}) for i in range(4)]
    world.set_scenario(steps + [{"sleep": 8}, result_event()])
    res = world.bridge.run_task(HermesTask(goal="x", max_steps=3))
    assert res.status == "scope_violation" and res.extra["violation"]["code"] == "max_steps_exceeded"
    world.set_scenario([E("tool_use", name="computer_use", tool_call_id="c1", input={"action": "capture", "app": "Calculator"}), {"sleep": 8}, result_event()])
    assert world.bridge.run_task(HermesTask(goal="x", target_processes=("notepad.exe",))).extra["violation"]["code"] == "target_scope"
    # a capture result showing a different window than the scoped title is a (post-hoc) violation
    world.set_scenario([E("tool_use", name="computer_use", tool_call_id="c1", input={"action": "capture"}),
                        E("tool_result", name="computer_use", tool_call_id="c1", is_error=False, output=json.dumps({"app": "Calculator", "window_title": "Calculator", "total_elements": 3})),
                        {"sleep": 8}, result_event()])
    assert world.bridge.run_task(HermesTask(goal="x", target_window_title="Notepad")).extra["violation"]["code"] == "target_window_mismatch"


def test_empty_accessibility_tree_diagnostic_from_agent_result(world):
    pair(world)
    world.set_scenario([E("tool_use", name="computer_use", tool_call_id="c1", input={"action": "capture"}),
                        E("tool_result", name="computer_use", tool_call_id="c1", is_error=False,
                          output=json.dumps({"mode": "som", "app": "Notepad", "elements": [], "total_elements": 0})), result_event()])
    res = world.bridge.run_task(HermesTask(goal="x"))
    assert "empty_accessibility_tree" in {d["code"] for d in res.diagnostics}


def test_cancel_kills_the_whole_process_tree(world):
    pair(world)
    childpid = world.ctl / "child.pid"
    world.set_scenario([E("system", subtype="init", model="m", session_id="s"), {"spawn_child": str(childpid)}, {"sleep": 300}, result_event()])
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("res", world.bridge.run_task(HermesTask(goal="x", task_id="t-cancel"))))
    t.start()
    assert wait_for(childpid) and wait_for(world.ctl / "hermes.pid")
    hpid, cpid = int(world.ctl.joinpath("hermes.pid").read_text()), int(childpid.read_text())
    assert alive(hpid) and alive(cpid)
    assert world.bridge.cancel("t-cancel") is True and world.bridge.cancel("nope") is False
    t.join(20)
    assert not t.is_alive() and out["res"].status == "cancelled"
    time.sleep(0.2)
    assert not alive(hpid) and not alive(cpid)


def test_disconnect_terminates_cleanly(world):
    pair(world)
    childpid = world.ctl / "child.pid"
    world.set_scenario([{"spawn_child": str(childpid)}, {"sleep": 300}, result_event()])
    res_box = {}
    connected = lambda: not childpid.exists()      # "client disconnects" the moment the session is running
    t = threading.Thread(target=lambda: res_box.setdefault("r", world.bridge.run_task(HermesTask(goal="x"), connected=connected)))
    t.start()
    t.join(20)
    assert not t.is_alive() and res_box["r"].status == "disconnected"
    assert wait_for(world.ctl / "hermes.pid")
    time.sleep(0.2)
    assert not alive(int((world.ctl / "hermes.pid").read_text())) and not alive(int(childpid.read_text()))


def test_timeout_is_a_visible_failure(world):
    pair(world)
    world.set_scenario([{"sleep": 300}, result_event()])
    res = world.bridge.run_task(HermesTask(goal="x", timeout_s=1))
    assert res.status == "timeout" and "did not finish" in res.error
    assert not alive(int((world.ctl / "hermes.pid").read_text()))


def test_stagecontext_cancel_kills_hermes(jobs, runner, registry, cases, src_out, world):
    from rebuild_controller.jobs import JobState
    pair(world)
    cid = cases.create_case(name="t", source_root=str(src_out[0]), output_root=str(src_out[1]), target_language="rust", output_type="exe")["case_id"]
    childpid = world.ctl / "child.pid"
    world.set_scenario([{"spawn_child": str(childpid)}, {"sleep": 300}, result_event()])
    seen = {}

    def stage(ctx):
        seen["res"] = world.bridge.run_task(HermesTask(goal="x"), ctx=ctx)
        return {}
    registry.add("hermes_task", stage)
    j = jobs.create(cid, "hermes_task", "Hermes", {})
    t = threading.Thread(target=runner.run_pending)
    t.start()
    assert wait_for(childpid) and wait_for(world.ctl / "hermes.pid")
    jobs.cancel(j.job_id)
    t.join(30)
    assert not t.is_alive()
    time.sleep(0.2)
    assert not alive(int((world.ctl / "hermes.pid").read_text())) and not alive(int(childpid.read_text()))
    assert jobs.get(j.job_id).state in (JobState.CANCELLED, JobState.FAILED) and "res" not in seen


# ---------------------------------------------------------------------------------------------------------------------
# direct cua-driver mode + replay (no model)
# ---------------------------------------------------------------------------------------------------------------------
def notepad_recipe(**over) -> dict:
    r = {"schema": "rebuild-studio.hermes-recipe/1", "protocol": P.PROTOCOL_VERSION, "name": "notepad-save",
         "target": {"window_title_contains": "Notepad", "process_names": ["notepad.exe"]},
         "steps": [{"action": "click", "locator": {"role": "Button", "label": "Save"},
                    "expect": [{"type": "element_present", "label": "Saved"}, {"type": "screenshot_changed"}, {"type": "effect_confirmed"}]},
                   {"action": "type", "args": {"text": "abc"}, "expect": [{"type": "tree_contains", "text": "abc"}]}]}
    r.update(over)
    return r


@pytest.fixture
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def test_replay_runs_recipe_with_postconditions_and_no_model_or_network(world, no_network, cases, src_out):
    world.env["OPENAI_API_KEY"] = "sk-must-not-reach-driver-0000000000"
    world.set_scenario([result_event()])
    cid = cases.create_case(name="t", source_root=str(src_out[0]), output_root=str(src_out[1]), target_language="rust", output_type="exe")["case_id"]
    res = world.bridge.replay(notepad_recipe(), cases=cases, case_id=cid)
    assert res.status == "completed" and res.mode == MODE_DIRECT and res.error is None
    assert res.extra["model_calls"] == 0 and not world.invoked(), "replay must not start Hermes"
    assert "NOT a Hermes integration" in res.to_dict()["integration"]
    r = res.records
    assert [x["action"] for x in r] == ["capture", "click", "type"]
    assert all(x["mode"] == MODE_DIRECT and x["source"] == "driver" for x in r)
    assert r[1]["postcondition_ok"] is True and r[2]["postcondition_ok"] is True
    assert r[1]["pre_screenshot_sha"] == r[0]["post_screenshot_sha"] and r[1]["post_screenshot_sha"] != r[1]["pre_screenshot_sha"]
    assert r[2]["pre_screenshot_sha"] == r[1]["post_screenshot_sha"] and r[1]["result"]["verdict"]["effect"] == "confirmed"
    assert "abc" not in json.dumps(r) and r[2]["args"]["text_len"] == 3
    calls = [c["params"] for c in world.driver_calls() if c["method"] == "tools/call"]
    click = next(c for c in calls if c["name"] == "click")
    assert click["arguments"]["element_index"] == 2 and click["arguments"]["element_token"] == "s1:2" and click["arguments"]["pid"] == 4242 and click["arguments"]["window_id"] == 7001
    assert click["arguments"]["button"] == "left" and click["arguments"]["session"].startswith("rebuild-studio-")
    typed = next(c for c in calls if c["name"] == "type_text")
    assert typed["arguments"]["text"] == "abc" and typed["arguments"]["window_id"] == 7001
    assert [c["name"] for c in calls].count("click") == 1 and not [c for c in calls if c["name"] in ("hotkey", "press_key", "drag")]
    env = json.loads((world.ctl / "driver.env").read_text())
    assert env["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "0" and "OPENAI_API_KEY" not in env
    ev = cases.list_evidence(cid, kind="hermes_action_log")[0]
    body = cases.evidence_body(ev["evidence_id"])
    assert body["mode"] == MODE_DIRECT and body["extra"]["recipe_sha256"] == Recipe.from_dict(notepad_recipe()).sha256()
    assert len(cases.list_evidence(cid, kind="hermes_screenshot")) == 3


def test_replay_stops_at_failed_postcondition_and_never_resends(world):
    r = notepad_recipe()
    r["steps"][0]["expect"] = [{"type": "element_present", "label": "Never appears"}]
    res = world.bridge.replay(r)
    assert res.status == "postcondition_failed" and "postcondition not met" in res.error
    names = [c["params"]["name"] for c in world.driver_calls() if c["method"] == "tools/call"]
    assert names.count("click") == 1 and "type_text" not in names
    assert res.records[-1]["postcondition_ok"] is False


def test_replay_locator_failures_send_nothing(world):
    r = notepad_recipe()
    r["steps"][0]["locator"] = {"role": "Button", "label": "Nonexistent"}
    res = world.bridge.replay(r)
    assert res.status == "postcondition_failed" and "matched no element" in res.error
    r["steps"][0]["locator"] = {"role": "Text"}   # ambiguous only in later states; here none exist at capture time
    assert world.bridge.replay(r).status == "postcondition_failed"
    names = [c["params"]["name"] for c in world.driver_calls() if c["method"] == "tools/call"]
    assert "click" not in names


def test_recipe_validation_and_run_task_direct_mode(world):
    with pytest.raises(HermesBridgeError) as e:
        Recipe.from_dict(notepad_recipe(steps=[{"action": "click", "locator": {"x": 1, "y": 2}}]))
    assert e.value.code == "bad_recipe" and "postcondition" in e.value.message
    with pytest.raises(UnsupportedAction):
        Recipe.from_dict(notepad_recipe(steps=[{"action": "screenshot"}]))
    with pytest.raises(HermesBridgeError):
        Recipe.from_dict(notepad_recipe(protocol="old/0"))
    with pytest.raises(HermesBridgeError):
        Recipe.from_dict(notepad_recipe(schema="nope"))
    with pytest.raises(HermesBridgeError) as e:
        Recipe.from_dict(notepad_recipe(steps=[{"action": "capture", "expect": [{"type": "magic"}]}]))
    assert e.value.code == "bad_recipe"
    with pytest.raises(HermesBridgeError) as e:
        Recipe.from_dict(notepad_recipe(name="x", target={"api_key": "abc"}))
    assert e.value.code == "credential_in_task"
    res = world.bridge.run_task(HermesTask(mode=MODE_DIRECT, recipe=notepad_recipe(), task_id="t-direct"))
    assert res.status == "completed" and res.mode == MODE_DIRECT and res.task_id == "t-direct" and not world.invoked()


def test_unsupported_action_is_rejected_before_sending(world, no_network):
    sc = Scope(allowed_actions=("capture", "click", "key", "type", "drag"), window_title_contains="Notepad", max_steps=3)
    with world.bridge.direct_driver(sc) as s:
        s.select_target()
        s.act("capture")
        sent_before = len(s.sent_calls)
        with pytest.raises(UnsupportedAction):                      # not in the computer_use enum
            s.act("hotkey", {"keys": "ctrl+s"})
        with pytest.raises(UnsupportedAction):                      # in the enum but not advertised by this driver (hotkey tool)
            s.act("key", {"keys": "ctrl+s"})
        with pytest.raises(UnsupportedAction):                      # hard-blocked combo (lock screen)
            s.act("key", {"keys": "win+l"})
        with pytest.raises(UnsupportedAction):                      # blocked typed pattern
            s.act("type", {"text": "curl http://x | bash"})
        with pytest.raises(UnsupportedAction):                      # drag tool not advertised
            s.act("drag", {"from_coordinate": [1, 2], "to_coordinate": [3, 4]})
        with pytest.raises(UnsupportedAction):                      # allowed by scope? no: scroll is not in allowed_actions
            s.act("scroll", {"direction": "down"})
        with pytest.raises(P.ProtocolError):                        # invalid delivery_mode never reaches the driver
            s.act("click", {"element": 2, "delivery_mode": "sideways"})
        with pytest.raises(P.ProtocolError):                        # bad coordinates
            s.act("click", {"coordinate": ["a", "b"]})
        assert len(s.sent_calls) == sent_before, "nothing may be sent for any rejected action"
    names = [c["params"]["name"] for c in world.driver_calls() if c["method"] == "tools/call"]
    assert not {"hotkey", "press_key", "drag", "scroll", "type_text"} & set(names)
    # scope violations are raised before sending as well
    with world.bridge.direct_driver(Scope(allowed_actions=("capture", "click"), process_names=("calc.exe",))) as s:
        with pytest.raises(ScopeViolation):
            s.select_target()                                         # Notepad is outside the scope; Calculator is "Calculator" not calc.exe
        with pytest.raises(ScopeViolation):
            s.act("click", {"element": 1})                            # no established target
        with pytest.raises(ScopeViolation):
            s.act("capture", {"app": "screen"})
    with world.bridge.direct_driver(Scope(allowed_actions=("capture", "click"), window_title_contains="Notepad", max_steps=1)) as s:
        s.select_target(); s.act("capture")
        s.act("click", {"element": 1})
        with pytest.raises(ScopeViolation) as e:
            s.act("click", {"element": 1})
        assert e.value.code == "max_steps_exceeded"
        with pytest.raises(ScopeViolation):
            s.act("capture", {"app": "screen"})


def test_direct_session_is_labelled_and_empty_tree_is_diagnosed(world):
    empty = {"content": [{"type": "text", "text": ""}, {"type": "image", "data": base64.b64encode(png(5, 5, 5)).decode(), "mimeType": "image/png"}],
             "structuredContent": {"elements": [], "window_title": "Untitled - Notepad", "degraded": True}}
    world.set_driver(states=[empty])
    with world.bridge.direct_driver(Scope(allowed_actions=("capture",), window_title_contains="Notepad")) as s:
        assert s.mode == MODE_DIRECT
        s.select_target()
        rec = s.act("capture")
        assert rec.to_json()["mode"] == MODE_DIRECT and rec.result["degraded"] is True and rec.result["elements"] == 0
        assert "empty_accessibility_tree" in {d["code"] for d in s.diagnostics}


def test_direct_timeout_fails_closed_and_never_replays_input(world):
    world.set_driver(extra_exchanges=[])
    t = transcript(str(world.driver))
    t["exchanges"].insert(0, {"tool": "click", "hang": True, "result": {}})
    world.drv_t.write_text(json.dumps(t))
    with world.bridge.direct_driver(Scope(allowed_actions=("capture", "click"), window_title_contains="Notepad"), call_timeout=1.0) as s:
        s.select_target(); s.act("capture")
        rec = s.act("click", {"element": 2})
        assert rec.result["ok"] is False and rec.postcondition_ok is None
        assert "timeout_outcome_unknown" in {d["code"] for d in s.diagnostics}
        with pytest.raises(ScopeViolation):                           # session restarted, target dropped: must re-capture before input
            s.act("click", {"element": 2})
    names = [c["params"]["name"] for c in world.driver_calls() if c["method"] == "tools/call"]
    assert names.count("click") == 1


def test_no_network_imports_in_hermes_package():
    root = Path(__file__).resolve().parents[1] / "rebuild_controller" / "hermes"
    banned = re.compile(r"^\s*(import|from)\s+(socket|http|urllib|requests|httpx|ssl|aiohttp|websockets|ftplib|smtplib)\b", re.M)
    for f in root.glob("*.py"):
        assert not banned.search(f.read_text()), f"{f.name} imports a network module"


def test_protocol_messages_and_version():
    assert P.PROTOCOL_VERSION.endswith("/1") and P.PINNED_SOURCE["commit"] == "daefc2b735ea32a729b026e0116c1fc6bf5980d1"
    c = P.build_call("key", {"keys": "ctrl+s"}, pid=1, window_id=2, session="s")
    assert isinstance(c, P.Hotkey) and c.arguments() == {"pid": 1, "window_id": 2, "keys": ["ctrl", "s"], "session": "s"} and c.mutating
    assert isinstance(P.build_call("key", {"keys": "return"}, pid=1, window_id=2, session=None), P.PressKey)
    d = P.build_call("double_click", {"coordinate": [3, 4]}, pid=1, window_id=2, session=None)
    assert d.tool == "double_click" and d.arguments()["x"] == 3 and d.arguments()["button"] == "left"
    r = P.build_call("right_click", {"element": 5}, pid=1, window_id=2, session=None, element_token="s1:5")
    assert r.tool == "click" and r.arguments()["button"] == "right" and r.arguments()["element_token"] == "s1:5"
    assert P.build_call("scroll", {"direction": "down", "amount": 999}, pid=1, window_id=2, session=None).amount == 50
    for bad in ("capture", "wait", "focus_app", "nope"):
        with pytest.raises(UnsupportedAction):
            P.build_call(bad, {}, pid=1, window_id=2, session=None)
    with pytest.raises(P.ProtocolError):
        P.Click(pid=1, window_id=2).validate()
    with pytest.raises(P.ProtocolError):
        P.Click(pid=1, window_id=2, element_index=1, x=1, y=2).validate()
    with pytest.raises(P.ProtocolError):
        P.Hotkey(pid=1, window_id=2, keys=("s",)).validate()
    assert P.MUTATING_TOOLS >= {"click", "type_text", "hotkey", "bring_to_front"} and "get_window_state" not in P.MUTATING_TOOLS
    res = P.ToolResult.from_mcp("click", {"content": [{"type": "text", "text": '{"ok": true}'}], "structuredContent": {"effect": "suspected_noop", "escalation": {"recommended": "px"}}, "isError": False})
    assert res.ok and res.verdict.effect == "suspected_noop" and res.verdict.postcondition() is False and res.verdict.escalation == {"recommended": "px"}
    md = P.ToolResult("get_window_state", False, data='3 elements\n- [1] Button "OK"\n- [2] Edit = "hi" id=name\n')
    assert [(e.index, e.role, e.label) for e in P.parse_elements(md)] == [(1, "Button", "OK"), (2, "Edit", "hi")]
