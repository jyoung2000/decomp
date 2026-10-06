import time
import threading

import pytest

from rebuild_controller.jobs import JobState, StageError


def _mk_case(cases, src_out):
    src, out = src_out
    return cases.create_case(name="t", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe")["case_id"]


def test_dag_gating_and_completion(jobs, runner, registry, cases, src_out):
    cid = _mk_case(cases, src_out)
    order = []
    registry.add("a", lambda ctx: order.append("a") or {"v": 1})
    registry.add("b", lambda ctx: order.append("b") or {"v": 2})
    a = jobs.create(cid, "a", "A", {})
    b = jobs.create(cid, "b", "B", {}, depends_on=[a.job_id])
    assert jobs.get(b.job_id).state == JobState.BLOCKED
    assert runner.run_pending() == 2
    assert order == ["a", "b"]
    assert jobs.get(a.job_id).state == JobState.COMPLETED
    assert jobs.get(b.job_id).result == {"v": 2}


def test_failure_blocks_dependents_and_retries(jobs, runner, registry, cases, src_out):
    cid = _mk_case(cases, src_out)
    calls = {"n": 0}

    def flaky(ctx):
        calls["n"] += 1
        raise StageError("nope", retry=True)
    registry.add("flaky", flaky)
    registry.add("after", lambda ctx: {})
    a = jobs.create(cid, "flaky", "F", {}, max_attempts=2)
    b = jobs.create(cid, "after", "After", {}, depends_on=[a.job_id])
    runner.run_pending()
    assert calls["n"] == 2
    assert jobs.get(a.job_id).state == JobState.FAILED
    assert jobs.get(b.job_id).state == JobState.BLOCKED
    assert "dependency" in jobs.get(b.job_id).blocker
    # resume re-queues the failed job
    jobs.resume(a.job_id)
    assert jobs.get(a.job_id).state == JobState.QUEUED


def test_blocker_is_visible_not_retried(jobs, runner, registry, cases, src_out):
    cid = _mk_case(cases, src_out)
    registry.add("needs_tool", lambda ctx: (_ for _ in ()).throw(StageError("rizin missing", blocker="install rizin")))
    j = jobs.create(cid, "needs_tool", "N", {})
    runner.run_pending()
    j = jobs.get(j.job_id)
    assert j.state == JobState.BLOCKED and j.blocker == "install rizin" and j.attempt == 1


def test_unknown_stage_is_blocked(jobs, runner, cases, src_out):
    cid = _mk_case(cases, src_out)
    j = jobs.create(cid, "ghost", "G", {})
    runner.run_pending()
    assert jobs.get(j.job_id).state == JobState.BLOCKED


def test_cancel_kills_process_tree(jobs, runner, registry, cases, src_out, events):
    cid = _mk_case(cases, src_out)
    started = threading.Event()

    def long(ctx):
        started.set()
        ctx.run(["sh", "-c", "sleep 30 & wait"], timeout=60)
        return {}
    registry.add("long", long)
    j = jobs.create(cid, "long", "L", {})
    t = threading.Thread(target=runner.run_pending); t.start()
    assert started.wait(5)
    time.sleep(0.3)
    jobs.cancel(j.job_id)
    t.join(15)
    assert not t.is_alive()
    assert jobs.get(j.job_id).state == JobState.CANCELLED
    kinds = [e["kind"] for e in events.events_since(0)]
    assert "job.cancel_requested" in kinds and "job.cancelled" in kinds


def test_timeout_is_visible_failure(jobs, runner, registry, cases, src_out):
    cid = _mk_case(cases, src_out)
    registry.add("slow", lambda ctx: ctx.run(["sleep", "5"], timeout=1) and {})
    j = jobs.create(cid, "slow", "S", {}, max_attempts=1)
    t0 = time.time(); runner.run_pending()
    assert time.time() - t0 < 5
    j = jobs.get(j.job_id)
    assert j.state == JobState.FAILED and "timeout" in j.error


def test_output_cap_marks_truncated(jobs, runner, registry, cases, src_out, settings):
    cid = _mk_case(cases, src_out)
    settings.limits.max_subprocess_output_bytes = 1000
    res = {}
    registry.add("big", lambda ctx: res.update(r=ctx.run(["sh", "-c", "head -c 100000 /dev/zero"])) or {})
    jobs.create(cid, "big", "B", {})
    runner.run_pending()
    assert res["r"].truncated and len(res["r"].stdout) <= 1000


def test_lease_recovery_after_crash(jobs, runner, registry, cases, src_out):
    cid = _mk_case(cases, src_out)
    registry.add("x", lambda ctx: {})
    j = jobs.create(cid, "x", "X", {})
    claimed = jobs.claim_next("dead-worker")
    assert claimed.state == JobState.RUNNING
    assert jobs.recover_stale(now=time.time() + 100) == [j.job_id]
    assert jobs.get(j.job_id).state == JobState.QUEUED
    runner.run_pending()
    assert jobs.get(j.job_id).state == JobState.COMPLETED
    assert jobs.get(j.job_id).attempt == 2


def test_heartbeat_from_other_worker_rejected(jobs, cases, src_out, registry):
    cid = _mk_case(cases, src_out)
    j = jobs.create(cid, "x", "X", {})
    jobs.claim_next("w1")
    assert jobs.heartbeat(j.job_id, "w2") is False
    assert jobs.heartbeat(j.job_id, "w1") is True


def test_needs_retest_cascades(jobs, runner, registry, cases, src_out):
    cid = _mk_case(cases, src_out)
    registry.add("x", lambda ctx: {})
    a = jobs.create(cid, "x", "A", {}); b = jobs.create(cid, "x", "B", {}, depends_on=[a.job_id])
    runner.run_pending()
    jobs.mark_needs_retest(a.job_id, "input changed")
    assert jobs.get(a.job_id).state == JobState.NEEDS_RETEST and jobs.get(b.job_id).state == JobState.NEEDS_RETEST


def test_queue_bound(jobs, cases, src_out):
    cid = _mk_case(cases, src_out)
    jobs.max_queue = 2
    jobs.create(cid, "x", "1", {}); jobs.create(cid, "x", "2", {})
    with pytest.raises(RuntimeError):
        jobs.create(cid, "x", "3", {})


def test_dynamic_dependency_and_blocked_cascade(jobs, runner, registry, cases, src_out):
    cid = _mk_case(cases, src_out)
    registry.add("x", lambda ctx: {})
    registry.add("needs_tool", lambda ctx: (_ for _ in ()).throw(StageError("tool missing", blocker="install tool")))
    a = jobs.create(cid, "x", "A", {})
    fanin = jobs.create(cid, "x", "fan-in", {}, depends_on=[a.job_id])
    late = jobs.create(cid, "needs_tool", "late", {})
    jobs.add_dependency(fanin.job_id, late.job_id)
    runner.run_pending()
    assert jobs.get(late.job_id).state == JobState.BLOCKED
    f = jobs.get(fanin.job_id)
    assert f.state == JobState.BLOCKED and "install tool" in f.blocker
    registry.add("needs_tool", lambda ctx: {})
    jobs.resume(late.job_id)
    runner.run_pending()
    assert jobs.get(fanin.job_id).state == JobState.COMPLETED
