"""Real isolation tests: they start helper programs (Python scripts via sys.executable, Windows system tools) under the
sandbox on this host and check the effect, not the configuration."""
from __future__ import annotations

import http.server
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebuild_controller import sandbox
from rebuild_controller.comparators.cli import run_steps
from rebuild_controller.sandbox import IsolationPolicy, OriginalExecutionNotPermitted

WIN = os.name == "nt"
win_only = pytest.mark.skipif(not WIN, reason="Windows isolation (Job Object / integrity levels)")
PY = sys.executable


def _script(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / "helpers" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _run(tmp_path: Path, argv: list[str], policy: IsolationPolicy | None = None, **kw) -> sandbox.RunResult:
    policy = policy or IsolationPolicy(wall_time_s=60)
    work = tmp_path / "work"
    iso = tmp_path / "iso"
    for d in (work, iso):
        sandbox.prepare_work_dir(d, policy)
    env = sandbox.build_env(iso, program_dirs=[str(Path(PY).parent)], policy=policy, declared=kw.pop("declared", None))
    return sandbox.run(argv, work=iso, cwd=work, policy=policy, env=env, **kw)


def _pid_alive(pid: int) -> bool:
    if WIN:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x00100000 | 0x1000, False, pid)   # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(ctypes.c_void_p(h), 0) == 0x102   # WAIT_TIMEOUT -> still running
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:   # zombie (killed, not yet reaped by init in containers) counts as dead
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


WRITE_PROBE = r'''
import sys, pathlib
target = pathlib.Path(sys.argv[1])
try:
    target.write_text("probe")
    print("WROTE"); sys.exit(0)
except PermissionError as e:
    print("DENIED", e.errno); sys.exit(13)
'''


# ------------------------------------------------------------------------------------------------ integrity level
@win_only
def test_low_integrity_denies_write_to_user_profile_but_work_dir_is_writable(tmp_path):
    probe = _script(tmp_path, "write_probe.py", WRITE_PROBE)
    target = Path(os.environ["USERPROFILE"]) / f"rebuild_lowil_probe_{uuid.uuid4().hex}.txt"
    try:
        r = _run(tmp_path, [PY, str(probe), str(target)])
        assert r.isolation["mode"] == "low" and r.isolation["integrity"].startswith("Low")
        assert r.returncode == 13 and b"DENIED" in r.stdout, (r.stdout, r.stderr)
        assert not target.exists()
        # documents folder (medium label) too
        docs = Path(os.environ["USERPROFILE"]) / "Documents"
        if docs.is_dir():
            t2 = docs / f"rebuild_lowil_probe_{uuid.uuid4().hex}.txt"
            r2 = _run(tmp_path, [PY, str(probe), str(t2)])
            assert r2.returncode == 13 and not t2.exists()
        # positive control: the labelled work dir is writable
        r3 = _run(tmp_path, [PY, str(probe), "inside.txt"])
        assert r3.returncode == 0 and (tmp_path / "work" / "inside.txt").read_text() == "probe"
    finally:
        if target.exists():
            target.unlink()


@win_only
def test_low_integrity_is_the_cause_medium_control_can_write(tmp_path):
    """Same write outside the work dir: denied at low IL, allowed at medium IL (recorded downgrade)."""
    probe = _script(tmp_path, "write_probe.py", WRITE_PROBE)
    outside = tmp_path / "outside"; outside.mkdir()
    low = _run(tmp_path, [PY, str(probe), str(outside / "a.txt")])
    assert low.returncode == 13 and not (outside / "a.txt").exists()
    med = _run(tmp_path, [PY, str(probe), str(outside / "b.txt")], IsolationPolicy(integrity="medium", downgrade_reason="test control"))
    assert med.returncode == 0 and (outside / "b.txt").exists()
    assert med.isolation["mode"] == "medium" and "test control" in med.isolation["downgrade"]


def test_medium_needs_a_recorded_reason():
    with pytest.raises(ValueError, match="downgrade_reason"):
        IsolationPolicy(integrity="medium")
    assert IsolationPolicy.from_spec({"integrity": "medium", "reason": "needs HKCU"}).downgrade_reason == "needs HKCU"


# ------------------------------------------------------------------------------------------------ tree kill / limits
TREE = r'''
import subprocess, sys, time, pathlib
child = subprocess.Popen([sys.executable, "-c", "import os,time,pathlib; pathlib.Path('grandchild.pid').write_text(str(os.getpid())); time.sleep(120)"])
for _ in range(100):
    if pathlib.Path("grandchild.pid").exists():
        break
    time.sleep(0.1)
print("spawned", flush=True)
if sys.argv[1] == "wait":
    time.sleep(120)
'''


