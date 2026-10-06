import pytest
from rebuild_controller.paths import PathPolicyError


def test_case_create_and_evidence(cases, src_out):
    src, out = src_out
    c = cases.create_case(name="t", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe")
    e1 = cases.add_evidence(c["case_id"], "inventory", "Inventory", body={"files": 1}, inputs={"root": "x"})
    e2 = cases.add_evidence(c["case_id"], "inventory", "Inventory", body={"files": 1}, inputs={"root": "x"})
    assert e1["evidence_id"] == e2["evidence_id"]  # dedup
    e3 = cases.add_evidence(c["case_id"], "inventory", "Inventory", body={"files": 2}, inputs={"root": "x"})
    assert e3["revision"] == 2
    assert cases.evidence_body(e3["evidence_id"]) == {"files": 2}
    assert cases.search_evidence(c["case_id"], "files")[0]["kind"] == "inventory"
    assert cases.invalidate_evidence(c["case_id"], kinds=["inventory"]) == 2
    assert cases.list_evidence(c["case_id"]) == []
    ok, why = cases.is_resumable(c["case_id"])
    assert ok, why


def test_case_rejects_overlap(cases, src_out):
    src, out = src_out
    with pytest.raises(PathPolicyError):
        cases.create_case(name="t", source_root=str(src), output_root=str(src / "out"), target_language="rust", output_type="exe")


def test_evidence_body_truncation(cases, src_out):
    src, out = src_out
    c = cases.create_case(name="t", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe")
    e = cases.add_evidence(c["case_id"], "text", "big", body_bytes=b"x" * 10000)
    b = cases.evidence_body(e["evidence_id"], max_bytes=100)
    assert b["truncated"] and b["total_bytes"] == 10000 and len(b["text"]) == 100
