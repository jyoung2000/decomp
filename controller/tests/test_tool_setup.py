"""Guided tool setup: local HTTP server + fake zip + temp lock. Plus one opt-in live install (REBUILD_LIVE_TOOLS=1)."""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rebuild_controller.api.server import create_app
from rebuild_controller.config import Limits, Settings
from rebuild_controller.services import StudioServices
from rebuild_controller.tool_setup import MARKER, ToolSetup, ToolSetupError

ENTRY = b"fake binary\n"
EXTRA = b"extra data\n"


def make_zip(root: str = "pkg", extra_members: dict[str, bytes] | None = None, entry: bytes = ENTRY) -> bytes:
    buf = io.BytesIO()
    pre = f"{root}/" if root else ""
    def zi(n: str) -> zipfile.ZipInfo:   # fixed timestamp: two make_zip() calls must be byte-identical
        return zipfile.ZipInfo(n, date_time=(2020, 1, 1, 0, 0, 0))

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zi(pre + "bin/tool.exe"), entry)
        z.writestr(zi(pre + "data/extra.bin"), EXTRA)
        z.writestr(zi("outside-root.txt"), b"ignored")
        for k, v in (extra_members or {}).items():
            z.writestr(zi(k), v)
    return buf.getvalue()


class Server:
    def __init__(self):
        self.routes: dict[str, tuple[int, bytes, float]] = {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                status, body, delay = outer.routes.get(self.path, (404, b"", 0))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    if delay:
                        step = max(1, len(body) // 20)
                        for i in range(0, len(body), step):
                            self.wfile.write(body[i:i + step])
                            self.wfile.flush()
                            time.sleep(delay)
                    else:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = Server()
    yield s
    s.close()


def lock_doc(server: Server, blob: bytes, *, sha: str | None = "auto", size: int | None = -1, root: str = "pkg",
             with_dep: bool = False, entry_sha: str | None = "auto") -> dict:
    sha = hashlib.sha256(blob).hexdigest() if sha == "auto" else sha
    size = len(blob) if size == -1 else size
    entry_sha = hashlib.sha256(ENTRY).hexdigest() if entry_sha == "auto" else entry_sha
    tools = {
        "faketool": {
            "version": "1.0", "role": "fake", "license": "MIT",
            "artifact": {"name": "fake.zip", "url": server.url("/fake.zip"), "size_bytes": size, "sha256": sha, "verify_required": sha is None},
            "layout": {"archive_root": root, "entry": "bin/tool.exe", "entry_sha256": entry_sha,
                       "extra_files": {"data/extra.bin": hashlib.sha256(EXTRA).hexdigest()},
                       **({"requires": ["basetool"]} if with_dep else {})},
            "install_dir": "faketool",
        }
    }
    if with_dep:
        tools["basetool"] = {
            "version": "2.0", "role": "base", "license": "MIT",
            "artifact": {"name": "base.zip", "url": server.url("/base.zip"), "size_bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()},
            "layout": {"archive_root": root, "entry": "bin/tool.exe", "entry_sha256": hashlib.sha256(ENTRY).hexdigest()},
            "install_dir": "basetool",
        }
    return {"schema_version": 1, "tools": tools}


@pytest.fixture
def make(tmp_path, server):
    def _make(blob: bytes | None = None, **kw) -> tuple[ToolSetup, Path]:
        blob = blob if blob is not None else make_zip()
        server.routes["/fake.zip"] = (200, blob, 0)
        server.routes["/base.zip"] = (200, blob, 0)
        lock = tmp_path / "lock.json"
        lock.write_text(json.dumps(lock_doc(server, blob, **kw)))
        tools = tmp_path / "tools"
        st = Settings(data_dir=tmp_path / "data", tools_dir=tools, limits=Limits())
        return ToolSetup(st, None, lock, allow_insecure_loopback=True), tools
    return _make


def run(ts: ToolSetup, name: str, **kw) -> dict:
    ts.install(name, **kw)
    assert ts.join(30)
    return ts.status(name)


def test_install_success_writes_marker_and_layout(make):
    blob = make_zip()
    ts, tools = make(blob)
    assert ts.status("faketool")["status"] == "not_installed"
    s = run(ts, "faketool")
    assert s["status"] == "installed", s
    d = tools / "faketool"
    assert (d / "bin" / "tool.exe").read_bytes() == ENTRY            # archive_root stripped
    assert not (d / "outside-root.txt").exists()
    marker = json.loads((d / MARKER).read_text())
    assert marker["name"] == "faketool" and marker["version"] == "1.0"
    assert marker["sha256"] == hashlib.sha256(blob).hexdigest() and marker["installed_at"]
    assert not list((tools / ".downloads").glob("*.part"))
    assert not list((tools / ".staging").iterdir())
    assert ts.snapshot()["any_installed"] is True


def test_wrong_hash_refused_nothing_installed(make):
    ts, tools = make(sha="0" * 64)
    s = run(ts, "faketool")
    assert s["status"] == "not_installed" and s["job"]["phase"] == "failed"
    err = s["job"]["error"]
    assert err["code"] == "checksum_mismatch"
    assert "does not match the pinned checksum; nothing was installed" in err["message"]
    assert not (tools / "faketool").exists()
    assert not list((tools / ".downloads").iterdir())


def test_size_mismatch_refused(make):
    ts, tools = make(size=10)
    s = run(ts, "faketool")
    assert s["job"]["error"]["code"] == "size_mismatch"
    assert not (tools / "faketool").exists() and not list((tools / ".downloads").iterdir())


def test_entry_hash_mismatch_refused(make):
    ts, tools = make(entry_sha="1" * 64)
    s = run(ts, "faketool")
    assert s["job"]["error"]["code"] == "checksum_mismatch" and not (tools / "faketool").exists()
    assert not list((tools / ".staging").iterdir())


def test_blocked_unverified_is_refused_synchronously(make):
    ts, tools = make(sha=None)
    assert ts.status("faketool")["status"] == "blocked_unverified"
    with pytest.raises(ToolSetupError) as ei:
        ts.install("faketool")
    assert ei.value.code == "blocked_unverified"
    assert not (tools / "faketool").exists()


def test_server_500_is_retryable_then_retry_succeeds(make, server):
    ts, tools = make()
    server.routes["/fake.zip"] = (500, b"boom", 0)
    s = run(ts, "faketool")
    err = s["job"]["error"]
    assert err["code"] == "download_failed" and err["retryable"] is True and "500" in err["message"]
    assert not list((tools / ".downloads").iterdir())
    server.routes["/fake.zip"] = (200, make_zip(), 0)
    assert run(ts, "faketool")["status"] == "installed"


def test_connection_refused_gives_offline_guidance(make, server):
    ts, tools = make()
    server.close()
    s = run(ts, "faketool")
    err = s["job"]["error"]
    assert err["code"] == "offline" and err["retryable"] is True
    assert "/fake.zip" in err["message"] and "Install from file" in err["next_action"]


def test_cancel_mid_download_cleans_part(make, server):
    blob = make_zip(extra_members={"pkg/big.bin": os.urandom(400_000)})
    ts, tools = make(blob)
    server.routes["/fake.zip"] = (200, blob, 0.05)
    ts.install("faketool")
    deadline = time.time() + 10
    while time.time() < deadline and ts.status("faketool")["job"]["bytes_done"] == 0:
        time.sleep(0.02)
    assert ts.status("faketool")["job"]["bytes_done"] > 0
    ts.cancel("faketool")
    assert ts.join(15)
    s = ts.status("faketool")
    assert s["status"] == "not_installed" and s["job"]["phase"] == "cancelled"
    assert not list((tools / ".downloads").iterdir())
    assert not (tools / "faketool").exists()
    server.routes["/fake.zip"] = (200, blob, 0)
    assert run(ts, "faketool")["status"] == "installed"   # retry after cancel works


@pytest.mark.parametrize("member", ["../evil.txt", "pkg/../../evil.txt", "/abs/evil.txt", "C:/evil.txt"])
def test_zip_slip_member_refused(make, member, tmp_path):
    blob = make_zip(extra_members={member: b"pwned"})
    ts, tools = make(blob)
    s = run(ts, "faketool")
    assert s["job"]["error"]["code"] == "unsafe_archive", s["job"]
    assert not (tools / "faketool").exists()
    assert not (tmp_path / "evil.txt").exists() and not (tools / "evil.txt").exists()


def test_symlink_member_refused(make):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pkg/bin/tool.exe", ENTRY)
        zi = zipfile.ZipInfo("pkg/link")
        zi.external_attr = 0o120777 << 16
        z.writestr(zi, "bin/tool.exe")
    ts, tools = make(buf.getvalue())
    assert run(ts, "faketool")["job"]["error"]["code"] == "unsafe_archive"


def test_dependency_installs_first(make):
    ts, tools = make(with_dep=True)
    order = []
    orig = ts._install_one

    def spy(n, c, loc):
        order.append(n)
        return orig(n, c, loc)

    ts._install_one = spy
    s = run(ts, "faketool")
    assert order == ["basetool", "faketool"]
    assert s["status"] == "installed" and ts.status("basetool")["status"] == "installed"


def test_dependency_failure_fails_dependent(make, server):
    ts, tools = make(with_dep=True)
    server.routes["/base.zip"] = (500, b"", 0)
    s = run(ts, "faketool")
    assert s["job"]["error"]["code"] == "dependency_failed"
    assert not (tools / "faketool").exists()


def test_remove_blocked_while_dependent_installed(make):
    ts, tools = make(with_dep=True)
    run(ts, "faketool")
    with pytest.raises(ToolSetupError) as ei:
        ts.remove("basetool")
    assert ei.value.code == "in_use"
    ts.remove("faketool")
    ts.remove("basetool")
    assert not (tools / "faketool").exists() and not (tools / "basetool").exists()


def test_install_from_file_verifies_and_installs(make, tmp_path):
    ts, tools = make()
    f = tmp_path / "dl" / "fake.zip"
    f.parent.mkdir()
    f.write_bytes(make_zip())
    ts.install_from_file("faketool", f)
    assert ts.join(30)
    assert ts.status("faketool")["status"] == "installed"
    assert json.loads((tools / "faketool" / MARKER).read_text())["source"] == "file"
    assert f.exists()   # the user's file is left alone


def test_install_from_file_wrong_file_refused(make, tmp_path):
    ts, tools = make()
    f = tmp_path / "other.zip"
    f.write_bytes(b"not it at all")
    ts.install_from_file("faketool", f)
    assert ts.join(30)
    s = ts.status("faketool")
    assert s["job"]["error"]["code"] in ("size_mismatch", "checksum_mismatch")
    assert not (tools / "faketool").exists() and f.exists()
    with pytest.raises(ToolSetupError):
        ts.install_from_file("faketool", tmp_path / "missing.zip")


def test_corrupt_detected_and_reinstall_repairs(make):
    ts, tools = make()
    run(ts, "faketool")
    (tools / "faketool" / "bin" / "tool.exe").write_bytes(b"tampered")
    ts._verified.clear()
    assert ts.status("faketool")["status"] == "corrupt"
    assert run(ts, "faketool")["status"] == "installed"
    assert (tools / "faketool" / "bin" / "tool.exe").read_bytes() == ENTRY


def test_progress_events_published(make, tmp_path):
    from rebuild_controller.events import EventLog
    from rebuild_controller.store.db import Database
    db = Database(tmp_path / "ev.sqlite3")
    ev = EventLog(db)
    seen = []
    ev.subscribe(seen.append)
    ts, tools = make()
    ts.events = ev
    run(ts, "faketool")
    kinds = [e["kind"] for e in seen]
    assert "tools.setup.started" in kinds and "tools.setup.progress" in kinds and "tools.setup.done" in kinds
    db.close()


def test_api_routes_and_error_shape(settings, tmp_path, server):
    blob = make_zip()
    server.routes["/fake.zip"] = (200, blob, 0)
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps(lock_doc(server, blob, sha="0" * 64)))
    st = StudioServices(settings)
    app = create_app(st, "tok")
    setup = app.state.tool_setup
    setup.lock_path, setup._lock_doc, setup.allow_insecure_loopback = lock, None, True
    with TestClient(app) as c:
        c.headers.update({"Authorization": "Bearer tok", "Origin": "http://localhost:5173"})
        snap = c.get("/tools/setup").json()
        assert snap["tools"][0]["name"] == "faketool" and snap["tools"][0]["status"] == "not_installed"
        assert c.post("/tools/setup/nope/install").json()["error"]["code"] == "unknown_tool"
        assert c.post("/tools/setup/faketool/install").status_code == 200
        assert setup.join(30)
        job = c.get("/tools/setup").json()["tools"][0]["job"]
        assert job["error"]["code"] == "checksum_mismatch"
        r = c.post("/tools/setup/faketool/install-from-file", json={"path": str(tmp_path / "nope.zip")})
        assert r.status_code == 400 and set(r.json()["error"]) >= {"code", "message", "affected", "next_action"}
        assert c.post("/tools/setup/faketool/cancel").json()["error"]["code"] == "not_running"
        assert c.delete("/tools/setup/faketool").status_code == 200
    st.stop()


def test_packaged_lock_copy_matches_docs():
    here = Path(__file__).resolve().parents[1] / "rebuild_controller" / "data" / "dependency-lock.json"
    docs = Path(__file__).resolve().parents[2] / "docs" / "dependency-lock.json"
    assert json.loads(here.read_text(encoding="utf-8")) == json.loads(docs.read_text(encoding="utf-8"))


def test_real_lock_lists_expected_tools():
    from rebuild_controller.tool_setup import find_lock_path
    ts = ToolSetup(Settings(tools_dir=Path("nonexistent-tools-dir")), None, find_lock_path())
    snap = {t["name"]: t for t in ts.snapshot()["tools"]}
    assert {"rizin", "gdre", "ilspycmd", "dotnet-runtime", "temurin-jre", "cfr", "jadx", "node"} <= set(snap)
    assert snap["ilspycmd"]["requires"] == ["dotnet-runtime"] and snap["node"]["optional"]


def test_default_tools_dir_is_platform_aware(monkeypatch, tmp_path):
    from rebuild_controller.config import default_tools_dir
    monkeypatch.delenv("REBUILD_STUDIO_TOOLS", raising=False)
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert default_tools_dir() == tmp_path / "RebuildStudio" / "tools"
    monkeypatch.setenv("REBUILD_STUDIO_TOOLS", str(tmp_path / "x"))
    assert default_tools_dir() == tmp_path / "x"


# ------------------------------------------------------------------------------------------------ live (opt-in)
FIX = Path(__file__).resolve().parents[2] / "fixtures" / "dotnetapp" / "original" / "dotnetapp.dll"


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("REBUILD_LIVE_TOOLS"), reason="set REBUILD_LIVE_TOOLS=1 to download the real tools (~37 MB)")
@pytest.mark.skipif(not FIX.exists(), reason="fixtures/dotnetapp built dll missing")
def test_live_install_dotnet_and_ilspycmd_then_decompile(tmp_path):
    from rebuild_controller.backends.ilspy import ILSpyBackend
    from rebuild_controller.tool_setup import find_lock_path
    tools = tmp_path / "tools"
    st = Settings(data_dir=tmp_path / "data", tools_dir=tools)
    ts = ToolSetup(st, None, find_lock_path())
    t0 = time.time()
    ts.install("ilspycmd")                       # pulls dotnet-runtime first
    assert ts.join(600)
    snap = {t["name"]: t for t in ts.snapshot()["tools"]}
    assert snap["dotnet-runtime"]["status"] == "installed", snap["dotnet-runtime"]
    assert snap["ilspycmd"]["status"] == "installed", snap["ilspycmd"]
    print(f"live install took {time.time() - t0:.1f}s; runtime={snap['dotnet-runtime']['installed_version']} ilspycmd={snap['ilspycmd']['installed_version']}")
    be = ILSpyBackend(st)
    exe = be.find_tool()
    assert exe is not None and exe.name == "ilspycmd.dll"
    out = tmp_path / "out"
    res = be.decompile(FIX, out)
    assert res.ok, res.error
    cs = list(out.rglob("*.cs"))
    assert cs, "no C# files produced"
    assert any("class" in p.read_text(encoding="utf-8", errors="replace") for p in cs)
