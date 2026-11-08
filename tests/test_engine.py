import threading
import time

import pytest

from engine import runs
from engine.config import Settings
from engine.context import FatalError, StepContext
from engine.workflow import StepSpec, Workflow, code, fake_llm, register_workflow, schema
from tests.conftest import BIG_INVOICE, SMALL_INVOICE, drive, event_types, make_worker, run_status, steps
from pydantic import BaseModel

# ---------------------------------------------------------------- test-only workflows


@code("t_flaky")
def t_flaky(ctx: StepContext) -> dict:
    if ctx.attempt < ctx.config.get("succeed_on", 3):
        raise RuntimeError(f"transient failure on attempt {ctx.attempt}")
    return {"ok_on_attempt": ctx.attempt}


@code("t_fatal")
def t_fatal(ctx: StepContext) -> dict:
    raise FatalError("bad input: retrying will not help")


@code("t_ok")
def t_ok(ctx: StepContext) -> dict:
    return {"step": ctx.step_key}


@code("t_slow")
def t_slow(ctx: StepContext) -> dict:
    ctx.sleep(ctx.config.get("seconds", 5))
    return {"slept": True}


@code("t_tool_then_crash")
def t_tool_then_crash(ctx: StepContext) -> dict:
    result = ctx.call_tool("post_to_ledger", {"vendor": "t", "amount_inr": 5, "invoice_no": "T-1"})
    if ctx.attempt == 1:
        raise RuntimeError("crashed after the side effect, before recording the step result")
    return result


@schema("TNumber")
class TNumber(BaseModel):
    value: int


@fake_llm("t_bad_then_good")
def t_bad_then_good(ctx, prompt, try_number):
    return "sure! here you go" if try_number == 1 else '{"value": 42}'


@fake_llm("t_always_bad")
def t_always_bad(ctx, prompt, try_number):
    return '{"value": "not a number"}'


register_workflow(Workflow("t_flaky", 1, (
    StepSpec("a", "code", config={"fn": "t_flaky"}, max_attempts=3),
    StepSpec("b", "code", needs=("a",), config={"fn": "t_ok"}),
)))
register_workflow(Workflow("t_fatal", 1, (
    StepSpec("a", "code", config={"fn": "t_fatal"}),
    StepSpec("b", "code", needs=("a",), config={"fn": "t_ok"}),
)))
register_workflow(Workflow("t_exhaust", 1, (
    StepSpec("a", "code", config={"fn": "t_flaky", "succeed_on": 99}, max_attempts=2),
)))
register_workflow(Workflow("t_slow", 1, (
    StepSpec("a", "code", config={"fn": "t_slow", "seconds": 8}),
    StepSpec("b", "code", needs=("a",), config={"fn": "t_ok"}),
)))
register_workflow(Workflow("t_tool", 1, (
    StepSpec("a", "code", config={"fn": "t_tool_then_crash"}, max_attempts=3),
)))
register_workflow(Workflow("t_llm", 1, (
    StepSpec("a", "llm", config={"schema": "TNumber", "fake": "t_bad_then_good", "prompt": "give a number"}),
)))
register_workflow(Workflow("t_llm_bad", 1, (
    StepSpec("a", "llm", config={"schema": "TNumber", "fake": "t_always_bad", "prompt": "give a number"}),
)))
register_workflow(Workflow("t_poison", 1, (
    StepSpec("a", "code", config={"fn": "t_ok"}, max_attempts=2),
)))


def ledger_counts(pool) -> tuple[int, int]:
    with pool.connection() as conn:
        effects = conn.execute("SELECT count(*) AS n FROM demo_ledger").fetchone()["n"]
        requests = conn.execute("SELECT count(*) AS n FROM demo_ledger_requests").fetchone()["n"]
    return effects, requests


# ---------------------------------------------------------------- the invoice workflow


def test_small_invoice_runs_through_without_approval(pool, worker):
    run_id = runs.create_run(pool, "invoice_approval", {"invoice_text": SMALL_INVOICE})
    assert drive(worker, pool, run_id) == "done"
    s = steps(pool, run_id)
    assert s["extract"]["output"]["amount_inr"] == 12400
    assert s["approve"]["status"] == "skipped"
    assert s["post"]["output"]["applied"] is True
    assert "Posted invoice INV-1001" in s["summary"]["output"]["summary"]
    assert ledger_counts(pool) == (1, 1)


