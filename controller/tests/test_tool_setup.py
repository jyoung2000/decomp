"""Guided tool setup: local HTTP server + fake zip + temp lock. Plus one opt-in live install (REBUILD_LIVE_TOOLS=1)."""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import sys
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


# ------------------------------------------------------------------------------------------------ post_install step
FAKE_INSTALLER = r"""
import os, sys, time
mode, out = sys.argv[1], sys.argv[2]
print("step 1: starting", flush=True)
if mode == "hang":
    for i in range(600):
        print(f"step {i + 2}: working", flush=True)
        time.sleep(0.2)
if mode == "fail":
    print("boom: network unreachable", file=sys.stderr, flush=True)
    sys.exit(3)
if mode == "ok":
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(os.environ.get("FAKE_ENV", "") + "|" + os.environ.get("FAKE_HOME", ""))
    print("installed", flush=True)
"""
needs_cmd = pytest.mark.skipif(os.name != "nt", reason="the fake installer is a .cmd wrapper (post_install is only used on Windows)")


def post_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("run.cmd", date_time=(2020, 1, 1, 0, 0, 0)), f'@"{sys.executable}" "%~dp0fake_installer.py" %*\r\n')
        z.writestr(zipfile.ZipInfo("fake_installer.py", date_time=(2020, 1, 1, 0, 0, 0)), FAKE_INSTALLER)
    return buf.getvalue()


