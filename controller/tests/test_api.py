import json
import threading

import pytest
from fastapi.testclient import TestClient

from rebuild_controller.api.server import create_app
from rebuild_controller.services import StudioServices

TOKEN = "t0k3n"


@pytest.fixture
def client(settings):
    st = StudioServices(settings)
    app = create_app(st, TOKEN)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}", "Origin": "http://localhost:5173"})
        yield c, st
    st.stop()


def test_auth_and_origin(client, src_out):
    c, st = client
    assert c.get("/health").json()["ok"]
    r = c.get("/cases", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "auth"
    r = c.get("/cases", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


@pytest.mark.parametrize("origin", ["http://tauri.localhost", "https://tauri.localhost", "tauri://localhost", "http://localhost:5173"])
def test_packaged_ui_origin_gets_cors(client, origin):
    """The installed Windows app calls the controller from http://tauri.localhost (cross-origin). Regression: the
    preflight used to hit the token check (401, no CORS headers) so the packaged UI showed 'Disconnected'."""
    c, st = client
    pre = c.options("/cases", headers={"Origin": origin, "Access-Control-Request-Method": "GET",
                                       "Access-Control-Request-Headers": "authorization", "Authorization": ""})
    assert pre.status_code == 204
    assert pre.headers["access-control-allow-origin"] == origin
    assert "authorization" in pre.headers["access-control-allow-headers"]
    r = c.get("/cases", headers={"Origin": origin})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == origin
    r = c.get("/cases", headers={"Origin": origin, "Authorization": "Bearer wrong"})
    assert r.status_code == 401 and r.headers["access-control-allow-origin"] == origin  # UI can read the error


@pytest.mark.parametrize("origin", ["http://localhost.evil.example", "http://127.0.0.1.evil.example:80", "https://evil.example", "null"])
def test_lookalike_origins_are_refused(client, origin):
    c, st = client
    r = c.options("/cases", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
    assert r.status_code == 403 and "access-control-allow-origin" not in r.headers
    assert c.get("/cases", headers={"Origin": origin}).status_code == 403


def test_case_lifecycle_plan_feedback_persist(client, src_out, settings):
    c, st = client
    src, out = src_out
    r = c.post("/cases", json={"name": "x", "source_root": str(src), "output_root": str(out), "target_language": "web", "output_type": "web"})
    assert r.status_code == 200, r.text
    cid = r.json()["case_id"]
    bad = c.post("/cases", json={"name": "x", "source_root": str(src), "output_root": str(src / "o"), "target_language": "web", "output_type": "web"})
    assert bad.status_code == 400 and bad.json()["error"]["next_action"]
    plan = c.get(f"/cases/{cid}/plan").json()
    assert plan["revision"] == 1 and any(i["kind"] == "discovery" for i in plan["items"])
    assert plan["progress"]["groups"]["discovery"]["total"] is None  # unknown denominator before scheduling
    assert plan["outcome"]["state"] == "untested" and plan["outcome"]["verification"]["declared"] == 0 and not plan["outcome"]["can_claim_complete"]
    assert c.get(f"/cases/{cid}").json()["outcome"]["state"] == "untested"
    fb = c.post(f"/cases/{cid}/feedback", json={"target_kind": "milestone", "target_id": plan["items"][0]["item_id"], "classification": "bug", "priority": "high",
                                                "comment": "button broken sk-abcdefghijklmnop1234", "attachments": [{"name": "../../x.png", "bytes_b64": "aGVsbG8="}]}).json()
    assert fb["status"] == "received" and "[redacted]" in fb["comment"] and fb["attachments"][0]["name"] == "x.png" and fb["plan_revision"] == 1
    tr = c.post(f"/feedback/{fb['feedback_id']}/triage", json={"create_work": True}).json()
    assert tr["status"] == "queued" and tr["linked_items"]
    assert c.get(f"/cases/{cid}/plan").json()["revision"] == 2
    # restart: new services on same data dir see the same feedback/plan
    st2 = StudioServices(settings)
    try:
        assert st2.feedback.get(fb["feedback_id"])["status"] == "queued"
        assert st2.plan.current_revision(cid) == 2
    finally:
        st2.stop()
    ev = c.get("/events", params={"since": 0}).json()
    assert [e["kind"] for e in ev][:1] == ["case.created"] and ev[-1]["seq"] > ev[0]["seq"]


def test_websocket_replay_and_live(client, src_out):
    c, st = client
    with c.websocket_connect(f"/ws?token={TOKEN}&since=0") as ws:
        src, out = src_out
        st.create_case(name="y", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe")
        got = json.loads(ws.receive_text())
        assert got["kind"] == "case.created" and got["seq"] >= 1
    with pytest.raises(Exception):
        with c.websocket_connect(f"/ws?token=bad&since=0") as ws:
            ws.receive_text()
