"""R11 (e): tests never use the real per-user data folder. ``tool_settings()`` is the Settings for tests that only need
tool discovery (module-level skip checks, backend construction): its data folder is a per-process temporary directory."""
from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

from rebuild_controller.config import Settings

_DIR: Path | None = None


def test_data_dir() -> Path:
    global _DIR
    if _DIR is None:
        _DIR = Path(tempfile.mkdtemp(prefix="rs-test-data-"))
        atexit.register(shutil.rmtree, _DIR, True)
    return _DIR


def tool_settings(**kw) -> Settings:
    return Settings(data_dir=test_data_dir(), **kw)
