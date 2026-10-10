"""Opt-in live check of the optional Ghidra headless decompiler (R1): ``pytest -m live tests/test_ghidra_live.py``.

Needs Ghidra 12.1.4 (GHIDRA_INSTALL_DIR or <tools>/ghidra from the lock's ``ghidra`` entry) and a JDK 21+ (<tools>/jdk21 from
``temurin-jdk21``, or JAVA_HOME). Runs a per-function decompile through a case and a whole-program run on the C benchmark row.
Skips (never fails) when Ghidra or Java 21 is not installed.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rebuild_controller.adapters.contract import Availability
from rebuild_controller.backends.ghidra import GhidraBackend
from rebuild_controller.config import Settings
from rebuild_controller.ids import sha256_file

pytestmark = pytest.mark.live
REPO = Path(__file__).resolve().parents[2]
C64 = REPO / "fixtures" / "bench" / "c_msvc_x64_o2" / "bin" / "benchc.exe"


@pytest.fixture(scope="module")
def ghidra():
    g = GhidraBackend(Settings())
    p = g.tool_probe()
    if p.availability != Availability.INSTALLED:
        pytest.skip(f"Ghidra not usable here: {p.detail}")
    assert p.version == "12.1.4", p.detail
    return g


def test_ghidra_whole_program_on_bench_row(ghidra):
    res = ghidra.decompile_all_path(C64, max_functions=1000, per_function_timeout=60, timeout=1800)
    assert res["exit_code"] == 0 and res["ghidra_version"] == "12.1.4"
    assert res["total_functions"] >= 50 and res["decompiled"] >= 0.95 * len(res["functions"])
    by_entry = {f["entry"]: f for f in res["functions"]}
    main = by_entry["0x140001900"]       # main of the C row (truth: fixtures/bench/c_msvc_x64_o2/truth/truth.json)
    assert main["ok"] and "stats" in main["code"] and "{" in main["code"]


def test_ghidra_per_function_through_a_case(ghidra, cases, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy2(C64, src / C64.name)
    c = cases.create_case(name="ghidra", source_root=str(src), output_root=str(tmp_path / "out"), target_language="rust", output_type="cli")
    mid = cases.add_module(c["case_id"], C64.name, sha256_file(src / C64.name), C64.stat().st_size, "pe", "native_pe", "x86_64")
    r = ghidra.op_decompile(cases, case_id=c["case_id"], module_id=mid, function="0x140001010")   # bench_base64
    assert r.ok, r.error
    assert r.data["is_real_decompiler"] and r.data["decompiler"] == "ghidra-12.1.4"
    text = r.data["decompiled"]["text"]
    assert r.data["decompiled"]["untrusted"] and r.data["addr"] == "0x140001010" and "{" in text and len(text) > 200
    ev = cases.get_evidence(r.evidence_ids[0])
    assert ev["producer"] == "ghidra" and ev["meta"]["untrusted"]
    again = ghidra.op_decompile(cases, case_id=c["case_id"], module_id=mid, function="0x140001010")
    assert again.ok and again.data["cached"] and again.evidence_ids == r.evidence_ids
