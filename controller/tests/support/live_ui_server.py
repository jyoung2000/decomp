"""Live UI harness: a REAL controller (uvicorn, real WebSocket/events API) over a temp data dir, wired to a local Ollama.

    python tests/support/live_ui_server.py --data <dir> [--port N] [--model qwen2.5:14b]

Writes <data>/controller.json {port, token, pid}, <data>/ui-case.json {case_id}, then waits for <data>/go and creates the
implement_loop job (the in-process job runner streams `ai.activity` events). Never mocked; nothing leaves this PC.
Touch <data>/stop to shut it down.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
DATA_SRC = ROOT / "tests" / "data" / "tinycalc"
MISSING = "qwen9-does-not-exist:1b"


def drain(studio, rounds: int = 400) -> None:
    from rebuild_controller.jobs import JobState
    for _ in range(rounds):
        time.sleep(0.2)
        if not studio.jobs.list(None, [JobState.QUEUED, JobState.RUNNING]):
            return
    raise RuntimeError("jobs did not settle")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--model", default="qwen2.5:14b")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434/v1")
    ap.add_argument("--attempts", type=int, default=3)
    a = ap.parse_args()
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    import secrets
    import uvicorn
    from rebuild_controller.api.server import create_app, write_controller_info
    from rebuild_controller.config import Settings, set_settings
    from rebuild_controller.implement import mismatch_digest
    from rebuild_controller.providers.ladder import put_ladder
    from rebuild_controller.reconstruct import _task_packet
    from rebuild_controller.services import StudioServices

    data = Path(a.data)
    data.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=data / "store")
    settings.limits.max_stage_seconds = 1800
    settings.limits.lease_timeout_seconds = 600
    set_settings(settings)
    settings.ensure_dirs()
    studio = StudioServices(settings, start_runner=True)
    studio.ai.advisor = None
    token = secrets.token_urlsafe(24)
    port = a.port
    if not port:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    app = create_app(studio, token)
    write_controller_info(data, port, token)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws_ping_interval=10, ws_ping_timeout=20))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)

    conn = studio.connections.create("local", "Ollama (this PC)", endpoint=a.ollama, auth_mode="local")
    p = studio.connections.probe(conn["connection_id"])
    print("probe:", p["state"], [m["id"] for m in p["models"]], flush=True)
    cc = conn["connection_id"]
    rev = put_ladder(studio.connections, "interpretation", [{"connection_id": cc, "model": MISSING}, {"connection_id": cc, "model": a.model}])["config_revision"]

    orig = data / "original"
    orig.mkdir(exist_ok=True)
    (orig / "README.txt").write_text("tinycalc: sum | max | avg of integers\n")
    case = studio.create_case(name="tinycalc", source_root=str(orig), output_root=str(data / "out"), target_language="rust", output_type="exe",
                              ai_policy={"mode": "assisted", "budget_usd": 0, "max_attempts": a.attempts, "max_output_tokens": 3000,
                                         "retry_backoff_s": 0.5, "request_timeout_s": 900, "locality": "local_only"},
                              launch_profile={"baseline_file": str(DATA_SRC / "expected" / "scenarios.json")})
    cid = case["case_id"]
    d = studio.jobs.create(cid, "discover_features", "discover", {}, milestone_id="M-FEATURES")
    studio.jobs.create(cid, "capture_original", "capture", {}, depends_on=[d.job_id], milestone_id="M-FEATURES")
    drain(studio)
    files = {"Cargo.toml": (DATA_SRC / "buggy" / "Cargo.toml").read_text(), "src/main.rs": (DATA_SRC / "buggy" / "src" / "main.rs").read_text()}
    buggy = studio.candidates.propose(cid, files, note="remake with a reported behaviour bug", author="user", base_candidate=None,
                                      plan_revision=studio.plan.current_revision(cid))
    b = studio.jobs.create(cid, "build_candidate", "build buggy", {"candidate_id": buggy["candidate_id"]})
    studio.jobs.create(cid, "compare_candidate", "compare buggy", {"candidate_id": buggy["candidate_id"]}, depends_on=[b.job_id])
    drain(studio)
    packet = _task_packet(studio, studio.cases.get_case(cid), "rust", "native")
    packet["current_candidate_verification"] = mismatch_digest(studio, cid, buggy["candidate_id"])
    packet["task"] = "The CURRENT FILES already implement this program but fail some declared scenarios. Repair them."
    pev = studio.cases.add_evidence(cid, "ai_task_packet", "Repair packet (tinycalc)", body=packet, meta={"untrusted": True})
    (data / "ui-case.json").write_text(json.dumps({"case_id": cid, "ladder_revision": rev, "model": a.model, "missing": MISSING}))
    print("ready", cid, "port", port, flush=True)
    go = data / "go"
    while not go.exists():
        time.sleep(0.5)
    loop = studio.jobs.create(cid, "implement_loop", "AI repair (live local)", {"candidate_id": buggy["candidate_id"], "packet": pev["evidence_id"]},
                              milestone_id="M-IMPL", max_attempts=1)
    print("loop job", loop.job_id, flush=True)
    stop = data / "stop"
    while not stop.exists():
        time.sleep(1)
    server.should_exit = True
    studio.stop()


if __name__ == "__main__":
    main()
