"""Postgres access: one connection pool per process, rows as dicts, JSON in and out."""

from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

SCHEMA = Path(__file__).with_name("schema.sql")

TABLES = ("artifacts", "demo_ledger", "demo_ledger_requests", "workers", "tool_calls", "approvals",
          "run_events", "step_runs", "runs", "workflows")


def make_pool(database_url: str, min_size: int = 1, max_size: int = 8) -> ConnectionPool:
    pool = ConnectionPool(database_url, min_size=min_size, max_size=max_size,
                          kwargs={"row_factory": dict_row}, open=False)
    pool.open(wait=True, timeout=15)
    return pool


def migrate(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        conn.execute(SCHEMA.read_text(encoding="utf-8"))


def reset(pool: ConnectionPool) -> None:
    """Delete all data (tests and local demos only)."""
    with pool.connection() as conn:
        conn.execute("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")


def jsonb(value) -> Jsonb:
    return Jsonb(value)
