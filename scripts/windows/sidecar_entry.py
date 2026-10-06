"""PyInstaller entry point for the Rebuild Studio controller sidecar (rebuild-controller.exe).

Equivalent to `python -m rebuild_controller.cli.main`: the Tauri shell starts it as `rebuild-controller serve`
and the controller writes <data_dir>/controller.json (see docs/API.md). PyInstaller cannot take `-m`, so this
file imports the same `main`.
"""
import multiprocessing
import sys

from rebuild_controller.cli.main import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