@pytest.fixture
def make_post(tmp_path, server):
    def _make(mode: str = "ok", *, timeout: float = 30, produces=("out/made.txt",), argv0: str | None = None):
        blob = post_zip()
        server.routes["/p.zip"] = (200, blob, 0)
        lock = {"schema_version": 1, "tools": {"posttool": {
            "version": "1.0", "role": "fake", "license": "MIT",
            "footprint": {"installer_download_bytes": 5, "disk_bytes": 10},
            "artifact": {"name": "p.zip", "url": server.url("/p.zip"), "size_bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()},
            "layout": {"archive_root": "", "entry": "run.cmd", "post_install": {
                "argv": [argv0 or "{staged}/run.cmd", mode, "{staged}/out/made.txt"],
                "env": {"FAKE_ENV": "{tools}/x", "FAKE_HOME": "{staged}/home"},
                "timeout_seconds": timeout, "progress_message": "Installing the fake toolchain", "log_name": "fake.log",
                "produces": list(produces)}},
            "install_dir": "posttool"}}}
        lp = tmp_path / "plock.json"
        lp.write_text(json.dumps(lock))
        tools = tmp_path / "ptools"
        st = Settings(data_dir=tmp_path / "pdata", tools_dir=tools, limits=Limits())
        return ToolSetup(st, None, lp, allow_insecure_loopback=True), tools
    return _make


def staging_empty(tools: Path) -> bool:
    return not any((tools / ".staging").glob("posttool-*"))


@needs_cmd
def test_post_install_success_activates_with_expanded_env_and_log(make_post):
    ts, tools = make_post("ok")
    s = run(ts, "posttool")
    assert s["status"] == "installed", s
    env_tools, env_staged = (tools / "posttool" / "out" / "made.txt").read_text().split("|")
    assert env_tools.replace("\\", "/") == str(tools).replace("\\", "/") + "/x"               # {tools} expanded
    assert ".staging" in env_staged and env_staged.replace("\\", "/").endswith("/home")       # {staged} expanded
    assert "installed" in (tools / ".logs" / "fake.log").read_text()
    assert (tools / "posttool" / MARKER).is_file() and staging_empty(tools)
    assert s["footprint"] == {"installer_download_bytes": 5, "disk_bytes": 10}
    (tools / "posttool" / "out" / "made.txt").unlink()           # a produced file that vanished => damaged, repairable
    assert ts.status("posttool")["status"] == "corrupt"


@needs_cmd
def test_post_install_nonzero_exit_activates_nothing_and_is_retryable(make_post):
    ts, tools = make_post("fail")
    s = run(ts, "posttool")
    assert s["status"] == "not_installed" and not (tools / "posttool").exists()
    err = s["job"]["error"]
    assert err["code"] == "post_install_failed" and err["retryable"] is True and "exit 3" in err["message"] and "nothing was installed" in err["message"]
    assert "boom" in (tools / ".logs" / "fake.log").read_text() and staging_empty(tools)
    assert "fake.log" in err["next_action"]


@needs_cmd
def test_post_install_missing_declared_output_activates_nothing(make_post):
    ts, tools = make_post("ok", produces=("out/made.txt", "out/never.txt"))
    s = run(ts, "posttool")
    assert s["status"] == "not_installed" and s["job"]["error"]["code"] == "post_install_failed" and "out/never.txt" in s["job"]["error"]["message"]
    assert not (tools / "posttool").exists() and staging_empty(tools)


@needs_cmd
def test_post_install_timeout_kills_the_installer_and_activates_nothing(make_post):
    ts, tools = make_post("hang", timeout=1.5)
    t0 = time.time()
    s = run(ts, "posttool")
    assert time.time() - t0 < 20
    assert s["status"] == "not_installed" and s["job"]["error"]["code"] == "post_install_timeout" and s["job"]["error"]["retryable"] is True
    assert not (tools / "posttool").exists() and staging_empty(tools)
    n = (tools / ".logs" / "fake.log").read_text().count("working")
    time.sleep(1.0)
    assert (tools / ".logs" / "fake.log").read_text().count("working") == n      # the process tree is really gone


@needs_cmd
def test_post_install_cancel_stops_the_installer_and_shows_progress(make_post):
    ts, tools = make_post("hang", timeout=60)
    ts.install("posttool")
    deadline = time.time() + 30
    job = None
    while time.time() < deadline:
        job = ts.status("posttool")["job"]
        if job and job["phase"] == "installing" and "step" in job["message"]:
            break
        time.sleep(0.1)
    assert job and job["phase"] == "installing" and job["message"].startswith("Installing the fake toolchain"), job
    ts.cancel("posttool")
    assert ts.join(30)
    s = ts.status("posttool")
    assert s["status"] == "not_installed" and s["job"]["cancelled"] is True and s["job"]["error"] is None
    assert not (tools / "posttool").exists() and staging_empty(tools)
    n = (tools / ".logs" / "fake.log").read_text().count("working")
    time.sleep(1.0)
    assert (tools / ".logs" / "fake.log").read_text().count("working") == n


@needs_cmd
def test_post_install_program_must_live_in_the_staged_download(make_post):
    ts, tools = make_post("ok", argv0=sys.executable)
    s = run(ts, "posttool")
    assert s["status"] == "not_installed" and s["job"]["error"]["code"] == "bad_lock" and not (tools / "posttool").exists()


def test_real_lock_has_private_rust_entry():
    from rebuild_controller.tool_setup import FRIENDLY, find_lock_path
    ts = ToolSetup(Settings(tools_dir=Path("nonexistent-tools-dir")), None, find_lock_path())
    t = {x["name"]: x for x in ts.snapshot()["tools"]}["rust"]
    assert t["title"] == FRIENDLY["rust"][0] == "Rust compiler (private)" and "Windows .exe" in t["purpose"] and not t["optional"]
    assert t["status"] == "not_installed" and t["blocked_reason"] is None and len(t["sha256"]) == 64
    assert t["url"] == "https://static.rust-lang.org/rustup/archive/1.29.1/x86_64-pc-windows-msvc/rustup-init.exe"
    assert t["footprint"]["disk_bytes"] > 100_000_000 and t["footprint"]["installer_download_bytes"] > 50_000_000
    lock = json.loads(Path(ts.lock_path).read_text(encoding="utf-8"))["tools"]["rust"]
    assert lock["artifact"]["sha256"] == lock["artifact"]["sha256_official"] == lock["layout"]["entry_sha256"]
    pi = lock["layout"]["post_install"]
    assert pi["argv"][1:] == ["--default-host", "x86_64-pc-windows-gnu", "--default-toolchain", lock["version"], "--profile", "minimal", "--no-modify-path", "-y"]
    assert pi["env"]["RUSTUP_HOME"] == "{staged}/rustup" and pi["env"]["CARGO_HOME"] == "{staged}/cargo"
    assert all(p.startswith(f"rustup/toolchains/{lock['version']}-x86_64-pc-windows-gnu/bin/") for p in pi["produces"])


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


@pytest.mark.live
@pytest.mark.skipif(os.name != "nt" or not os.environ.get("REBUILD_LIVE_TOOLS"), reason="opt-in (REBUILD_LIVE_TOOLS=1): downloads Node.js + playwright-core")
def test_live_installed_app_browser_comparison_uses_tools_page_node_playwright_and_edge(tmp_path, monkeypatch):
    """What the installed (frozen) app does: Node.js + playwright-core from the Tools page, Microsoft Edge as the browser."""
    import sys
    import time as _t
    from rebuild_controller.comparators import web
    from rebuild_controller.config import Settings, set_settings
    tools = tmp_path / "tools"
    s = Settings(data_dir=tmp_path / "data"); s.tools_dir = tools; s.ensure_dirs(); set_settings(s)
    ts = ToolSetup(s)
    ts.install("playwright-core")          # pulls in its dependency (node) first
    deadline = _t.monotonic() + 600
    while ts.status("playwright-core")["status"] != "installed" and _t.monotonic() < deadline:
        assert ts.status("playwright-core")["status"] in ("installing", "not_installed"), ts.status("playwright-core")
        _t.sleep(1)
    assert ts.status("node")["status"] == "installed" and ts.status("playwright-core")["status"] == "installed"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("PATH", os.environ.get("SystemRoot", r"C:\Windows") + r"\System32")   # no developer node on PATH
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "none"))                  # no downloaded Chromium
    assert web.harness_dir() == tools / "harness"
    assert web.web_available() == (True, "ok"), web.web_available()
    assert web.chromium_path().lower().endswith("msedge.exe")
    site = tmp_path / "site"; site.mkdir()
    (site / "index.html").write_text("<!doctype html><title>t</title><h1 id=h>Hello installed app</h1>", encoding="utf-8")
    rec = web.run_web_scenario(site.joinpath("index.html").as_uri(), {"text_selectors": ["#h"], "screenshot": False, "sw": False}, tmp_path / "out")
    assert "Hello installed app" in json.dumps(rec)


