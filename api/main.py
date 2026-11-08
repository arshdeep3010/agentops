"""HTTP API + the one-page UI.

POST /api/runs                      start a run           GET /api/runs            list runs
GET  /api/runs/{id}                 run, steps, graph     GET /api/runs/{id}/stream  live events (SSE)
POST /api/runs/{id}/cancel          cancel                POST /api/runs/{id}/replay  replay from a step
GET  /api/approvals?pending=true    approvals inbox       POST /api/approvals/{id}   approve / reject
GET  /api/workers                   workers + heartbeats  POST /api/workers/{id}/kill  demo: hard-kill one
POST /api/examples/{name}/preview   open the original web example
POST /api/runs/{id}/preview         open what a run merged (after the merge approval)

The same app runs locally (uvicorn) and on AWS Lambda (behind the Lambda Web Adapter, see
infra/). On Lambda, nothing runs between requests, so every call that creates work also "kicks"
the worker function.
"""

import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

import workflows  # noqa: F401 - registers workflows, code steps, tools, schemas
from engine import aws, runs
from engine.config import Settings, get_settings
from engine.db import make_pool, migrate
from engine.supervisor import Supervisor
from engine.workflow import WORKFLOWS
from sdlc import fakes_shoplite, preview, repo
from sdlc import workflow as sdlc_workflow

REPLAY_ONLY = ("This demo can't change the task: to keep it free, the AI's replies are a recording of a real run "
               "of the coupon task. The tests, checks, sandbox, approvals and merged shop are real. To build your own "
               "feature, run it from https://github.com/arshdeep3010/agentops with LLM_MODE=live and your own key.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agentops.api")
STATIC = Path(__file__).parent / "static"
TERMINAL = set(runs.TERMINAL_RUN)


class StartRun(BaseModel):
    workflow: str
    input: dict = Field(default_factory=dict)
    budget_inr: float | None = Field(default=None, ge=0)


class Decision(BaseModel):
    decision: str = Field(pattern="^(approved|rejected|changes)$")
    by: str = Field(default="you", max_length=80)
    note: str = Field(default="", max_length=500)


class Replay(BaseModel):
    from_step: str
    overrides: dict[str, dict] = Field(default_factory=dict)


class EventHub:
    """One LISTEN connection for the whole API; wakes the SSE streams of the affected run."""

    def __init__(self, database_url: str):
        self.url = database_url
        self.waiters: dict[str, set[asyncio.Event]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self) -> None:
        while not self._stop.is_set():
            try:
                with psycopg.connect(self.url, autocommit=True) as conn:
                    conn.execute("LISTEN run_events")
                    while not self._stop.is_set():  # notifies() returns after each 1 s timeout
                        for note in conn.notifies(timeout=1.0):
                            self._wake(note.payload.split(":")[0])
            except Exception:  # noqa: BLE001 - reconnect; SSE streams also poll as a fallback
                log.warning("event listener reconnecting")
                self._stop.wait(1.0)

    def _wake(self, run_id: str) -> None:
        for ev in list(self.waiters.get(run_id, ())):
            self.loop.call_soon_threadsafe(ev.set)

    async def wait(self, run_id: str, timeout: float) -> None:
        ev = asyncio.Event()
        self.waiters.setdefault(run_id, set()).add(ev)
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self.waiters[run_id].discard(ev)

    def stop(self) -> None:
        self._stop.set()


