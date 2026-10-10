"""R2: the MCP tools served by the running controller over streamable HTTP (loopback + the controller's bearer token),
and the "Copy MCP config" endpoint."""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

pytest.importorskip("mcp")
from fastapi.testclient import TestClient  # noqa: E402

from rebuild_controller.api.server import create_app  # noqa: E402
from rebuild_controller.mcp.server import TOOLSETS  # noqa: E402
from rebuild_controller.services import StudioServices  # noqa: E402

TOKEN = "t0k3n-mcp"
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


@pytest.fixture
def client(settings):
    st = StudioServices(settings)
    app = create_app(st, TOKEN)
    with TestClient(app, base_url="http://127.0.0.1:48123") as c:
        yield c, st
    st.stop()


def rpc(c, body, token=TOKEN, **headers):
    h = dict(ACCEPT)
    if token:
        h["Authorization"] = f"Bearer {token}"
    h.update(headers)
    return c.post("/mcp", content=json.dumps(body), headers=h)


def test_mcp_http_requires_the_controller_token(client):
    c, _ = client
    assert rpc(c, INIT, token=None).status_code == 401
    assert rpc(c, INIT, token="wrong").status_code == 401
    assert rpc(c, INIT, Origin="http://evil.example").status_code == 403


def test_mcp_http_serves_the_same_tools(client):
    c, _ = client
    r = rpc(c, INIT)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "rebuild-studio"
    r = rpc(c, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == set(TOOLSETS["all"])
    r = rpc(c, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_cases", "arguments": {}}})
    env = json.loads(r.json()["result"]["content"][0]["text"])
    assert env["ok"] and env["data"]["cases"] == []
    r = rpc(c, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "list_sessions", "arguments": {"shell": "x"}}})
    assert r.json()["result"]["isError"] is True, "unknown arguments are rejected over HTTP too"


def test_mcp_http_rejects_foreign_host_header(client):
    c, _ = client
    r = rpc(c, INIT, Host="evil.example")
    assert r.status_code in (400, 403, 421)


@pytest.mark.parametrize("cl", ["claude-code", "codex", "gemini", "hermes", "generic"])
def test_mcp_config_snippets(client, cl):
    c, st = client
    h = {"Authorization": f"Bearer {TOKEN}"}
    s = c.get(f"/mcp/config?client={cl}&transport=stdio", headers=h).json()
    assert s["transport"] == "stdio" and "--toolset" in s["text"] and TOKEN not in s["text"]
    assert str(st.settings.data_dir).replace("\\", "\\\\") in s["text"] or str(st.settings.data_dir) in s["text"]
    hs = c.get(f"/mcp/config?client={cl}&transport=http&toolset=re", headers=h).json()
    assert "http://127.0.0.1:48123/mcp" in hs["text"] and f"Bearer {TOKEN}" in hs["text"]
    assert any("restart" in n for n in hs["notes"])
    if cl in ("claude-code", "gemini", "generic"):
        json.loads(s["text"]); json.loads(hs["text"])
    if cl == "codex":
        import tomllib
        assert tomllib.loads(s["text"])["mcp_servers"]["rebuild_studio"]["args"][-2:] == ["--toolset", "all"]
        assert tomllib.loads(hs["text"])["mcp_servers"]["rebuild_studio"]["url"].endswith("/mcp")
    if cl == "claude-code":
        assert s["command"].startswith("claude mcp add")


def test_mcp_config_validation(client):
    c, _ = client
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert c.get("/mcp/config?client=vim", headers=h).status_code == 400
    assert c.get("/mcp/config?transport=pigeon", headers=h).status_code == 400
    assert c.get("/mcp/config?toolset=everything", headers=h).status_code == 400
    assert c.get("/mcp/config").status_code == 401


async def test_real_mcp_client_over_http(settings):
    """A real MCP client (streamable HTTP transport) against a real uvicorn server on loopback."""
    import httpx2
    import uvicorn
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    st = StudioServices(settings)
    app = create_app(st, TOKEN)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
        async with Client(streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http)) as c:
            names = {x.name for x in (await c.list_tools()).tools}
            assert "open_binary" in names and "decompile" in names
            r = await c.call_tool("list_sessions", {})
            assert json.loads(r.content[0].text)["ok"] is True
    finally:
        server.should_exit = True
        t.join(timeout=10)
        st.stop()


def test_docs_tool_reference_is_current():
    """docs/API.md embeds `rebuild-mcp --list-tools --markdown`; regenerate it when tools change."""
    from pathlib import Path
    from rebuild_controller.mcp.server import DOC_BEGIN, DOC_END, tool_reference_markdown
    doc = (Path(__file__).resolve().parents[2] / "docs" / "API.md").read_text("utf-8").replace("\r\n", "\n")
    start, end = doc.index(DOC_BEGIN), doc.index(DOC_END) + len(DOC_END)
    assert doc[start:end] + "\n" == tool_reference_markdown(), "regenerate: rebuild-mcp --list-tools --markdown"
    for name in TOOLSETS["re"]:
        assert f"| `{name}` |" in doc
