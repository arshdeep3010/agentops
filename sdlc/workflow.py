"""The SDLC pipeline as an AgentOps workflow. AI proposes, gates decide.

  load_repo → requirements → [approve] → design → [approve] → write_tests (red gate, locked)
            → build ⇄ quality_gates → review → release → [approve merge] → merge

Loops: a failed gate sends work back with its evidence (on_fail.rewind_to), and a person can
"request changes" at any approval. Every loop has a budget; when it runs out, a person decides.
"""

import json
from pathlib import Path

from engine.config import ROOT, get_settings
from engine.context import FatalError, GateFailed, StepContext
from engine.workflow import StepSpec, Workflow, code, gate, register_workflow, tool
from sdlc import fakes  # noqa: F401 - registers the offline model replies
from sdlc import gates, prompts, repo, sandbox
from sdlc import schemas  # noqa: F401 - registers the output schemas

# On Lambda only /tmp is writable, so RESULTS_DIR moves this there (it's a scratch copy then:
# everything a person needs is also stored as run artifacts in Postgres).
RESULTS = Path(get_settings().results_dir) if get_settings().results_dir else ROOT / "results" / "sdlc"
NO_AUTO = {"ref": "input.auto_approve", "op": "falsy"}  # benchmarks may skip the human gates


def _shown(path) -> str:
    """Repo-relative when possible, so results don't leak home-folder paths into PR text."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _files(step_output: dict | None) -> dict[str, str]:
    return {f["path"]: f["content"] for f in (step_output or {}).get("files", [])}


# ---------------------------------------------------------------- phase 0: the repo

@code("sdlc_load_repo")
def load_repo(ctx: StepContext) -> dict:
    try:
        base = repo.load(repo.example_dir(ctx.input.get("example", "")))
    except repo.RepoError as exc:
        raise FatalError(str(exc)) from exc
    listing = "\n\n".join(f"### {path}\n```\n{text}\n```" for path, text in base.items())
    return {"example": ctx.input["example"], "files": base, "listing": listing, "file_count": len(base)}


# ---------------------------------------------------------------- phases 1–2: requirements, design

def _requirements_md(r: dict) -> str:
    lines = [f"# Requirements\n\n{r['summary']}\n\n## User stories"]
    lines += [f"- **{s['id']}** As a {s['as_a']}, I want {s['i_want']}, so that {s['so_that']}."
              for s in r["user_stories"]]
    lines.append("\n## Acceptance criteria")
    lines += [f"- **{a['id']}** ({a['story_id']}) Given {a['given']}, when {a['when']}, then {a['then']}."
              for a in r["acceptance_criteria"]]
    if r.get("open_questions"):
        lines.append("\n## Open questions\n" + "\n".join(f"- {q}" for q in r["open_questions"]))
    if r.get("out_of_scope"):
        lines.append("\n## Out of scope\n" + "\n".join(f"- {q}" for q in r["out_of_scope"]))
    return "\n".join(lines)


@gate("sdlc_check_requirements")
def check_requirements(ctx: StepContext, output: dict) -> tuple[bool, dict]:
    output["markdown"] = _requirements_md(output)
    ctx.save_artifact("requirements.md", output["markdown"])
    return True, {}


def _design_md(d: dict) -> str:
    lines = [f"# Design\n\n{d['approach']}\n\n## Files"]
    lines += [f"- `{f['path']}` ({f['change']}): {f['purpose']}" for f in d["files"]]
    lines.append("\n## Tests\n" + "\n".join(f"- `{t}`" for t in d["test_files"]))
    if d.get("interfaces"):
        lines.append("\n## Interfaces")
        lines += [f"- `{i['signature']}`: {i['description']}" for i in d["interfaces"]]
    if d.get("risks"):
        lines.append("\n## Risks\n" + "\n".join(f"- {r}" for r in d["risks"]))
    adr = d["adr"]
    lines.append(f"\n## ADR: {adr['title']}\n\n**Decision:** {adr['decision']}")
    if adr.get("alternatives"):
        lines.append("**Alternatives:** " + "; ".join(adr["alternatives"]))
    if adr.get("consequences"):
        lines.append(f"**Consequences:** {adr['consequences']}")
    return "\n".join(lines)


@gate("sdlc_check_design")
def check_design(ctx: StepContext, output: dict) -> tuple[bool, dict]:
    base = ctx.outputs["load_repo"]["files"]
    problems = []
    for f in output["files"]:
        if f["change"] == "modify" and f["path"] not in base:
            problems.append(f"{f['path']} is marked modify but doesn't exist")
        if f["change"] == "create" and f["path"] in base:
            problems.append(f"{f['path']} is marked create but already exists")
    for t in output["test_files"]:
        if t in base:
            problems.append(f"{t} already exists; new tests go in a new file")
    if problems:
        return False, {"summary": "the design doesn't match the repository", "problems": problems}
    output["markdown"] = _design_md(output)
    ctx.save_artifact("design.md", output["markdown"])
    return True, {}


# ---------------------------------------------------------------- phase 3: tests first

@gate("sdlc_check_tests_red")
def check_tests_red(ctx: StepContext, output: dict) -> tuple[bool, dict]:
    ac_ids = [a["id"] for a in ctx.outputs["requirements"]["acceptance_criteria"]]
    try:
        passed, report = gates.red_check(ctx.outputs["load_repo"]["files"], _files(output),
                                         ctx.outputs["design"], output["coverage_map"], ac_ids)
    except sandbox.SandboxUnavailable as exc:
        raise FatalError(str(exc)) from exc
    output["red"] = {k: v for k, v in report.items() if k != "lock"}
    if passed:
        output["lock"] = report["lock"]  # from now on these exact bytes are the spec
        ctx.save_artifact("red-test-run.txt", report.get("pytest", ""), "text/plain")
    return passed, report


# ---------------------------------------------------------------- phases 4–5: build, gates

@code("sdlc_quality_gates")
def quality_gates(ctx: StepContext) -> dict:
    base = ctx.outputs["load_repo"]["files"]
    tests, code_files = _files(ctx.outputs["write_tests"]), _files(ctx.outputs["build"])
    try:
        report = gates.quality_check(base, tests, code_files, ctx.outputs["design"],
                                     ctx.outputs["write_tests"]["lock"])
    except sandbox.SandboxUnavailable as exc:
        raise FatalError(str(exc)) from exc
    diff = repo.unified_diff(base, {**tests, **code_files})
    output = {**report, "diff": diff, "files_changed": sorted(code_files), "build_iteration": ctx.iteration}
    ctx.save_artifact("gate-report.json", json.dumps(report, indent=2), "application/json")
    ctx.save_artifact("changes.diff", diff, "text/x-diff")
    if not report["passed"]:
        failed = [c for c in report["checks"] if not c["passed"]]
        raise GateFailed({"summary": report["summary"], "failed_checks": failed}, output)
    return output


# ---------------------------------------------------------------- phase 6: review

@gate("sdlc_check_review")
def check_review(ctx: StepContext, output: dict) -> tuple[bool, dict]:
    """Keep only comments that point at a real line of a changed file (same grounding rule as
    FitCheck). Blocking comments send the change back to build."""
    code_files = _files(ctx.outputs["build"])
    kept, dropped = [], []
    for c in output["comments"]:
        text = code_files.get(c["file"])
        (kept if text is not None and c["line"] <= len(text.splitlines()) else dropped).append(c)
    output["comments"], output["dropped_ungrounded"] = kept, dropped
    blocking = [c for c in kept if c["severity"] == "blocking"]
    md = ["# Review", f"Verdict: {output['verdict']}", ""]
    md += [f"- **{c['severity']}** `{c['file']}:{c['line']}`: {c['comment']}" for c in kept] or ["No comments."]
    if dropped:
        md.append(f"\n{len(dropped)} comment(s) dropped: they didn't point at a changed line.")
    ctx.save_artifact("review.md", "\n".join(md))
    if blocking:
        feedback = {"summary": f"{len(blocking)} blocking review comment(s)", "comments": blocking}
        # Same blocking points as last round, word for word: another round won't converge.
        last = {(c["file"], c["comment"].strip()) for c in (ctx.history[-1].get("comments", []) if ctx.history else [])}
        if last and all((c["file"], c["comment"].strip()) in last for c in blocking):
            feedback.update(summary="the reviewer repeated last round's blocking comments", escalate_now=True)
        return False, feedback
    return True, {}


# ---------------------------------------------------------------- phase 7: release

def traceability(requirements: dict, coverage_map: list[dict], by_test: dict) -> list[dict]:
    """Each acceptance criterion → the tests that check it → the changed lines those tests ran."""
    tests_for = {}
    for e in coverage_map:
        tests_for.setdefault(e["ac_id"], []).extend(gates.test_name(t) for t in e["tests"])
    rows = []
    for ac in requirements["acceptance_criteria"]:
        names = sorted(set(tests_for.get(ac["id"], [])))
        lines: dict[str, set[int]] = {}
        for t in names:
            for path, ls in by_test.get(t, {}).items():
                lines.setdefault(path, set()).update(ls)
        rows.append({"ac": ac["id"], "story": ac["story_id"], "then": ac["then"], "tests": names,
                     "code": [f"{p}:{repo.ranges(ls)}" for p, ls in sorted(lines.items())]})
    return rows


def _pr_markdown(ctx: StepContext, matrix: list[dict]) -> tuple[str, str]:
    req, design = ctx.outputs["requirements"], ctx.outputs["design"]
    gates_out, review = ctx.outputs["quality_gates"], ctx.outputs["review"]
    title = ctx.input.get("title") or req["title"]
    md = [f"# {title}", "", req["summary"], "", "## What changed"]
    md += [f"- `{f['path']}` ({f['change']}): {f['purpose']}" for f in design["files"]]
    md += [f"- `{t}` (new tests)" for t in design["test_files"]]
    md += ["", "## Design decision", f"**{design['adr']['title']}:** {design['adr']['decision']}", "",
           "## Quality gates", "| Gate | Result |", "|---|---|"]
    md += [f"| {c['name']} | {'pass' if c['passed'] else 'FAIL'} |" for c in gates_out["checks"]]
    cov = next((c for c in gates_out["checks"] if c["name"] == "coverage"), {})
    md += ["", f"{gates_out.get('tests_run', 0)} tests ran; {cov.get('changed_lines_covered', 0):.0%} of changed "
           f"lines covered. Build attempts: {gates_out.get('build_iteration', 0) + 1}.", "",
           "## Traceability", "| Criterion | Then | Tests | Code |", "|---|---|---|---|"]
    md += [f"| {r['ac']} ({r['story']}) | {r['then']} | {', '.join(f'`{t}`' for t in r['tests'])} | "
           f"{', '.join(f'`{c}`' for c in r['code']) or '–'} |" for r in matrix]
    md += ["", "## Review", f"Verdict: **{review['verdict']}**"]
    md += [f"- {c['severity']}: `{c['file']}:{c['line']}` {c['comment']}" for c in review["comments"]]
    if review.get("escalated"):
        md += ["", "## Needs your decision",
               "The AI reviewer still had blocking comments after 3 rounds. All automated gates passed. "
               "Decide at the merge approval:"]
        md += [f"- `{c['file']}:{c['line']}` {c['comment']} (scenario: {c.get('scenario', '')})"
               for c in review["unresolved"].get("comments", [])]
    md += ["", "---", f"Generated by AgentOps. {_approvals(ctx)} "
           "Tests were written first, locked, and gated in a sandbox."]
    return title, "\n".join(md)


def _approvals(ctx: StepContext) -> str:
    """Who approved what, read from the approval steps' own outputs: a skipped gate has none,
    so the PR never claims a person signed off when nobody did."""
    said = []
    for what, key in (("Requirements", "approve_requirements"), ("Design", "approve_design")):
        out = ctx.outputs.get(key) or {}
        said.append(f"{what} approved by {out['by']}." if out.get("approved") else
                    f"{what} not approved by a person (approval skipped for this run).")
    # The PR is written before the merge approval, so that one can only be pending or skipped.
    said.append("Merge not approved by a person (approval skipped for this run)." if ctx.input.get("auto_approve")
                else "Merge waits for a person's approval.")
    return " ".join(said)


@tool("sdlc_publish_local")
def publish_local(ctx: StepContext, args: dict, idempotency_key: str) -> dict:
    """Write the pull request (description + patch) to results/sdlc/<run>/.
    A GitHub PR tool would send the same content, with the idempotency key as the branch name."""
    out = RESULTS / str(ctx.run_id)
    out.mkdir(parents=True, exist_ok=True)
    (out / "PR.md").write_text(args["markdown"], encoding="utf-8")
    (out / "changes.patch").write_text(args["patch"], encoding="utf-8", newline="\n")
    return {"location": _shown(out), "files": ["PR.md", "changes.patch"]}


@code("sdlc_release")
def release(ctx: StepContext) -> dict:
    matrix = traceability(ctx.outputs["requirements"], ctx.outputs["write_tests"]["coverage_map"],
                          ctx.outputs["quality_gates"].get("traceability", {}))
    title, md = _pr_markdown(ctx, matrix)
    ctx.save_artifact("PR.md", md)
    published = ctx.call_tool("sdlc_publish_local", {"markdown": md, "patch": ctx.outputs["quality_gates"]["diff"]})
    return {"title": title, "markdown": md, "matrix": matrix, **published}


@tool("sdlc_merge_local")
def merge_local(ctx: StepContext, args: dict, idempotency_key: str) -> dict:
    """Write the merged repository to results/sdlc/<run>/merged/ (stand-in for merging the PR)."""
    target = RESULTS / str(ctx.run_id) / "merged"
    for rel, text in args["files"].items():
        path = target / repo.safe_path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    out = {"merged_into": _shown(target), "file_count": len(args["files"])}
    bucket = get_settings().preview_bucket
    if bucket:  # on AWS: the preview function serves the merged shop from here
        from engine import aws

        key = f"previews/{ctx.run_id}.json"
        aws.put_json(bucket, key, {"example": ctx.input.get("example", ""), "files": args["files"]})
        out["preview_copy"] = f"s3://{bucket}/{key}"
    return out


# ---------------------------------------------------------------- the workflow

def _llm(key, title, needs, schema_name, fake, system, prompt, **kw) -> StepSpec:
    config = {"schema": schema_name, "fake": fake, "system": system, "prompt": prompt}
    config.update(kw.pop("config", {}))
    return StepSpec(key, "llm", title, needs=needs, config=config, timeout_s=420, max_attempts=3, **kw)


SDLC = register_workflow(Workflow(
    name="sdlc",
    # v3: review sees its own history, blocking needs a scenario, escalates after 3 rounds.
    # v4: prompts allow the repo's own packages and web pages (for the ShopLite example).
    # v5: prompts ask for real repo data in requirements and forbid placeholder tests.
    # v6: tests of existing behaviour may pass; each acceptance criterion needs one failing test.
    version=6,
    description="Coding task → requirements → design → tests first → build → gates → review → PR.",
    steps=(
        StepSpec("load_repo", "code", "Load repo", config={"fn": "sdlc_load_repo"}),
        _llm("requirements", "Requirements", ("load_repo",), "Requirements", "sdlc_requirements",
             prompts.REQUIREMENTS_SYSTEM, prompts.REQUIREMENTS, config={"gate": "sdlc_check_requirements"}),
        StepSpec("approve_requirements", "approval", "Approve requirements", needs=("requirements",), when=NO_AUTO,
                 config={"title": "Approve the requirements?", "rewind_on_changes": "requirements",
                         "show": {"requirements": "$requirements.markdown"}}),
        _llm("design", "Design", ("approve_requirements",), "Design", "sdlc_design",
             prompts.DESIGN_SYSTEM, prompts.DESIGN, config={"gate": "sdlc_check_design"},
             on_fail={"rewind_to": "design", "max_loops": 2}),
        StepSpec("approve_design", "approval", "Approve design", needs=("design",), when=NO_AUTO,
                 config={"title": "Approve the design?", "rewind_on_changes": "design",
                         "show": {"design": "$design.markdown"}}),
        _llm("write_tests", "Tests first", ("approve_design",), "TestFiles", "sdlc_tests",
             prompts.TESTS_SYSTEM, prompts.TESTS, config={"gate": "sdlc_check_tests_red"},
             on_fail={"rewind_to": "write_tests", "max_loops": 3}),
        _llm("build", "Build", ("write_tests",), "CodeFiles", "sdlc_build", prompts.BUILD_SYSTEM, prompts.BUILD),
        StepSpec("quality_gates", "code", "Quality gates", needs=("build",), timeout_s=600,
                 config={"fn": "sdlc_quality_gates"}, on_fail={"rewind_to": "build", "max_loops": 4}),
        _llm("review", "Review", ("quality_gates",), "Review", "sdlc_review", prompts.REVIEW_SYSTEM, prompts.REVIEW,
             config={"gate": "sdlc_check_review"},
             # AI review is advisory: after 3 rounds, open comments go to the person at the merge approval.
             on_fail={"rewind_to": "build", "max_loops": 3, "on_exhausted": "escalate"}),
        StepSpec("release", "code", "Open PR", needs=("review",), config={"fn": "sdlc_release"}),
        StepSpec("approve_merge", "approval", "Approve merge", needs=("release",), when=NO_AUTO,
                 config={"title": "Merge this pull request?", "rewind_on_changes": "build",
                         "show": {"pull_request": "$release.markdown", "saved_to": "$release.location"}}),
        StepSpec("merge", "code", "Merge", needs=("approve_merge",), config={"fn": "sdlc_merge"}),
    ),
))



@code("sdlc_merge")
def merge(ctx: StepContext) -> dict:
    files = {**ctx.outputs["load_repo"]["files"], **_files(ctx.outputs["write_tests"]),
             **_files(ctx.outputs["build"])}
    return ctx.call_tool("sdlc_merge_local", {"files": files})
