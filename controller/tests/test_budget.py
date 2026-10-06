import threading

import pytest

from rebuild_controller.budget import (BudgetError, BudgetExhausted, BudgetLedger, BudgetNotFound, DuplicateReservation,
                                       ReservationStateError)


@pytest.fixture
def ledger(db, events):
    return BudgetLedger(db, events)


def test_create_get_and_remaining(ledger):
    b = ledger.create("job:j1", "job:j1", 1.50)
    assert b["limit_usd"] == 1.5 and b["remaining_usd"] == 1.5 and b["spent_usd"] == 0 and not b["exhausted"]
    assert ledger.get("job:j1")["scope"] == "job:j1"
    with pytest.raises(BudgetError):
        ledger.create("job:j1", "job:j1", 2)
    assert ledger.ensure("job:j1", "job:j1", 99)["limit_usd"] == 1.5  # ensure never changes an existing limit
    with pytest.raises(BudgetNotFound):
        ledger.get("nope")
    with pytest.raises(ValueError):
        ledger.create("bad", "x", -1)
    with pytest.raises(ValueError):
        ledger.create("bad", "x", float("nan"))


def test_reserve_holds_money_and_settle_converts_to_spend(ledger):
    ledger.create("b", "case:c1", 1.0)
    r = ledger.reserve("b", 0.40, "k1")
    assert r["state"] == "held"
    b = ledger.get("b")
    assert b["reserved_usd"] == pytest.approx(0.40) and b["remaining_usd"] == pytest.approx(0.60)
    s = ledger.settle(r["reservation_id"], 0.25, {"input_tokens": 100, "output_tokens": 10})
    assert s["state"] == "settled" and s["actual_usd"] == pytest.approx(0.25) and s["usage"]["input_tokens"] == 100
    b = ledger.get("b")
    assert b["reserved_usd"] == pytest.approx(0) and b["spent_usd"] == pytest.approx(0.25) and b["remaining_usd"] == pytest.approx(0.75)


def test_duplicate_reservation_rejected_even_after_settle_or_release(ledger):
    ledger.create("b", "case:c1", 1.0)
    r = ledger.reserve("b", 0.1, "same-key")
    with pytest.raises(DuplicateReservation):
        ledger.reserve("b", 0.1, "same-key")
    ledger.settle(r["reservation_id"], 0.05)
    with pytest.raises(DuplicateReservation):  # a settled request must never be paid for twice
        ledger.reserve("b", 0.1, "same-key")
    assert ledger.get("b")["reserved_usd"] == pytest.approx(0)
    # the unique constraint (not just the pre-check) is the guard: even another budget cannot reuse the key
    ledger.create("b2", "case:c2", 1.0)
    with pytest.raises(DuplicateReservation):
        ledger.reserve("b2", 0.1, "same-key")


def test_exhausted_budget_raises_and_changes_nothing(ledger):
    ledger.create("b", "job:j", 0.10)
    ledger.reserve("b", 0.08, "a")
    with pytest.raises(BudgetExhausted) as ei:
        ledger.reserve("b", 0.05, "b")  # reservations count against the limit, so concurrent calls cannot overspend
    assert ei.value.available == pytest.approx(0.02) and ei.value.budget_id == "b"
    assert ledger.get("b")["reserved_usd"] == pytest.approx(0.08)
    assert len(ledger.reservations("b")) == 1  # nothing was written for the rejected attempt
    ledger.reserve("b", 0.02, "c")  # exact fit is allowed
    assert ledger.get("b")["exhausted"]


def test_release_returns_money_and_blocks_double_close(ledger):
    ledger.create("b", "job:j", 0.10)
    r = ledger.reserve("b", 0.10, "a")
    rel = ledger.release(r["reservation_id"])
    assert rel["state"] == "released" and rel["actual_usd"] == 0
    assert ledger.get("b")["remaining_usd"] == pytest.approx(0.10)
    with pytest.raises(ReservationStateError):
        ledger.settle(r["reservation_id"], 0.01)
    with pytest.raises(ReservationStateError):
        ledger.release(r["reservation_id"])
    ledger.reserve("b", 0.10, "b")  # freed money is reusable


