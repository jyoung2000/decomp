import os

import pytest
from pathlib import Path

# R11 (e): tests never fall back to the real per-user data folder. Set before anything reads settings, and inherited by
# every subprocess a test starts (CLI, rebuild-mcp, serve): without an explicit data folder they fail instead.
from rebuild_controller import config as _config  # noqa: E402
from rebuild_controller.store import safety as _safety  # noqa: E402

_config.require_explicit_data_dir()
os.environ.pop("REBUILD_STUDIO_DATA", None)
# Deterministic package-identity guard: tests that exercise it install a stub; subprocesses skip the real probe (honoured
# only together with REBUILD_STUDIO_REQUIRE_EXPLICIT_DATA=1). This matters when the test runner itself was started from a
# packaged app (e.g. Claude Desktop).
os.environ[_safety.ENV_TEST_NO_PACKAGE_GUARD] = "1"
os.environ.setdefault("REBUILD_STUDIO_MCP_PORT", "0")       # controllers started by tests use a random port

from rebuild_controller.config import Limits, Settings, set_settings  # noqa: E402
from rebuild_controller.events import EventLog
from rebuild_controller.jobs import JobStore, JobRunner, StageRegistry
from rebuild_controller.store.db import Database
from rebuild_controller.cases import CaseStore


@pytest.fixture(autouse=True)
def _r11_isolation():
    """No settings leak between tests (get_settings() without a data folder raises) and no real package probe."""
    _config._settings = None
    os.environ.pop("REBUILD_STUDIO_DATA", None)   # `serve` / `rebuild-mcp --data-dir` set it process-wide
    _safety.set_package_probe(lambda: None)
    yield
    os.environ.pop("REBUILD_STUDIO_DATA", None)
    _safety.set_package_probe(None)
    _config._settings = None


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path / "data", limits=Limits(lease_timeout_seconds=2, worker_heartbeat_seconds=0, max_stage_seconds=20))
    s.ensure_dirs()
    set_settings(s)
    return s


@pytest.fixture
def db(settings) -> Database:
    d = Database(settings.db_path)
    yield d
    d.close()


@pytest.fixture
def events(db) -> EventLog:
    return EventLog(db)


@pytest.fixture
def jobs(db, events, settings) -> JobStore:
    return JobStore(db, events, lease_timeout=settings.limits.lease_timeout_seconds)


@pytest.fixture
def registry() -> StageRegistry:
    return StageRegistry()


@pytest.fixture
def runner(jobs, events, registry, settings) -> JobRunner:
    return JobRunner(jobs, events, registry, settings.limits)


@pytest.fixture
def cases(db, events, settings) -> CaseStore:
    return CaseStore(db, events, settings)


@pytest.fixture
def src_out(tmp_path: Path):
    src = tmp_path / "src"; src.mkdir()
    (src / "app.txt").write_text("hello")
    out = tmp_path / "out"
    return src, out
