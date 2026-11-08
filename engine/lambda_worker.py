"""The worker as an AWS Lambda function: wake up, drain the queue, exit.

Locally a worker is a long-running process (`python -m engine.worker`). On Lambda nothing runs
between requests, so the API "kicks" this function whenever it creates work (a new run, an
approval, a replay), and a schedule sweeps every few minutes in case a kick was missed or a
function died mid-step. The engine itself is unchanged: steps are claimed with SKIP LOCKED,
held with a lease and fenced, so several of these can run at once, and a function that hits its
15-minute limit mid-step is just a crashed worker: its lease runs out and the next one reclaims it.
"""

import logging
import os
import time

from engine import aws, runs
from engine.config import get_settings
from engine.db import make_pool
from engine.worker import Worker

log = logging.getLogger("agentops.lambda_worker")
logging.getLogger().setLevel(logging.INFO)

# Start a step only with this much time left. LLM steps may take up to 7 min (timeout_s=420).
RESERVE_S = float(os.environ.get("WORKER_RESERVE_S", "450"))
# With nothing claimable now, wait for queued work (a retry backoff) at most this long.
MAX_IDLE_WAIT_S = 20.0

_worker: Worker | None = None


def _get_worker(context) -> Worker:
    """One pool and Worker per execution environment, reused while the function stays warm."""
    global _worker
    if _worker is None:
        import workflows  # noqa: F401 - registers workflow code, tools and schemas

        settings = get_settings()
        pool = make_pool(settings.database_url, min_size=settings.db_pool_min, max_size=3)
        stream = getattr(context, "log_stream_name", "") or str(os.getpid())
        _worker = Worker(pool, settings, "lambda-" + stream.rsplit("]", 1)[-1][-10:])
    return _worker


def handler(event, context) -> dict:
    worker = _get_worker(context)
    worker.register()
    settings = worker.settings
    done, reason = 0, "idle"
    while True:
        left = context.get_remaining_time_in_millis() / 1000
        if left < RESERVE_S:
            reason = "out of time"
            break
        if worker.run_once():
            done += 1
            continue
        wait = runs.next_work_in(worker.pool)
        if wait is None or wait > MAX_IDLE_WAIT_S:
            break  # nothing soon: a kick or the sweep will start the next worker
        time.sleep(max(0.2, min(wait, MAX_IDLE_WAIT_S)) + 0.1)
    # Out of time with work left: hand over to a fresh function instead of waiting for the sweep.
    if reason == "out of time" and settings.worker_function:
        wait = runs.next_work_in(worker.pool)
        if wait is not None and wait < MAX_IDLE_WAIT_S:
            aws.invoke_async(settings.worker_function, {"source": "handover"})
            reason = "handed over"
    log.info("worker %s ran %d steps (%s, source=%s)", worker.id, done, reason, (event or {}).get("source"))
    return {"worker": worker.id, "steps": done, "stopped": reason}
