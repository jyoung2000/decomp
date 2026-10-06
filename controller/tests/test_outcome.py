"""Outcome derivation: pipeline progress must never be presented as measured behavioural parity."""
import pytest

from rebuild_controller.outcome import STATES, case_outcome, derive_outcome


def facts(**kw):
    base = {"scenarios_declared": 0, "scenario_verdicts": [], "stale": False, "features": [], "unsupported_modules": [], "unknown_scope": False,
            "scaffold_only": False, "built": False, "delivered": False, "recovered": False, "pipeline_done": 0, "pipeline_total": 10}
    base.update(kw)
    return base


def test_delivered_with_zero_verified_is_not_verified():
    o = derive_outcome(facts(scenarios_declared=8, built=True, delivered=True, recovered=True, pipeline_done=10))
    assert o["state"] == "delivered" and o["label"] == "Delivered — not verified"
    assert o["can_claim_complete"] is False
    assert o["verification"]["passed"] == 0 and o["verification"]["untested"] == 8 and o["verification"]["state"] == "untested"
    assert o["pipeline"]["text"] == "Pipeline: 10/10 steps" and o["verification"]["text"] == "Behavior verified: 0 of 8 scenarios"


def test_no_baseline_means_untested_even_when_built():
    o = derive_outcome(facts(built=True))
    assert o["state"] == "built" and o["verification"]["state"] == "untested" and o["verification"]["declared"] == 0
    assert any("No scenarios are declared" in x for x in o["outstanding"])
    assert "no scenarios declared" in o["verification"]["text"]


def test_nothing_done_is_untested():
    assert derive_outcome(facts())["state"] == "untested"


def test_empty_scenario_list_never_fully_matched():
    o = derive_outcome(facts(scenarios_declared=0, scenario_verdicts=[], built=True, delivered=True))
    assert o["state"] != "fully_matched" and o["verification"]["verdict"] == "untested"


def test_wrong_remake_is_tested_failed():
    o = derive_outcome(facts(scenarios_declared=8, scenario_verdicts=["fail"] * 8, built=True, delivered=True))
    assert o["state"] == "tested" and o["verification"]["verdict"] == "failed"
    assert o["verification"]["failed"] == 8 and o["verification"]["passed"] == 0 and not o["can_claim_complete"]


def test_errors_count_as_failed():
    o = derive_outcome(facts(scenarios_declared=2, scenario_verdicts=["error", "pass"]))
    assert o["verification"]["failed"] == 1 and o["state"] == "partially_matched"


def test_partial_pass():
    o = derive_outcome(facts(scenarios_declared=9, scenario_verdicts=["pass"] * 5 + ["fail"] * 2, built=True))
    v = o["verification"]
    assert o["state"] == "partially_matched" and (v["passed"], v["failed"], v["untested"]) == (5, 2, 2)
    assert not o["can_claim_complete"]


def test_all_pass_but_some_not_run_is_partial():
    o = derive_outcome(facts(scenarios_declared=4, scenario_verdicts=["pass", "pass"]))
    assert o["state"] == "partially_matched" and o["verification"]["untested"] == 2


def test_fully_matched_only_within_declared_coverage():
    o = derive_outcome(facts(scenarios_declared=8, scenario_verdicts=["pass"] * 8, built=True, delivered=True,
                             features=[{"id": "a", "title": "A", "has_scenario": True}, {"id": "b", "title": "Windows apphost", "has_scenario": False}],
                             unsupported_modules=["Installer"], unknown_scope=True))
    assert o["state"] == "fully_matched" and o["can_claim_complete"]
    assert o["scope_statement"].startswith("Scenario results cover only the 8 declared scenarios")
    c = o["coverage"]
    assert c["features_without_oracle"] == 1 and c["features_without_oracle_titles"] == ["Windows apphost"]
    assert c["unsupported_modules"] == 1 and c["unknown_scope"] is True
    # outstanding work is still listed next to a full match
    assert len(o["outstanding"]) == 3


def test_scaffold_only_is_never_matched():
    o = derive_outcome(facts(scaffold_only=True, scenarios_declared=9, scenario_verdicts=["fail"] * 9, built=True, delivered=True))
    assert o["state"] == "scaffolded" and o["scaffold_only"] and o["pipeline"]["scaffolded"]
    assert o["pipeline"]["built"] and o["pipeline"]["delivered"] and not o["can_claim_complete"]
    # contradictory evidence (a scaffold that "passes everything") is downgraded, never rounded up
    o2 = derive_outcome(facts(scaffold_only=True, scenarios_declared=2, scenario_verdicts=["pass", "pass"]))
    assert o2["state"] == "scaffolded" and o2["verification"]["state"] != "fully_matched" and not o2["can_claim_complete"]


