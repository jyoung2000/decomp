import threading
from rebuild_controller.store.db import MIGRATIONS, Database


def test_migrations_idempotent(settings):
    d1 = Database(settings.db_path); d1.close()
    d2 = Database(settings.db_path)
    assert d2.query_one("SELECT value FROM meta WHERE key='schema_version'")["value"] == str(MIGRATIONS[-1][0])
    assert d2.query_one("SELECT COUNT(*) AS n FROM ai_config_revisions")["n"] == 0      # migration 2 (AI ladder) applied once
    d2.close()


def test_transaction_rollback(db):
    try:
        with db.transaction():
            db.execute("INSERT INTO meta(key,value) VALUES('x','1')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert db.query_one("SELECT value FROM meta WHERE key='x'") is None


def test_nested_transactions_commit_once(db):
    with db.transaction():
        db.execute("INSERT INTO meta(key,value) VALUES('a','1')")
        with db.transaction():
            db.execute("INSERT INTO meta(key,value) VALUES('b','2')")
    assert len(db.query("SELECT * FROM meta WHERE key IN ('a','b')")) == 2


def test_events_sequenced_and_replayable(events):
    seen = []
    events.subscribe(seen.append)
    e1 = events.emit("t.one", {"n": 1}, case_id="c1")
    e2 = events.emit("t.two", {"n": 2}, case_id="c2")
    assert e2["seq"] == e1["seq"] + 1
    assert [e["kind"] for e in events.events_since(0)] == ["t.one", "t.two"]
    assert [e["kind"] for e in events.events_since(e1["seq"])] == ["t.two"]
    assert [e["kind"] for e in events.events_since(0, case_id="c1")] == ["t.one"]
    assert len(seen) == 2


def test_events_concurrent_emit_unique_seq(events):
    def work():
        for i in range(50):
            events.emit("t.x", {"i": i})
    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    seqs = [e["seq"] for e in events.events_since(0, limit=10000)]
    assert len(seqs) == 200 and seqs == sorted(seqs) and len(set(seqs)) == 200
