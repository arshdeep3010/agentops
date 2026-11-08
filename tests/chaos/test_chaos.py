"""Chaos test: real worker processes, killed at random (hard kill, no cleanup), restarted.

Run:  pytest -m chaos -s            (CHAOS_RUNS=500 for the full benchmark)
Pass: every run finishes, every side effect is applied exactly once.
Writes results/chaos.json with the numbers for the README.
"""

import json
import os
import random
import statistics
import time
from pathlib import Path

import pytest

from engine import runs
from engine.supervisor import Supervisor
from tests.conftest import TEST_URL

pytestmark = pytest.mark.chaos

RUNS = int(os.environ.get("CHAOS_RUNS", "100"))
WORKERS = int(os.environ.get("CHAOS_WORKERS", "3"))
LEASE_S = float(os.environ.get("CHAOS_LEASE_S", "2"))
TIMEOUT_S = float(os.environ.get("CHAOS_TIMEOUT_S", "900"))
RESULTS = Path(__file__).resolve().parents[2] / "results"


def test_random_worker_kills_lose_nothing_and_repeat_nothing(pool):
    sup = Supervisor(WORKERS, env={"DATABASE_URL": TEST_URL, "LEASE_SECONDS": str(LEASE_S),
                                   "POLL_SECONDS": "0.05", "LLM_MODE": "fake", "SPAWN_WORKERS": "0"},
                     restart_after_s=1.0)
    run_ids = [runs.create_run(pool, "chaos", {"n": f"C{i}"}) for i in range(RUNS)]
    kills: list[tuple[float, str]] = []
    began = time.time()
    sup.start()
    try:
        while time.time() - began < TIMEOUT_S:
            with pool.connection() as conn:
                open_runs = conn.execute("SELECT count(*) AS n FROM runs WHERE status NOT IN "
                                         "('done', 'failed', 'cancelled')").fetchone()["n"]
            if open_runs == 0:
                break
            time.sleep(random.uniform(0.6, 1.4))
            victim = random.choice(list(sup.workers))
            if sup.kill(victim):
                kills.append((time.time(), victim))
        elapsed = time.time() - began
    finally:
        sup.stop()

    with pool.connection() as conn:
        statuses = conn.execute("SELECT status, count(*) AS n FROM runs GROUP BY status").fetchall()
        per_run = conn.execute(
            "SELECT r.id, (SELECT count(*) FROM demo_ledger l WHERE l.run_id = r.id) AS effects "
            "FROM runs r").fetchall()
        requests = conn.execute("SELECT count(*) AS n FROM demo_ledger_requests").fetchone()["n"]
        reclaims = conn.execute("SELECT at, data FROM run_events WHERE type = 'step_reclaimed' ORDER BY at").fetchall()
        stale = conn.execute("SELECT count(*) AS n FROM run_events WHERE type = 'stale_result_discarded'").fetchone()["n"]
        reused = conn.execute("SELECT count(*) AS n FROM run_events WHERE type = 'tool_result_reused'").fetchone()["n"]
        steps_total = conn.execute("SELECT count(*) AS n FROM step_runs").fetchone()["n"]
        attempts = conn.execute("SELECT sum(attempt) AS n FROM step_runs").fetchone()["n"]

    # How long until a killed worker's step was picked up by someone else.
    resume = []
    for killed_at, victim in kills:
        nxt = next((r for r in reclaims if r["data"]["from_worker"] == victim
                    and r["at"].timestamp() >= killed_at), None)
        if nxt:
            resume.append(nxt["at"].timestamp() - killed_at)

    by_status = {s["status"]: s["n"] for s in statuses}
    duplicates = sum(1 for r in per_run if r["effects"] > 1)
    missing = sum(1 for r in per_run if r["effects"] == 0)
    result = {
        "runs": RUNS, "workers": WORKERS, "lease_s": LEASE_S, "elapsed_s": round(elapsed, 1),
        "runs_by_status": by_status, "worker_kills": len(kills), "steps_taken_over": len(reclaims),
        "runs_lost": RUNS - by_status.get("done", 0),
        "duplicate_side_effects": duplicates, "missing_side_effects": missing,
        "ledger_requests_per_run": round(requests / RUNS, 3),
        "tool_results_reused": reused, "stale_results_discarded": stale,
        "extra_attempts_per_step": round((attempts - steps_total) / steps_total, 3),
        "resume_after_kill_s": {
            "median": round(statistics.median(resume), 2) if resume else None,
            "p95": round(sorted(resume)[int(len(resume) * 0.95) - 1], 2) if len(resume) >= 20 else None,
            "max": round(max(resume), 2) if resume else None, "samples": len(resume)},
        "steps_per_s": round(steps_total / elapsed, 2),
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "chaos.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("\n" + json.dumps(result, indent=2))

    assert result["runs_lost"] == 0, by_status
    assert duplicates == 0 and missing == 0
    assert len(kills) > 0 and len(reclaims) > 0  # the test really did break things
