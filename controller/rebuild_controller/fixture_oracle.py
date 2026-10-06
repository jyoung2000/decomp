"""Convert a fixture oracle (fixtures/<name>/expected/scenarios.json) into the verifier's frozen baseline schema.

The fixture harness is an independent evaluation tool; the converter only re-shapes data, it never alters expected values.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def is_fixture_oracle(doc: dict[str, Any]) -> bool:
    return "fixture" in doc and "scenarios" in doc and "launcher" in doc


def convert(doc: dict[str, Any], original_root: Path) -> dict[str, Any]:
    launcher = doc["launcher"]
    launch: dict[str, Any]
    if launcher and launcher[0] == "wine":
        launch = {"type": "exe", "path": launcher[1].replace("{ORIG}/", "")}
    elif launcher and launcher[0] == "dotnet":
        launch = {"type": "dotnet", "path": launcher[1].replace("{ORIG}/", "")}
    else:
        launch = {"type": "command", "command": [c.replace("{ORIG}", str(original_root)) for c in launcher]}
    scenarios = []
    for sc in doc["scenarios"]:
        steps_in, steps_exp = [], []
        for stp in sc["steps"]:
            steps_in.append({"args": list(stp["args"]), "stdin": stp.get("stdin") or ""})
            steps_exp.append({"args": list(stp["args"]), "exit_code": stp["exit_code"], "stdout": stp.get("stdout", ""), "stderr": stp.get("stderr", ""), "runner": doc.get("oracle", {}).get("runner", "")})
        files = {name: info["sha256"] for name, info in sc.get("final_files", {}).items() if info.get("sha256")}
        setup = {name: _inline_setup(spec, original_root) for name, spec in (sc.get("setup_files", {}) or {}).items()}
        scenarios.append({"id": sc["id"], "feature_id": sc.get("feature"), "title": sc.get("description", sc["id"]), "steps": steps_in,
                          "setup_files": setup, "channels": ["exit_code", "stdout", "stderr", "files"],
                          "normalize": {"stdout": ["crlf"], "stderr": ["crlf"]}, "expected": {"steps": steps_exp, "files": files}})
    return {"kind": "cli", "launch": launch, "scenarios": scenarios, "oracle": doc.get("oracle", {}), "fixture": doc["fixture"],
            "observable_on": doc.get("observable_on"), "newline_policy": doc.get("newline_policy")}


def load_baseline_file(path: Path, original_root: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text("utf-8"))
    return convert(doc, original_root) if is_fixture_oracle(doc) else doc


def _inline_setup(spec: Any, original_root: Path) -> dict[str, Any]:
    """Resolve fixture setup specs (from_original / flip_byte / text / hex) into concrete bytes so candidate runs never read the original root."""
    import hashlib
    if isinstance(spec, str):
        data = spec.encode("utf-8")
    elif "hex" in spec:
        data = bytes.fromhex(spec["hex"])
    elif "text" in spec:
        data = spec["text"].encode("utf-8")
    elif "from_original" in spec:
        data = (original_root / spec["from_original"]).read_bytes()
    else:
        raise ValueError(f"unsupported setup spec {spec}")
    if isinstance(spec, dict) and "flip_byte" in spec:
        b = bytearray(data); i = int(spec["flip_byte"]); b[i] ^= 0xFF; data = bytes(b)
    return {"hex": data.hex(), "sha256": hashlib.sha256(data).hexdigest()}
