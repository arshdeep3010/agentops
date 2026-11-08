"""What a step sees while it runs: inputs, earlier outputs, idempotent tools, and stop signals."""

import threading
import time
from dataclasses import dataclass, field

from psycopg_pool import ConnectionPool

from engine.db import jsonb
from engine.runs import append_event
from engine.workflow import TOOLS


class StepError(Exception):
    """Base for errors a step raises on purpose."""


class RetryableError(StepError):
    """Transient: try again later (timeouts, rate limits, flaky dependencies)."""


class FatalError(StepError):
    """Retrying won't help (bad input, validation failed for good, business rule)."""


class Cancelled(StepError):
    """The run was cancelled while this step ran."""


class LeaseLost(StepError):
    """Another worker owns this step now; drop the work quietly."""


class StepTimeout(RetryableError):
    pass


class GateFailed(StepError):
    """A gate said no. The engine loops back (step.on_fail) with `feedback`, or fails the step."""

    def __init__(self, feedback: dict, output: dict | None = None):
        super().__init__(feedback.get("summary", "gate failed"))
        self.feedback = feedback
        self.output = output or {}


@dataclass
class StepContext:
    pool: ConnectionPool
    run_id: object
    step_key: str
    attempt: int
    worker_id: str
    input: dict
    outputs: dict
    config: dict
    deadline: float
    iteration: int = 0
    feedback: dict | None = None  # why this step was sent back (gate failure or human request)
    history: list | None = None   # this step's own earlier gate failures, oldest first
    stop: threading.Event = field(default_factory=threading.Event)
    stop_reason: str = ""
    tokens: int = 0
    cost_inr: float = 0.0
    _tool_calls: int = 0

    @property
    def scope(self) -> dict:
        return {"input": self.input, **self.outputs, "feedback": self.feedback, "iteration": self.iteration,
                "history": self.history or []}

    def check(self) -> None:
        """Call between units of work: stops promptly on cancel, lease loss or timeout."""
        if self.stop.is_set():
            raise Cancelled() if self.stop_reason == "cancel" else LeaseLost()
        if time.monotonic() > self.deadline:
            raise StepTimeout("step timed out")

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            self.check()
            self.stop.wait(min(left, 0.1))
        self.check()

    def log(self, message: str, **data) -> None:
        with self.pool.connection() as conn, conn.transaction():
            append_event(conn.cursor(), self.run_id, "step_log", self.step_key,
                         {"message": message, "attempt": self.attempt, **data})

    def save_artifact(self, name: str, content: str, media_type: str = "text/markdown") -> None:
        from engine.runs import save_artifact
        save_artifact(self.pool, self.run_id, self.step_key, self.iteration, name, content, media_type)

    def add_usage(self, tokens: int, cost_inr: float) -> None:
        self.tokens += tokens
        self.cost_inr += cost_inr

    def call_tool(self, name: str, args: dict) -> dict:
        """Side effects happen once per logical call, however often the step is retried.

        The key is the same on every attempt (run, step, tool, call number) and is passed to
        the tool, so an external API that honours idempotency keys dedupes even the window
        between "the API did it" and "we recorded that it did".
        """
        self.check()
        self._tool_calls += 1
        key = f"{self.run_id}:{self.step_key}:{name}:{self._tool_calls}"
        with self.pool.connection() as conn:
            row = conn.execute("SELECT result FROM tool_calls WHERE idempotency_key = %s", (key,)).fetchone()
        if row:
            with self.pool.connection() as conn, conn.transaction():
                append_event(conn.cursor(), self.run_id, "tool_result_reused", self.step_key,
                             {"tool": name, "key": key})
            return row["result"]
        result = TOOLS[name](self, args, key)
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("INSERT INTO tool_calls (idempotency_key, run_id, tool, result) VALUES (%s, %s, %s, %s) "
                         "ON CONFLICT (idempotency_key) DO NOTHING", (key, self.run_id, name, jsonb(result)))
            append_event(conn.cursor(), self.run_id, "tool_called", self.step_key,
                         {"tool": name, "key": key, "attempt": self.attempt})
        return result
