"""Runtime configuration and resource bounds for the controller."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


ENV_REQUIRE_EXPLICIT = "REBUILD_STUDIO_REQUIRE_EXPLICIT_DATA"


class DataDirRequired(RuntimeError):
    """Raised in test/script contexts when no explicit data folder was given (R11: never fall back to the real one)."""


def test_context() -> bool:
    """True in test/script contexts: pytest (conftest) and the repo scripts set REBUILD_STUDIO_REQUIRE_EXPLICIT_DATA=1."""
    return os.environ.get(ENV_REQUIRE_EXPLICIT) == "1"


def require_explicit_data_dir() -> None:
    """Called by tests and scripts: from now on (and in child processes) the per-user data folder is never used implicitly."""
    os.environ[ENV_REQUIRE_EXPLICIT] = "1"


def user_data_dir() -> Path:
    """The per-user data folder the installed app uses (no environment override, no guard)."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "RebuildStudio"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "rebuild-studio"


def _same_path(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except (OSError, ValueError):
        return False


def guard_data_dir(path: Path) -> Path:
    """In test/script contexts refuse the real per-user data folder even when it is named explicitly."""
    if test_context() and _same_path(Path(path), user_data_dir()):
        raise DataDirRequired(f"refusing to use the real Rebuild Studio data folder {path} from a test or script; "
                              "pass a temporary --data-dir / Settings(data_dir=...)")
    return Path(path)


def default_data_dir() -> Path:
    env = os.environ.get("REBUILD_STUDIO_DATA")
    if env:
        return guard_data_dir(Path(env))
    if test_context():
        raise DataDirRequired("no data folder given: tests and scripts must pass an explicit temporary data folder "
                              "(Settings(data_dir=...), --data-dir or REBUILD_STUDIO_DATA); the per-user folder is never used "
                              f"implicitly while {ENV_REQUIRE_EXPLICIT}=1")
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "RebuildStudio"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "rebuild-studio"


def default_tools_dir() -> Path:
    """REBUILD_STUDIO_TOOLS, else the per-user RebuildStudio/tools folder under LOCALAPPDATA on Windows, else /opt/rebuild-tools."""
    env = os.environ.get("REBUILD_STUDIO_TOOLS")
    if env:
        return Path(env)
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "RebuildStudio" / "tools"
    return Path("/opt/rebuild-tools")


@dataclass
class Limits:
    """Hard bounds. Every subprocess/stage is checked against these."""
    max_subprocess_output_bytes: int = 8 * 1024 * 1024
    max_stage_seconds: int = 1800
    max_attempts: int = 3
    max_queue_length: int = 10_000
    max_archive_expansion_bytes: int = 4 * 1024 * 1024 * 1024
    max_archive_entries: int = 200_000
    max_inventory_files: int = 500_000
    max_context_bytes: int = 256 * 1024
    worker_heartbeat_seconds: int = 5
    lease_timeout_seconds: int = 30
    max_concurrent_jobs: int = max(1, (os.cpu_count() or 2) - 1)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=default_data_dir)
    limits: Limits = field(default_factory=Limits)
    tools_dir: Path = field(default_factory=lambda: default_tools_dir())

    def __post_init__(self) -> None:
        self.data_dir = guard_data_dir(Path(self.data_dir))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "studio.sqlite3"

    @property
    def cases_dir(self) -> Path:
        return self.data_dir / "cases"

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "blobs"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.cases_dir, self.blobs_dir):
            d.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(s: Settings) -> None:
    global _settings
    _settings = s
