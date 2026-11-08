"""Tests run against a real Postgres (the engine's guarantees live in SQL, so mocks would lie).

Start one with `docker compose up -d db`. Tests use a separate database, agentops_test.
"""

import os
import time

import psycopg
import pytest

import workflows  # noqa: F401 - registers the demo workflows
from engine import runs
from engine.config import Settings
from engine.db import make_pool, migrate, reset
from engine.worker import Worker

ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL", "postgresql://agentops:agentops@localhost:54329/agentops")
TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://agentops:agentops@localhost:54329/agentops_test")

SMALL_INVOICE = "Vendor: Acme Supplies\nInvoice No: INV-1001\nDate: 2026-11-01\nTotal: ₹12,400"
BIG_INVOICE = "Vendor: Unknown Traders\nInvoice No: INV-2002\nDate: 2026-11-02\nTotal: ₹62,000"


def _ensure_database() -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=5) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = 'agentops_test'").fetchone():
            conn.execute("CREATE DATABASE agentops_test")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(database_url=TEST_URL, llm_mode="fake", lease_seconds=1.0, poll_seconds=0.05,
                    backoff_base_s=0.05, spawn_workers=0)


@pytest.fixture(scope="session")
def pool(settings):
    try:
        _ensure_database()
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc}); run `docker compose up -d db`")
    p = make_pool(settings.database_url, max_size=12)
    migrate(p)
    yield p
    p.close()


@pytest.fixture(autouse=True)
def clean(request):
    if "pool" in request.fixturenames:
        reset(request.getfixturevalue("pool"))
        runs._workflow_cache.clear()


@pytest.fixture
def worker(pool, settings) -> Worker:
    w = Worker(pool, settings, "w1")
    w.register()
    return w


def make_worker(pool, settings, worker_id: str) -> Worker:
    w = Worker(pool, settings, worker_id)
    w.register()
    return w


def run_status(pool, run_id) -> str:
    with pool.connection() as conn:
        return conn.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()["status"]


def steps(pool, run_id) -> dict[str, dict]:
    with pool.connection() as conn:
        return {r["step_key"]: r for r in conn.execute(
            "SELECT * FROM step_runs WHERE run_id = %s", (run_id,)).fetchall()}


def event_types(pool, run_id) -> list[str]:
    return [e["type"] for e in runs.events(pool, run_id)]


def drive(worker: Worker, pool, run_id, until=("done", "failed", "cancelled", "waiting"), timeout=10.0) -> str:
    """Run steps in-process until the run reaches one of `until` (waits out retry backoff)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = run_status(pool, run_id)
        if status in until:
            return status
        if not worker.run_once():
            time.sleep(0.02)
    raise AssertionError(f"run still {run_status(pool, run_id)} after {timeout}s")
