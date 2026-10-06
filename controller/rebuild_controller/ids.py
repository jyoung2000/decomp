"""Stable identifiers and content hashing."""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path


def new_id(prefix: str) -> str:
    """Time-ordered unique id: <prefix>_<ms hex><random>. Sorts chronologically."""
    ms = int(time.time() * 1000)
    return f"{prefix}_{ms:012x}{secrets.token_hex(5)}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def stable_json_hash(obj) -> str:
    import json
    return sha256_text(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time() % 1) * 1000):03d}Z"


def now_ts() -> float:
    return time.time()


def hostname() -> str:
    return os.environ.get("COMPUTERNAME") or os.uname().nodename if hasattr(os, "uname") else "unknown"