def test_overrun_is_recorded_not_clamped(ledger):
    ledger.create("b", "job:j", 0.10)
    r = ledger.reserve("b", 0.05, "a")
    ledger.settle(r["reservation_id"], 0.08)
    b = ledger.get("b")
    assert b["spent_usd"] == pytest.approx(0.08)
    with pytest.raises(BudgetExhausted):
        ledger.reserve("b", 0.05, "b")  # only 0.02 left
    r2 = ledger.reserve("b", 0.02, "c")
    ledger.settle(r2["reservation_id"], 0.5)  # a wrong ceiling pushes spend past the limit; reads as exhausted, remaining < 0
    b = ledger.get("b")
    assert b["exhausted"] and b["remaining_usd"] < 0
    with pytest.raises(BudgetExhausted):
        ledger.reserve("b", 0.0001, "d")


def test_amount_validation(ledger):
    ledger.create("b", "job:j", 1)
    for bad in (-0.1, float("inf"), float("nan"), True, "1"):
        with pytest.raises(ValueError):
            ledger.reserve("b", bad, "k")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ledger.reserve("b", 0.1, "")
    with pytest.raises(BudgetNotFound):
        ledger.reserve("missing", 0.1, "k")
    assert ledger.reserve("b", 0, "zero")["amount_usd"] == 0  # zero-cost (local) reservations are allowed


def test_budget_updated_events_carry_state(ledger, events):
    ledger.create("case:c1", "case:c1", 1.0)
    r = ledger.reserve("case:c1", 0.3, "k")
    ledger.settle(r["reservation_id"], 0.2)
    evs = [e for e in events.events_since(0) if e["kind"] == "budget.updated"]
    assert [e["payload"]["reason"] for e in evs] == ["create", "reserve", "settle"]
    last = evs[-1]
    assert last["case_id"] == "c1" and last["payload"]["spent_usd"] == pytest.approx(0.2) and last["payload"]["remaining_usd"] == pytest.approx(0.8)
    assert last["payload"]["reservation_id"] == r["reservation_id"]


def test_set_limit_and_list_prefix(ledger):
    ledger.create("jev:setup", "jev:setup", 0.05)
    ledger.create("jev:monthly:2026-10", "jev:monthly:2026-10", 1.0)
    ledger.create("job:1", "job:1", 5)
    assert [b["budget_id"] for b in ledger.list("jev:")] == ["jev:monthly:2026-10", "jev:setup"]
    assert ledger.set_limit("job:1", 7)["limit_usd"] == 7
    assert len(ledger.list()) == 3


def test_stale_reservations_are_assumed_spent(ledger):
    ledger.create("b", "job:j", 1.0)
    r = ledger.reserve("b", 0.3, "k")
    assert ledger.sweep_stale(3600) == []  # fresh
    import time
    assert ledger.sweep_stale(60, now=time.time() + 3600) == [r["reservation_id"]]
    b = ledger.get("b")
    assert b["spent_usd"] == pytest.approx(0.3) and b["reserved_usd"] == pytest.approx(0)
    assert ledger.get_reservation(r["reservation_id"])["usage"]["reason"] == "stale_swept_assumed_spent"


def test_concurrent_reservations_never_overspend(ledger):
    ledger.create("b", "job:j", 1.0)
    results: list[object] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            ledger.reserve("b", 0.1, f"k{i}")
            out: object = "ok"
        except BudgetExhausted:
            out = "exhausted"
        with lock:
            results.append(out)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(25)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("ok") == 10 and results.count("exhausted") == 15
    assert ledger.get("b")["reserved_usd"] == pytest.approx(1.0)


def test_subscription_quota_is_separate_and_unknown_by_default(ledger):
    q = ledger.get_quota("conn_x")
    assert q["quota_known"] is False and q["usd_tracked"] is False and q["limit"] is None
    with pytest.raises(ValueError):
        ledger.set_quota("conn_x", quota_known=True)  # a known quota needs a unit and limit
    ledger.set_quota("conn_x", quota_known=False, source="vendor publishes no machine-readable quota")
    ledger.set_quota("conn_y", quota_known=True, unit="messages/5h", limit=45, used=3, source="user entered")
    snap = ledger.snapshot()
    assert {q["connection_id"]: q["quota_known"] for q in snap["quotas"]} == {"conn_x": False, "conn_y": True}
    assert snap["budgets"] == []  # quotas never appear as dollar budgets
