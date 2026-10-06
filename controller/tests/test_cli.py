"""Tests for `rebuildctl` (rebuild_controller.cli.main)."""
import json
import os
import sys
import types
from pathlib import Path

import pytest

from rebuild_controller.cli import controller_status
from rebuild_controller.cli import main as cli

CASE = "case_" + "a" * 22
JOB = "job_" + "b" * 22


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# ------------------------------------------------------------------------------------------------ parsing
def test_parser_accepts_documented_commands():
    p = cli.build_parser()
    a = p.parse_args(["doctor", "--smoke", "--json"])
    assert a.smoke and a.json and a.fn is cli.cmd_doctor
    a = p.parse_args(["rebuild", "--source", "/s", "--output", "/o", "--language", "rust_bevy", "--type", "installer",
                      "--ai", "assist_on_failure", "--execute-original", "--wait", "--json"])
    assert (a.source, a.output, a.language, a.type, a.ai) == ("/s", "/o", "rust_bevy", "installer", "assist_on_failure")
    assert a.execute_original and a.wait and a.json
    assert p.parse_args(["status", CASE]).case_id == CASE
    assert p.parse_args(["cancel", JOB]).id == JOB
    assert p.parse_args(["resume", CASE]).id == CASE
    assert p.parse_args(["jobs", "--case", CASE, "--state", "failed"]).state == ["failed"]
    e = p.parse_args(["evidence", "search", CASE, "main", "--kind", "strings", "--limit", "5"])
    assert (e.query, e.kind, e.limit) == ("main", ["strings"], 5)
    assert p.parse_args(["export-plan", CASE]).case_id == CASE
    s = p.parse_args(["serve", "--port", "8123", "--data-dir", "/d"])
    assert (s.port, s.data_dir) == (8123, "/d")
    assert p.parse_args(["mcp"]).fn is cli.cmd_mcp


@pytest.mark.parametrize("argv", [
    ["status", "not-an-id"], ["cancel", "../../x"], ["rebuild", "--source", "/s"], ["rebuild", "--source", "/s", "--output", "/o", "--language", "cobol"],
    ["rebuild", "--source", "/s", "--output", "/o", "--ai", "yolo"], ["evidence", "search"], ["jobs", "--state", "weird"], [],
])
def test_parser_rejects_bad_input(argv, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2


# ------------------------------------------------------------------------------------------------ fake studio flows
class FakeJobs:
    def __init__(self, script):
        self.script = list(script)    # list of state-lists, consumed one per poll
        self.last = self.script[-1] if self.script else []

    def list(self, case_id=None, states=None):
        states_now = self.script.pop(0) if len(self.script) > 1 else self.last
        return [types.SimpleNamespace(to_dict=lambda i=i, s=s: {"job_id": f"job_{i:022x}", "case_id": case_id or CASE, "stage": "inventory",
                                                                "state": s, "attempt": 1, "progress": {"done": 1, "total": 2}, "error": "boom" if s == "failed" else None})
                for i, s in enumerate(states_now)]


class FakeStudio:
    def __init__(self, script, tmp_path):
        self.jobs = FakeJobs(script)
        self.settings = types.SimpleNamespace(data_dir=tmp_path)
        self.created = None
        self.stopped = False
        self.cancelled = None

    def create_case(self, **kw):
        self.created = kw
        return {"case_id": CASE, **kw}

    def start_rebuild(self, case_id):
        return {"job_ids": ["job_x"]}

    def stop(self):
        self.stopped = True

    def cancel(self, job_id=None, case_id=None):
        self.cancelled = (job_id, case_id)
        return [JOB]

    def resume(self, job_id=None, case_id=None):
        return [JOB]


@pytest.fixture
def fake_open(monkeypatch, tmp_path):
    holder = {}

    def install(script):
        st = FakeStudio(script, tmp_path)
        holder["studio"] = st
        monkeypatch.setattr(cli, "_open_studio", lambda args, start_runner=False: (holder.update(start_runner=start_runner), st)[1])
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        return st
    install.holder = holder
    return install


def test_rebuild_wait_success_and_arguments(fake_open, capsys):
    st = fake_open([["queued", "running"], ["running", "completed"], ["completed", "completed"]])
    code, out, err = run(capsys, "rebuild", "--source", "/data/src/MyGame", "--output", "/data/out", "--language", "rust", "--type", "exe",
                         "--ai", "assisted", "--budget-usd", "3", "--execute-original", "--wait")
    assert code == 0, (out, err)
    assert st.created == {"name": "MyGame", "source_root": "/data/src/MyGame", "output_root": "/data/out", "target_language": "rust",
                          "output_type": "exe", "ai_policy": {"mode": "assisted", "budget_usd": 3.0}, "launch_profile": {"execute_original": True}}
    assert fake_open.holder["start_runner"] is True and st.stopped is True
    assert "completed" in out


def test_rebuild_wait_failure_exit_code_and_json(fake_open, capsys):
    fake_open([["running"], ["completed", "failed"]])
    code, out, err = run(capsys, "rebuild", "--source", "/s", "--output", "/o", "--wait", "--json")
    assert code == 1
    data = json.loads(out)
    assert data["ok"] is False and data["failed"][0]["error"] == "boom" and data["jobs"] == {"completed": 1, "failed": 1}


def test_rebuild_without_wait_does_not_start_runner_and_warns(fake_open, capsys):
    st = fake_open([["queued"]])
    code, out, err = run(capsys, "rebuild", "--source", "/s", "--output", "/o")
    assert code == 0 and out.strip() == CASE
    assert fake_open.holder["start_runner"] is False and st.stopped is False
    assert "no controller is running" in err
    assert st.created["launch_profile"] == {"execute_original": False} and st.created["ai_policy"] == {"mode": "no_ai"}


def test_wait_timeout(fake_open, capsys, monkeypatch):
    fake_open([["running"]])
    t = iter([0, 5, 10, 20, 30, 40, 50])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(t, 999))
    code, out, err = run(capsys, "rebuild", "--source", "/s", "--output", "/o", "--wait", "--timeout", "3")
    assert code == 1 and "timed out" in err


