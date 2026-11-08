from fastapi.testclient import TestClient

from api.main import create_app
from tests.conftest import BIG_INVOICE, SMALL_INVOICE, drive


def client(settings) -> TestClient:
    return TestClient(create_app(settings))


def test_start_watch_and_inspect_a_run(pool, settings, worker):
    with client(settings) as c:
        assert [w["name"] for w in c.get("/api/workflows").json()] == ["chaos", "invoice_approval", "sdlc"]
        run_id = c.post("/api/runs", json={"workflow": "invoice_approval",
                                           "input": {"invoice_text": SMALL_INVOICE}}).json()["id"]
        drive(worker, pool, run_id)
        run = c.get(f"/api/runs/{run_id}").json()
        assert run["status"] == "done" and run["workflow"] == "invoice_approval"
        assert run["steps"]["post"]["status"] == "done"
        assert [s["key"] for s in run["graph"]["steps"]][0] == "extract"
        assert c.get("/api/runs").json()[0]["id"] == run_id

        with c.stream("GET", f"/api/runs/{run_id}/stream") as resp:
            body = "".join(resp.iter_text())
        assert "run_started" in body and "run_done" in body and "event: end" in body


def test_approve_through_the_inbox(pool, settings, worker):
    with client(settings) as c:
        run_id = c.post("/api/runs", json={"workflow": "invoice_approval",
                                           "input": {"invoice_text": BIG_INVOICE}}).json()["id"]
        drive(worker, pool, run_id)
        inbox = c.get("/api/approvals").json()
        assert len(inbox) == 1 and inbox[0]["run_id"] == run_id
        r = c.post(f"/api/approvals/{inbox[0]['id']}", json={"decision": "approved", "by": "arshdeep"})
        assert r.status_code == 200
        assert c.post(f"/api/approvals/{inbox[0]['id']}", json={"decision": "approved"}).status_code == 409
        drive(worker, pool, run_id)
        assert c.get(f"/api/runs/{run_id}").json()["status"] == "done"
        assert c.get("/api/approvals").json() == []


def test_cancel_and_replay(pool, settings, worker):
    with client(settings) as c:
        run_id = c.post("/api/runs", json={"workflow": "invoice_approval",
                                           "input": {"invoice_text": SMALL_INVOICE}}).json()["id"]
        drive(worker, pool, run_id)
        new = c.post(f"/api/runs/{run_id}/replay", json={"from_step": "summary"}).json()["id"]
        drive(worker, pool, new)
        replayed = c.get(f"/api/runs/{new}").json()
        assert replayed["replay_of"] == run_id and replayed["steps"]["extract"]["reused"] is True
        assert c.post(f"/api/runs/{run_id}/replay", json={"from_step": "nope"}).status_code == 400

        other = c.post("/api/runs", json={"workflow": "invoice_approval",
                                          "input": {"invoice_text": SMALL_INVOICE}}).json()["id"]
        assert c.post(f"/api/runs/{other}/cancel").json()["status"] == "cancelled"


def test_bad_requests_are_rejected(pool, settings):
    with client(settings) as c:
        assert c.post("/api/runs", json={"workflow": "nope"}).status_code == 404
        assert c.post("/api/runs", json={"workflow": "t_flaky"}).status_code == 404  # test workflows hidden
        assert c.post("/api/runs", json={"workflow": "chaos", "input": {"x": "a" * 30_000}}).status_code == 413
        assert c.post("/api/workers/w1/kill").status_code == 400  # no supervisor in tests
        assert c.get("/health").json()["status"] == "ok"
