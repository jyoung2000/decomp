"""Tests for the Cutter plugin's pure-Python client (clients/cutter/rebuild_studio_cutter/client.py).

Cutter and Qt are not needed and are not used: the client is exercised against the REAL controller API (StudioServices +
create_app served by uvicorn in a thread on a loopback port) over the real PE fixture analysed by the real rizin. Cutter's side
is a stand-in ``cmdj`` callable that returns what ``cutter.cmdj("ij")`` returns (checked against the installed rizin when present).

NOT covered here (needs a running Cutter, a Windows/desktop gate): plugin loading, the dock widget, ``seekChanged`` delivery.
"""
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

uvicorn = pytest.importorskip("uvicorn")

from rebuild_controller.api.server import create_app, write_controller_info  # noqa: E402
from rebuild_controller.jobs import JobState  # noqa: E402
from rebuild_controller.services import StudioServices  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "clients" / "cutter"))
from rebuild_studio_cutter import client as C  # noqa: E402

FIX = REPO / "fixtures" / "pecli" / "original"
PE = FIX / "pecli.exe"
TOKEN = "cutter-test-token-0123456789"
PE_BADDR = 0x140000000
RIZIN = Path("/opt/rebuild-tools/rizin-src-install/bin/rizin")

pytestmark = pytest.mark.skipif(not PE.exists(), reason="fixtures/pecli/original/pecli.exe missing")


# ------------------------------------------------------------------------------------------------ fixtures
class Live:
    def __init__(self, studio, server, thread, case_id, module_id, data_dir):
        self.studio, self.server, self.thread = studio, server, thread
        self.case_id, self.module_id, self.data_dir = case_id, module_id, data_dir
        self.stopped = False

    def stop(self):
        if not self.stopped:
            self.stopped = True
            self.server.should_exit = True
            self.thread.join(15)

    def cmdj(self, path=PE, baddr=PE_BADDR):
        """What cutter.cmdj('ij') returns (shape verified against rizin 0.9.1 in test_ij_shape_matches_real_rizin)."""
        def cmdj(cmd):
            assert cmd == "ij"
            return {"core": {"file": str(path), "fd": 3, "format": "pe64"}, "bin": {"baddr": baddr, "arch": "x86", "bits": 64}}
        return cmdj

    def session(self, **kw):
        return C.StudioSession(self.cmdj(), data_dir=self.data_dir, **kw)

    def functions(self):
        ev = self.studio.cases.list_evidence(self.case_id, kind="native.functions", module_id=self.module_id)[-1]
        return self.studio.cases.evidence_body(ev["evidence_id"])["functions"]

    def decompiled_addrs(self):
        return [e["meta"]["address"] for e in self.studio.cases.list_evidence(self.case_id, kind="native.decompile")]


def drain(studio, max_rounds=400):
    for _ in range(max_rounds):
        n = studio.runner.run_pending()
        if n == 0 and not studio.jobs.list(None, [JobState.QUEUED, JobState.RUNNING]):
            return
    raise AssertionError("pipeline did not settle")


@pytest.fixture
def live(settings, tmp_path):
    studio = StudioServices(settings)
    server = thread = None
    try:
        info = studio.registry.info("rizin")
        if info.availability.name == "MISSING":
            pytest.skip("rizin backend not available on this host")
        case = studio.create_case(name="pecli-cutter", source_root=str(FIX), output_root=str(tmp_path / "out"), target_language="rust",
                                  output_type="exe", settings={"decompile_limit": 5})
        cid = case["case_id"]
        studio.start_rebuild(cid)
        drain(studio)
        jobs = {j.stage: j for j in studio.jobs.list(cid)}
        assert jobs["analyze_module"].state == JobState.COMPLETED, jobs["analyze_module"].error
        module_id = studio.cases.modules(cid)[0]["module_id"]

        app = create_app(studio, TOKEN)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        assert server.started, "uvicorn did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        write_controller_info(settings.data_dir, port, TOKEN)
        lv = Live(studio, server, thread, cid, module_id, settings.data_dir)
        yield lv
    finally:
        if server is not None:
            server.should_exit = True
            if thread is not None:
                thread.join(15)
        studio.stop()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def dead_client(timeout=1.0):
    return C.StudioClient(C.Pairing(port=free_port(), token="x"), timeout=timeout)


