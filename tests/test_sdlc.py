"""The SDLC pipeline, offline (fake model replies) but with the real Docker sandbox and gates."""

import json

import pytest

from engine import runs
from engine.workflow import fake_llm
from sdlc import fakes, gates, repo, sandbox
from sdlc.schemas import Design
from sdlc import workflow as sdlc_workflow
from sdlc.workflow import traceability
from tests.conftest import drive, event_types, run_status, steps

needs_sandbox = pytest.mark.skipif(not sandbox.available(), reason="sandbox image not built "
                                   "(docker build -t agentops-sandbox:2 sandbox)")
TASK = ("Let staff refund paid orders, fully or in parts, but never more than was paid. "
        "Refunding an unpaid order must fail. Keep a record of each refund.")
BASE = repo.load(repo.example_dir("shop"))


@pytest.fixture(autouse=True)
def results_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sdlc_workflow, "RESULTS", tmp_path / "sdlc")


@fake_llm("t_build_never_right")
def _never_right(ctx, prompt, try_number):
    return json.dumps({"files": [{"path": "shop/refunds.py", "content": fakes.REFUNDS_WRONG},
                                 {"path": "shop/orders.py", "content": fakes.ORDERS}]})


@fake_llm("t_tests_touch_conftest")
def _tests_bad_path(ctx, prompt, try_number):
    return json.dumps({"files": [{"path": "tests/conftest.py", "content": "x = 1\n"}],
                       "coverage_map": [{"ac_id": "AC-1", "tests": ["test_x"]}]})


def approval_id(pool, run_id, step_key):
    with pool.connection() as conn:
        return conn.execute("SELECT id FROM approvals WHERE run_id = %s AND step_key = %s",
                            (run_id, step_key)).fetchone()["id"]


def go(worker, pool, run_id):
    return drive(worker, pool, run_id, timeout=180)


# ---------------------------------------------------------------- the whole pipeline


@needs_sandbox
def test_task_goes_from_requirements_to_merged_code(pool, worker):
    run_id = runs.create_run(pool, "sdlc", {"task": TASK, "example": "shop"})

    assert go(worker, pool, run_id) == "waiting"
    assert "AC-3" in steps(pool, run_id)["requirements"]["output"]["markdown"]
    runs.decide_approval(pool, approval_id(pool, run_id, "approve_requirements"), "approved", "arshdeep")

    assert go(worker, pool, run_id) == "waiting"
    runs.decide_approval(pool, approval_id(pool, run_id, "approve_design"), "approved", "arshdeep")

    assert go(worker, pool, run_id) == "waiting"  # stops for the merge approval
    s = steps(pool, run_id)
    # Tests were red first, then locked.
    assert s["write_tests"]["output"]["red"]["collection_error"] is True
    assert set(s["write_tests"]["output"]["lock"]) == {"tests/test_refunds.py"}
    # The first build forgot the refund limit; the gates caught it and sent it back once.
    assert s["build"]["iteration"] == 1
    types = event_types(pool, run_id)
    assert types.count("gate_failed") == 1 and "step_rewound" in types
    first_failure = next(e for e in runs.events(pool, run_id) if e["type"] == "gate_failed")
    failed = first_failure["data"]["feedback"]["failed_checks"]
    assert [c["name"] for c in failed] == ["tests"]
    assert any("test_refund_more_than_left_is_rejected" in f for f in failed[0]["failed"])
    # Second build passed every gate.
    report = s["quality_gates"]["output"]
    assert report["passed"] and {c["name"] for c in report["checks"]} >= {
        "test_lock", "scope_lock", "secrets", "syntax", "tests", "coverage", "lint", "types", "security"}
    # The review comment that pointed at a file outside the diff was dropped.
    assert len(s["review"]["output"]["dropped_ungrounded"]) == 1
    # Traceability: every criterion → its tests → the changed lines they ran.
    matrix = s["release"]["output"]["matrix"]
    assert [r["ac"] for r in matrix] == ["AC-1", "AC-2", "AC-3", "AC-4", "AC-5"]
    assert all(r["tests"] and any(c.startswith("shop/refunds.py:") for c in r["code"]) for r in matrix)
    # The PR says who really approved what.
    pr = s["release"]["output"]["markdown"]
    assert "Requirements approved by arshdeep." in pr and "Design approved by arshdeep." in pr
    assert "Merge waits for a person's approval." in pr

    runs.decide_approval(pool, approval_id(pool, run_id, "approve_merge"), "approved", "arshdeep")
    assert go(worker, pool, run_id) == "done"
    out = sdlc_workflow.RESULTS / str(run_id)
    assert (out / "PR.md").read_text(encoding="utf-8").startswith("# ")
    assert "+    left = order.refundable()" in (out / "changes.patch").read_text(encoding="utf-8")
    assert (out / "merged" / "shop" / "refunds.py").exists()
    with pool.connection() as conn:
        names = {r["name"] for r in conn.execute("SELECT name FROM artifacts WHERE run_id = %s", (run_id,))}
    assert names >= {"requirements.md", "design.md", "red-test-run.txt", "gate-report.json", "changes.diff",
                     "review.md", "PR.md"}
    # The event log still explains everything, loops included.
    rebuilt = runs.rebuild(runs.events(pool, run_id), list(s))
    assert rebuilt["run"] == "done"
    assert rebuilt["steps"] == {k: v["status"] for k, v in steps(pool, run_id).items()}