def test_timeout_kills_the_whole_tree(tmp_path):
    s = _script(tmp_path, "tree.py", TREE)
    t0 = time.monotonic()
    r = _run(tmp_path, [PY, str(s), "wait"], IsolationPolicy(wall_time_s=4))
    assert time.monotonic() - t0 < 30
    assert r.timed_out and r.returncode is None and "wall_time" in r.triggered
    pid = int((tmp_path / "work" / "grandchild.pid").read_text())
    deadline = time.monotonic() + 5
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _pid_alive(pid), "grandchild survived the timeout"


def test_leftover_background_child_is_killed_when_main_exits(tmp_path):
    s = _script(tmp_path, "tree.py", TREE)
    r = _run(tmp_path, [PY, str(s), "exit"], IsolationPolicy(wall_time_s=60))
    assert r.returncode == 0 and not r.timed_out and b"spawned" in r.stdout
    pid = int((tmp_path / "work" / "grandchild.pid").read_text())
    deadline = time.monotonic() + 5
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _pid_alive(pid)
    if WIN:
        assert "leftover_processes_killed" in r.triggered and r.leftovers_killed >= 1


def test_memory_cap_triggers(tmp_path):
    s = _script(tmp_path, "mem.py", "import sys\ntry:\n    b = bytearray(1536 * 1024 * 1024)\n    print('ALLOCATED')\nexcept MemoryError:\n    print('MEMERR'); sys.exit(7)\n")
    r = _run(tmp_path, [PY, str(s)], IsolationPolicy(process_memory_bytes=256 * 1024 * 1024, job_memory_bytes=512 * 1024 * 1024))
    assert r.returncode == 7 and b"MEMERR" in r.stdout, (r.stdout, r.stderr)
    assert r.limits["process_memory_bytes"] == 256 * 1024 * 1024
    if WIN:
        assert "process_memory" in r.triggered and "process_memory" in r.isolation["job_limits"]


def test_output_cap_truncates_and_records(tmp_path):
    s = _script(tmp_path, "spam.py", "import sys\nsys.stdout.write('x' * 1_000_000)\nsys.stderr.write('e' * 10)\n")
    r = _run(tmp_path, [PY, str(s)], IsolationPolicy(stdout_cap_bytes=1000))
    assert r.returncode == 0
    assert len(r.stdout) == 1000 and r.stdout_truncated and "output_cap:stdout" in r.triggered
    assert r.stderr == b"e" * 10 and not r.stderr_truncated


@win_only
def test_active_process_limit_triggers(tmp_path):
    s = _script(tmp_path, "fork.py", "import subprocess, sys\ntry:\n    subprocess.run([sys.executable, '-c', 'pass'])\n    print('SPAWNED')\nexcept OSError as e:\n    print('REFUSED'); sys.exit(9)\n")
    # The base interpreter (not a venv launcher, which would add a second process) uses the whole budget of 1:
    # the next CreateProcess fails with a quota error on every host (venv locally, plain python on CI).
    base = getattr(sys, "_base_executable", None) or PY
    r = _run(tmp_path, [base, str(s)], IsolationPolicy(max_processes=1))
    assert r.returncode == 9 and b"REFUSED" in r.stdout, (r.stdout, r.stderr)
    assert "active_processes" in r.triggered


# ------------------------------------------------------------------------------------------------ environment
ENV_DUMP = "import os, json\nprint(json.dumps(dict(os.environ)))\n"