# ------------------------------------------------------------------------------------------------ pairing
def test_pairing_from_controller_json(live, monkeypatch):
    p = C.load_pairing(live.data_dir)
    assert p.token == TOKEN and p.base_url == f"http://127.0.0.1:{p.port}" and p.pid
    assert TOKEN not in repr(p)                                  # the token must not leak through repr/logs
    client = C.StudioClient(p)
    assert client.health()["ok"] is True
    names = [c["name"] for c in client.list_cases()]
    assert "pecli-cutter" in names
    # explicit file and the env override resolve the same pairing
    f = live.data_dir / "controller.json"
    assert C.load_pairing(path=f).port == p.port
    monkeypatch.setenv("REBUILD_STUDIO_CONTROLLER_JSON", str(f))
    assert C.load_pairing().port == p.port
    monkeypatch.delenv("REBUILD_STUDIO_CONTROLLER_JSON")
    monkeypatch.setenv("REBUILD_STUDIO_DATA", str(live.data_dir))
    assert C.load_pairing().token == TOKEN and C.default_data_dir() == live.data_dir


def test_pairing_ignores_host_key_and_rejects_bad_files(tmp_path):
    f = tmp_path / "controller.json"
    f.write_text(json.dumps({"port": 4321, "token": "t", "pid": 1, "host": "evil.example"}))
    assert C.load_pairing(tmp_path).base_url == "http://127.0.0.1:4321"      # token is never sent to a file-chosen host
    for bad in ("not json", "[]", json.dumps({"port": 0, "token": "t"}), json.dumps({"port": 99999, "token": "t"}),
                json.dumps({"port": 1234, "token": ""}), json.dumps({"port": "x", "token": "t"}), json.dumps({"token": "t"})):
        f.write_text(bad)
        with pytest.raises(C.PairingError) as ei:
            C.load_pairing(tmp_path)
        assert ei.value.next_action
    f.unlink()
    with pytest.raises(C.PairingError) as ei:
        C.load_pairing(tmp_path)
    assert "start Rebuild Studio" in str(ei.value)


def test_wrong_and_rotated_token(live):
    f = live.data_dir / "controller.json"
    good = json.loads(f.read_text())
    sess = live.session()
    assert sess.reconnect()["ok"]
    f.write_text(json.dumps({**good, "token": "stale-token"}))
    with pytest.raises(C.ApiError) as ei:
        live.session().client.list_cases()
    assert ei.value.status == 401 and ei.value.code == "auth" and "reconnect" in ei.value.next_action
    v = live.session().view_for_address(0x140001190)
    assert v.status == "error" and v.next_action                   # surfaced as a view, not an exception
    f.write_text(json.dumps(good))                                  # app relaunched: controller.json rewritten
    assert sess.reconnect()["ok"]                                   # reconnect re-reads it


