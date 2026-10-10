"""R10 dependency health: fake tool dirs, a fake download server (with HTTP Range), fake services. No real tools needed."""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import socket
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rebuild_controller import dependency_health as dhm
from rebuild_controller.api.server import create_app
from rebuild_controller.config import Limits, Settings
from rebuild_controller.dependency_health import DependencyHealth, InstallQueue, lock_hash_problems, quick_profile
from rebuild_controller.services import StudioServices
from rebuild_controller.tool_setup import MARKER, ToolSetup

REPO = Path(__file__).resolve().parents[2]
ENTRY = b"fake tool\n"


def make_zip(entry: bytes = ENTRY, pad: int = 0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr(zipfile.ZipInfo("pkg/bin/tool.exe", date_time=(2020, 1, 1, 0, 0, 0)), entry)
        if pad:
            z.writestr(zipfile.ZipInfo("pkg/pad.bin", date_time=(2020, 1, 1, 0, 0, 0)), bytes(range(256)) * (pad // 256))
    return buf.getvalue()


class RangeServer:
    """Serves blobs; honours Range; ``drop_after[path] = n`` closes the connection after n bytes (once)."""

    def __init__(self):
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.drop_after: dict[str, int] = {}
        self.range_requests: list[str] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                status, body = outer.routes.get(self.path, (404, b""))
                rng = self.headers.get("Range")
                start = 0
                if rng and status == 200:
                    outer.range_requests.append(rng)
                    start = int(rng.split("=")[1].split("-")[0])
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
                else:
                    self.send_response(status)
                chunk = body[start:]
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                cut = outer.drop_after.pop(self.path, None)
                try:
                    if cut is not None:
                        self.wfile.write(chunk[:cut])
                        self.wfile.flush()
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                    self.wfile.write(chunk)
                except OSError:
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, p: str) -> str:
        return f"http://127.0.0.1:{self.port}{p}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = RangeServer()
    yield s
    s.close()


def _tool(server: RangeServer, name: str, blob: bytes, *, requires=(), optional=False, sha: str | None = None) -> dict:
    server.routes[f"/{name}.zip"] = (200, blob)
    return {"version": "1.0", "role": name, "license": "MIT", "optional": optional,
            "artifact": {"name": f"{name}.zip", "url": server.url(f"/{name}.zip"), "size_bytes": len(blob),
                         "sha256": sha or hashlib.sha256(blob).hexdigest()},
            "layout": {"archive_root": "pkg", "entry": "bin/tool.exe", "entry_sha256": hashlib.sha256(ENTRY).hexdigest(),
                       **({"requires": list(requires)} if requires else {})},
            "install_dir": name}


NEEDS = {
    "schema_version": 1,
    "core": {"title": "Core", "recommended": ["core"]},
    "profiles": {"web": {"title": "Web apps", "required": ["webtool"], "optional": ["extra"]}, "unknown": {"title": "Unknown"}},
    "targets": {"rust": {"title": "Rust", "required": ["compiler"]}, "web": {"title": "HTML"}},
    "auto_target": {"web": ["web"], "default": "rust"},
    "comparators": {"web": {"title": "browser comparisons", "required": ["browserlib"], "services": ["browser"]}, "cli": {}},
    "services": {"browser": {"title": "Browser"}, "local_ai_server": {"title": "Local AI"}},
}


@pytest.fixture
def env(tmp_path, server, monkeypatch):
    """A studio with a fake lock (base <- webtool, compiler, browserlib, extra, core) next to a test needs table."""
    def build(*, svc_state="ok", needs=None, extra_tools=None):
        blob = make_zip()
        tools = {
            "base": _tool(server, "base", blob),
            "webtool": _tool(server, "webtool", blob, requires=["base"]),
            "compiler": _tool(server, "compiler", make_zip(pad=2_000_000)),
            "browserlib": _tool(server, "browserlib", blob, optional=True),
            "extra": _tool(server, "extra", blob, optional=True),
            "core": _tool(server, "core", blob),
            **(extra_tools or {}),
        }
        lockdir = tmp_path / "lock"
        lockdir.mkdir(exist_ok=True)
        (lockdir / "dependency-lock.json").write_text(json.dumps({"schema_version": 1, "tools": tools}))
        (lockdir / "dependency-needs.json").write_text(json.dumps(needs or NEEDS))
        st_settings = Settings(data_dir=tmp_path / "data", tools_dir=tmp_path / "tools", limits=Limits())
        studio = StudioServices(st_settings)
        ts = ToolSetup(st_settings, studio.events, lockdir / "dependency-lock.json", allow_insecure_loopback=True)
        state = {"browser": svc_state}
        probes = {"browser": lambda dh, need: {"name": "browser", "state": state["browser"],
                                                "sentence": "Browser found." if state["browser"] == "ok" else "No browser was found.",
                                                "action": None},
                  "rizin_worker": lambda dh, need: {"name": "rizin_worker", "state": "ok", "sentence": "Ready.", "action": None}}
        dh = DependencyHealth(studio, ts, alt_probes={}, service_probes=probes)
        dh.queue.poll_s = 0.05
        dh._chk_webview2 = lambda: {"name": "webview2", "state": "ok", "sentence": "Installed."}   # host registry is not under test
        studio.__dict__["_tool_setup"] = ts
        studio.__dict__["_dependency_health"] = dh
        return studio, dh, ts, state
    built = []

    def _b(**kw):
        r = build(**kw)
        built.append(r[0])
        return r
    yield _b
    for s in built:
        try:
            s.dependency_health.queue.join(30)
            s.stop()
        except Exception:
            pass


def web_project(studio, tmp_path, *, target="web", lp=None, ai=None, name="site"):
    src = tmp_path / f"src-{name}"
    src.mkdir()
    (src / "index.html").write_text("<html><body>hi</body></html>")
    (src / "app.js").write_text("console.log(1)")
    out = tmp_path / f"out-{name}"
    out.mkdir()
    return studio.create_case(name=name, source_root=str(src), output_root=str(out), target_language=target, output_type="web" if target == "web" else "exe",
                              ai_policy=ai or {"mode": "no_ai"}, launch_profile=lp or {"execute_original": False})


def install_now(dh: DependencyHealth, names: list[str], **kw):
    dh.install(items=names, **kw)
    assert dh.queue.join(60)
    return dh.queue.snapshot()


# ---------------------------------------------------------------------------------------------- the table + lock gate
def test_needs_table_is_mirrored_byte_identically_and_names_exist_in_the_lock():
    docs = (REPO / "docs" / "dependency-needs.json").read_bytes()
    pkg = (REPO / "controller" / "rebuild_controller" / "data" / "dependency-needs.json").read_bytes()
    assert docs == pkg
    needs = json.loads(docs)
    lock = json.loads((REPO / "docs" / "dependency-lock.json").read_text(encoding="utf-8"))
    tools = set(lock["tools"])
    for section in ("profiles", "targets", "comparators"):
        for key, row in needs[section].items():
            assert set(row.get("required") or []) <= tools, (section, key)
    assert set(needs["core"]["recommended"]) <= tools
    assert {"native_pe", "dotnet", "godot", "jvm", "android", "web", "unknown"} <= set(needs["profiles"])
    assert needs["targets"]["rust"]["required"] == ["rust"]
    assert needs["comparators"]["web"]["required"] == ["node", "playwright-core"]


def test_packaged_build_gate_every_lock_tool_has_a_hash():
    lock = json.loads((REPO / "docs" / "dependency-lock.json").read_text(encoding="utf-8"))
    assert lock_hash_problems(lock) == []
    bad = {"tools": {"x": {"artifact": {"sha256": None}}, "y": {"artifact": {"sha256": "abc"}}, "z": {"artifact": {"sha256": "a" * 64}}}}
    assert lock_hash_problems(bad) == ["x: artifact.sha256 is missing or not a sha256", "y: artifact.sha256 is missing or not a sha256"]


def test_quick_profile_detects_without_tools(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    assert quick_profile(tmp_path) == "web"
    assert quick_profile(tmp_path / "missing") == "unknown"


# ---------------------------------------------------------------------------------------------- tool states
def test_missing_corrupt_wrong_version_and_smoke_fail_are_reported(env, tmp_path):
    studio, dh, ts, _ = env()
    web_project(studio, tmp_path)
    r = dh.report(refresh=True)
    t = {x["name"]: x for x in r["tools"]}
    assert t["webtool"]["state"] == "not_installed" and t["webtool"]["required"] is True
    assert [b["name"] for b in t["webtool"]["needed_by"]] == ["site"]
    assert r["missing_required"] == ["base", "webtool"]                       # dependency first
    assert r["overall"] == "error" and "webtool" in r["sentence"] and "needed by site" in r["sentence"]
    assert r["fix"]["kind"] == "install" and r["fix"]["items"] == ["base", "webtool"]
    assert r["fix"]["label"].startswith("Install what's missing (2 items, ")

    install_now(dh, ["webtool", "core"])
    r = dh.report(refresh=True)
    assert r["overall"] == "ok", r["sentence"]
    t = {x["name"]: x for x in r["tools"]}
    assert t["webtool"]["state"] == "installed" and t["webtool"]["hash_ok"] is True

    # corrupt: the entry file changed after installation
    (tmp_path / "tools" / "webtool" / "bin" / "tool.exe").write_bytes(b"tampered")
    r = dh.report(refresh=True)
    assert {x["name"]: x for x in r["tools"]}["webtool"]["state"] == "corrupt"
    assert r["overall"] == "error" and r["fix"]["kind"] == "repair" and r["fix"]["confirm"] is True
    install_now(dh, ["webtool"], repair=True)                                  # one confirmation -> reinstalled
    assert dh.report(refresh=True)["overall"] == "ok"

    # wrong version: marker says another version
    mk = tmp_path / "tools" / "core" / MARKER
    m = json.loads(mk.read_text())
    m["version"] = "0.9"
    mk.write_text(json.dumps(m))
    assert {x["name"]: x for x in dh.report(refresh=True)["tools"]}["core"]["state"] == "update_available"

    # smoke fail: the installed tool does not start
    def boom(name, root, layout):
        from rebuild_controller.tool_setup import ToolSetupError
        if name == "webtool":
            raise ToolSetupError("version_check_failed", "The tool's version check failed (exit 5)")
    ts._version_check = boom
    r = dh.report(smoke=True)
    w = {x["name"]: x for x in r["tools"]}["webtool"]
    assert w["state"] == "broken" and w["smoke"]["ok"] is False and "exit 5" in w["smoke"]["message"]
    assert r["overall"] == "error" and r["fix"] == {"kind": "repair", "items": ["webtool"], "confirm": True, "label": "Repair webtool"}


def test_service_down_and_no_disk_space_and_offline(env, tmp_path, monkeypatch, server):
    studio, dh, ts, state = env(svc_state="down")
    web_project(studio, tmp_path, lp={"execute_original": True, "kind": "web"})
    install_now(dh, ["webtool", "browserlib", "core"])
    r = dh.report(refresh=True)
    svc = {s["name"]: s for s in r["services"]}
    assert svc["browser"]["required"] is True and svc["browser"]["state"] == "down"
    assert r["overall"] == "error" and r["sentence"] == "No browser was found."
    state["browser"] = "ok"
    assert dh.report(refresh=True)["overall"] == "ok"

    # no disk space for what is missing
    (tmp_path / "tools" / "webtool").rename(tmp_path / "gone")
    monkeypatch.setattr(dhm, "_free_bytes", lambda p: 1000)
    r = dh.report(refresh=True)
    disk = {h["name"]: h for h in r["host"]}["disk_space"]
    assert disk["state"] == "error" and "Free up disk space" in disk["next_action"]
    assert r["overall"] == "error" and r["sentence"].startswith("Only ")

    # offline: the download server cannot be reached
    monkeypatch.setattr(dhm, "_free_bytes", lambda p: 10**12)
    server.close()
    r = dh.report(network=True)
    hosts = {h["name"]: h for h in r["host"]}["download_hosts"]
    assert hosts["state"] == "warn" and "127.0.0.1" in hosts["sentence"]


def test_tools_folder_not_writable_blocks(env, tmp_path, monkeypatch):
    studio, dh, ts, _ = env()
    real = Path.write_bytes

    def deny(self, data):
        if self.name.startswith(".write-test-"):
            raise PermissionError(13, "Access is denied")
        return real(self, data)
    monkeypatch.setattr(Path, "write_bytes", deny)
    r = dh.report(refresh=True)
    w = {h["name"]: h for h in r["host"]}["tools_write"]
    assert w["state"] == "error" and "cannot write" in w["sentence"] and r["overall"] == "error"


# ---------------------------------------------------------------------------------------------- install queue
def test_install_all_queue_dependency_order_one_failing_item_and_retry(env, tmp_path, server):
    bad_blob = make_zip(entry=b"other")
    studio, dh, ts, _ = env(extra_tools={"broken": _tool(server, "broken", bad_blob, sha="0" * 64),
                                          "needsbroken": _tool(server, "needsbroken", make_zip(), requires=["broken"])})
    snap = install_now(dh, ["webtool", "needsbroken", "compiler"])
    st = {it["name"]: it for it in snap["items"]}
    assert [it["name"] for it in snap["items"]] == ["base", "webtool", "broken", "needsbroken", "compiler"]
    assert st["base"]["status"] == st["webtool"]["status"] == st["compiler"]["status"] == "done"
    assert st["broken"]["status"] == "failed" and st["broken"]["error"]["code"] == "checksum_mismatch"
    assert st["broken"]["error"]["next_action"]
    assert st["needsbroken"]["status"] == "skipped" and "broken" in st["needsbroken"]["error"]["message"]
    assert snap["state"] == "failed" and snap["failed"] == ["broken", "needsbroken"] and snap["done"] == 3
    assert snap["bytes_done"] >= st["compiler"]["bytes_total"]
    # the source gets fixed; Retry installs only what failed
    good = make_zip(entry=b"other")
    server.routes["/broken.zip"] = (200, good)
    lock = json.loads(Path(ts.lock_path).read_text())
    lock["tools"]["broken"]["artifact"]["sha256"] = hashlib.sha256(good).hexdigest()
    lock["tools"]["broken"]["layout"]["entry_sha256"] = hashlib.sha256(b"other").hexdigest()
    Path(ts.lock_path).write_text(json.dumps(lock))
    ts._lock_doc = None
    dh.queue.retry()
    assert dh.queue.join(60)
    snap = dh.queue.snapshot()
    assert [it["name"] for it in snap["items"]] == ["broken", "needsbroken"] and snap["state"] == "done"


def test_interrupted_download_resumes_with_http_range(env, tmp_path, server):
    studio, dh, ts, _ = env()
    blob = server.routes["/compiler.zip"][1]
    server.drop_after["/compiler.zip"] = len(blob) // 2
    snap = install_now(dh, ["compiler"])
    it = snap["items"][0]
    assert it["status"] == "failed" and it["error"]["retryable"] is True and it["error"]["code"] == "download_failed", it["error"]
    part = tmp_path / "tools" / ".downloads" / "compiler.zip.part"
    assert part.is_file() and 0 < part.stat().st_size < len(blob)                # verified-so-far bytes kept
    kept = part.stat().st_size
    snap = dh.queue.retry()
    assert dh.queue.join(60)
    assert dh.queue.snapshot()["items"][0]["status"] == "done"
    assert server.range_requests == [f"bytes={kept}-"]
    assert ts.status("compiler")["status"] == "installed"
    assert not list((tmp_path / "tools" / ".downloads").iterdir())


def test_queue_interrupted_by_app_exit_is_reported_then_resumed(env, tmp_path):
    studio, dh, ts, _ = env()
    qf = tmp_path / "tools" / dhm.QUEUE_FILE
    qf.parent.mkdir(parents=True, exist_ok=True)
    qf.write_text(json.dumps({"id": "x", "state": "running", "reason": "user", "started_at": "t", "finished_at": None,
                              "items": [{"name": "core", "title": "core", "status": "installing", "repair": False, "requested": True,
                                         "bytes_total": 1, "bytes_done": 0, "phase": "downloading", "message": "", "error": None}]}))
    stale = tmp_path / "tools" / ".staging" / "core-deadbeef"
    stale.mkdir(parents=True)
    orphan = tmp_path / "tools" / ".downloads" / "nothing.zip.part"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"x")
    foreign = tmp_path / "tools" / ".downloads" / "someone-elses-folder"
    foreign.mkdir()
    q = InstallQueue(ts, state_path=qf)
    assert q.snapshot()["state"] == "interrupted" and q.snapshot()["items"][0]["status"] == "interrupted"
    dh.queue = q
    out = dh.startup()                                     # auto-install is off: cleaned, reported, not resumed
    assert out["resumed"] is False and str(stale) in out["cleaned"] and str(orphan) in out["cleaned"]
    assert foreign.is_dir()                                # only ToolSetup's own partial downloads are cleaned
    r = dh.report(refresh=True)
    assert r["fix"]["kind"] in ("resume_queue", "install")
    dh.queue.resume()
    assert dh.queue.join(60) and dh.queue.snapshot()["state"] == "done"


# ---------------------------------------------------------------------------------------------- preflight + API
def test_preflight_blocks_start_with_the_right_list_then_one_click_install(env, tmp_path):
    studio, dh, ts, _ = env()
    app = create_app(studio, "tok")
    with TestClient(app) as c:
        c.headers.update({"Authorization": "Bearer tok", "Origin": "http://localhost:5173"})
        src = tmp_path / "srcA"
        src.mkdir()
        (src / "index.html").write_text("<html></html>")
        out = tmp_path / "outA"
        out.mkdir()
        created = c.post("/cases", json={"name": "A", "source_root": str(src), "output_root": str(out), "target_language": "rust",
                                         "output_type": "exe"}).json()
        cid = created["case_id"]
        assert created["dependency_preflight"]["ok"] is False
        pf = c.get(f"/cases/{cid}/preflight").json()
        assert pf["profile"] == "web" and pf["target"] == "rust"
        assert [m["name"] for m in pf["missing"]] == ["base", "webtool", "compiler"]
        assert [m["name"] for m in pf["optional"]] == ["extra"]
        assert pf["install"]["items"] == ["base", "webtool", "compiler"]
        assert pf["install"]["label"].startswith("Install what's missing (3 items, ")
        r = c.post(f"/cases/{cid}/start")
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "dependencies_missing" and "webtool" in err["message"] and "Install what's missing" in err["next_action"]
        assert studio.cases.get_case(cid)["status"] == "created"                  # nothing scheduled
        q = c.post("/health/dependencies/install", json={"case_id": cid}).json()
        assert [it["name"] for it in q["items"]] == ["base", "webtool", "compiler"]
        assert dh.queue.join(60)
        assert c.get("/health/dependencies/install").json()["state"] == "done"
        assert c.get(f"/cases/{cid}/preflight").json()["ok"] is True
        assert c.post(f"/cases/{cid}/start").status_code == 200
        rep = c.get("/health/dependencies?refresh=1").json()
        assert {"tools", "services", "host", "queue", "settings", "overall", "sentence", "fix"} <= set(rep)
        assert c.post("/health/dependencies/install", json={"items": ["nope"]}).json()["error"]["code"] == "unknown_tool"
        assert c.post("/health/dependencies/install/cancel").json()["error"]["code"] == "not_running"
        assert "dependencies" in studio.doctor()


def test_auto_install_setting_defaults_off_is_asked_once_and_installs_on_yes(env, tmp_path):
    studio, dh, ts, _ = env()
    assert dh.get_settings() == {"auto_install": False, "asked": False, "updated_at": None}
    case = web_project(studio, tmp_path)
    assert dh.on_case_created(case["case_id"])["install"] is not None
    assert not dh.queue.busy() and ts.status("webtool")["status"] == "not_installed"   # nothing without a click
    out = dh.put_settings(auto_install=True)
    assert out["asked"] is True and [i["name"] for i in out["install"]["items"]][:2] == ["base", "webtool"]
    assert dh.queue.join(60)
    assert ts.status("webtool")["status"] == "installed" and ts.status("core")["status"] == "installed"
    assert dh.put_settings(auto_install=False)["install"] is None


def test_local_ai_service_offers_to_start_an_installed_ollama_only(env, tmp_path, monkeypatch):
    studio, dh, ts, _ = env()
    monkeypatch.setattr(dhm, "_port_open", lambda h, p, timeout=0.5: False)
    monkeypatch.setattr(dhm, "find_ollama", lambda: None)
    s = dh._svc_local_ai({"endpoints": [{"endpoint": "http://127.0.0.1:11434/v1", "server": "ollama"}]})
    assert s["state"] == "down" and s["action"]["kind"] == "link" and "never installs" in s["next_action"]
    with pytest.raises(Exception) as ei:
        dh.start_ollama(wait_s=0.1)
    assert getattr(ei.value, "code", None) == "not_installed"
    fake = tmp_path / "ollama.exe"
    fake.write_bytes(b"")
    monkeypatch.setattr(dhm, "find_ollama", lambda: fake)
    s = dh._svc_local_ai({"endpoints": [{"endpoint": "http://127.0.0.1:11434/v1", "server": "ollama"}]})
    assert s["action"] == {"kind": "start_ollama", "label": "Start Ollama"}
    started: list[list[str]] = []
    up = {"v": False}
    monkeypatch.setattr(dhm, "_spawn_detached", lambda argv: (started.append(argv), up.update(v=True)))
    monkeypatch.setattr(dhm, "_port_open", lambda h, p, timeout=0.5: up["v"])
    assert dh.start_ollama(wait_s=2)["running"] is True
    assert started == [[str(fake), "serve"]]
    lm = dh._svc_local_ai({"endpoints": [{"endpoint": "http://127.0.0.1:1234/v1", "server": None}]})
    assert lm["state"] == "ok"
    up["v"] = False
    lm = dh._svc_local_ai({"endpoints": [{"endpoint": "http://127.0.0.1:1234/v1", "server": None}]})
    assert lm["action"] is None and "LM Studio" in lm["sentence"]


def test_web_comparator_needs_browser_tools_only_when_the_project_compares(env, tmp_path):
    studio, dh, ts, _ = env()
    a = web_project(studio, tmp_path, name="a")
    b = web_project(studio, tmp_path, name="b", lp={"execute_original": True, "kind": "web"})
    na, nb = dh.case_needs(studio.cases.get_case(a["case_id"])), dh.case_needs(studio.cases.get_case(b["case_id"]))
    assert "browserlib" in na["optional"] and "browserlib" in nb["required"]
    assert na["services"]["browser"]["required"] is False and nb["services"]["browser"]["required"] is True