def test_env_does_not_leak_host_secret(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("REBUILD_TEST_SECRET", "s3cr3t-sentinel")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    s = _script(tmp_path, "env.py", ENV_DUMP)
    runs = run_steps({"type": "command", "command": [PY, str(s)], "env": {"DECLARED": "yes"}}, tmp_path, [{"args": [], "env": {"STEP": "{work}"}}],
                     tmp_path / "scen" / "w1")
    assert runs[0]["exit_code"] == 0, runs[0]["stderr"]
    env = json.loads(runs[0]["stdout"])
    upper = {k.upper(): v for k, v in env.items()}
    assert "REBUILD_TEST_SECRET" not in upper and "OPENAI_API_KEY" not in upper
    assert "s3cr3t-sentinel" not in runs[0]["stdout"]
    assert upper["DECLARED"] == "yes" and upper["STEP"] == str(tmp_path / "scen" / "w1")
    iso = str(tmp_path / "scen" / ".w1.isohome")
    for k in ("TEMP", "TMP", "HOME") + (("USERPROFILE", "APPDATA", "LOCALAPPDATA") if WIN else ()):
        assert upper[k].startswith(iso), (k, upper[k])
    assert runs[0]["isolation"]["env"] == "allowlist"
    # the isolated home never shows up in the compared work dir
    assert not any(p.name == ".home" for p in (tmp_path / "scen" / "w1").rglob("*"))


# ------------------------------------------------------------------------------------------------ consent
def test_original_run_refused_without_consent(tmp_path):
    s = _script(tmp_path, "hello.py", "print('hello')\n")
    launch = {"type": "command", "command": [PY, str(s)]}
    with pytest.raises(OriginalExecutionNotPermitted, match="^Original execution needs your permission"):
        run_steps(launch, tmp_path, [{"args": []}], tmp_path / "w0", role="original")
    with pytest.raises(OriginalExecutionNotPermitted):
        run_steps(launch, tmp_path, [{"args": []}], tmp_path / "w0", role="original", consent={"allowed": False})
    assert not (tmp_path / "w0").exists()          # nothing ran, nothing prepared
    runs = run_steps(launch, tmp_path, [{"args": []}], tmp_path / "w1", role="original", consent={"allowed": True, "at": "now"})
    assert runs[0]["stdout"].strip() == "hello" and runs[0]["role"] == "original"


@pytest.fixture
def studio(settings):
    from rebuild_controller.services import StudioServices
    st = StudioServices(settings)
    yield st
    st.stop()


def test_case_consent_default_false_grant_revoke_and_stage_blocked(studio, src_out, tmp_path):
    from rebuild_controller.cases import require_original_execution_consent
    from rebuild_controller.jobs.runner import StageError
    from rebuild_controller.stages import stage_capture_original
    src, out = src_out
    s = _script(tmp_path, "orig.py", "import pathlib\npathlib.Path('ran.txt').write_text('ran')\nprint('orig')\n")
    case = studio.create_case(name="c", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe",
                              launch_profile={"execute_original": False, "kind": "cli", "launch": {"type": "command", "command": [PY, str(s)]},
                                              "scenarios": [{"id": "s1", "steps": [{"args": []}]}]})
    cid = case["case_id"]
    marker = studio.cases.case_root(cid) / "capture" / "s1" / "ran.txt"
    c = studio.cases.original_execution_consent(cid)
    assert c["allowed"] is False and c["at"] is None
    with pytest.raises(OriginalExecutionNotPermitted):
        require_original_execution_consent(studio.cases.get_case(cid))
    # runtime observation switched on but no consent recorded (e.g. consent revoked): the stage is blocked, nothing runs
    lp = {**studio.cases.get_case(cid)["launch_profile"], "execute_original": True}
    studio.db.update("cases", "case_id", cid, {"launch_profile": lp})
    ctx = SimpleNamespace(job=SimpleNamespace(case_id=cid, inputs={}), services={"studio": studio}, progress=lambda **k: None, heartbeat=lambda *a, **k: None)
    with pytest.raises(StageError) as ei:
        stage_capture_original(ctx)
    assert str(ei.value).startswith("Original execution needs your permission") and ei.value.blocker
    assert not marker.exists()
    g = studio.cases.set_original_execution_consent(cid, True, via="test", note="user clicked allow")
    assert g["allowed"] and g["at"] and g["via"] == "test" and g["history"][-1]["allowed"] is True
    res = stage_capture_original(ctx)
    assert res["source"] == "capture_original" and marker.read_text() == "ran"
    r = studio.cases.set_original_execution_consent(cid, False, via="test")
    assert r["allowed"] is False and [h["allowed"] for h in r["history"]] == [True, False]


def test_case_created_with_execute_original_records_consent(studio, src_out):
    src, out = src_out
    c = studio.create_case(name="c", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe",
                           launch_profile={"execute_original": True})
    k = studio.cases.original_execution_consent(c["case_id"])
    assert k["allowed"] and k["via"] == "create_case" and k["at"]


def test_consent_api_endpoints(settings, src_out):
    from fastapi.testclient import TestClient
    from rebuild_controller.api.server import create_app
    from rebuild_controller.services import StudioServices
    st = StudioServices(settings)
    try:
        with TestClient(create_app(st, "tok")) as cl:
            cl.headers.update({"Authorization": "Bearer tok", "Origin": "http://localhost:5173"})
            src, out = src_out
            cid = cl.post("/cases", json={"name": "x", "source_root": str(src), "output_root": str(out), "target_language": "rust", "output_type": "exe"}).json()["case_id"]
            assert cl.get(f"/cases/{cid}/consent/original-execution").json()["allowed"] is False
            g = cl.put(f"/cases/{cid}/consent/original-execution", json={"allow": True, "note": "ok"}).json()
            assert g["allowed"] is True and g["at"] and g["via"] == "api"
            case = cl.get(f"/cases/{cid}").json()
            assert case["launch_profile"]["allow_original_execution"] is True and case["launch_profile"]["execute_original"] is True
            assert cl.put(f"/cases/{cid}/consent/original-execution", json={"allow": False}).json()["allowed"] is False
            iso = cl.get("/isolation").json()
            assert "network_default" in iso and iso["network_default"].startswith("open")
    finally:
        st.stop()


# ------------------------------------------------------------------------------------------------ network
@win_only
def test_appcontainer_mode_blocks_network_low_mode_does_not(tmp_path):
    from rebuild_controller import _sandbox_win as w
    curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    if not curl.exists():
        pytest.skip("curl.exe not present")
    existed = w.appcontainer_profile_exists()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"PONG")

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    try:
        low = _run(tmp_path / "a", [str(curl), "-s", "-m", "5", url], IsolationPolicy(wall_time_s=30))
        assert low.returncode == 0 and low.stdout == b"PONG"
        assert low.isolation["network"].startswith("open")
        ac = _run(tmp_path / "b", [str(curl), "-s", "-m", "5", url], IsolationPolicy(integrity="appcontainer", wall_time_s=30))
        assert ac.isolation["mode"] == "appcontainer" and ac.isolation["network"].startswith("blocked")
        assert ac.returncode not in (0, None) and b"PONG" not in ac.stdout, (ac.returncode, ac.stdout, ac.stderr)
    finally:
        srv.shutdown(); srv.server_close()
        if not existed:
            w.remove_appcontainer_profile()


