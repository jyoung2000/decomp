"""Live RE harness for the R6 end-to-end spec (ui/e2e/re-real-controller.spec.ts): a REAL controller (StudioServices + uvicorn,
real rizin) over a fresh temporary data folder, with one project on the pecli fixture whose installation folder has been
scanned (the real inventory scan; no analysis is started, so the app offers 'Analyse now').

    python tests/support/live_re_server.py --data <dir> --port N --token T

<dir> must be empty, missing, or a folder this harness made before (it holds a ``.rs-e2e-re`` marker): its contents are
replaced. Writes <dir>/re-case.json {case_id, module_id, name, rizin, rizin_version, reason}. Tools come from
REBUILD_STUDIO_TOOLS (default D:/rs-test-tools when that folder exists); the per-user data and tools folders are never used.
Touch <dir>/stop (or kill the process) to shut it down. Nothing is mocked.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))
FIXTURE = REPO / "fixtures" / "pecli" / "original"
MARKER = ".rs-e2e-re"
DEFAULT_TOOLS = Path("D:/rs-test-tools")


def fresh_dir(d: Path) -> None:
    if d.exists() and any(d.iterdir()):
        if not (d / MARKER).is_file():
            raise SystemExit(f"refusing to reuse {d}: it is not empty and was not made by this harness")
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    (d / MARKER).write_text("made by tests/support/live_re_server.py; deleted and recreated on every run\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", required=True)
    a = ap.parse_args()
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    if not os.environ.get("REBUILD_STUDIO_TOOLS"):
        if not DEFAULT_TOOLS.is_dir():
            raise SystemExit("set REBUILD_STUDIO_TOOLS to a test tools folder (the per-user tools folder is never used here)")
        os.environ["REBUILD_STUDIO_TOOLS"] = str(DEFAULT_TOOLS)
    os.environ.pop("REBUILD_STUDIO_DATA", None)

    import uvicorn
    from rebuild_controller.api.server import create_app, write_controller_info
    from rebuild_controller.backends.inventory import inventory_root
    from rebuild_controller.backends.rizin_worker import find_rizin
    from rebuild_controller.config import Settings, set_settings
    from rebuild_controller.services import StudioServices

    data = Path(a.data).resolve()
    fresh_dir(data)
    settings = Settings(data_dir=data / "store")
    set_settings(settings)
    settings.ensure_dirs()
    studio = StudioServices(settings, start_runner=False)      # no pipeline jobs: the spec drives everything through the app
    studio.ai.advisor = None
    app = create_app(studio, a.token)
    write_controller_info(data, a.port, a.token)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=a.port, log_level="warning", ws_ping_interval=10, ws_ping_timeout=20))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)

    src = data / "original"
    shutil.copytree(FIXTURE, src)                                # the fixture folder itself is never touched
    case = studio.create_case(name="pecli (R6 e2e)", source_root=str(src), output_root=str(data / "out"), target_language="auto",
                              output_type="exe", ai_policy={"mode": "no_ai"})
    cid = case["case_id"]
    inv = inventory_root(src, settings.limits)                  # the scan the pipeline's inventory stage runs
    module_id = None
    for f in inv["files"]:
        d = f.get("detect", {})
        if f["path"] in inv["modules"] and f.get("sha256"):
            mid = studio.cases.add_module(cid, f["path"], f["sha256"], f["size"], d["format"], d["profile"], d.get("arch"),
                                          {"flags": d.get("flags", {}), "bits": d.get("bits")})
            if f["path"].lower().endswith("pecli.exe"):
                module_id = mid
    studio.cases.add_evidence(cid, "inventory", "Installation inventory", body=inv, inputs={"root": str(src)},
                              meta={"file_count": inv["file_count"], "module_count": inv["module_count"]})
    tool = find_rizin(settings)
    info = {"case_id": cid, "module_id": module_id, "name": case["name"], "rizin": bool(tool and tool.version),
            "rizin_version": getattr(tool, "version", None), "tools": os.environ["REBUILD_STUDIO_TOOLS"],
            "reason": None if tool and tool.version else f"rizin was not found under REBUILD_STUDIO_TOOLS={os.environ['REBUILD_STUDIO_TOOLS']}"}
    (data / "re-case.json").write_text(json.dumps(info))
    print("ready", json.dumps(info), "port", a.port, flush=True)
    stop = data / "stop"
    while not stop.exists():
        time.sleep(0.5)
    server.should_exit = True
    studio.stop()


if __name__ == "__main__":
    main()