def test_stale_results_do_not_count():
    o = derive_outcome(facts(scenarios_declared=3, scenario_verdicts=["pass"] * 3, stale=True, built=True))
    assert o["state"] == "built" and o["verification"]["passed"] == 0 and o["verification"]["untested"] == 3 and o["verification"]["stale"]
    assert any("out of date" in x for x in o["outstanding"])


def test_report_cannot_exceed_declared():
    o = derive_outcome(facts(scenarios_declared=2, scenario_verdicts=["pass"] * 3))
    assert o["verification"]["declared"] == 3 and o["verification"]["untested"] == 0


def test_state_always_known_and_only_fully_matched_claims_complete():
    for d in (0, 3):
        for verdicts in ([], ["pass"] * d, ["fail"] * d, ["pass", "fail", "error"][:d]):
            for flags in ({}, {"built": True}, {"delivered": True, "built": True}, {"recovered": True}, {"scaffold_only": True}):
                o = derive_outcome(facts(scenarios_declared=d, scenario_verdicts=verdicts, **flags))
                assert o["state"] in STATES
                assert o["can_claim_complete"] == (o["state"] == "fully_matched")
                if o["state"] == "fully_matched":
                    assert o["verification"]["passed"] == o["verification"]["declared"] > 0


# -- collected from a real (in-memory) studio ----------------------------------------------------------------------------

@pytest.fixture
def studio(settings):
    from rebuild_controller.services import StudioServices
    s = StudioServices(settings)
    yield s
    s.stop()


def _case(studio, tmp_path):
    src = tmp_path / "src"; src.mkdir()
    out = tmp_path / "out"
    c = studio.create_case(name="o", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe")
    studio.plan.initialize(c["case_id"], c)
    return c["case_id"]


def test_collected_outcome_scaffold_with_no_baseline(studio, tmp_path):
    cid = _case(studio, tmp_path)
    assert case_outcome(studio, cid)["state"] == "untested"
    cand = studio.candidates.create(cid, target_language="rust", output_type="exe", plan_revision=1, meta={"origin": "scaffold"})
    dist = tmp_path / "dist"; dist.mkdir(); (dist / "a").write_text("x")
    studio.candidates.mark_built(cand["candidate_id"], dist)
    studio.cases.set_case_status(cid, "delivered")
    o = case_outcome(studio, cid)
    assert o["state"] == "scaffolded" and o["pipeline"]["built"] and o["pipeline"]["delivered"] and o["verification"]["declared"] == 0


def test_collected_outcome_partial_and_features_without_oracle(studio, tmp_path):
    cid = _case(studio, tmp_path)
    studio.ledger.add(cid, "A", feature_id="f.a")
    studio.ledger.add(cid, "B (no scenario)", feature_id="f.b")
    baseline = {"kind": "cli", "launch": {}, "scenarios": [{"id": "s1", "feature_id": "f.a"}, {"id": "s2", "feature_id": "f.a"}, {"id": "s3", "feature_id": "f.a"}]}
    studio.verifier.freeze_baseline(cid, baseline, producer="fixture_oracle")
    cand = studio.candidates.create(cid, target_language="rust", output_type="exe", plan_revision=1, meta={"author": "model", "proposed_files": ["src/main.rs"]})
    dist = tmp_path / "dist"; dist.mkdir(); (dist / "a").write_text("x")
    cand = studio.candidates.mark_built(cand["candidate_id"], dist)
    report = {"candidate_id": cand["candidate_id"], "build_hash": cand["build_hash"],
              "scenarios": [{"scenario": "s1", "verdict": "pass"}, {"scenario": "s2", "verdict": "fail"}]}   # s3 never ran
    studio.cases.add_evidence(cid, "verification_report", "r", body=report, inputs={"c": cand["candidate_id"]}, producer="verifier")
    o = case_outcome(studio, cid)
    v = o["verification"]
    assert o["state"] == "partially_matched" and (v["declared"], v["passed"], v["failed"], v["untested"]) == (3, 1, 1, 1)
    assert o["coverage"]["features_without_oracle_titles"] == ["B (no scenario)"]
    assert not o["scaffold_only"]
    # a report for an older build of the candidate is ignored
    studio.cases.add_evidence(cid, "verification_report", "r2", body={**report, "build_hash": "other", "scenarios": [{"scenario": "s1", "verdict": "pass"}] * 3},
                              inputs={"c": "x"}, producer="verifier")
    assert case_outcome(studio, cid)["verification"]["passed"] == 1
    # invalidation makes results stale and uncounted
    studio.verifier.invalidate(cid, "inputs changed", candidate_id=cand["candidate_id"])
    o = case_outcome(studio, cid)
    assert o["verification"]["stale"] and o["verification"]["passed"] == 0 and o["state"] == "built"