def _sse(event: str, data, event_id: int | None = None) -> str:
    # The id lets the browser resume after a dropped stream (it sends Last-Event-ID back).
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(jsonable_encoder(data))}\n\n"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = make_pool(settings.database_url, min_size=settings.db_pool_min, max_size=10)
        migrate(pool)
        runs.sync_workflows(pool)
        hub = EventHub(settings.database_url)
        if settings.event_listen:
            hub.start(asyncio.get_running_loop())
        sup = None
        if settings.spawn_workers > 0:
            sup = Supervisor(settings.spawn_workers, env={"DATABASE_URL": settings.database_url},
                             restart_after_s=settings.restart_killed_after_s)
            sup.start()
        app.state.pool, app.state.hub, app.state.sup = pool, hub, sup
        try:
            yield
        finally:
            hub.stop()
            if sup:
                sup.stop()
            pool.close()

    app = FastAPI(title="AgentOps", version="0.1.0", lifespan=lifespan)

    def pool(request: Request):
        return request.app.state.pool

    allowed = {w.strip() for w in settings.allowed_workflows.split(",") if w.strip()}
    last_kick: dict[str, float] = {}

    def kick(reason: str) -> None:
        """Wake a worker function (AWS). Locally, workers poll the queue and need no kick."""
        if settings.worker_kick == "lambda" and settings.worker_function:
            last_kick["any"] = time.monotonic()
            aws.invoke_async(settings.worker_function, {"source": reason})

    def check_quota(request: Request) -> None:
        if settings.max_runs_per_day <= 0:
            return
        with pool(request).connection() as conn:
            n = conn.execute("SELECT count(*) AS n FROM runs WHERE started_at > now() - interval '1 day'"
                             ).fetchone()["n"]
        if n >= settings.max_runs_per_day:
            raise HTTPException(429, f"this demo runs at most {settings.max_runs_per_day} tasks a day; "
                                     "try again tomorrow, or run it locally (see the README)")

    def is_recorded_task(data: dict) -> bool:
        """Offline replay has the AI's replies for one task only; any other task would get them anyway."""
        task = " ".join(str(data.get("task", "")).split())
        return data.get("example") == "shoplite" and task == fakes_shoplite.TASK

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/workflows")
    def list_workflows() -> list[dict]:
        return [wf.graph() for name, wf in sorted(WORKFLOWS.items())
                if not name.startswith("t_") and (not allowed or name in allowed)]

    @app.post("/api/runs", status_code=201)
    def start_run(body: StartRun, request: Request) -> dict:
        if (body.workflow not in WORKFLOWS or body.workflow.startswith("t_")
                or (allowed and body.workflow not in allowed)):
            raise HTTPException(404, f"unknown workflow {body.workflow!r}")
        if len(json.dumps(body.input)) > 20_000:
            raise HTTPException(413, "input too large")
        if settings.llm_mode == "fake" and body.workflow == "sdlc" and not is_recorded_task(body.input):
            raise HTTPException(400, REPLAY_ONLY)
        check_quota(request)
        run_id = runs.create_run(pool(request), body.workflow, body.input, budget_inr=body.budget_inr)
        kick("run_started")
        return {"id": run_id}

    @app.get("/api/runs")
    def list_runs(request: Request, limit: int = 30) -> list[dict]:
        with pool(request).connection() as conn:
            return conn.execute(
                "SELECT r.id, r.status, r.started_at, r.finished_at, r.cost_inr, r.tokens_used, r.error, "
                "r.replay_of, w.name AS workflow, w.version, r.input->>'example' AS example "
                "FROM runs r JOIN workflows w ON w.id = r.workflow_id "
                "WHERE w.name NOT LIKE 't\\_%%' ORDER BY r.started_at DESC LIMIT %s", (min(limit, 100),)).fetchall()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: UUID, request: Request) -> dict:
        with pool(request).connection() as conn:
            cur = conn.cursor()
            run = cur.execute(
                "SELECT r.*, w.name AS workflow, w.version, w.graph FROM runs r "
                "JOIN workflows w ON w.id = r.workflow_id WHERE r.id = %s", (run_id,)).fetchone()
            if not run:
                raise HTTPException(404, "run not found")
            step_rows = cur.execute(
                "SELECT step_key, status, attempt, max_attempts, worker_id, started_at, finished_at, output, "
                "error, reused, lease_until, iteration, attempt_floor, feedback FROM step_runs WHERE run_id = %s",
                (run_id,)).fetchall()
            approvals = cur.execute(
                "SELECT id, step_key, request, decision, decided_by, note, requested_at, decided_at "
                "FROM approvals WHERE run_id = %s", (run_id,)).fetchall()
        return {**run, "steps": {s["step_key"]: s for s in step_rows}, "approvals": approvals}

    @app.get("/api/runs/{run_id}/artifacts")
    def list_artifacts(run_id: UUID, request: Request) -> list[dict]:
        with pool(request).connection() as conn:
            return conn.execute(
                "SELECT id, step_key, iteration, name, media_type, length(content) AS size, created_at "
                "FROM artifacts WHERE run_id = %s ORDER BY created_at", (run_id,)).fetchall()

    @app.get("/api/artifacts/{artifact_id}")
    def get_artifact(artifact_id: UUID, request: Request) -> dict:
        with pool(request).connection() as conn:
            row = conn.execute("SELECT id, run_id, step_key, iteration, name, media_type, sha256, content "
                               "FROM artifacts WHERE id = %s", (artifact_id,)).fetchone()
        if not row:
            raise HTTPException(404, "artifact not found")
        return row

    @app.get("/api/runs/{run_id}/events")
    def get_events(run_id: UUID, request: Request, after: int = 0) -> list[dict]:
        return runs.events(pool(request), run_id, after)

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: UUID, request: Request, after: int = 0) -> StreamingResponse:
        p, hub = pool(request), request.app.state.hub
        resume = request.headers.get("last-event-id", "")
        if resume.isdigit():
            after = max(after, int(resume))
        started = time.monotonic()

        async def gen():
            last = after
            while True:
                batch = await asyncio.to_thread(runs.events, p, run_id, last)
                for e in batch:
                    last = e["seq"]
                    yield _sse("event", e, last)
                if batch and batch[-1]["type"] in {f"run_{s}" for s in TERMINAL}:
                    yield _sse("end", {"seq": last}, last)
                    return
                if await request.is_disconnected():
                    return
                if settings.stream_max_s and time.monotonic() - started > settings.stream_max_s:
                    return  # Lambda has a time limit: end cleanly; the browser reconnects and resumes
                if settings.worker_kick == "lambda" and time.monotonic() - last_kick.get("any", 0) > 15:
                    # Someone is watching and work has sat unclaimed (a missed kick, or a function
                    # that died mid-step): wake a worker now rather than wait for the sweep.
                    wait = await asyncio.to_thread(runs.next_work_in, p, run_id)
                    if wait is not None and wait < -5:
                        await asyncio.to_thread(kick, "stream")
                if settings.event_listen:
                    await hub.wait(str(run_id), timeout=2.0)  # woken by NOTIFY; polls every 2 s regardless
                else:
                    await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: UUID, request: Request) -> dict:
        try:
            return {"status": runs.cancel_run(pool(request), run_id)}
        except runs.EngineError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/runs/{run_id}/replay", status_code=201)
    def replay(run_id: UUID, body: Replay, request: Request) -> dict:
        check_quota(request)
        try:
            new_id = runs.replay_run(pool(request), run_id, body.from_step, body.overrides)
        except runs.EngineError as exc:
            raise HTTPException(400, str(exc)) from exc
        kick("replay")
        return {"id": new_id}

    @app.get("/api/approvals")
    def list_approvals(request: Request, pending: bool = True) -> list[dict]:
        where = "WHERE a.decision IS NULL AND s.status = 'waiting'" if pending else ""
        with pool(request).connection() as conn:
            return conn.execute(
                "SELECT a.id, a.run_id, a.step_key, a.request, a.decision, a.requested_at FROM approvals a "
                "JOIN step_runs s ON s.run_id = a.run_id AND s.step_key = a.step_key "
                f"{where} ORDER BY a.requested_at DESC LIMIT 50").fetchall()

    @app.post("/api/approvals/{approval_id}")
    def decide(approval_id: UUID, body: Decision, request: Request) -> dict:
        try:
            out = runs.decide_approval(pool(request), approval_id, body.decision, body.by, body.note)
        except runs.EngineError as exc:
            raise HTTPException(409, str(exc)) from exc
        kick("approval")
        return out

    @app.get("/api/workers")
    def list_workers(request: Request) -> list[dict]:
        with pool(request).connection() as conn:
            rows = conn.execute(
                "SELECT w.id, w.pid, w.started_at, w.last_seen, w.steps_done, s.step_key AS current_step, "
                "s.run_id AS current_run, extract(epoch FROM now() - w.last_seen) AS seen_ago_s "
                "FROM workers w LEFT JOIN step_runs s ON s.id = w.current_step AND s.status = 'running' "
                "ORDER BY w.id").fetchall()
        sup = request.app.state.sup
        live = sup.status() if sup else {}
        for r in rows:
            r["managed"] = r["id"] in live
            r.update(live.get(r["id"], {}))
            if not r["managed"]:
                r["alive"] = float(r["seen_ago_s"]) < 5
        return rows

    @app.post("/api/workers/{worker_id}/kill")
    def kill(worker_id: str, request: Request) -> dict:
        sup = request.app.state.sup
        if not sup:
            raise HTTPException(400, "workers aren't managed by this server (SPAWN_WORKERS=0)")
        if not sup.kill(worker_id):
            raise HTTPException(409, "that worker isn't running")
        return {"killed": worker_id, "restart_in_s": sup.restart_after_s}

    @app.post("/api/examples/{name}/preview")
    def preview_example(name: str) -> dict:
        """Open the original example (before any run changed it)."""
        try:
            return {"url": preview.link("original", name) or preview.start(repo.example_dir(name), name)}
        except (repo.RepoError, preview.PreviewError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/runs/{run_id}/preview")
    def preview_run(run_id: UUID, request: Request) -> dict:
        """Open what a run merged. Only after a person approved the merge: see sdlc/preview.py."""
        with pool(request).connection() as conn:
            row = conn.execute(
                "SELECT r.input, w.name AS workflow, s.status AS merge_status FROM runs r "
                "JOIN workflows w ON w.id = r.workflow_id "
                "LEFT JOIN step_runs s ON s.run_id = r.id AND s.step_key = 'merge' WHERE r.id = %s",
                (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "run not found")
        if row["workflow"] != "sdlc" or row["merge_status"] != "done":
            raise HTTPException(409, "there's nothing to open until the merge is approved and done")
        example = row["input"].get("example", "")
        try:
            return {"url": preview.link(str(run_id), example) or
                    preview.start(sdlc_workflow.RESULTS / str(run_id) / "merged", example)}
        except preview.PreviewError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/health")
    def health(request: Request) -> dict:
        with pool(request).connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok", "llm_mode": settings.llm_mode,
                "model": settings.llm_model if settings.llm_mode == "live" else "fake"}

    return app


app = create_app()
