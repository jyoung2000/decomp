"""MCP client configuration snippets ("Copy MCP config" in the app; `GET /mcp/config`).

Two transports:
  * stdio  - the client starts `rebuild-mcp` itself. Stable across app restarts (recommended).
  * http   - the running desktop app serves the same tools at http://127.0.0.1:<port>/mcp with the controller's
             per-launch bearer token. Valid only while this controller runs (the token and port change on restart).

Formats mirror scripts/install-clients.py (Claude Code / Gemini JSON, Codex TOML, Hermes YAML) plus a generic
`mcpServers` JSON object. Nothing here writes a file; the text is shown and copied by the user.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

CLIENTS = ("claude-code", "codex", "gemini", "hermes", "generic")
TRANSPORTS = ("stdio", "http")
SERVER_KEY = {"claude-code": "rebuild-studio", "codex": "rebuild_studio", "gemini": "rebuild-studio", "hermes": "rebuild_studio",
              "generic": "rebuild-studio"}


def server_command() -> tuple[str, list[str], str]:
    """(command, base args, how it was found) for launching the stdio server on this machine."""
    cands: list[tuple[Path, str]] = []
    inst = os.environ.get("REBUILD_STUDIO_INSTALL")
    if inst:
        cands += [(Path(inst) / "runtime" / "Scripts" / "rebuild-mcp.exe", "installed app (REBUILD_STUDIO_INSTALL)")]
    exe_dir = Path(sys.executable).resolve().parent
    cands += [(exe_dir / "runtime" / "Scripts" / "rebuild-mcp.exe", "next to the controller"),
              (exe_dir / "Scripts" / ("rebuild-mcp.exe" if os.name == "nt" else "rebuild-mcp"), "controller's Python environment"),
              (exe_dir / ("rebuild-mcp.exe" if os.name == "nt" else "rebuild-mcp"), "controller's Python environment")]
    for p, how in cands:
        if p.is_file():
            return str(p), [], how
    found = shutil.which("rebuild-mcp")
    if found:
        return found, [], "PATH"
    return sys.executable, ["-m", "rebuild_controller.mcp.server"], "python module (no rebuild-mcp executable found)"


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def render(client: str, transport: str, *, toolset: str = "all", data_dir: Optional[str] = None, url: Optional[str] = None,
           token: Optional[str] = None) -> dict[str, Any]:
    if client not in CLIENTS:
        raise ValueError(f"client must be one of {', '.join(CLIENTS)}")
    if transport not in TRANSPORTS:
        raise ValueError("transport must be stdio or http")
    key = SERVER_KEY[client]
    notes: list[str] = []
    if transport == "stdio":
        cmd, base, how = server_command()
        args = base + ["--toolset", toolset]
        env = {"REBUILD_STUDIO_DATA": data_dir} if data_dir else {}
        notes.append(f"Starts rebuild-mcp ({how}); works whether or not the desktop app is running.")
        entry: dict[str, Any] = {"command": cmd, "args": args}
        if env:
            entry["env"] = env
        if client == "claude-code":
            text = json.dumps({"mcpServers": {key: {"type": "stdio", **entry}}}, indent=2)
            env_flags = "".join(f" -e {k}={_q(v)}" for k, v in env.items())
            cli = f"claude mcp add --scope user{env_flags} {key} -- {_q(cmd)} " + " ".join(_q(a) for a in args)
            return _out(client, transport, "json", ".mcp.json (project) or ~/.claude.json", text, notes, cli)
        if client == "codex":
            lines = [f"[mcp_servers.{key}]", f"command = {_q(cmd)}", "args = [" + ", ".join(_q(a) for a in args) + "]",
                     "startup_timeout_sec = 30"]
            if env:
                lines += ["", f"[mcp_servers.{key}.env]"] + [f"{k} = {_q(v)}" for k, v in env.items()]
            return _out(client, transport, "toml", "~/.codex/config.toml", "\n".join(lines) + "\n", notes)
        if client == "hermes":
            lines = ["mcp_servers:", f"  {key}:", f"    command: {_q(cmd)}", "    args: [" + ", ".join(_q(a) for a in args) + "]",
                     "    timeout: 120", "    connect_timeout: 60"]
            if env:
                lines += ["    env:"] + [f"      {k}: {_q(v)}" for k, v in env.items()]
            return _out(client, transport, "yaml", "~/.hermes/config.yaml (or %LOCALAPPDATA%\\hermes)", "\n".join(lines) + "\n", notes)
        where = "~/.gemini/settings.json" if client == "gemini" else "your client's MCP settings"
        return _out(client, transport, "json", where, json.dumps({"mcpServers": {key: entry}}, indent=2), notes)

    if not url or not token:
        raise ValueError("http transport needs the controller url and token")
    notes.append("Uses the running desktop app (same tools, loopback only). The token and port change when the app restarts; "
                 "copy the config again after a restart, or use stdio for a permanent setup.")
    headers = {"Authorization": f"Bearer {token}"}
    if client == "claude-code":
        text = json.dumps({"mcpServers": {key: {"type": "http", "url": url, "headers": headers}}}, indent=2)
        cli = f"claude mcp add --transport http --scope user {key} {_q(url)} --header {_q('Authorization: Bearer ' + token)}"
        return _out(client, transport, "json", ".mcp.json (project) or ~/.claude.json", text, notes, cli)
    if client == "codex":
        text = "\n".join([f"[mcp_servers.{key}]", f"url = {_q(url)}", f"http_headers = {{ Authorization = {_q('Bearer ' + token)} }}"]) + "\n"
        return _out(client, transport, "toml", "~/.codex/config.toml", text, notes)
    if client == "gemini":
        return _out(client, transport, "json", "~/.gemini/settings.json",
                    json.dumps({"mcpServers": {key: {"httpUrl": url, "headers": headers}}}, indent=2), notes)
    if client == "hermes":
        text = "\n".join(["mcp_servers:", f"  {key}:", f"    url: {_q(url)}", "    headers:",
                          f"      Authorization: {_q('Bearer ' + token)}", "    timeout: 120"]) + "\n"
        return _out(client, transport, "yaml", "~/.hermes/config.yaml", text, notes)
    return _out(client, transport, "json", "your client's MCP settings",
                json.dumps({"mcpServers": {key: {"type": "http", "url": url, "headers": headers}}}, indent=2), notes)


def _out(client: str, transport: str, fmt: str, where: str, text: str, notes: list[str], command: Optional[str] = None) -> dict[str, Any]:
    out = {"client": client, "transport": transport, "format": fmt, "where": where, "text": text, "notes": notes}
    if command:
        out["command"] = command
    return out