EXAMPLE_PECLI = Path(__file__).resolve().parents[2] / "examples" / "pecli-rust-from-evidence"


@pytest.mark.live
@pytest.mark.skipif(os.name != "nt" or not os.environ.get("REBUILD_LIVE_TOOLS"),
                    reason="opt-in (REBUILD_LIVE_TOOLS=1): installs the real private Rust toolchain (~147 MB download, ~850 MB on disk)")
@pytest.mark.skipif(not (EXAMPLE_PECLI / "src" / "main.rs").exists(), reason="examples/pecli-rust-from-evidence missing")
def test_live_install_private_rust_then_build_pecli_with_clean_path(tmp_path, monkeypatch):
    """A clean machine: nothing but System32 on PATH, no Visual Studio, no admin. Guided setup installs the GNU toolchain
    privately, and the Rust builder (through the sandbox) compiles the pecli remake with it."""
    import shutil
    import subprocess
    from types import SimpleNamespace
    from rebuild_controller.builders import rust
    from rebuild_controller.tool_setup import find_lock_path
    monkeypatch.setenv("PATH", os.environ.get("SystemRoot", r"C:\Windows") + r"\System32")
    for k in ("RUSTUP_HOME", "CARGO_HOME", "RUSTUP_TOOLCHAIN", "CARGO_BUILD_TARGET", "RUSTFLAGS", "RUSTC"):
        monkeypatch.delenv(k, raising=False)
    assert shutil.which("cargo") is None and shutil.which("gcc") is None and shutil.which("link") is None
    tools = tmp_path / "tools"
    st = Settings(data_dir=tmp_path / "data", tools_dir=tools)
    ts = ToolSetup(st, None, find_lock_path())
    t0 = time.time()
    ts.install("rust")
    assert ts.join(2400)
    took = time.time() - t0
    s = ts.status("rust")
    assert s["status"] == "installed", (s["job"], (tools / ".logs" / "rust-install.log").read_text(errors="replace")[-2000:])
    size = sum(f.stat().st_size for f in (tools / "rust").rglob("*") if f.is_file())
    print(f"live rust install: {took:.0f}s, {size / 1e6:.0f} MB on disk, toolchain {rust.private_rust(tools)['toolchain']}")
    p = rust.private_rust(tools)
    assert p and p["toolchain"] == "1.97.0-x86_64-pc-windows-gnu"
    assert "rustc 1.97.0" in subprocess.run([p["rustc"], "--version"], capture_output=True, text=True).stdout
    # the real builder, in the sandbox, case-local CARGO_HOME, PATH restricted
    src = tmp_path / "cand" / "source"
    shutil.copytree(EXAMPLE_PECLI, src, ignore=shutil.ignore_patterns("evidence", "target", "*.py", "README.md"))
    logs: list[str] = []
    ctx = SimpleNamespace(job=SimpleNamespace(case_id=None, inputs={}), services={"studio": SimpleNamespace(settings=st)}, limits=st.limits,
                          heartbeat=lambda *a, **k: None, log=lambda m, **k: logs.append(m),
                          run=lambda cmd, timeout=30: SimpleNamespace(text=subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout))
    t1 = time.time()
    info = rust.build_rust(ctx, src, tmp_path / "cand" / "dist")
    print(f"live pecli build: {time.time() - t1:.1f}s; {info['toolchain']}; private={info['private_toolchain']}")
    exe = Path(info["binary"])
    assert exe.is_file() and exe.suffix == ".exe" and info["private_toolchain"] == p["toolchain"]
    assert "rustc 1.97.0" in info["toolchain"] and info["build_isolation"]["mode"] == "low"
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert r.returncode in (0, 1, 2) and (r.stdout or r.stderr).strip(), (r.returncode, r.stdout, r.stderr)
    ts.remove("rust")
    assert not (tools / "rust").exists()