def test_cancel_and_resume_dispatch_on_id_kind(fake_open, capsys):
    st = fake_open([["queued"]])
    code, out, _ = run(capsys, "cancel", CASE, "--json")
    assert code == 0 and json.loads(out) == {"action": "cancel", "job_ids": [JOB]}
    assert st.cancelled == (None, CASE)
    code, out, _ = run(capsys, "resume", JOB)
    assert code == 0 and JOB in out


def test_jobs_listing_json_and_table(fake_open, capsys):
    pytest.importorskip("rebuild_controller.jobs")
    fake_open([["running", "failed"]])
    code, out, _ = run(capsys, "jobs", "--case", CASE, "--json")
    rows = json.loads(out)
    assert code == 0 and [r["state"] for r in rows] == ["running", "failed"]
    code, out, _ = run(capsys, "jobs", "--case", CASE)
    assert "job" in out.splitlines()[0] and "failed" in out


# ------------------------------------------------------------------------------------------------ serve / mcp wiring
def test_serve_is_guarded_when_api_server_missing(capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "rebuild_controller.api.server", None)  # forces ImportError
    code, out, err = run(capsys, "serve", "--port", "0")
    assert code == 3 and "not available" in err


def test_serve_calls_integrator_entry_point_with_supported_kwargs(capsys, monkeypatch, tmp_path):
    seen = {}
    mod = types.ModuleType("rebuild_controller.api.server")
    mod.serve = lambda port=0, data_dir=None: seen.update(port=port, data_dir=data_dir)
    monkeypatch.setitem(sys.modules, "rebuild_controller.api.server", mod)
    monkeypatch.delenv("REBUILD_STUDIO_DATA", raising=False)
    code, _, _ = run(capsys, "serve", "--port", "8123", "--data-dir", str(tmp_path))
    assert code == 0 and seen == {"port": 8123, "data_dir": str(tmp_path)}
    monkeypatch.delenv("REBUILD_STUDIO_DATA", raising=False)


def test_mcp_subcommand_forwards_arguments(capsys, monkeypatch):
    seen = {}
    from rebuild_controller.mcp import server
    monkeypatch.setattr(server, "main", lambda argv=None: seen.setdefault("argv", list(argv)) and 0)
    code, _, _ = run(capsys, "mcp", "--toolset", "minimal", "--diagnostic")
    assert code == 0 and seen["argv"] == ["--toolset", "minimal", "--diagnostic"]


