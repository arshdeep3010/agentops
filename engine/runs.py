"""The run state machine. Every change is one transaction: update the projection
(runs / step_runs) and append the event that explains it, so the two never disagree.

Status of a step:  pending → queued → running → done
                                   ↘ waiting (approval) → done / failed
                   pending → skipped (condition false)      running → queued (retry, lease expiry)
"""

import json
import logging
from uuid import UUID

from psycopg import Cursor
from psycopg_pool import ConnectionPool

from engine.db import jsonb
from engine.workflow import WORKFLOWS, Workflow, condition_holds

log = logging.getLogger(__name__)

TERMINAL_RUN = ("done", "failed", "cancelled")
OK_STEP = ("done", "skipped")
BAD_STEP = ("failed", "cancelled")
ACTIVE_STEP = ("queued", "running")

_workflow_cache: dict[str, Workflow] = {}


class EngineError(RuntimeError):
    pass


# ---------------------------------------------------------------- events

def append_event(cur: Cursor, run_id, type_: str, step_key: str | None = None,
                 data: dict | None = None) -> int:
    seq = cur.execute(
        "INSERT INTO run_events (run_id, step_key, type, data) VALUES (%s, %s, %s, %s) RETURNING seq",
        (run_id, step_key, type_, jsonb(data or {})),
    ).fetchone()["seq"]
    # Delivered to LISTENers when the transaction commits, never before.
    cur.execute("SELECT pg_notify('run_events', %s)", (f"{run_id}:{seq}",))
    return seq


def events(pool: ConnectionPool, run_id, after: int = 0, limit: int = 500) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT seq, run_id, step_key, type, data, at FROM run_events "
            "WHERE run_id = %s AND seq > %s ORDER BY seq LIMIT %s", (run_id, after, limit)).fetchall()


# ---------------------------------------------------------------- workflows

def ensure_workflow(pool: ConnectionPool, wf: Workflow) -> UUID:
    """Store a workflow version once. Changing a stored version is refused: bump the version."""
    graph = wf.graph()
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO workflows (name, version, graph) VALUES (%s, %s, %s) "
            "ON CONFLICT (name, version) DO NOTHING RETURNING id",
            (wf.name, wf.version, jsonb(graph))).fetchone()
        if row:
            return row["id"]
        row = conn.execute("SELECT id, graph FROM workflows WHERE name = %s AND version = %s",
                           (wf.name, wf.version)).fetchone()
        if json.dumps(row["graph"], sort_keys=True) != json.dumps(graph, sort_keys=True):
            raise EngineError(f"workflow {wf.name} v{wf.version} changed: bump its version")
        return row["id"]


def sync_workflows(pool: ConnectionPool) -> dict[str, UUID]:
    return {name: ensure_workflow(pool, wf) for name, wf in WORKFLOWS.items()}


def load_workflow(cur: Cursor, workflow_id) -> Workflow:
    key = str(workflow_id)
    if key not in _workflow_cache:
        row = cur.execute("SELECT graph FROM workflows WHERE id = %s", (workflow_id,)).fetchone()
        if not row:
            raise EngineError(f"unknown workflow {workflow_id}")
        _workflow_cache[key] = Workflow.from_graph(row["graph"])
    return _workflow_cache[key]


# ---------------------------------------------------------------- runs

def create_run(pool: ConnectionPool, workflow: str | UUID, input_: dict, *,
               budget_inr: float | None = None, overrides: dict | None = None,
               replay_of=None, reuse: dict[str, dict] | None = None) -> UUID:
    """Start a run. `workflow` is a registered name (latest version) or a pinned workflow id."""
    workflow_id = ensure_workflow(pool, WORKFLOWS[workflow]) if isinstance(workflow, str) else workflow
    with pool.connection() as conn, conn.transaction():
        cur = conn.cursor()
        wf = load_workflow(cur, workflow_id)
        run_id = cur.execute(
            "INSERT INTO runs (workflow_id, status, input, overrides, budget_inr, replay_of) "
            "VALUES (%s, 'running', %s, %s, %s, %s) RETURNING id",
            (workflow_id, jsonb(input_), jsonb(overrides or {}), budget_inr, replay_of)).fetchone()["id"]
        for spec in wf.steps:
            cur.execute("INSERT INTO step_runs (run_id, step_key, status, max_attempts) "
                        "VALUES (%s, %s, 'pending', %s)", (run_id, spec.key, spec.max_attempts))
        append_event(cur, run_id, "run_started", data={
            "workflow": wf.name, "version": wf.version, "replay_of": str(replay_of) if replay_of else None})
        for key, output in (reuse or {}).items():
            cur.execute("UPDATE step_runs SET status = 'done', output = %s, reused = true, "
                        "finished_at = now() WHERE run_id = %s AND step_key = %s",
                        (jsonb(output), run_id, key))
            append_event(cur, run_id, "step_reused", key, {"from_run": str(replay_of)})
        advance(cur, run_id)
    return run_id


