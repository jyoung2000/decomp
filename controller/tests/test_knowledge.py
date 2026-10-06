import json
from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"


@pytest.fixture
def studio(settings):
    from rebuild_controller.services import StudioServices
    s = StudioServices(settings)
    yield s
    s.stop()


def test_signature_lifecycle_and_rollback(studio):
    k = studio.knowledge
    sig = k.propose(kind="signature", name="crt.startup", body={"pattern": "48 83 EC 28 ?? ?? ?? ??", "symbol": "__crt_start", "arch": "x86_64"},
                    constraints={"arch": "x86_64"}, author="model", source="mcp", confidence=0.8,
                    acceptance={"positives": ["4883ec28e8aabbccdd"], "negatives": ["4883ec20e8aabbccdd"]})
    assert sig["state"] == "proposed" and sig["version"] == 1
    k2 = k.validate(sig["knowledge_id"])
    assert k2["state"] == "promoted" and k2["regression"]["ok"] and k2["regression"]["false_positives"] == 0
    assert k.applicable("signature", {"arch": "x86_64"}) and not k.applicable("signature", {"arch": "x86"})
    # a worse v2 (matches a negative) is quarantined, v1 stays promoted
    v2 = k.propose(kind="signature", name="crt.startup", body={"pattern": "48 83 EC"}, constraints={"arch": "x86_64"}, author="model", source="mcp", confidence=0.9,
                   acceptance={"positives": ["4883ec28"], "negatives": ["4883ec20"]})
    assert v2["version"] == 2 and v2["lineage"] == sig["knowledge_id"]
    assert k.validate(v2["knowledge_id"])["state"] == "quarantined"
    assert k.get(sig["knowledge_id"])["state"] == "promoted"
    # a good v3 supersedes; rollback restores v1
    v3 = k.propose(kind="signature", name="crt.startup", body={"pattern": "48 83 EC 28"}, constraints={"arch": "x86_64"}, author="user", source="ui", confidence=1.0,
                   acceptance={"positives": ["4883ec28"], "negatives": ["4883ec20"]})
    assert k.validate(v3["knowledge_id"])["state"] == "promoted" and k.get(sig["knowledge_id"])["state"] == "rolled_back"
    restored = k.rollback(v3["knowledge_id"])
    assert restored is None  # lineage of v3 is v2 (quarantined) → nothing restored automatically
    assert k.get(v3["knowledge_id"])["state"] == "rolled_back"


def test_model_cannot_self_promote_and_bad_proposals_rejected(studio):
    k = studio.knowledge
    with pytest.raises(ValueError):
        k.propose(kind="exploit", name="x", body={}, constraints={}, author="model", source="mcp", confidence=0.5)
    with pytest.raises(ValueError):
        k.propose(kind="signature", name="bad name!", body={}, constraints={}, author="model", source="mcp", confidence=0.5)
    p = k.propose(kind="rewrite", name="idiom.xor", body={"match": r"(\w+) \^= \1", "replace": r"\1 = 0"}, constraints={}, author="model", source="mcp", confidence=0.5,
                  acceptance={"cases": [{"input": "a ^= a", "expected": "a = 0"}], "must_not_change": ["a ^= b"]})
    assert k.get(p["knowledge_id"])["state"] == "proposed"  # proposing never promotes
    assert k.validate(p["knowledge_id"])["state"] == "promoted"
    crash = k.propose(kind="rewrite", name="idiom.crash", body={"match": "(", "replace": ""}, constraints={}, author="model", source="mcp", confidence=0.5, acceptance={"cases": [{"input": "x", "expected": "x"}]})
    assert k.validate(crash["knowledge_id"])["state"] == "quarantined"


def test_parser_validator_rejects_malformed(studio):
    k = studio.knowledge
    p = k.propose(kind="parser", name="pcli.header", body={"magic": "50434c49", "struct": [{"name": "version", "type": "u32"}, {"name": "count", "type": "u32"}]},
                  constraints={"format": "pcli"}, author="model", source="mcp", confidence=0.7,
                  acceptance={"golden": [{"hex": "50434c490100000003000000", "expect": {"version": 1, "count": 3}}], "malformed": ["4e4f5045", "50434c4901"]})
    r = k.validate(p["knowledge_id"])
    assert r["state"] == "promoted" and r["regression"]["malformed_accepted"] == []


def test_corrupted_entry_quarantined(studio):
    k = studio.knowledge
    p = k.propose(kind="template", name="t1", body={"text": "fn {{name}}() {}", "placeholders": ["name"]}, constraints={}, author="user", source="ui", confidence=1.0)
    k.validate(p["knowledge_id"])
    blob = k.blobs.path_for(p["body_sha"])
    blob.write_bytes(b'{"body": {"text": "evil"}}')  # tamper
    bad = k.check_integrity()
    assert [b["knowledge_id"] for b in bad] == [p["knowledge_id"]] and k.get(p["knowledge_id"])["state"] == "quarantined"
    assert k.applicable("template", {}) == []


def test_reuse_on_second_fixture_without_ai(studio, tmp_path):
    """Validated knowledge from one program is reused on a second supported fixture with the AI adapter replaced by one that raises."""
    from rebuild_controller.knowledge.apply import reuse_on_module
    from rebuild_controller.providers.mock import RaisingProvider
    k = studio.knowledge
    pe1 = DATA / "sample_pe.exe"
    assert pe1.exists()
    b = pe1.read_bytes()
    # derive a signature from fixture 1: the first 8 bytes of its PE entry prologue region (a real byte pattern from program 1)
    import pefile
    pe = pefile.PE(str(pe1)); ep = pe.OPTIONAL_HEADER.AddressOfEntryPoint; off = pe.get_offset_from_rva(ep); pe.close()
    pattern = " ".join(f"{x:02X}" for x in b[off:off + 8])
    sig = k.propose(kind="signature", name="mingw.entry", body={"pattern": pattern, "symbol": "mainCRTStartup"}, constraints={"arch": "x86_64", "format": "pe"},
                    author="model", source="mcp", confidence=0.7, acceptance={"positives": [b[off:off + 16].hex()], "negatives": ["00" * 16]})
    assert k.validate(sig["knowledge_id"])["state"] == "promoted"
    # second fixture: the pecli original (a different program, same toolchain family)
    pe2 = Path(__file__).resolve().parents[2] / "fixtures" / "pecli" / "original"
    if not (pe2 / "pecli.exe").exists():
        pytest.skip("pecli fixture missing")
    case = studio.create_case(name="reuse", source_root=str(pe2), output_root=str(tmp_path / "out"), target_language="rust", output_type="exe")
    from rebuild_controller.ids import sha256_file
    mid = studio.cases.add_module(case["case_id"], "pecli.exe", sha256_file(pe2 / "pecli.exe"), (pe2 / "pecli.exe").stat().st_size, "pe", "native_pe", "x86_64")
    # replace every AI adapter with one that raises on any request
    if studio.connections is not None:
        for c in studio.connections.list():
            studio.connections.set_adapter_override(c["connection_id"], RaisingProvider())
    rep = reuse_on_module(studio, case["case_id"], mid)
    assert rep["ai_calls_during"] == 0 and rep["applicable_entries"] == 1
    assert rep["matches"] and rep["matches"][0]["symbol"] == "mainCRTStartup" and rep["matches"][0]["count"] >= 1
    # not applicable to a different architecture context
    rep_x86 = studio.knowledge.applicable("signature", {"arch": "x86", "format": "pe"})
    assert rep_x86 == []
    ev = studio.cases.evidence_body(rep["evidence_id"])
    assert ev["ai_calls_during"] == 0
