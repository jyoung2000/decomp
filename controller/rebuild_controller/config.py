"""Runtime configuration and resource bounds for the controller."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def default_data_dir() -> Path:
    env = os.environ.get("REBUILD_STUDIO_DATA")
    if env:
        return Path(env)
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