def advance(cur: Cursor, run_id) -> str:
    """Queue every step whose inputs are ready, skip the ones whose condition is false,
    then settle the run status. Called inside the transaction that changed a step."""
    run = cur.execute("SELECT id, workflow_id, status, input, cancel_requested FROM runs "
                      "WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
    if run["status"] in TERMINAL_RUN:
        return run["status"]
    wf = load_workflow(cur, run["workflow_id"])
    rows = {r["step_key"]: r for r in cur.execute(
        "SELECT step_key, status, output FROM step_runs WHERE run_id = %s", (run_id,)).fetchall()}

    def scope() -> dict:
        outs = {k: r["output"] for k, r in rows.items() if r["status"] == "done"}
        return {"input": run["input"], **outs}

    if not run["cancel_requested"] and not any(r["status"] in BAD_STEP for r in rows.values()):
        changed = True
        while changed:  # a skip can make the next step ready, so repeat until stable
            changed = False
            for key in wf.order():
                spec, row = wf.step(key), rows[key]
                if row["status"] != "pending":
                    continue
                if not all(rows[d]["status"] in OK_STEP for d in spec.needs):
                    continue
                if condition_holds(spec.when, scope()):
                    cur.execute("UPDATE step_runs SET status = 'queued', queued_at = now(), "
                                "available_at = now() WHERE run_id = %s AND step_key = %s", (run_id, key))
                    row["status"] = "queued"
                    append_event(cur, run_id, "step_queued", key)
                else:
                    cur.execute("UPDATE step_runs SET status = 'skipped', finished_at = now() "
                                "WHERE run_id = %s AND step_key = %s", (run_id, key))
                    row["status"] = "skipped"
                    append_event(cur, run_id, "step_skipped", key, {"when": spec.when})
                changed = True

    statuses = [r["status"] for r in rows.values()]
    running = any(s == "running" for s in statuses)
    if run["cancel_requested"]:
        new = "running" if running else "cancelled"
    elif any(s in BAD_STEP for s in statuses):
        new = "running" if running else "failed"
    elif all(s in OK_STEP for s in statuses):
        new = "done"
    elif "waiting" in statuses and not any(s in ACTIVE_STEP for s in statuses):
        new = "waiting"
    else:
        new = "running"

    if new != run["status"]:
        if new in TERMINAL_RUN:
            # Nothing else may start once the run is over.
            cur.execute("UPDATE step_runs SET status = 'cancelled', finished_at = now() "
                        "WHERE run_id = %s AND status IN ('pending', 'queued', 'waiting')", (run_id,))
            cur.execute("UPDATE runs SET status = %s, finished_at = now() WHERE id = %s", (new, run_id))
            append_event(cur, run_id, f"run_{new}")
        else:
            cur.execute("UPDATE runs SET status = %s WHERE id = %s", (new, run_id))
            if new == "waiting":
                append_event(cur, run_id, "run_waiting")
            elif run["status"] == "waiting":
                append_event(cur, run_id, "run_resumed")
    return new


def fail_run(cur: Cursor, run_id, reason: str) -> None:
    cur.execute("UPDATE runs SET error = %s WHERE id = %s AND error IS NULL", (reason, run_id))
    cur.execute("UPDATE step_runs SET status = 'cancelled', finished_at = now() "
                "WHERE run_id = %s AND status IN ('pending', 'queued', 'waiting')", (run_id,))
    cur.execute("UPDATE step_runs SET status = 'failed', error = %s, finished_at = now() "
                "WHERE run_id = %s AND status = 'running'", (reason, run_id))
    cur.execute("UPDATE runs SET status = 'failed', finished_at = now() WHERE id = %s "
                "AND status NOT IN ('done', 'failed', 'cancelled')", (run_id,))
    append_event(cur, run_id, "run_failed", data={"reason": reason})


# ---------------------------------------------------------------- claiming work

_CLAIM = """
WITH next AS (
    SELECT sr.id, sr.status AS prev_status, sr.worker_id AS prev_worker
    FROM step_runs sr JOIN runs r ON r.id = sr.run_id
    WHERE r.cancel_requested = false
      AND r.status IN ('running', 'waiting')
      AND ((sr.status = 'queued' AND sr.available_at <= now())
           OR (sr.status = 'running' AND sr.lease_until < now()))
    ORDER BY sr.available_at
    FOR UPDATE OF sr SKIP LOCKED
    LIMIT 1
)
UPDATE step_runs s
SET status = 'running', worker_id = %(worker)s, attempt = s.attempt + 1,
    lease_until = now() + make_interval(secs => %(lease)s), started_at = now(), error = NULL
FROM next
WHERE s.id = next.id
RETURNING s.id, s.run_id, s.step_key, s.attempt, s.attempt_floor, s.max_attempts, s.iteration,
          next.prev_status, next.prev_worker
"""


def claim(pool: ConnectionPool, worker_id: str, lease_s: float) -> dict | None:
    """Take one ready step (or one whose worker's lease ran out). Never blocks on other workers."""
    with pool.connection() as conn, conn.transaction():
        cur = conn.cursor()
        while True:
            row = cur.execute(_CLAIM, {"worker": worker_id, "lease": lease_s}).fetchone()
            if not row:
                return None
            reclaimed = row["prev_status"] == "running"
            if reclaimed:
                append_event(cur, row["run_id"], "step_reclaimed", row["step_key"], {
                    "from_worker": row["prev_worker"], "to_worker": worker_id, "attempt": row["attempt"],
                    "try": row["attempt"] - row["attempt_floor"],
                    "reason": "lease expired"})
            if row["attempt"] - row["attempt_floor"] > row["max_attempts"]:
                # Poison step: it keeps killing workers. Stop retrying.
                reason = f"gave up after {row['max_attempts']} attempts (workers kept dying)"
                cur.execute("UPDATE step_runs SET status = 'failed', error = %s, finished_at = now(), "
                            "lease_until = NULL WHERE id = %s", (reason, row["id"]))
                append_event(cur, row["run_id"], "step_failed", row["step_key"], {"error": reason})
                advance(cur, row["run_id"])
                continue
            append_event(cur, row["run_id"], "step_started", row["step_key"],
                         {"worker": worker_id, "attempt": row["attempt"],
                          "try": row["attempt"] - row["attempt_floor"], "round": row["iteration"] + 1})
            cur.execute("UPDATE workers SET current_step = %s, last_seen = now() WHERE id = %s",
                        (row["id"], worker_id))
            return dict(row, reclaimed=reclaimed)


def next_work_in(pool: ConnectionPool, run_id=None) -> float | None:
    """Seconds until some step can be claimed (0 or less: now), or None if nothing is queued or
    running. A running step counts from when its lease runs out: that is when it could be reclaimed.
    Serverless workers use this to decide whether to wait a little or exit."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT extract(epoch FROM min(CASE WHEN sr.status = 'queued' THEN sr.available_at "
            "ELSE sr.lease_until END) - now()) AS s "
            "FROM step_runs sr JOIN runs r ON r.id = sr.run_id "
            "WHERE r.cancel_requested = false AND r.status IN ('running', 'waiting') "
            "AND sr.status IN ('queued', 'running') AND (%(run)s::uuid IS NULL OR sr.run_id = %(run)s::uuid)",
            {"run": run_id}).fetchone()
    return None if row["s"] is None else float(row["s"])


def heartbeat(pool: ConnectionPool, step_run_id, attempt: int, worker_id: str, lease_s: float) -> str:
    """Extend our lease. Returns "ok", "lost" (someone else owns the step now) or "cancel"."""
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "UPDATE step_runs s SET lease_until = now() + make_interval(secs => %s) FROM runs r "
            "WHERE s.id = %s AND s.attempt = %s AND s.status = 'running' AND r.id = s.run_id "
            "RETURNING r.cancel_requested", (lease_s, step_run_id, attempt)).fetchone()
        conn.execute("UPDATE workers SET last_seen = now() WHERE id = %s", (worker_id,))
    if not row:
        return "lost"
    return "cancel" if row["cancel_requested"] else "ok"


def _own(cur: Cursor, step_run_id, attempt: int) -> dict | None:
    """Lock the step only if we still hold it (fencing: a newer attempt wins)."""
    return cur.execute(
        "SELECT id, run_id, step_key, attempt, attempt_floor, max_attempts, iteration, worker_id FROM step_runs "
        "WHERE id = %s AND attempt = %s AND status = 'running' FOR UPDATE",
        (step_run_id, attempt)).fetchone()


def _stale(cur: Cursor, step_run_id, attempt: int, what: str) -> None:
    row = cur.execute("SELECT run_id, step_key, attempt FROM step_runs WHERE id = %s",
                      (step_run_id,)).fetchone()
    if row:
        append_event(cur, row["run_id"], "stale_result_discarded", row["step_key"], {
            "stale_attempt": attempt, "current_attempt": row["attempt"], "what": what})


def complete_step(pool: ConnectionPool, step_run_id, attempt: int, output: dict, *,
                  tokens: int = 0, cost_inr: float = 0.0, ms: int = 0) -> bool:
    with pool.connection() as conn, conn.transaction():
        cur = conn.cursor()
        own = _own(cur, step_run_id, attempt)
        if not own:
            _stale(cur, step_run_id, attempt, "result")
            return False
        run_id = own["run_id"]
        cur.execute("UPDATE step_runs SET status = 'done', output = %s, finished_at = now(), "
                    "lease_until = NULL WHERE id = %s", (jsonb(output), step_run_id))
        run = cur.execute("UPDATE runs SET tokens_used = tokens_used + %s, cost_inr = cost_inr + %s "
                          "WHERE id = %s RETURNING cost_inr, budget_inr", (tokens, cost_inr, run_id)).fetchone()
        cur.execute("UPDATE workers SET current_step = NULL, steps_done = steps_done + 1 WHERE id = %s",
                    (own["worker_id"],))
        append_event(cur, run_id, "step_done", own["step_key"], {
            "attempt": attempt, "worker": own["worker_id"], "ms": ms, "tokens": tokens, "cost_inr": cost_inr})
        if run["budget_inr"] is not None and run["cost_inr"] > run["budget_inr"]:
            fail_run(cur, run_id, f"budget exceeded: ₹{run['cost_inr']:.2f} > ₹{run['budget_inr']:.2f}")
        else:
            advance(cur, run_id)
        return True


def fail_step(pool: ConnectionPool, step_run_id, attempt: int, error: str, *, retryable: bool,
              backoff_base_s: float = 1.0) -> str:
    """Retry later (with backoff) if allowed, else fail the step. Returns what happened."""
    with pool.connection() as conn, conn.transaction():
        cur = conn.cursor()
        own = _own(cur, step_run_id, attempt)
        if not own:
            _stale(cur, step_run_id, attempt, "error")
            return "stale"
        cur.execute("UPDATE workers SET current_step = NULL WHERE id = %s", (own["worker_id"],))
        tries = attempt - own["attempt_floor"]
        if retryable and tries < own["max_attempts"]:
            delay = backoff_base_s * 2 ** (tries - 1)
            cur.execute("UPDATE step_runs SET status = 'queued', error = %s, worker_id = NULL, "
                        "lease_until = NULL, available_at = now() + make_interval(secs => %s) "
                        "WHERE id = %s", (error, delay, step_run_id))
            append_event(cur, own["run_id"], "step_retry", own["step_key"],
                         {"error": error, "attempt": attempt, "retry_in_s": delay})
            return "retry"
        cur.execute("UPDATE step_runs SET status = 'failed', error = %s, finished_at = now(), "
                    "lease_until = NULL WHERE id = %s", (error, step_run_id))
        cur.execute("UPDATE runs SET error = %s WHERE id = %s AND error IS NULL",
                    (f"{own['step_key']}: {error}", own["run_id"]))
        append_event(cur, own["run_id"], "step_failed", own["step_key"],
                     {"error": error, "attempt": attempt, "retryable": retryable})
        advance(cur, own["run_id"])
        return "failed"


def cancel_step(pool: ConnectionPool, step_run_id, attempt: int) -> None:
    with pool.connection() as conn, conn.transaction():
        cur = conn.cursor()
        own = _own(cur, step_run_id, attempt)
        if not own:
            return
        cur.execute("UPDATE step_runs SET status = 'cancelled', finished_at = now(), lease_until = NULL "
                    "WHERE id = %s", (step_run_id,))
        cur.execute("UPDATE workers SET current_step = NULL WHERE id = %s", (own["worker_id"],))
        append_event(cur, own["run_id"], "step_cancelled", own["step_key"], {"attempt": attempt})
        advance(cur, own["run_id"])


# ---------------------------------------------------------------- human approval

def request_approval(pool: ConnectionPool, step_run_id, attempt: int, request: dict) -> bool:
    """Park the step. No worker holds it while it waits, for minutes or days."""
    with pool.connection() as conn, conn.transaction():
        cur = conn.cursor()
        own = _own(cur, step_run_id, attempt)
        if not own:
            _stale(cur, step_run_id, attempt, "approval request")
            return False
        cur.execute("UPDATE step_runs SET status = 'waiting', lease_until = NULL WHERE id = %s",
                    (step_run_id,))
        cur.execute("UPDATE workers SET current_step = NULL, steps_done = steps_done + 1 WHERE id = %s",
                    (own["worker_id"],))
        approval_id = cur.execute(
            "INSERT INTO approvals (run_id, step_key, request) VALUES (%s, %s, %s) "
            "ON CONFLICT (run_id, step_key) DO UPDATE SET request = EXCLUDED.request, decision = NULL, "
            "decided_by = NULL, note = NULL, decided_at = NULL, requested_at = now() RETURNING id",
            (own["run_id"], own["step_key"], jsonb(request))).fetchone()["id"]
        append_event(cur, own["run_id"], "approval_requested", own["step_key"],
                     {"approval_id": str(approval_id), "title": request.get("title")})
        advance(cur, own["run_id"])
        return True


def decide_approval(pool: ConnectionPool, approval_id, decision: str, by: str, note: str = "") -> dict:
    if decision not in ("approved", "rejected", "changes"):
        raise EngineError("decision must be 'approved', 'rejected' or 'changes'")
    with pool.connection() as conn, conn.transaction():
        cur = conn.cursor()
        appr = cur.execute("SELECT id, run_id, step_key, decision FROM approvals WHERE id = %s FOR UPDATE",
                           (approval_id,)).fetchone()
        if not appr:
            raise EngineError("approval not found")
        if appr["decision"]:
            raise EngineError(f"already {appr['decision']}")
        step = cur.execute("SELECT id FROM step_runs WHERE run_id = %s AND step_key = %s "
                           "AND status = 'waiting' FOR UPDATE", (appr["run_id"], appr["step_key"])).fetchone()
        if not step:
            raise EngineError("this run is no longer waiting for that approval")
        cur.execute("UPDATE approvals SET decision = %s, decided_by = %s, note = %s, decided_at = now() "
                    "WHERE id = %s", (decision, by, note, approval_id))
        run_id, key = appr["run_id"], appr["step_key"]
        if decision == "changes":
            run = cur.execute("SELECT workflow_id FROM runs WHERE id = %s", (run_id,)).fetchone()
            target = load_workflow(cur, run["workflow_id"]).step(key).config.get("rewind_on_changes")
            if not target:
                raise EngineError("this approval can't send work back; approve or reject it")
            if not note.strip():
                raise EngineError("say what should change")
            append_event(cur, run_id, "approval_changes", key, {"by": by, "note": note, "rewind_to": target})
            rewind(cur, run_id, target, {"human_feedback": note, "by": by}, f"changes requested by {by}", key)
        elif decision == "approved":
            output = {"approved": True, "by": by, "note": note}
            cur.execute("UPDATE step_runs SET status = 'done', output = %s, finished_at = now() WHERE id = %s",
                        (jsonb(output), step["id"]))
            append_event(cur, run_id, "approval_given", key, {"by": by, "note": note})
            append_event(cur, run_id, "step_done", key, {"by": by})
        else:
            reason = f"rejected by {by}" + (f": {note}" if note else "")
            cur.execute("UPDATE step_runs SET status = 'failed', error = %s, finished_at = now() WHERE id = %s",
                        (reason, step["id"]))
            cur.execute("UPDATE runs SET error = %s WHERE id = %s AND error IS NULL", (reason, run_id))
            append_event(cur, run_id, "approval_rejected", key, {"by": by, "note": note})
            append_event(cur, run_id, "step_failed", key, {"error": reason})
        status = advance(cur, run_id)
        return {"run_id": run_id, "run_status": status}


# ---------------------------------------------------------------- loops (gates and change requests)

def rewind(cur: Cursor, run_id, to_step: str, feedback: dict, reason: str, by_step: str) -> int:
    """Send `to_step` and everything after it back to pending for another iteration.
    `attempt` keeps growing (it's the fencing token); `attempt_floor` resets the retry budget."""
    run = cur.execute("SELECT workflow_id FROM runs WHERE id = %s", (run_id,)).fetchone()
    keys = sorted(load_workflow(cur, run["workflow_id"]).downstream(to_step))
    # The rewound step sees what it produced last time, so it can fix rather than start over.
    before = cur.execute("SELECT output FROM step_runs WHERE run_id = %s AND step_key = %s",
                         (run_id, to_step)).fetchone()
    feedback = {**feedback, "previous_output": before["output"] if before else None}
    cur.execute(
        "UPDATE step_runs SET status = 'pending', output = NULL, error = NULL, worker_id = NULL, "
        "lease_until = NULL, started_at = NULL, finished_at = NULL, reused = false, attempt_floor = attempt, "
        # Only steps that actually ran start a new round; ones still waiting their turn don't.
        "iteration = CASE WHEN status IN ('pending', 'queued') THEN iteration ELSE iteration + 1 END, "
        "feedback = CASE WHEN step_key = %s THEN %s ELSE NULL END "
        "WHERE run_id = %s AND step_key = ANY(%s)", (to_step, jsonb(feedback), run_id, keys))
    iteration = cur.execute("SELECT iteration FROM step_runs WHERE run_id = %s AND step_key = %s",
                            (run_id, to_step)).fetchone()["iteration"]
    append_event(cur, run_id, "step_rewound", to_step,
                 {"iteration": iteration, "reason": reason, "by": by_step, "steps": keys})
    return iteration


def gate_failed(pool: ConnectionPool, step_run_id, attempt: int, output: dict, feedback: dict,
                on_fail: dict | None) -> str:
    """A gate said no. Loop back with the feedback if the loop budget allows, else fail."""
    with pool.connection() as conn, conn.transaction():
        cur = conn.cursor()
        own = _own(cur, step_run_id, attempt)
        if not own:
            _stale(cur, step_run_id, attempt, "gate result")
            return "stale"
        run_id, key = own["run_id"], own["step_key"]
        cur.execute("UPDATE workers SET current_step = NULL, steps_done = steps_done + 1 WHERE id = %s",
                    (own["worker_id"],))
        append_event(cur, run_id, "gate_failed", key, {"iteration": own["iteration"], "feedback": feedback})
        # Keep what failed: a step that rewinds itself gets it back as previous_output.
        cur.execute("UPDATE step_runs SET output = %s WHERE id = %s", (jsonb(output), step_run_id))
        target = (on_fail or {}).get("rewind_to")
        max_loops = int((on_fail or {}).get("max_loops", 3))
        if target:
            loops = cur.execute("SELECT iteration FROM step_runs WHERE run_id = %s AND step_key = %s",
                                (run_id, target)).fetchone()["iteration"]
            stalled = bool(feedback.get("escalate_now")) and (on_fail or {}).get("on_exhausted") == "escalate"
            if loops < max_loops and not stalled:
                rewind(cur, run_id, target, feedback, f"{key} gate failed", key)
                advance(cur, run_id)
                return "rewound"
            reason = ("gate is repeating itself; a person needs to look" if stalled else
                      f"gate still failing after {max_loops} loops; a person needs to look")
            if (on_fail or {}).get("on_exhausted") == "escalate":
                # Advisory gate (e.g. AI review): out of rounds, so carry the open issues forward
                # for a person to decide at a later approval, instead of throwing the run away.
                escalated = {**output, "unresolved": feedback, "escalated": True}
                cur.execute("UPDATE step_runs SET status = 'done', output = %s, finished_at = now(), "
                            "lease_until = NULL WHERE id = %s", (jsonb(escalated), step_run_id))
                append_event(cur, run_id, "gate_escalated", key, {"reason": reason, "feedback": feedback})
                append_event(cur, run_id, "step_done", key, {"attempt": attempt, "escalated": True})
                advance(cur, run_id)
                return "escalated"
        else:
            reason = "gate failed"
        cur.execute("UPDATE step_runs SET status = 'failed', output = %s, error = %s, finished_at = now(), "
                    "lease_until = NULL WHERE id = %s", (jsonb(output), reason, step_run_id))
        cur.execute("UPDATE runs SET error = %s WHERE id = %s AND error IS NULL", (f"{key}: {reason}", run_id))
        append_event(cur, run_id, "step_failed", key, {"error": reason, "attempt": attempt})
        advance(cur, run_id)
        return "failed"


def save_artifact(pool: ConnectionPool, run_id, step_key: str, iteration: int, name: str, content: str,
                  media_type: str = "text/markdown") -> None:
    import hashlib
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO artifacts (run_id, step_key, iteration, name, media_type, sha256, content) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (run_id, step_key, iteration, name) "
            "DO UPDATE SET content = EXCLUDED.content, sha256 = EXCLUDED.sha256, created_at = now()",
            (run_id, step_key, iteration, name, media_type, digest, content))


# ---------------------------------------------------------------- cancel and replay

def cancel_run(pool: ConnectionPool, run_id) -> str:
    with pool.connection() as conn, conn.transaction():
        cur = conn.cursor()
        run = cur.execute("SELECT status FROM runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
        if not run:
            raise EngineError("run not found")
        if run["status"] in TERMINAL_RUN:
            return run["status"]
        cur.execute("UPDATE runs SET cancel_requested = true WHERE id = %s", (run_id,))
        append_event(cur, run_id, "run_cancel_requested")
        # Running steps stop at their next heartbeat; advance() settles the rest.
        return advance(cur, run_id)


def replay_run(pool: ConnectionPool, run_id, from_step: str, overrides: dict | None = None) -> UUID:
    """New run on the same workflow version: steps before `from_step` keep their output,
    `from_step` and everything after it runs again (with optional config overrides)."""
    with pool.connection() as conn:
        cur = conn.cursor()
        run = cur.execute("SELECT workflow_id, input, overrides, budget_inr FROM runs WHERE id = %s",
                          (run_id,)).fetchone()
        if not run:
            raise EngineError("run not found")
        wf = load_workflow(cur, run["workflow_id"])
        if from_step not in {s.key for s in wf.steps}:
            raise EngineError(f"unknown step {from_step!r}")
        rerun = wf.downstream(from_step)
        done = cur.execute("SELECT step_key, output FROM step_runs WHERE run_id = %s AND status = 'done'",
                           (run_id,)).fetchall()
    reuse = {r["step_key"]: r["output"] for r in done if r["step_key"] not in rerun}
    merged = dict(run["overrides"])
    for key, patch in (overrides or {}).items():
        merged[key] = {**merged.get(key, {}), **patch}
    return create_run(pool, run["workflow_id"], run["input"], budget_inr=run["budget_inr"],
                      overrides=merged, replay_of=run_id, reuse=reuse)


# ---------------------------------------------------------------- event-sourced rebuild

_STEP_STATUS_BY_EVENT = {
    "step_queued": "queued", "step_started": "running", "step_reclaimed": "running",
    "step_retry": "queued", "step_done": "done", "step_skipped": "skipped", "step_failed": "failed",
    "step_cancelled": "cancelled", "approval_requested": "waiting", "step_reused": "done",
}
_REWIND_EVENT = "step_rewound"
_RUN_STATUS_BY_EVENT = {"run_started": "running", "run_waiting": "waiting", "run_resumed": "running",
                        "run_done": "done", "run_failed": "failed", "run_cancelled": "cancelled"}


def rebuild(event_rows: list[dict], step_keys: list[str]) -> dict:
    """Recompute run and step status from the event log alone (used to verify the projection)."""
    steps = {k: "pending" for k in step_keys}
    run = "running"
    for e in event_rows:
        if e["type"] in _RUN_STATUS_BY_EVENT:
            run = _RUN_STATUS_BY_EVENT[e["type"]]
            if run in TERMINAL_RUN:
                steps = {k: ("cancelled" if s in ("pending", "queued", "waiting") else s)
                         for k, s in steps.items()}
                if run == "failed":
                    steps = {k: ("failed" if s == "running" else s) for k, s in steps.items()}
        elif e["type"] == _REWIND_EVENT:
            for k in e["data"]["steps"]:
                steps[k] = "pending"
        elif e["type"] in _STEP_STATUS_BY_EVENT and e["step_key"]:
            steps[e["step_key"]] = _STEP_STATUS_BY_EVENT[e["type"]]
    return {"run": run, "steps": steps}
