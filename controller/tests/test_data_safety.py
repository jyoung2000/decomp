"""R11 data safety: backups + rotation, startup integrity + recovery, one writer per data folder (lock, forwarding of the
CLI and rebuild-mcp to a live controller), the Windows package-context guard, and the no-real-data-folder default."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rebuild_controller import config
from rebuild_controller.config import DataDirRequired, Settings, get_settings
from rebuild_controller.store import safety
from rebuild_controller.store.db import Database

CONTROLLER = Path(__file__).resolve().parents[1]
RESCUE = Path("D:/rs-db-rescue/studio.sqlite3")   # read-only copy of the store damaged on the genuine install (2026-10-10)


# ------------------------------------------------------------------------------------------------ helpers
def _make_store(data_dir: Path, tmp: Path, n_cases: int = 2) -> list[str]:
    from rebuild_controller.services import StudioServices
    s = Settings(data_dir=data_dir)
    st = StudioServices(s)
    ids = []
    try:
        for i in range(n_cases):
            src, out = tmp / f"src{i}", tmp / f"out{i}"
            src.mkdir(parents=True, exist_ok=True)
            (src / "a.txt").write_text("x")
            ids.append(st.create_case(name=f"p{i}", source_root=str(src), output_root=str(out), target_language="rust",
                                      output_type="exe")["case_id"])
        for i in range(300):   # enough pages that damage in the middle of the file hits b-tree pages
            st.events.emit("test.filler", {"i": i, "pad": "x" * 200})
    finally:
        st.stop()
    return ids


def _corrupt(db: Path) -> None:
    for suffix in ("-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    size = db.stat().st_size
    assert size > 8 * 4096
    with open(db, "r+b") as f:
        for page in range(2, min(size // 4096, 40), 3):
            f.seek(page * 4096)
            f.write(b"\xde\xad\xbe\xef" * 1024)


def _cases_in(db: Path) -> int:
    c = sqlite3.connect(str(db))
    try:
        return c.execute("SELECT count(*) FROM cases").fetchone()[0]
    finally:
        c.close()


def _env(data_dir: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("REBUILD_STUDIO_DATA", None)
    env.update({"REBUILD_NO_LOCAL_DETECT": "1", "REBUILD_NO_DEP_STARTUP": "1", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
                "REBUILD_STUDIO_LOCK_WAIT_SECS": "20", "REBUILD_MCP_IDLE_RELEASE_SECS": "600"})
    env.update(extra)
    return env


def _start_controller(data_dir: Path, tmp: Path) -> subprocess.Popen:
    log = open(tmp / f"controller-{time.time_ns()}.log", "wb")
    p = subprocess.Popen([sys.executable, "-m", "rebuild_controller.cli.main", "serve", "--data-dir", str(data_dir)],
                         cwd=str(CONTROLLER), env=_env(data_dir), stdout=log, stderr=subprocess.STDOUT)
    from rebuild_controller.remote import live_controller
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if p.poll() is not None:
            log.close()
            raise AssertionError(f"controller exited {p.returncode}: {Path(log.name).read_text(errors='replace')[-2000:]}")
        if live_controller(data_dir) is not None:
            return p
        time.sleep(0.2)
    p.kill()
    raise AssertionError("controller did not become ready")


def _stop(p: subprocess.Popen) -> None:
    p.terminate()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(timeout=10)


# ------------------------------------------------------------------------------------------------ (e) defaults
def test_no_implicit_real_data_folder_in_tests():
    assert config.test_context()
    with pytest.raises(DataDirRequired):
        get_settings()
    with pytest.raises(DataDirRequired):
        Settings()
    with pytest.raises(DataDirRequired):
        Settings(data_dir=config.user_data_dir())
    os.environ["REBUILD_STUDIO_DATA"] = str(config.user_data_dir())
    try:
        with pytest.raises(DataDirRequired):
            config.default_data_dir()
    finally:
        del os.environ["REBUILD_STUDIO_DATA"]


def test_cli_and_mcp_refuse_without_a_data_folder(tmp_path):
    env = _env(tmp_path)
    r = subprocess.run([sys.executable, "-m", "rebuild_controller.cli.main", "jobs"], cwd=str(CONTROLLER), env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 2 and "explicit" in r.stderr, r.stderr
    r = subprocess.run([sys.executable, "-m", "rebuild_controller.mcp.server"], cwd=str(CONTROLLER), env=env, input="",
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 2 and "explicit" in r.stderr, r.stderr


# ------------------------------------------------------------------------------------------------ (a) backups
def test_backup_is_verified_and_counts_projects(tmp_path):
    data = tmp_path / "data"
    _make_store(data, tmp_path)
    b = safety.backup_store(data / "studio.sqlite3", "manual")
    assert b["integrity"] == "ok" and b["projects"] == 2 and Path(b["path"]).parent == data / "backups"
    assert safety.quick_check(b["path"], full=True) == (True, "ok")
    listed = safety.list_backups(data)
    assert [x["name"] for x in listed] == [b["name"]] and listed[0]["projects"] == 2


def test_backup_of_a_damaged_store_is_refused_and_not_kept(tmp_path):
    data = tmp_path / "data"
    _make_store(data, tmp_path)
    _corrupt(data / "studio.sqlite3")
    with pytest.raises(safety.BackupFailed):
        safety.backup_store(data / "studio.sqlite3", "manual")
    assert safety.list_backups(data) == [] and not list((data / "backups").glob(".tmp-*"))


def test_rotation_keeps_last_five_plus_one_per_day_for_seven_days(tmp_path):
    data = tmp_path / "data"
    _make_store(data, tmp_path, n_cases=1)
    db = data / "studio.sqlite3"
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    taken = []
    for day in range(10, -1, -1):          # 11 days, three backups a day (06:00, 12:00, 18:00) ...
        for hour in (6, 12, 18):
            t = now - timedelta(days=day) + timedelta(hours=hour - 12)
            if t <= now:
                taken.append(t)
    for t in taken:
        safety.backup_store(db, "periodic", now=t, rotate=False)
    removed = safety.rotate_backups(data / "backups", now=now)
    kept = safety.list_backups(data)
    kept_times = sorted(datetime.fromisoformat(b["taken_at"]) for b in kept)
    newest5 = sorted(taken)[-5:]
    assert set(newest5) <= set(kept_times)
    days = {t.date() for t in kept_times}
    assert days == {(now - timedelta(days=d)).date() for d in range(7)}, "one per day for the last 7 days, nothing older"
    for d in days:
        newest_of_day = max(t for t in taken if t.date() == d)
        assert newest_of_day in kept_times
    assert len(kept) == len(set(newest5) | {max(t for t in taken if t.date() == d) for d in days})
    assert len(removed) == len(taken) - len(kept)
    assert not [p for p in (data / "backups").glob("*.json") if not (data / "backups" / p.name[:-5]).exists()], "no orphaned metadata"


def test_backup_before_schema_migration(tmp_path):
    data = tmp_path / "data"
    _make_store(data, tmp_path, n_cases=1)
    db = data / "studio.sqlite3"
    c = sqlite3.connect(str(db))
    c.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    c.execute("DROP TABLE ai_config_revisions")
    c.execute("ALTER TABLE ai_calls DROP COLUMN detail")
    c.commit()
    c.close()
    d = Database(db)
    d.close()
    pre = [b for b in safety.list_backups(data) if b["reason"] == "pre-migration-v1"]
    assert len(pre) == 1 and pre[0]["projects"] == 1
    c = sqlite3.connect(pre[0]["path"])
    assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1", "the copy is the pre-upgrade store"
    c.close()
    Database(db).close()
    assert len([b for b in safety.list_backups(data) if b["reason"].startswith("pre-migration")]) == 1, "no backup without a migration"


def test_maintenance_endpoints(settings, tmp_path):
    from fastapi.testclient import TestClient
    from rebuild_controller.api.server import create_app
    from rebuild_controller.services import StudioServices
    st = StudioServices(settings)
    try:
        with TestClient(create_app(st, "tok"), base_url="http://127.0.0.1:1") as c:
            h = {"Authorization": "Bearer tok"}
            assert c.get("/maintenance/backups", headers=h).json()["backups"] == []
            r = c.post("/maintenance/backup", headers=h)
            assert r.status_code == 200 and r.json()["integrity"] == "ok"
            j = c.get("/maintenance/backups", headers=h).json()
            assert j["backups"][0]["name"] == r.json()["name"] and j["policy"]["keep_last"] == 5 and j["policy"]["keep_daily_days"] == 7
            assert j["backups_dir"].endswith("backups") and j["last_backup"]
            assert c.post("/maintenance/backup").status_code == 401
    finally:
        st.stop()


def test_damage_errors_are_recognised():
    assert safety.is_damage_error(sqlite3.DatabaseError("database disk image is malformed"))
    assert safety.is_damage_error(sqlite3.DatabaseError("file is not a database"))
    assert not safety.is_damage_error(sqlite3.OperationalError("database is locked"))
    assert not safety.is_damage_error(sqlite3.IntegrityError("UNIQUE constraint failed"))


# ------------------------------------------------------------------------------------------------ (b) startup integrity + recovery
def _serve_in_process(data: Path, monkeypatch, **env: str) -> int:
    import uvicorn
    from rebuild_controller.api import server
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    for k, v in {"REBUILD_NO_LOCAL_DETECT": "1", "REBUILD_NO_DEP_STARTUP": "1", **env}.items():
        monkeypatch.setenv(k, v)
    try:
        server.serve(port=0, data_dir=str(data))
    except SystemExit as e:
        return int(e.code)
    return 0


def test_corrupted_store_exits_with_typed_code_and_restore_brings_the_projects_back(tmp_path, monkeypatch):
    data = tmp_path / "data"
    ids = _make_store(data, tmp_path, n_cases=3)
    assert _serve_in_process(data, monkeypatch) == 0          # healthy start -> verified "start" backup
    good = safety.list_backups(data)
    assert good and good[0]["reason"] == "start" and good[0]["projects"] == 3
    _corrupt(data / "studio.sqlite3")
    damaged_bytes = (data / "studio.sqlite3").read_bytes()

    assert _serve_in_process(data, monkeypatch) == safety.EXIT_STORE_DAMAGED
    rec = safety.read_recovery_file(data)
    assert rec["status"] == "damaged" and rec["exit_code"] == 70 and rec["backups"][0]["name"] == good[0]["name"]
    assert rec["backups"][0]["projects"] == 3
    assert (data / "studio.sqlite3").read_bytes() == damaged_bytes, "nothing touched before the user chooses"

    assert _serve_in_process(data, monkeypatch, REBUILD_STUDIO_RECOVER=f"restore:{good[0]['name']}") == 0
    assert safety.read_recovery_file(data) is None
    from rebuild_controller.services import StudioServices
    st = StudioServices(Settings(data_dir=data))
    try:
        assert {c["case_id"] for c in st.cases.list_cases()} == set(ids)
    finally:
        st.stop()
    kept = list((data / "damaged").glob("studio-*.sqlite3"))
    assert len(kept) == 1 and kept[0].read_bytes() == damaged_bytes, "the damaged file is kept, never deleted"
    log = [json.loads(x) for x in (data / "recovery-log.jsonl").read_text().splitlines()]
    assert log[-1]["mode"] == "restore" and log[-1]["restored"] == good[0]["name"]


def test_start_empty_keeps_the_damaged_file(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _make_store(data, tmp_path, n_cases=1)
    _corrupt(data / "studio.sqlite3")
    assert _serve_in_process(data, monkeypatch) == safety.EXIT_STORE_DAMAGED
    assert safety.read_recovery_file(data)["backups"] == []
    assert _serve_in_process(data, monkeypatch, REBUILD_STUDIO_RECOVER="empty") == 0
    assert _cases_in(data / "studio.sqlite3") == 0
    assert len(list((data / "damaged").glob("studio-*.sqlite3"))) == 1


def test_bad_recovery_choice_leaves_files_alone(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _make_store(data, tmp_path, n_cases=1)
    _corrupt(data / "studio.sqlite3")
    before = (data / "studio.sqlite3").read_bytes()
    assert _serve_in_process(data, monkeypatch, REBUILD_STUDIO_RECOVER="restore") == safety.EXIT_STORE_DAMAGED  # no backup exists
    assert (data / "studio.sqlite3").read_bytes() == before and not (data / "damaged").exists()
    assert "recovery failed" in safety.read_recovery_file(data)["detail"]
    with pytest.raises(ValueError):
        safety.recover_store(data, "restore", "..\\..\\evil.sqlite3")


@pytest.mark.skipif(not RESCUE.is_file(), reason="the incident copy is only on the development machine")
def test_the_store_damaged_on_the_genuine_install_is_detected(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    shutil.copyfile(RESCUE, data / "studio.sqlite3")      # a copy; the evidence folder is never written
    ok, detail = safety.quick_check(data / "studio.sqlite3")
    assert not ok and detail
    assert _serve_in_process(data, monkeypatch) == safety.EXIT_STORE_DAMAGED
    assert _serve_in_process(data, monkeypatch, REBUILD_STUDIO_RECOVER="empty") == 0
    assert (data / "damaged").is_dir()


def test_real_controller_process_exits_70_on_a_damaged_store(tmp_path):
    data = tmp_path / "data"
    _make_store(data, tmp_path, n_cases=1)
    _corrupt(data / "studio.sqlite3")
    r = subprocess.run([sys.executable, "-m", "rebuild_controller.cli.main", "serve", "--data-dir", str(data)],
                       cwd=str(CONTROLLER), env=_env(data), capture_output=True, text=True, timeout=120)
    assert r.returncode == safety.EXIT_STORE_DAMAGED, r.stderr[-2000:]
    assert "damaged" in r.stderr and "Traceback" not in r.stderr
    assert (data / "store-recovery.json").is_file()


# ------------------------------------------------------------------------------------------------ (c) one writer per data folder
def test_lock_contention_holder_and_yield(tmp_path):
    a = safety.StoreLock(tmp_path, "mcp")
    b = safety.StoreLock(tmp_path, "controller")
    assert a.acquire()
    assert not b.acquire(wait=0)
    assert safety.read_holder(tmp_path)["role"] == "mcp"
    with pytest.raises(safety.DataFolderInUse, match="rebuild-mcp"):
        b.require()

    def holder():   # a direct-mode client that lets go when asked
        while not safety.yield_requested(tmp_path):
            time.sleep(0.05)
        a.release()
    t = threading.Thread(target=holder)
    t.start()
    assert b.acquire(wait=10, request_yield=True)
    t.join()
    assert safety.read_holder(tmp_path)["role"] == "controller" and not safety.yield_requested(tmp_path)
    b.release()
    assert a.acquire()
    a.release()


def test_serve_refuses_a_folder_in_use(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    held = safety.StoreLock(data, "cli")
    assert held.acquire()
    try:
        assert _serve_in_process(data, monkeypatch, REBUILD_STUDIO_LOCK_WAIT_SECS="0.5") == safety.EXIT_DATA_IN_USE
    finally:
        held.release()


def test_cli_direct_mode_takes_the_lock(tmp_path, capsys):
    from rebuild_controller.cli import main as cli
    data = tmp_path / "data"
    held = safety.StoreLock(data, "mcp")
    assert held.acquire()
    try:
        assert cli.main(["jobs", "--data-dir", str(data)]) == 3
        assert "in use by rebuild-mcp" in capsys.readouterr().err
    finally:
        held.release()
    assert cli.main(["jobs", "--data-dir", str(data)]) == 0
    probe = safety.StoreLock(data, "x")
    assert probe.acquire(), "the CLI released the folder when the command ended"
    probe.release()


def test_second_processes_go_through_the_live_controller(tmp_path, monkeypatch, capsys):
    """CLI and rebuild-mcp against a live controller: forwarded over HTTP; the SQLite file is never opened by them."""
    data = tmp_path / "data"
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    (src / "a.txt").write_text("x")
    proc = _start_controller(data, tmp_path)
    try:
        # CLI in this process, with sqlite3.connect booby-trapped for the data folder
        real_connect = sqlite3.connect

        def guarded(path, *a, **k):
            if str(data) in str(path) or "studio.sqlite3" in str(path):
                raise AssertionError(f"the CLI opened the store directly: {path}")
            return real_connect(path, *a, **k)
        monkeypatch.setattr(sqlite3, "connect", guarded)
        from rebuild_controller.cli import main as cli
        code = cli.main(["rebuild", "--source", str(src), "--output", str(out), "--data-dir", str(data), "--json"])
        o = capsys.readouterr()
        assert code == 0, o.err
        case_id = json.loads(o.out)["case_id"]
        assert cli.main(["status", case_id, "--data-dir", str(data), "--json"]) == 0
        st = json.loads(capsys.readouterr().out)
        assert st["case"]["case_id"] == case_id and st["controller"]["running"] is True
        assert cli.main(["store", "backup", "--data-dir", str(data)]) == 0
        assert "backup taken by the running app" in capsys.readouterr().out
        assert cli.main(["store", "restore", "--latest", "--data-dir", str(data)]) == 3
        monkeypatch.setattr(sqlite3, "connect", real_connect)

        # rebuild-mcp (stdio) in a separate process: sees the same case, reports forwarding
        r = subprocess.run([sys.executable, "-c", _MCP_CLIENT, str(data)], cwd=str(CONTROLLER), env=_env(data),
                           capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stderr[-3000:]
        res = json.loads(r.stdout.strip().splitlines()[-1])
        assert case_id in res["cases"] and res["mode"] == "forwarding"
    finally:
        _stop(proc)


_MCP_CLIENT = r'''
import asyncio, json, os, sys
from mcp.client.stdio import StdioServerParameters
from mcp import Client

async def main(data):
    params = StdioServerParameters(command=sys.executable, args=["-m", "rebuild_controller.mcp.server", "--data-dir", data,
                                   "--toolset", "minimal", "--diagnostic"], env=dict(os.environ))
    async with Client(params) as c:
        r = await c.call_tool("list_cases", {})
        env = json.loads(r.content[0].text)
        assert env["ok"], env
        d = await c.call_tool("admin_diagnostics", {})
        diag = json.loads(d.content[0].text)
        print(json.dumps({"cases": [x["case_id"] for x in env["data"]["cases"]], "mode": diag["data"].get("mode")}))

asyncio.run(main(sys.argv[1]))
'''


def test_direct_mcp_yields_the_folder_to_a_starting_controller(tmp_path):
    """rebuild-mcp opened the folder itself (no app running); the app starts: it asks, rebuild-mcp lets go, the app starts,
    and the next MCP call is forwarded."""
    data = tmp_path / "data"
    script = r'''
import asyncio, json, os, sys, subprocess, time
from mcp.client.stdio import StdioServerParameters
from mcp import Client

async def main(data, py, ctl_dir):
    params = StdioServerParameters(command=py, args=["-m", "rebuild_controller.mcp.server", "--data-dir", data, "--toolset",
                                   "minimal", "--diagnostic", "--runner", "never"], env=dict(os.environ))
    async with Client(params) as c:
        r = json.loads((await c.call_tool("list_cases", {})).content[0].text)
        assert r["ok"], r
        d1 = json.loads((await c.call_tool("admin_diagnostics", {})).content[0].text)["data"]
        proc = subprocess.Popen([py, "-m", "rebuild_controller.cli.main", "serve", "--data-dir", data], cwd=ctl_dir,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            sys.path.insert(0, ctl_dir)
            from rebuild_controller.remote import live_controller
            t0 = time.monotonic()
            while live_controller(data) is None:
                assert proc.poll() is None, f"controller exited {proc.returncode}"
                assert time.monotonic() - t0 < 60, "controller did not start"
                await asyncio.sleep(0.2)
            d2 = json.loads((await c.call_tool("admin_diagnostics", {})).content[0].text)["data"]
            r2 = json.loads((await c.call_tool("list_cases", {})).content[0].text)
            print(json.dumps({"before": d1.get("mode"), "after": d2.get("mode"), "ok": r2["ok"]}))
        finally:
            proc.terminate(); proc.wait(15)

asyncio.run(main(sys.argv[1], sys.executable, sys.argv[2]))
'''
    r = subprocess.run([sys.executable, "-c", script, str(data), str(CONTROLLER)], cwd=str(CONTROLLER), env=_env(data),
                       capture_output=True, text=True, timeout=240)
    assert r.returncode == 0, r.stderr[-3000:]
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res == {"before": None, "after": "forwarding", "ok": True}


# ------------------------------------------------------------------------------------------------ (d) package-context guard
def test_package_name_query_protocol_with_a_stub():
    def no_package(length, buf):
        return safety.APPMODEL_ERROR_NO_PACKAGE
    assert safety.query_package_full_name(no_package) is None

    name = "Claude_1.0.0.0_x64__pzs8sxrjxfjjc"

    def packaged(length_ref, buf):
        length = ctypes.cast(length_ref, ctypes.POINTER(ctypes.c_uint32)).contents
        if buf is None or length.value < len(name) + 1:
            length.value = len(name) + 1
            return safety.ERROR_INSUFFICIENT_BUFFER
        ctypes.memmove(buf, ctypes.create_unicode_buffer(name), (len(name) + 1) * ctypes.sizeof(ctypes.c_wchar))
        return 0
    assert safety.query_package_full_name(packaged) == name

    def weird(length, buf):
        return 87   # ERROR_INVALID_PARAMETER
    assert safety.query_package_full_name(weird) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API")
def test_real_probe_answers_without_raising():
    safety.set_package_probe(None)
    os.environ.pop(safety.ENV_TEST_NO_PACKAGE_GUARD, None)
    try:
        v = safety.package_full_name()
        assert v is None or isinstance(v, str)
    finally:
        os.environ[safety.ENV_TEST_NO_PACKAGE_GUARD] = "1"


def test_packaged_process_never_opens_the_store(tmp_path, monkeypatch, capsys):
    safety.set_package_probe(lambda: "Claude_1.0.0.0_x64__pzs8sxrjxfjjc")
    data = tmp_path / "data"
    from rebuild_controller.cli import main as cli
    assert cli.main(["jobs", "--data-dir", str(data)]) == 3
    assert "Windows app package" in capsys.readouterr().err
    assert not (data / "studio.sqlite3").exists()
    assert _serve_in_process(data, monkeypatch) == safety.EXIT_PACKAGED
    assert not (data / "studio.sqlite3").exists()
    from rebuild_controller.mcp.server import ServerConfig, StoreGate, ToolFailure
    gate = StoreGate(Settings(data_dir=data), ServerConfig(toolset="minimal"))
    with pytest.raises(ToolFailure) as e:
        gate.open_direct()
    assert e.value.code == "packaged_client" and "HTTP" in e.value.next_action
    assert not (data / "studio.sqlite3").exists() and not gate.lock.held


# ------------------------------------------------------------------------------------------------ stable MCP endpoint
def test_stable_mcp_token_is_accepted_on_mcp_only(settings):
    from fastapi.testclient import TestClient
    from rebuild_controller.api.server import create_app, load_mcp_endpoint
    from rebuild_controller.services import StudioServices
    ep = load_mcp_endpoint(settings.data_dir)
    assert load_mcp_endpoint(settings.data_dir) == ep and len(ep["token"]) >= 32
    st = StudioServices(settings)
    try:
        with TestClient(create_app(st, "launch", mcp_token=ep["token"]), base_url="http://127.0.0.1:47815") as c:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            h = {"Authorization": f"Bearer {ep['token']}", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
            assert c.post("/mcp", content=json.dumps(body), headers=h).status_code == 200
            assert c.get("/cases", headers={"Authorization": f"Bearer {ep['token']}"}).status_code == 401
            os.environ["REBUILD_STUDIO_INSTALL"] = str(settings.data_dir)
            try:
                cfg = c.get("/mcp/config?client=claude-code", headers={"Authorization": "Bearer launch"}).json()
            finally:
                del os.environ["REBUILD_STUDIO_INSTALL"]
            assert cfg["transport"] == "http" and cfg["recommended_transport"] == "http"
            assert ep["token"] in cfg["text"] and "launch" not in cfg["text"] and any("stay the same" in n for n in cfg["notes"])
            cfg = c.get("/mcp/config?client=claude-code", headers={"Authorization": "Bearer launch"}).json()
            assert cfg["transport"] == "stdio", "not an installed app -> stdio"
    finally:
        st.stop()
