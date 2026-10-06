from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..ids import sha256_bytes, sha256_file
from .base import ComparisonResult


def compare_files(expected: dict[str, str], actual_root: Path, *, ignore: set[str] | None = None) -> ComparisonResult:
    ignore = ignore or set()
    actual = {p.relative_to(actual_root).as_posix(): sha256_file(p) for p in actual_root.rglob("*") if p.is_file()}
    exp = {k: v for k, v in expected.items() if k not in ignore}
    act = {k: v for k, v in actual.items() if k not in ignore}
    mism = {k: {"expected": exp.get(k), "actual": act.get(k)} for k in sorted(set(exp) | set(act)) if exp.get(k) != act.get(k)}
    return ComparisonResult("files", "sha256:exact", "pass" if not mism else "fail", {"mismatches": mism},
                            original_hash=sha256_bytes(json.dumps(exp, sort_keys=True).encode()), candidate_hash=sha256_bytes(json.dumps(act, sort_keys=True).encode()))


def compare_state(expected: Any, actual: Any, *, ignore_keys: set[str] | None = None) -> ComparisonResult:
    ignore_keys = ignore_keys or set()

    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if k not in ignore_keys}
        if isinstance(o, list):
            return [strip(v) for v in o]
        return o
    e, a = strip(expected), strip(actual)
    ok = e == a
    return ComparisonResult("state", "json:exact" + (f"(ignore {sorted(ignore_keys)})" if ignore_keys else ""), "pass" if ok else "fail",
                            {"expected": e, "actual": a} if not ok else {}, original_hash=sha256_bytes(json.dumps(e, sort_keys=True, default=str).encode()),
                            candidate_hash=sha256_bytes(json.dumps(a, sort_keys=True, default=str).encode()))