def test_console_script_entry_points_resolve():
    from rebuild_controller.mcp.server import main as mcp_main
    assert callable(cli.main) and callable(mcp_main)


# ------------------------------------------------------------------------------------------------ helpers
def test_controller_status_reads_pidfile_without_leaking_token(tmp_path):
    assert controller_status(tmp_path) == {"running": False, "pid": None, "port": None}
    (tmp_path / "controller.json").write_text(json.dumps({"pid": os.getpid(), "port": 4321, "token": "SECRET"}))
    st = controller_status(tmp_path)
    assert st == {"running": True, "pid": os.getpid(), "port": 4321}
    (tmp_path / "controller.json").write_text(json.dumps({"pid": 2**22 + 12345, "port": 1, "token": "x"}))
    assert controller_status(tmp_path)["running"] is False
    (tmp_path / "controller.json").write_text("not json")
    assert controller_status(tmp_path)["running"] is False


# ------------------------------------------------------------------------------------------------ real controller
@pytest.fixture
def real_env(tmp_path, monkeypatch):
    pytest.importorskip("rebuild_controller.services")
    from rebuild_controller import config
    from rebuild_controller.config import Settings, set_settings
    from rebuild_controller.services import StudioServices
    previous = config._settings
    data = tmp_path / "data"
    set_settings(Settings(data_dir=data))
    try:
        try:
            StudioServices().stop()
        except Exception as exc:
            pytest.skip(f"StudioServices cannot be constructed here: {type(exc).__name__}: {exc}")
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.txt").write_text("hello")
        yield types.SimpleNamespace(data=str(data), src=str(src), out=str(tmp_path / "out"))
    finally:
        config._settings = previous


def test_doctor_json_against_real_install(real_env, capsys):
    code, out, err = run(capsys, "doctor", "--json", "--data-dir", real_env.data)
    assert code == 0, err
    rep = json.loads(out)
    assert rep["mode"] in ("full", "registry-only") and isinstance(rep["backends"], list) and "summary" in rep
    code, out, _ = run(capsys, "doctor", "--data-dir", real_env.data)
    assert code == 0 and "backend" in out.splitlines()[0]


def test_real_case_lifecycle_through_the_cli(real_env, capsys):
    d = ["--data-dir", real_env.data]
    code, out, err = run(capsys, "rebuild", "--source", real_env.src, "--output", real_env.out, "--language", "rust", *d)
    assert code == 0, err
    case_id = out.strip().splitlines()[-1]
    assert case_id.startswith("case_")
    code, out, _ = run(capsys, "status", case_id, "--json", *d)
    st = json.loads(out)
    assert code == 0 and st["case"]["case_id"] == case_id and sum(st["jobs"].values()) >= 1
    code, out, _ = run(capsys, "jobs", "--case", case_id, "--json", *d)
    assert code == 0 and len(json.loads(out)) >= 1
    code, out, _ = run(capsys, "evidence", "search", case_id, "zzz-no-match", "--json", *d)
    assert code == 0 and json.loads(out) == []
    code, out, _ = run(capsys, "export-plan", case_id, "--json", *d)
    paths = json.loads(out)
    assert code == 0 and Path(paths["json"]).is_file() and Path(paths["html"]).is_file()
    assert Path(paths["json"]).parent == (Path(real_env.out) / "reports").resolve()
    assert json.loads(Path(paths["json"]).read_text())["case_id"] == case_id
    code, out, _ = run(capsys, "cancel", case_id, "--json", *d)
    assert code == 0 and json.loads(out)["action"] == "cancel"
    code, out, _ = run(capsys, "status", "case_" + "0" * 22, *d)
    assert code == 1


def test_rebuild_wait_stops_when_only_blocked_jobs_remain(fake_open, capsys):
    """Nothing queued or running and something blocked: the run cannot progress without the user, so --wait must return
    the blockers promptly instead of sitting until --timeout (seen with the installed app on a web project)."""
    fake_open([["completed", "blocked"]])
    code, out, err = run(capsys, "rebuild", "--source", "/s", "--output", "/o", "--wait", "--json", "--timeout", "30")
    data = json.loads(out)
    assert code == 1 and data["ok"] is False and data["jobs"] == {"completed": 1, "blocked": 1}
    assert [b["stage"] for b in data["blocked"]] == ["inventory"]