@needs_sandbox
def test_auto_approve_skips_the_human_gates(pool, worker):
    run_id = runs.create_run(pool, "sdlc", {"task": TASK, "example": "shop", "auto_approve": True})
    assert go(worker, pool, run_id) == "done"
    s = steps(pool, run_id)
    assert all(s[k]["status"] == "skipped" for k in ("approve_requirements", "approve_design", "approve_merge"))
    # Nobody approved anything, and the PR must not claim otherwise.
    pr = s["release"]["output"]["markdown"]
    assert "approved by a person" not in pr.replace("not approved by a person", "")
    assert pr.count("not approved by a person (approval skipped for this run)") == 3


@needs_sandbox
def test_shoplite_gets_discount_coupons_frontend_and_backend(pool, worker):
    """The demo: a one-sentence task on the ShopLite web shop, through every gate, to merged code."""
    from sdlc import fakes_shoplite

    run_id = runs.create_run(pool, "sdlc", {"task": fakes_shoplite.TASK, "example": "shoplite",
                                            "auto_approve": True})
    assert go(worker, pool, run_id) == "done"
    s = steps(pool, run_id)
    assert s["write_tests"]["output"]["red"]["red_tests"]  # all 5 failed before the code existed
    assert s["build"]["iteration"] == 1  # first build refused "save10"; the locked tests sent it back
    failed = next(e for e in runs.events(pool, run_id) if e["type"] == "gate_failed")["data"]["feedback"]
    assert any("test_coupon_codes_ignore_case_and_spaces" in f for f in failed["failed_checks"][0]["failed"])
    report = s["quality_gates"]["output"]
    assert report["passed"] and report["tests_run"] == 12  # 7 existing shop tests + 5 new ones
    assert "shoplite/static/index.html" in report["files_changed"]  # the page changed too
    matrix = s["release"]["output"]["matrix"]
    assert [r["ac"] for r in matrix] == ["AC-1", "AC-2", "AC-3", "AC-4", "AC-5"]
    assert any(c.startswith("shoplite/coupons.py:") for c in matrix[1]["code"])
    merged = sdlc_workflow.RESULTS / str(run_id) / "merged"
    assert 'id="coupon-code"' in (merged / "shoplite" / "static" / "index.html").read_text(encoding="utf-8")
    assert "code.strip().upper()" in (merged / "shoplite" / "coupons.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- loops


def test_request_changes_sends_requirements_back_with_the_note(pool, worker):
    run_id = runs.create_run(pool, "sdlc", {"task": TASK, "example": "shop"})
    go(worker, pool, run_id)
    appr = approval_id(pool, run_id, "approve_requirements")
    with pytest.raises(runs.EngineError, match="say what should change"):
        runs.decide_approval(pool, appr, "changes", "arshdeep", "")
    runs.decide_approval(pool, appr, "changes", "arshdeep", "Partial refunds of line items too")
    r = steps(pool, run_id)["requirements"]
    assert r["iteration"] == 1 and r["status"] == "queued"
    assert r["feedback"]["human_feedback"] == "Partial refunds of line items too"
    assert r["feedback"]["previous_output"]["summary"]  # the model sees what it wrote last time
    assert steps(pool, run_id)["design"]["iteration"] == 0  # never ran, so it isn't on a new round
    assert go(worker, pool, run_id) == "waiting"  # asked again
    with pool.connection() as conn:
        again = conn.execute("SELECT decision FROM approvals WHERE id = %s", (appr,)).fetchone()
    assert again["decision"] is None


@needs_sandbox
def test_gate_loop_budget_runs_out_and_a_person_must_look(pool, worker):
    run_id = runs.create_run(pool, "sdlc", {"task": TASK, "example": "shop", "auto_approve": True},
                             overrides={"build": {"fake": "t_build_never_right"}})
    assert go(worker, pool, run_id) == "failed"
    s = steps(pool, run_id)
    assert s["build"]["iteration"] == 4  # 1 try + 4 loops
    assert "a person needs to look" in s["quality_gates"]["error"]
    assert s["release"]["status"] == "cancelled"


def test_tests_that_touch_protected_files_are_rejected(pool, worker):
    run_id = runs.create_run(pool, "sdlc", {"task": TASK, "example": "shop", "auto_approve": True},
                             overrides={"write_tests": {"fake": "t_tests_touch_conftest"}})
    assert go(worker, pool, run_id) == "failed"
    feedback = next(e for e in runs.events(pool, run_id) if e["type"] == "gate_failed")["data"]["feedback"]
    assert any("not one of the test files" in p for p in feedback["problems"])
    assert any("protected" in p for p in feedback["problems"])
    assert steps(pool, run_id)["write_tests"]["iteration"] == 3


# ---------------------------------------------------------------- gates directly


@needs_sandbox
def test_red_gate_rejects_tests_that_already_pass():
    design = {"test_files": ["tests/test_new.py"]}
    tests = {"tests/test_new.py": "from shop.orders import Order\n\n\ndef test_status():\n"
                                  "    assert Order(id='x').status == 'pending'\n"}
    passed, report = gates.red_check(BASE, tests, design, [{"ac_id": "AC-1", "tests": ["test_status"]}], ["AC-1"])
    assert not passed and report["passing_before_code"] == ["test_status"]
    assert report["criteria_without_failing_test"] == ["AC-1"]


@needs_sandbox
def test_red_gate_allows_regression_tests_next_to_a_failing_one():
    design = {"test_files": ["tests/test_new.py"]}
    tests = {"tests/test_new.py": "from shop.orders import Order\n\n\ndef test_status():\n"
                                  "    assert Order(id='x').status == 'pending'\n\n\n"
                                  "def test_refund():\n    from shop.refunds import refund\n    assert refund\n"}
    cmap = [{"ac_id": "AC-1", "tests": ["test_status", "test_refund"]}]
    passed, report = gates.red_check(BASE, tests, design, cmap, ["AC-1"])
    assert passed, report
    assert report["red_tests"] == ["test_refund"] and report["regression_tests"] == ["test_status"]


def test_red_gate_rejects_placeholder_tests_before_running_them():
    # A live model once wrote "pass" bodies when it couldn't reach an exact value; they can never fail.
    design = {"test_files": ["tests/test_new.py"]}
    tests = {"tests/test_new.py": "import pytest\n\n\ndef test_todo():\n    # check the discount later\n    pass\n\n\n"
                                  "def test_raises():\n    with pytest.raises(ValueError):\n        int('x')\n\n\n"
                                  "def test_real():\n    assert 1 + 1 == 2\n"}
    cmap = [{"ac_id": "AC-1", "tests": ["test_todo", "test_raises", "test_real"]}]
    passed, report = gates.red_check(BASE, tests, design, cmap, ["AC-1"])
    assert not passed
    assert [p for p in report["problems"] if "checks nothing" in p] == [
        "tests/test_new.py::test_todo checks nothing (no assert or pytest.raises): make it call the code "
        "and assert on the result"]


def test_quality_gate_catches_tampering_scope_and_secrets():
    design = {"files": [{"path": "shop/refunds.py"}]}
    tests = {"tests/test_refunds.py": fakes.TESTS}
    lock = {"tests/test_refunds.py": gates.sha256(fakes.TESTS)}
    code = {"shop/refunds.py": fakes.REFUNDS_RIGHT, "tests/test_refunds.py": "def test_x():\n    pass\n",
            "shop/extra.py": 'KEY = "nvapi-' + "a" * 30 + '"\n', "pyproject.toml": "[tool]\n"}
    report = gates.quality_check(BASE, {**tests, "tests/test_refunds.py": "changed"}, code, design, lock)
    assert not report["passed"]
    assert set(report["failed"]) == {"test_lock", "scope_lock", "secrets"}
    assert "sandbox checks skipped" in report["skipped"]  # nothing ran in Docker


@needs_sandbox
def test_quality_gate_flags_lint_types_and_uncovered_lines():
    design = {"files": [{"path": "shop/refunds.py"}, {"path": "shop/orders.py"}]}
    tests = {"tests/test_refunds.py": fakes.TESTS}
    sloppy = fakes.REFUNDS_RIGHT + "\n\nimport os\n\n\ndef unused(x: int) -> str:\n    return x + 1\n"
    report = gates.quality_check(BASE, tests, {"shop/refunds.py": sloppy, "shop/orders.py": fakes.ORDERS},
                                 design, {"tests/test_refunds.py": gates.sha256(fakes.TESTS)})
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["tests"]["passed"]
    assert not by_name["lint"]["passed"] and "F401" in by_name["lint"]["detail"]
    assert not by_name["types"]["passed"]
    assert by_name["coverage"]["changed_lines_covered"] < 1.0


def test_paths_and_design_rules():
    for bad in ("../etc/passwd", "/abs.py", "a;rm -rf.py", "a b.py", ".git/config"):
        with pytest.raises(repo.RepoError):
            repo.safe_path(bad)
    assert repo.is_protected("tests/conftest.py") and repo.is_protected("requirements-dev.txt")
    assert not repo.is_protected("shop/orders.py")
    good = dict(fakes.DESIGN)
    with pytest.raises(ValueError, match="protected"):
        Design.model_validate({**good, "files": [{"path": "pyproject.toml", "change": "modify", "purpose": "x x"}]})
    with pytest.raises(ValueError, match="tests/test_"):
        Design.model_validate({**good, "test_files": ["shop/check.py"]})


def test_traceability_maps_criteria_to_tests_to_lines():
    req = {"acceptance_criteria": [{"id": "AC-1", "story_id": "US-1", "then": "x"},
                                   {"id": "AC-2", "story_id": "US-1", "then": "y"}]}
    rows = traceability(req, [{"ac_id": "AC-1", "tests": ["test_a[0]", "test_b"]}],
                        {"test_a": {"shop/r.py": [3, 4, 5]}, "test_b": {"shop/r.py": [9]}})
    assert rows[0]["tests"] == ["test_a", "test_b"] and rows[0]["code"] == ["shop/r.py:3-5, 9"]
    assert rows[1]["tests"] == [] and rows[1]["code"] == []


def test_unknown_example_fails_fast(pool, worker):
    run_id = runs.create_run(pool, "sdlc", {"task": TASK, "example": "../../etc"})
    assert go(worker, pool, run_id) == "failed"
    assert run_status(pool, run_id) == "failed"


# ---------------------------------------------------------------- AI review is advisory


@fake_llm("t_review_always_blocks")
def _review_blocks(ctx, prompt, try_number):
    return json.dumps({"verdict": "changes", "comments": [
        {"file": "shop/refunds.py", "line": 1, "severity": "blocking",
         "comment": f"I still don't like this (earlier rounds I remember: {len(ctx.history)})",
         "scenario": "refund(paid_order(), 10) returns the wrong thing"}]})


@needs_sandbox
def test_review_that_never_approves_escalates_to_the_person_instead_of_failing(pool, worker):
    run_id = runs.create_run(pool, "sdlc", {"task": TASK, "example": "shop", "auto_approve": True},
                             overrides={"review": {"fake": "t_review_always_blocks"}})
    assert go(worker, pool, run_id) == "done"
    s = steps(pool, run_id)
    review = s["review"]["output"]
    # The loop budget is on build rounds (3), shared with the quality gates, which used one
    # (the first build is wrong on purpose). So the reviewer ran 3 times.
    assert review["escalated"] and s["review"]["iteration"] == 2 and s["build"]["iteration"] == 3
    # Each round the reviewer saw its own earlier rounds.
    assert "earlier rounds I remember: 2" in review["unresolved"]["comments"][0]["comment"]
    assert "## Needs your decision" in s["release"]["output"]["markdown"]
    assert "gate_escalated" in event_types(pool, run_id)


def test_blocking_review_comment_needs_a_scenario():
    from sdlc.schemas import Review
    with pytest.raises(ValueError, match="needs a scenario"):
        Review.model_validate({"verdict": "changes", "comments": [
            {"file": "a.py", "line": 1, "severity": "blocking", "comment": "this looks wrong"}]})
    Review.model_validate({"verdict": "approve", "comments": [
        {"file": "a.py", "line": 1, "severity": "suggestion", "comment": "could be cleaner"}]})


@fake_llm("t_review_repeats")
def _review_repeats(ctx, prompt, try_number):
    return json.dumps({"verdict": "changes", "comments": [
        {"file": "shop/refunds.py", "line": 1, "severity": "blocking", "comment": "Same point as before, unchanged.",
         "scenario": "refund(paid_order(), 10) returns the wrong thing"}]})


@needs_sandbox
def test_reviewer_repeating_itself_escalates_without_burning_rounds(pool, worker):
    run_id = runs.create_run(pool, "sdlc", {"task": TASK, "example": "shop", "auto_approve": True},
                             overrides={"review": {"fake": "t_review_repeats"}})
    assert go(worker, pool, run_id) == "done"
    s = steps(pool, run_id)
    assert s["review"]["output"]["escalated"]
    assert s["review"]["iteration"] == 1  # reviewed twice, saw the same comment, stopped
    assert "repeated last round" in s["review"]["output"]["unresolved"]["summary"]


def test_coverage_map_accepts_qualified_test_names():
    assert gates.test_name("tests/test_r.py::TestRefunds::test_full[0]") == "test_full"
    assert gates.test_name("TestRefunds.test_full") == "test_full"
    assert gates.test_name("test_full") == "test_full"