def test_big_invoice_waits_for_a_human_without_holding_a_worker(pool, worker):
    run_id = runs.create_run(pool, "invoice_approval", {"invoice_text": BIG_INVOICE})
    assert drive(worker, pool, run_id) == "waiting"
    approve = steps(pool, run_id)["approve"]
    assert approve["status"] == "waiting" and approve["lease_until"] is None
    assert worker.run_once() is False  # nothing to do while it waits
    with pool.connection() as conn:
        appr = conn.execute("SELECT id, request FROM approvals WHERE run_id = %s", (run_id,)).fetchone()
    assert appr["request"]["details"]["amount_inr"] == 62000
    assert "vendor not on the approved list" in appr["request"]["details"]["flags"]

    runs.decide_approval(pool, appr["id"], "approved", "arshdeep", "checked with vendor")
    assert drive(worker, pool, run_id) == "done"
    assert "after approval" in steps(pool, run_id)["summary"]["output"]["summary"]
    types = event_types(pool, run_id)
    assert types.index("run_waiting") < types.index("approval_given") < types.index("run_resumed")


def test_rejected_approval_fails_the_run_and_posts_nothing(pool, worker):
    run_id = runs.create_run(pool, "invoice_approval", {"invoice_text": BIG_INVOICE})
    drive(worker, pool, run_id)
    with pool.connection() as conn:
        appr_id = conn.execute("SELECT id FROM approvals WHERE run_id = %s", (run_id,)).fetchone()["id"]
    runs.decide_approval(pool, appr_id, "rejected", "arshdeep", "not our vendor")
    assert run_status(pool, run_id) == "failed"
    s = steps(pool, run_id)
    assert s["post"]["status"] == "cancelled" and s["summary"]["status"] == "cancelled"
    assert ledger_counts(pool) == (0, 0)
    with pytest.raises(runs.EngineError):
        runs.decide_approval(pool, appr_id, "approved", "someone")


def test_duplicate_invoice_is_not_posted_twice(pool, worker):
    first = runs.create_run(pool, "invoice_approval", {"invoice_text": SMALL_INVOICE})
    drive(worker, pool, first)
    second = runs.create_run(pool, "invoice_approval", {"invoice_text": SMALL_INVOICE})
    assert drive(worker, pool, second) == "done"
    s = steps(pool, second)
    assert s["check_rules"]["output"]["duplicate"] is True
    assert s["post"]["status"] == "skipped"
    assert "already posted" in s["summary"]["output"]["summary"]
    assert ledger_counts(pool) == (1, 1)


# ---------------------------------------------------------------- retries and failures


def test_transient_errors_are_retried_with_backoff(pool, worker):
    run_id = runs.create_run(pool, "t_flaky", {})
    assert drive(worker, pool, run_id) == "done"
    assert steps(pool, run_id)["a"]["output"] == {"ok_on_attempt": 3}
    assert event_types(pool, run_id).count("step_retry") == 2


def test_fatal_error_is_not_retried_and_stops_the_run(pool, worker):
    run_id = runs.create_run(pool, "t_fatal", {})
    assert drive(worker, pool, run_id) == "failed"
    s = steps(pool, run_id)
    assert s["a"]["attempt"] == 1 and s["a"]["status"] == "failed"
    assert s["b"]["status"] == "cancelled"


def test_retries_stop_at_max_attempts(pool, worker):
    run_id = runs.create_run(pool, "t_exhaust", {})
    assert drive(worker, pool, run_id) == "failed"
    assert steps(pool, run_id)["a"]["attempt"] == 2


# ---------------------------------------------------------------- leases, crashes, fencing


def test_dead_worker_step_is_reclaimed_and_its_late_result_is_discarded(pool, settings):
    run_id = runs.create_run(pool, "t_flaky", {}, overrides={"a": {"succeed_on": 1}})
    a, b = make_worker(pool, settings, "wa"), make_worker(pool, settings, "wb")
    claimed = runs.claim(pool, "wa", lease_s=0.2)  # worker A takes the step... and "dies"
    assert claimed["attempt"] == 1
    time.sleep(0.4)

    assert drive(b, pool, run_id) == "done"  # worker B takes over after the lease runs out
    s = steps(pool, run_id)
    assert s["a"]["attempt"] == 2 and s["a"]["worker_id"] == "wb"

    # Worker A wakes up and tries to report its old attempt: fenced off.
    assert runs.complete_step(pool, claimed["id"], 1, {"zombie": True}) is False
    assert steps(pool, run_id)["a"]["output"] == {"ok_on_attempt": 2}
    events = runs.events(pool, run_id)
    reclaim = next(e for e in events if e["type"] == "step_reclaimed")
    assert reclaim["data"]["from_worker"] == "wa" and reclaim["data"]["to_worker"] == "wb"
    assert any(e["type"] == "stale_result_discarded" for e in events)


def test_step_that_keeps_killing_workers_gives_up(pool, settings):
    run_id = runs.create_run(pool, "t_poison", {})
    for _ in range(2):
        assert runs.claim(pool, "doomed", lease_s=0.05)
        time.sleep(0.1)
    assert runs.claim(pool, "doomed", lease_s=0.05) is None
    assert run_status(pool, run_id) == "failed"
    assert "workers kept dying" in steps(pool, run_id)["a"]["error"]