def test_loopback_requests_ignore_proxy_env(live, monkeypatch):
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(k, f"http://127.0.0.1:{free_port()}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert C.StudioClient.from_data_dir(live.data_dir).health()["ok"]


# ------------------------------------------------------------------------------------------------ module mapping
def test_module_mapping_by_sha256(live, tmp_path):
    sess = live.session()
    m = sess.attach()
    assert (m.case_id, m.module_id) == (live.case_id, live.module_id)
    assert m.rel_path == "pecli.exe" and m.has_functions
    assert sess.file_sha256 == live.studio.cases.get_module(live.module_id)["sha256"]
    # the hash decides, not the path: a byte-identical copy elsewhere maps to the same module
    copy = tmp_path / "elsewhere" / "renamed.bin"
    copy.parent.mkdir()
    shutil.copyfile(PE, copy)
    s2 = C.StudioSession(live.cmdj(copy), data_dir=live.data_dir)
    assert s2.attach().module_id == live.module_id
    # a modified copy has another hash and must NOT be mapped
    patched = tmp_path / "patched.exe"
    patched.write_bytes(PE.read_bytes() + b"\x00")
    s3 = C.StudioSession(live.cmdj(patched), data_dir=live.data_dir)
    with pytest.raises(C.BinaryError) as ei:
        s3.attach()
    assert "sha256" in str(ei.value) and ei.value.next_action
    v = s3.view_for_address(0x140001190)
    assert v.status == "not_mapped" and v.next_action and v.function is None


def test_same_binary_in_two_cases_prefers_analysed_one_and_can_be_pinned(live, tmp_path):
    other = live.studio.create_case(name="pecli-second", source_root=str(FIX), output_root=str(tmp_path / "out2"), target_language="rust",
                                    output_type="exe", settings={"decompile_limit": 1})
    mod = live.studio.cases.get_module(live.module_id)         # register the same file in a case that was never analysed
    live.studio.cases.add_module(other["case_id"], mod["rel_path"], mod["sha256"], mod["size"], mod["format"], mod["profile"], mod["arch"])
    sess = live.session()
    sess.attach()
    assert len(sess.matches) == 2 and sess.matches[0].case_id == live.case_id and sess.matches[0].has_functions
    assert not sess.matches[1].has_functions
    sess.pin_case(other["case_id"])
    assert sess.binding.case_id == other["case_id"]
    v = sess.view_for_address(0x140001190)
    assert v.status == "no_evidence" and v.next_action and "analyze_module" in v.next_action
    sess.pin_case(None)
    sess.attach()
    assert sess.binding.case_id == live.case_id


def test_ij_shape_variants():
    assert C.file_info_from_ij({"core": {"file": "/a/b.exe"}, "bin": {"baddr": 4096}})["path"] == "/a/b.exe"
    assert C.file_info_from_ij({"core": {"file": {"path": "/a/c.exe"}}})["path"] == "/a/c.exe"        # tolerated alternative shape
    assert C.file_info_from_ij({"core": {"file": "file:///a/d%20e.exe"}})["path"] == "/a/d e.exe"
    for bad in (None, [], {}, {"core": {}}, {"core": {"file": ""}}, {"core": {"file": "dbg://1234"}}, {"core": {"file": "malloc://512"}}):
        with pytest.raises(C.BinaryError):
            C.file_info_from_ij(bad)


@pytest.mark.skipif(not RIZIN.exists(), reason="rizin not installed on this host")
def test_ij_shape_matches_real_rizin(live):
    out = subprocess.run([str(RIZIN), "-q", "-c", "ij", str(PE)], capture_output=True, text=True, timeout=60, check=True).stdout
    info = C.file_info_from_ij(json.loads(out))
    assert info["path"] == str(PE) and info["baddr"] == PE_BADDR
    sess = C.StudioSession(lambda cmd: json.loads(subprocess.run([str(RIZIN), "-q", "-c", cmd, str(PE)], capture_output=True, text=True,
                                                               timeout=60, check=True).stdout), data_dir=live.data_dir)
    assert sess.attach().module_id == live.module_id


# ------------------------------------------------------------------------------------------------ evidence views
def test_view_from_decompile_evidence_and_function_lookup(live):
    funcs = live.functions()
    assert len(funcs) > 20
    daddr = live.decompiled_addrs()[0]
    fn = next(f for f in funcs if f["addr"] == daddr)
    sess = live.session()
    v = sess.view_for_address(daddr)
    assert v.status == "ready" and v.ok and v.source == "decompile"
    assert v.function["addr"] == daddr and v.function["name"] == fn["name"] and v.function["size"] == fn["size"]
    assert v.decompiled_text and "(" in v.decompiled_text
    assert v.is_real_decompiler is True and v.decompiler
    assert v.case_id == live.case_id and v.module_id == live.module_id
    assert v.target_evidence_id in v.evidence_ids and len(v.evidence_ids) >= 2       # decompile evidence + functions evidence
    # an address in the middle of the function resolves to the same function; the view keeps the cursor address
    mid = int(daddr, 16) + max(1, fn["size"] // 2)
    v2 = sess.view_for_address(mid)
    assert v2.function["addr"] == daddr and v2.address == f"0x{mid:x}"
    # hex strings are accepted as well as ints
    assert sess.view_for_address(daddr).function["addr"] == daddr
    # an address outside every function
    v3 = sess.view_for_address(0x10)
    assert v3.status == "no_function" and "0x10" in v3.message and not v3.ok
    assert sess.view_for_address("zzz").status == "error"
    # a function without decompile evidence still gets a (metadata only) view with an honest next action
    plain = next(f for f in funcs if f["addr"] not in set(live.decompiled_addrs()))
    v4 = sess.view_for_address(plain["addr"])
    assert v4.status == "ready" and v4.source == "functions" and v4.decompiled_text is None and v4.next_action
    assert "metadata only" in v4.message


def test_briefing_for_known_address(live):
    addr = live.decompiled_addrs()[0]
    sess = live.session()
    assert sess.view_for_address(addr).source == "decompile"            # no briefing evidence exists yet
    # a briefing is created by the controller (MCP get_function_briefing); do the same in-process through the real rizin backend
    pkt = live.studio.get_function_briefing(live.case_id, live.module_id, addr)
    assert pkt["ok"], pkt["error"]
    sess.invalidate()
    v = sess.view_for_address(addr, force=True)
    assert v.status == "ready" and v.source == "briefing"
    assert v.briefing["function"]["addr"] == addr and v.briefing["module"]["module_id"] == live.module_id
    assert v.briefing["strings"]["untrusted"] is True                       # untrusted envelope preserved
    assert v.target_evidence_id == pkt["evidence_ids"][0] and v.target_evidence_id in v.evidence_ids
    assert v.decompiled_text and v.decompiler
    assert isinstance(v.briefing["callees"], list) and isinstance(v.briefing["callers"], list)
    text = C.render_view_text(v)
    assert v.function["name"] in text and "Callees" in text and "untrusted" in text and addr in text
    # stale evidence is not served: invalidating the briefing falls back to the decompile view
    live.studio.cases.invalidate_evidence(live.case_id, kinds=["native.briefing"], module_id=live.module_id)
    sess.invalidate()
    assert sess.view_for_address(addr, force=True).source == "decompile"


def test_no_evidence_when_analysis_is_stale(live):
    live.studio.cases.invalidate_evidence(live.case_id, kinds=["native.functions"], module_id=live.module_id)
    v = live.session().view_for_address(0x140001190)
    assert v.status == "no_evidence" and "analyze_module" in v.next_action and v.case_id == live.case_id


def test_base_address_mismatch_is_flagged(live):
    daddr = live.decompiled_addrs()[0]
    ok = C.StudioSession(live.cmdj(baddr=PE_BADDR), data_dir=live.data_dir).view_for_address(daddr)
    assert ok.status == "ready" and not ok.warnings
    bad = C.StudioSession(live.cmdj(baddr=0x10000000), data_dir=live.data_dir).view_for_address(daddr)
    assert bad.status == "ready" and any("base" in w and "0x10000000" in w and "0x140000000" in w for w in bad.warnings)
    assert "WARNING" in C.render_view_text(bad)


# ------------------------------------------------------------------------------------------------ feedback
def test_feedback_submission_as_evidence_feedback(live):
    addr = live.decompiled_addrs()[0]
    sess = live.session()
    v = sess.view_for_address(addr)
    fb = sess.submit_feedback(v, "This looks like the argument parser; key sk-abcdefghijklmnop1234 should be redacted",
                              classification="question", priority="high", expected="named parse_args", actual=v.function["name"])
    assert fb["feedback_id"].startswith("fb_") and fb["status"] == "received"
    assert fb["target_kind"] == "evidence" and fb["target_id"] == v.target_evidence_id
    assert "[redacted]" in fb["comment"] and "sk-abcdefghijklmnop1234" not in fb["comment"]
    ctx = fb["context"]
    assert ctx["address"] == addr and ctx["function_address"] == addr and ctx["module_id"] == live.module_id
    assert ctx["source"] == "cutter" and ctx["file_sha256"] == sess.file_sha256 and v.target_evidence_id in ctx["evidence_ids"]
    # persisted by the controller, listed through the API, visible in the studio store
    listed = sess.client._request("GET", f"/cases/{live.case_id}/feedback")
    assert [f["feedback_id"] for f in listed] == [fb["feedback_id"]]
    assert live.studio.feedback.get(fb["feedback_id"])["context"]["module_id"] == live.module_id
    # a cursor in the middle of the function gets the cursor address in context
    mid = int(addr, 16) + 1
    fb2 = sess.submit_feedback(sess.view_for_address(mid), "second", classification="change", priority="low")
    assert fb2["context"]["address"] == f"0x{mid:x}" and fb2["context"]["function_address"] == addr


def test_feedback_validation_errors(live):
    sess = live.session()
    good = sess.view_for_address(live.decompiled_addrs()[0])
    nofn = sess.view_for_address(0x10)
    for view, comment, kw, code in ((nofn, "x", {}, "no_target"), (good, "   ", {}, "empty"), (good, "x" * 20001, {}, "too_long"),
                                    (good, "x", {"classification": "nonsense"}, "rejected"), (good, "x", {"priority": "urgent"}, "rejected")):
        with pytest.raises(C.ApiError) as ei:
            sess.submit_feedback(view, comment, **kw)
        assert ei.value.code == code and ei.value.next_action
    assert live.studio.feedback.list(live.case_id) == []                    # nothing was written for rejected input
    # server-side validation (bad enum) arrives as ApiError with the HTTP status, not a crash
    with pytest.raises(C.ApiError) as ei:
        sess.client.create_feedback(live.case_id, {"target_kind": "evidence", "target_id": "ev_x", "classification": "nonsense",
                                                   "priority": "low", "comment": "c"})
    assert ei.value.status == 422


# ------------------------------------------------------------------------------------------------ following the cursor
def test_seek_follower_coalesces_and_dedupes(live):
    funcs = live.functions()
    daddrs = live.decompiled_addrs()
    a, b = int(daddrs[0], 16), int(daddrs[1], 16)
    sess = live.session()
    got = []
    fol = C.SeekFollower(sess, got.append, run_async=True)
    fol.on_seek(a)
    assert fol.wait_idle(15)
    assert [v.function["addr"] for v in got] == [daddrs[0]]
    fol.on_seek(a + 1)                                         # same function: no new view
    fol.on_seek(a + 2)
    assert fol.wait_idle(15) and len(got) == 1
    for off in range(0, 6):                                    # a burst: only the newest is guaranteed to be delivered
        fol.on_seek(a if off % 2 == 0 else b)
    fol.on_seek(b)
    assert fol.wait_idle(15)
    assert got[-1].function["addr"] == daddrs[1]
    n = len(got)
    fol.on_seek(b, force=True)                                 # explicit refresh re-emits
    assert fol.wait_idle(15) and len(got) == n + 1
    fol.enabled = False
    fol.on_seek(a)
    assert fol.wait_idle(5) and len(got) == n + 1
    fol.on_seek("not-an-address", force=True)                  # ignored, no exception
    assert fol.wait_idle(5)
    assert len(funcs) > 2


def test_seek_follower_sync_mode_reports_errors_as_views(live, tmp_path):
    patched = tmp_path / "other.exe"
    patched.write_bytes(PE.read_bytes() + b"\x01")
    sess = C.StudioSession(live.cmdj(patched), data_dir=live.data_dir)
    got = []
    C.SeekFollower(sess, got.append, run_async=False).on_seek(0x140001190)
    assert [v.status for v in got] == ["not_mapped"]


# ------------------------------------------------------------------------------------------------ controller down
def test_controller_down_is_graceful(tmp_path):
    port = free_port()
    (tmp_path / "controller.json").write_text(json.dumps({"port": port, "token": "t", "pid": 1}))
    client = C.StudioClient.from_data_dir(tmp_path, timeout=1.0)
    with pytest.raises(C.ControllerUnavailable) as ei:
        client.health()
    assert str(port) in str(ei.value) and "start Rebuild Studio" in str(ei.value) and ei.value.code == "controller_unavailable"
    with pytest.raises(C.ControllerUnavailable):
        client.list_cases()
    sess = C.StudioSession(lambda cmd: {"core": {"file": str(PE)}, "bin": {"baddr": PE_BADDR}}, data_dir=tmp_path, timeout=1.0)
    with pytest.raises(C.ControllerUnavailable):
        sess.reconnect()
    v = sess.view_for_address(0x140001190)                   # never raises
    assert v.status == "unavailable" and v.next_action and not v.ok and v.function is None
    with pytest.raises(C.ApiError):                          # feedback needs a ready view, so it refuses locally
        sess.submit_feedback(v, "hello")
    got = []
    C.SeekFollower(sess, got.append, run_async=False).on_seek(0x140001190)
    assert [x.status for x in got] == ["unavailable"]
    assert "Next:" in C.render_view_text(v)


def test_controller_stops_while_plugin_is_open(live):
    sess = live.session()
    addr = live.decompiled_addrs()[0]
    assert sess.view_for_address(addr).ok
    live.stop()
    v = sess.view_for_address(int(addr, 16) + 0x10000, force=True)      # forced: must go to the network again
    assert v.status == "unavailable" and "cannot reach" in v.message
    # feedback on a still-displayed view fails with a clear error and does not raise anything else
    good = C.FunctionView(status="ready", case_id=live.case_id, module_id=live.module_id, target_evidence_id="ev_x", address=addr,
                          function={"addr": addr, "name": "f"})
    with pytest.raises(C.ControllerUnavailable) as ei:
        sess.submit_feedback(good, "note")
    assert ei.value.next_action


def test_non_json_and_http_error_bodies(tmp_path):
    """A server that is not the controller (wrong port in a stale controller.json) must produce ApiError, not a crash."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                body = b"<html>not json</html>"
                self.send_response(200)
            else:
                body = b"nope"
                self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = C.StudioClient(C.Pairing(port=srv.server_address[1], token="t"), timeout=2)
        with pytest.raises(C.ApiError) as ei:
            c.health()
        assert ei.value.code == "bad_response"
        with pytest.raises(C.ApiError) as ei:
            c.list_cases()
        assert ei.value.status == 500
    finally:
        srv.shutdown()


# ------------------------------------------------------------------------------------------------ plugin package
def test_package_import_is_cutter_free_and_plugin_degrades_clearly():
    import importlib
    for m in [m for m in sys.modules if m == "cutter" or m.startswith(("PySide6", "PySide2"))]:
        pytest.skip("a cutter/PySide module is already imported in this process")
    pkg = importlib.import_module("rebuild_studio_cutter")
    assert callable(pkg.create_cutter_plugin)
    assert "cutter" not in sys.modules and not any(m.startswith(("PySide6", "PySide2")) for m in sys.modules)
    from rebuild_studio_cutter import plugin
    with pytest.raises(plugin.CutterUnavailable) as ei:
        pkg.create_cutter_plugin()
    assert "inside Cutter" in str(ei.value) and "plugins/python" in str(ei.value)
