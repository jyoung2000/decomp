import pytest
from pathlib import Path

from rebuild_controller.config import Limits, Settings, set_settings
from rebuild_controller.events import EventLog
from rebuild_controller.jobs import JobStore, JobRunner, StageRegistry
from rebuild_controller.store.db import Database
from rebuild_controller.cases import CaseStore


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
