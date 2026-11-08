"""The settings the AWS deployment turns on (infra/lambda.tf), tested without AWS."""

import time

from fastapi.testclient import TestClient

from api.main import create_app
from engine import lambda_worker, runs
from tests.conftest import SMALL_INVOICE, drive, run_status


class FakeContext:
    log_stream_name = "2026/11/08/[$LATEST]0123456789abcdef"

    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def get_remaining_time_in_millis(self) -> int:
        return int((self.end - time.monotonic()) * 1000)


def test_public_limits_allowed_workflows_and_daily_runs(pool, settings):
    s = settings.model_copy(update={"allowed_workflows": "invoice_approval", "max_runs_per_day": 2})
    with TestClient(create_app(s)) as c:
        assert [w["name"] for w in c.get("/api/workflows").json()] == ["invoice_approval"]
        assert c.post("/api/runs", json={"workflow": "chaos", "input": {}}).status_code == 404
        body = {"workflow": "invoice_approval", "input": {"invoice_text": SMALL_INVOICE}}
        assert c.post("/api/runs", json=body).status_code == 201
        assert c.post("/api/runs", json=body).status_code == 201
        r = c.post("/api/runs", json=body)
        assert r.status_code == 429 and "2 tasks a day" in r.json()["detail"]


def test_new_work_kicks_the_worker_function(pool, settings, worker, monkeypatch):
    kicks = []
    monkeypatch.setattr("engine.aws.invoke_async", lambda fn, payload: kicks.append((fn, payload["source"])))
    s = settings.model_copy(update={"worker_kick": "lambda", "worker_function": "agentops-worker"})
    with TestClient(create_app(s)) as c:
        run_id = c.post("/api/runs", json={"workflow": "invoice_approval",
                                           "input": {"invoice_text": SMALL_INVOICE}}).json()["id"]
        assert kicks == [("agentops-worker", "run_started")]
        drive(worker, pool, run_id)
        c.post(f"/api/runs/{run_id}/replay", json={"from_step": "extract"})
        assert kicks[-1] == ("agentops-worker", "replay")


def test_stream_events_carry_ids_and_resume_from_last_event_id(pool, settings, worker):
    with TestClient(create_app(settings)) as c:
        run_id = c.post("/api/runs", json={"workflow": "invoice_approval",
                                           "input": {"invoice_text": SMALL_INVOICE}}).json()["id"]
        drive(worker, pool, run_id)
        with c.stream("GET", f"/api/runs/{run_id}/stream") as r:
            full = "".join(r.iter_text())
        ids = [int(line[4:]) for line in full.splitlines() if line.startswith("id: ")]
        assert ids[0] == 1 and "run_started" in full
        with c.stream("GET", f"/api/runs/{run_id}/stream", headers={"Last-Event-ID": "3"}) as r:
            resumed = "".join(r.iter_text())
        assert "run_started" not in resumed
        assert [int(x[4:]) for x in resumed.splitlines() if x.startswith("id: ")][0] == 4


def test_next_work_in_sees_queued_and_waiting_runs(pool, settings, worker):
    assert runs.next_work_in(pool) is None
    run_id = runs.create_run(pool, "invoice_approval", {"invoice_text": SMALL_INVOICE})
    assert runs.next_work_in(pool) <= 0  # the first step is ready now
    assert runs.next_work_in(pool, run_id) <= 0
    drive(worker, pool, run_id)
    assert runs.next_work_in(pool) is None


def test_lambda_worker_drains_the_queue_then_exits(pool, settings, monkeypatch):
    monkeypatch.setattr(lambda_worker, "_worker", None)
    monkeypatch.setattr(lambda_worker, "get_settings", lambda: settings)
    monkeypatch.setattr(lambda_worker, "RESERVE_S", 1.0)
    run_id = runs.create_run(pool, "invoice_approval", {"invoice_text": SMALL_INVOICE})
    out = lambda_worker.handler({"source": "test"}, FakeContext(60))
    assert out["stopped"] == "idle" and out["steps"] >= 3
    assert out["worker"] == "lambda-6789abcdef"  # from the log stream: one id per warm copy
    assert run_status(pool, run_id) == "done"
    lambda_worker._worker.pool.close()


def test_lambda_worker_hands_over_when_time_runs_out(pool, settings, monkeypatch):
    handed = []
    s = settings.model_copy(update={"worker_function": "agentops-worker"})
    monkeypatch.setattr(lambda_worker, "_worker", None)
    monkeypatch.setattr(lambda_worker, "get_settings", lambda: s)
    monkeypatch.setattr("engine.aws.invoke_async", lambda fn, payload: handed.append(payload["source"]))
    runs.create_run(pool, "invoice_approval", {"invoice_text": SMALL_INVOICE})
    out = lambda_worker.handler({}, FakeContext(5))  # less time left than RESERVE_S
    assert out == {"worker": out["worker"], "steps": 0, "stopped": "handed over"}
    assert handed == ["handover"]
    lambda_worker._worker.pool.close()