@win_only
def test_describe_host_reports_low_integrity_available():
    d = sandbox.describe_host()
    assert d["modes"]["low"]["available"] is True and d["default_mode"] == "low"
    assert d["network_default"].startswith("open")


# ------------------------------------------------------------------------------------------------ previews / builders
def test_spawn_kill_tree_for_previews(tmp_path):
    s = _script(tmp_path, "tree.py", TREE)
    state = tmp_path / "state"
    pol = IsolationPolicy(wall_time_s=None, ui_restrictions="interactive")
    sandbox.prepare_work_dir(state, pol)
    env = sandbox.build_env(state, program_dirs=[str(Path(PY).parent)], policy=pol)
    p = sandbox.spawn([PY, str(s), "wait"], work=state, cwd=state, policy=pol, env=env)
    deadline = time.monotonic() + 20
    while not (state / "grandchild.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    gpid = int((state / "grandchild.pid").read_text())
    assert p.poll() is None and _pid_alive(gpid)
    p.kill_tree()
    deadline = time.monotonic() + 5
    while _pid_alive(gpid) and time.monotonic() < deadline:
        time.sleep(0.1)
    diag = ""
    if not WIN and _pid_alive(gpid):
        try:
            st = Path(f"/proc/{gpid}/stat").read_text().split()
            diag = f"grandchild state={st[2]} ppid={st[3]} pgid={st[4]} main_pid={getattr(getattr(p, 'proc', None), 'pid', '?')}"
        except OSError as e:
            diag = repr(e)
    assert not _pid_alive(gpid), diag
    assert p.poll() is not None


@pytest.mark.skipif(not WIN or not __import__("shutil").which("cargo"), reason="Windows + cargo needed")
def test_cargo_build_script_cannot_write_user_profile(tmp_path, settings):
    from rebuild_controller.builders.rust import build_rust
    target = Path(os.environ["USERPROFILE"]) / f"rebuild_buildrs_probe_{uuid.uuid4().hex}.txt"
    src = tmp_path / "cand" / "source"; (src / "src").mkdir(parents=True)
    (src / "Cargo.toml").write_text('[package]\nname = "probe"\nversion = "0.1.0"\nedition = "2021"\n')
    (src / "src" / "main.rs").write_text('fn main() { println!("hi"); }\n')
    (src / "build.rs").write_text('fn main() { let ok = std::fs::write(r"%s", "x").is_ok(); println!("cargo:warning=profile_write_ok={}", ok);'
                                  ' println!("cargo:warning=secret={:?}", std::env::var("REBUILD_TEST_SECRET").ok()); }\n' % target)
    os.environ["REBUILD_TEST_SECRET"] = "s3cr3t"
    logs = []
    ctx = SimpleNamespace(job=SimpleNamespace(case_id=None, inputs={}), services={}, limits=settings.limits, heartbeat=lambda *a, **k: None,
                          log=lambda m, **k: logs.append(m), run=lambda *a, **k: SimpleNamespace(text="rustc (test)"))
    try:
        info = build_rust(ctx, src, tmp_path / "cand" / "dist.staging")
        assert "profile_write_ok=false" in info["build_log"] and "secret=None" in info["build_log"], info["build_log"][-2000:]
        assert not target.exists()
        assert info["build_isolation"]["mode"] == "low" and (tmp_path / "cand" / "dist.staging" / "probe.exe").exists()
    finally:
        os.environ.pop("REBUILD_TEST_SECRET", None)
        if target.exists():
            target.unlink()