def test_concurrent_workers_never_run_the_same_step_twice(pool, settings):
    run_ids = [runs.create_run(pool, "chaos", {"n": i}) for i in range(15)]
    fast = Settings(**{**settings.model_dump(), "lease_seconds": 30})
    workers = [make_worker(pool, fast, f"c{i}") for i in range(4)]
    stop = threading.Event()

    def loop(w):
        while not stop.is_set():
            if not w.run_once():
                time.sleep(0.01)

    threads = [threading.Thread(target=loop, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    end = time.monotonic() + 60
    while time.monotonic() < end and any(run_status(pool, r) != "done" for r in run_ids):
        time.sleep(0.1)
    stop.set()
    for t in threads:
        t.join()

    assert all(run_status(pool, r) == "done" for r in run_ids)
    with pool.connection() as conn:
        started = conn.execute("SELECT count(*) AS n FROM run_events WHERE type = 'step_started'").fetchone()["n"]
        busy = conn.execute("SELECT count(DISTINCT worker_id) AS n FROM step_runs").fetchone()["n"]
    assert started == 15 * 4  # every step started exactly once
    assert busy > 1  # and the work really was shared
    assert ledger_counts(pool) == (15, 15)


# ---------------------------------------------------------------- side effects


def test_side_effect_happens_once_even_when_the_step_is_retried(pool, worker):
    run_id = runs.create_run(pool, "t_tool", {})
    assert drive(worker, pool, run_id) == "done"
    assert steps(pool, run_id)["a"]["attempt"] == 2
    assert ledger_counts(pool) == (1, 1)  # the retry reused the recorded result
    assert "tool_result_reused" in event_types(pool, run_id)


def test_ledger_dedupes_a_repeated_key(pool, worker):
    """The window between "the API did it" and "we recorded it": the key makes the API dedupe."""
    from engine.workflow import TOOLS
    run_id = runs.create_run(pool, "t_tool", {})
    ctx = type("Ctx", (), {"pool": pool, "run_id": run_id})()
    first = TOOLS["post_to_ledger"](ctx, {"vendor": "x", "amount_inr": 1, "invoice_no": "1"}, "k1")
    again = TOOLS["post_to_ledger"](ctx, {"vendor": "x", "amount_inr": 1, "invoice_no": "1"}, "k1")
    assert first["applied"] is True and again["applied"] is False
    assert ledger_counts(pool) == (1, 2)


# ---------------------------------------------------------------- cancel, replay, budget


def test_cancel_stops_a_running_step(pool, settings):
    run_id = runs.create_run(pool, "t_slow", {})
    w = make_worker(pool, Settings(**{**settings.model_dump(), "lease_seconds": 0.6}), "wc")
    t = threading.Thread(target=w.run_once)
    t.start()
    time.sleep(0.5)
    assert steps(pool, run_id)["a"]["status"] == "running"
    began = time.monotonic()
    runs.cancel_run(pool, run_id)
    t.join(timeout=5)
    assert time.monotonic() - began < 2
    assert run_status(pool, run_id) == "cancelled"
    s = steps(pool, run_id)
    assert s["a"]["status"] == "cancelled" and s["b"]["status"] == "cancelled"


def test_replay_reuses_earlier_steps_and_never_repeats_their_side_effects(pool, worker):
    original = runs.create_run(pool, "invoice_approval", {"invoice_text": SMALL_INVOICE})
    drive(worker, pool, original)
    replay = runs.replay_run(pool, original, "summary", overrides={"summary": {"system": "Be brief."}})
    assert drive(worker, pool, replay) == "done"
    s = steps(pool, replay)
    assert all(s[k]["reused"] for k in ("extract", "check_rules", "post"))
    assert not s["summary"]["reused"] and s["summary"]["status"] == "done"
    assert s["approve"]["status"] == "skipped"
    assert ledger_counts(pool) == (1, 1)
    with pool.connection() as conn:
        row = conn.execute("SELECT replay_of, overrides FROM runs WHERE id = %s", (replay,)).fetchone()
    assert row["replay_of"] == original and row["overrides"]["summary"]["system"] == "Be brief."


def test_replay_from_the_middle_reruns_downstream_only(pool, worker):
    original = runs.create_run(pool, "invoice_approval", {"invoice_text": SMALL_INVOICE})
    drive(worker, pool, original)
    replay = runs.replay_run(pool, original, "check_rules")
    drive(worker, pool, replay)
    s = steps(pool, replay)
    assert s["extract"]["reused"] and not s["check_rules"]["reused"]
    # The first run already posted it, so the rerun sees a duplicate and skips posting.
    assert s["check_rules"]["output"]["duplicate"] is True and s["post"]["status"] == "skipped"


def test_budget_is_enforced(pool, settings):
    pricey = Settings(**{**settings.model_dump(), "llm_price_in_per_m": 1_000_000, "llm_price_out_per_m": 0})
    w = make_worker(pool, pricey, "wp")
    run_id = runs.create_run(pool, "invoice_approval", {"invoice_text": SMALL_INVOICE}, budget_inr=1.0)
    assert drive(w, pool, run_id) == "failed"
    with pool.connection() as conn:
        assert "budget exceeded" in conn.execute("SELECT error FROM runs WHERE id = %s", (run_id,)).fetchone()["error"]
    assert ledger_counts(pool) == (0, 0)


# ---------------------------------------------------------------- LLM output validation


def test_invalid_model_reply_is_retried_with_the_error(pool, worker):
    run_id = runs.create_run(pool, "t_llm", {})
    assert drive(worker, pool, run_id) == "done"
    assert steps(pool, run_id)["a"]["output"] == {"value": 42}
    assert "step_log" in event_types(pool, run_id)


def test_model_that_never_validates_fails_without_step_retries(pool, worker):
    run_id = runs.create_run(pool, "t_llm_bad", {})
    assert drive(worker, pool, run_id) == "failed"
    a = steps(pool, run_id)["a"]
    assert a["attempt"] == 1 and "failed schema validation 3 times" in a["error"]


# ---------------------------------------------------------------- event log and workflows


def test_status_rebuilt_from_events_matches_the_tables(pool, worker):
    ids = [
        runs.create_run(pool, "invoice_approval", {"invoice_text": SMALL_INVOICE}),
        runs.create_run(pool, "invoice_approval", {"invoice_text": BIG_INVOICE}),  # waits
        runs.create_run(pool, "t_flaky", {}),
        runs.create_run(pool, "t_fatal", {}),
    ]
    for r in ids:
        drive(worker, pool, r)
    cancelled = runs.create_run(pool, "t_flaky", {})
    runs.cancel_run(pool, cancelled)
    ids.append(cancelled)
    for r in ids:
        rebuilt = runs.rebuild(runs.events(pool, r), list(steps(pool, r)))
        assert rebuilt["run"] == run_status(pool, r)
        assert rebuilt["steps"] == {k: v["status"] for k, v in steps(pool, r).items()}, r


def test_changing_a_stored_workflow_version_is_refused(pool):
    runs.ensure_workflow(pool, Workflow("t_versioned", 1, (StepSpec("a", "code", config={"fn": "t_ok"}),)))
    changed = Workflow("t_versioned", 1, (StepSpec("a", "code", config={"fn": "t_flaky"}),))
    with pytest.raises(runs.EngineError, match="bump its version"):
        runs.ensure_workflow(pool, changed)
    runs.ensure_workflow(pool, Workflow("t_versioned", 2, changed.steps))  # a new version is fine


def test_runs_in_flight_keep_their_workflow_version(pool, worker):
    v1 = Workflow("t_pinned", 1, (StepSpec("a", "code", config={"fn": "t_ok"}),))
    register_workflow(v1)
    run_id = runs.create_run(pool, "t_pinned", {})
    register_workflow(Workflow("t_pinned", 2, (StepSpec("a", "code", config={"fn": "t_ok"}),
                                               StepSpec("extra", "code", needs=("a",), config={"fn": "t_ok"}))))
    drive(worker, pool, run_id)
    assert set(steps(pool, run_id)) == {"a"}


def test_invalid_workflows_are_rejected():
    with pytest.raises(ValueError, match="cycle"):
        Workflow("bad", 1, (StepSpec("a", "code", needs=("b",)), StepSpec("b", "code", needs=("a",))))
    with pytest.raises(ValueError, match="unknown step"):
        Workflow("bad", 1, (StepSpec("a", "code", needs=("zzz",)),))
    with pytest.raises(ValueError, match="unknown kind"):
        Workflow("bad", 1, (StepSpec("a", "magic"),))


def test_cut_off_model_reply_is_retried_with_a_bigger_limit(pool, settings):
    """Live mode: a reply that hit max_tokens is retried with twice the room, not re-validated."""
    from engine.llm import Reply
    live = Settings(**{**settings.model_dump(), "llm_mode": "live", "llm_max_tokens": 1000})
    w = make_worker(pool, live, "wl")
    seen = []

    def complete(messages, max_tokens=0, attempts=3):
        seen.append(max_tokens)
        if len(seen) == 1:
            return Reply('{"value": 4', 10, 1000, truncated=True)
        return Reply('{"value": 42}', 10, 5)

    w.chat.complete = complete
    run_id = runs.create_run(pool, "t_llm", {})
    assert drive(w, pool, run_id) == "done"
    assert seen == [1000, 2000]
    assert steps(pool, run_id)["a"]["output"] == {"value": 42}
