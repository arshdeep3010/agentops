"""Gates are code, not AI: each returns pass/fail with the evidence, and the model can't argue.

red_check      every acceptance criterion needs a test that FAILS before any code exists;
               tests with no assert are rejected; tests of existing behaviour may pass
quality_check  tests green, test lock, scope lock, protected files, secrets, syntax,
               lint (ruff), types (mypy), security (bandit), coverage of changed lines
"""

import ast
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from sdlc import repo, sandbox

COVERAGE_MIN = 0.80
SECRET = re.compile(
    r"(AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|nvapi-[A-Za-z0-9_-]{20,}|"
    r"sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|xox[abp]-[A-Za-z0-9-]{10,})")
MISSING_CODE = re.compile(r"(ModuleNotFoundError|ImportError|AttributeError|NameError|cannot import name)")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail[:4000], **self.data}


# ---------------------------------------------------------------- sandbox scripts

def _pytest_script(mark: str, coverage: bool) -> str:
    runner = ("python -m coverage run --rcfile=/opt/agentops/coveragerc -m pytest" if coverage
              else "python -m pytest")
    script = (
        f"{runner} -q -p no:cacheprovider --rootdir=. --junitxml=/tmp/junit.xml tests "
        f"> /tmp/pytest.out 2>&1; code=$?; "
        f"echo '{mark} EXIT'; echo $code; "
        f"echo '{mark} PYTEST'; tail -c 8000 /tmp/pytest.out; echo; "
        f"echo '{mark} JUNIT'; cat /tmp/junit.xml 2>/dev/null; echo; "
    )
    if coverage:
        script += (f"python -m coverage json --rcfile=/opt/agentops/coveragerc --show-contexts "
                   f"-o /tmp/cov.json -q >/dev/null 2>&1; echo '{mark} COVERAGE'; cat /tmp/cov.json 2>/dev/null; echo; ")
    return script


def _junit(xml_text: str) -> list[dict]:
    """[{file, name, function, outcome}] from pytest's JUnit XML."""
    if not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return []
    out = []
    for case in root.iter("testcase"):
        outcome = "passed"
        for tag in ("failure", "error", "skipped"):
            if case.find(tag) is not None:
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
        classname, name = case.get("classname", ""), case.get("name", "")
        message = ""
        for tag in ("failure", "error"):
            el = case.find(tag)
            if el is not None:
                message = (el.get("message") or "") + "\n" + (el.text or "")
        out.append({"file": classname.replace(".", "/") + ".py" if classname else "", "name": name,
                    "function": name.split("[")[0], "outcome": outcome, "message": message[:1500]})
    return out


def _syntax(files: dict[str, str]) -> list[str]:
    errors = []
    for path, text in files.items():
        if path.endswith(".py"):
            try:
                compile(text, path, "exec")
            except SyntaxError as exc:
                errors.append(f"{path}:{exc.lineno}: {exc.msg}")
    return errors


def test_name(name: str) -> str:
    """"tests/test_x.py::TestA::test_b[1]", "TestA.test_b" and "test_b" all mean test_b."""
    return re.split(r"::|\.", name.split("[")[0])[-1].strip()


def test_functions(text: str) -> set[str]:
    return set(re.findall(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(", text, re.MULTILINE))


def _checks_something(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the test asserts, expects an exception, or calls a mock's assert_* method."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in ("raises", "warns", "fail") or name.startswith("assert"):
                return True
    return False


def tests_without_checks(text: str) -> list[str]:
    """Test functions that can't fail: no assert, no pytest.raises (placeholders like a bare `pass`)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return sorted(n.name for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith("test_")
                  and not _checks_something(n))


# ---------------------------------------------------------------- red check (tests first)

def red_check(base: dict[str, str], tests: dict[str, str], design: dict, coverage_map: list[dict],
              ac_ids: list[str], timeout_s: float = 120) -> tuple[bool, dict]:
    problems: list[str] = []
    allowed = set(design.get("test_files", []))
    for path in tests:
        if path not in allowed:
            problems.append(f"{path} is not one of the test files in the approved design ({sorted(allowed)})")
        if repo.is_protected(path):
            problems.append(f"{path} is a protected file")
    problems += [f"syntax error {e}" for e in _syntax(tests)]

    defined = set().union(*(test_functions(t) for t in tests.values())) if tests else set()
    if not defined:
        problems.append("no test functions found")
    for path, text in tests.items():
        for name in tests_without_checks(text):
            problems.append(f"{path}::{name} checks nothing (no assert or pytest.raises): make it call the "
                            f"code and assert on the result")
    covered = {}
    for entry in coverage_map:
        for name in entry.get("tests", []):
            if test_name(name) not in defined:
                problems.append(f"coverage map names {name!r}, which isn't defined in the test files")
        covered.setdefault(entry.get("ac_id"), []).extend(entry.get("tests", []))
    for ac in ac_ids:
        if not covered.get(ac):
            problems.append(f"acceptance criterion {ac} has no test")
    if problems:
        return False, {"summary": "tests are not acceptable yet", "problems": problems}

    ws = repo.materialize({**base, **tests})
    try:
        mark = sandbox.marker()
        res = sandbox.run(ws, _pytest_script(mark, coverage=False), timeout_s)
    finally:
        repo.remove(ws)
    parts = sandbox.sections(res.stdout, mark)
    exit_code = int((parts.get("EXIT") or "-1").strip() or -1)
    cases = _junit(parts.get("JUNIT", ""))
    new_files = {p.removesuffix(".py") for p in tests}
    new_cases = [c for c in cases if c["file"].removesuffix(".py") in new_files]
    output = parts.get("PYTEST", "")[-3000:]

    if res.timed_out:
        return False, {"summary": "the tests timed out in the sandbox", "pytest": output}
    collection_failed = exit_code in (2, 4) or (not new_cases and exit_code != 0)
    if collection_failed:
        if MISSING_CODE.search(output + " ".join(c["message"] for c in cases)):
            red = sorted(defined)
            return True, {"summary": f"red as expected: {len(red)} tests fail because the code doesn't exist yet",
                          "red_tests": red, "collection_error": True,
                          "lock": {p: sha256(t) for p, t in tests.items()}, "pytest": output}
        return False, {"summary": "the tests could not run, and not because the code is missing", "pytest": output}

    # Tests that guard existing behaviour may pass today, but every acceptance criterion needs at least
    # one test that fails now: otherwise nothing proves the new behaviour is missing, so nothing tests it.
    passing = sorted(c["name"] for c in new_cases if c["outcome"] == "passed")
    failing = {test_name(c["name"]) for c in new_cases if c["outcome"] != "passed"}
    not_red = [ac for ac in ac_ids if not any(test_name(t) in failing for t in covered.get(ac, []))]
    if not_red:
        return False, {"summary": f"{', '.join(not_red)} {'has' if len(not_red) == 1 else 'have'} no test that fails "
                                  f"before the code exists, so nothing tests the new behaviour",
                       "criteria_without_failing_test": not_red, "passing_before_code": passing,
                       "fix": "For each criterion listed, add or rewrite a test that calls the new code from the "
                              "design and asserts the new behaviour. Tests of existing behaviour may keep passing.",
                       "pytest": output}
    red = sorted(c["name"] for c in new_cases if c["outcome"] != "passed")
    summary = f"red as expected: {len(red)} new tests fail" + (f", {len(passing)} guard existing behaviour" if passing else "")
    return True, {"summary": summary, "red_tests": red, "regression_tests": passing, "collection_error": False,
                  "lock": {p: sha256(t) for p, t in tests.items()}, "pytest": output}


# ---------------------------------------------------------------- quality gates

def quality_check(base: dict[str, str], tests: dict[str, str], code: dict[str, str], design: dict,
                  lock: dict[str, str], timeout_s: float = 180) -> dict:
    checks: list[Check] = []

    # Host-side checks first: cheap, and they don't trust anything the code prints.
    touched_tests = sorted(p for p in code if repo.is_test_path(p))
    tampered = sorted(p for p, h in lock.items() if sha256(tests.get(p, "")) != h)
    checks.append(Check("test_lock", not touched_tests and not tampered,
                        "" if not (touched_tests or tampered) else
                        f"locked tests were changed: {touched_tests + tampered}"))

    allowed = {f["path"] for f in design.get("files", [])}
    outside = sorted(set(code) - allowed)
    protected = sorted(p for p in code if repo.is_protected(p))
    checks.append(Check("scope_lock", not outside and not protected,
                        (f"files outside the approved design: {outside}. " if outside else "") +
                        (f"protected files: {protected}" if protected else "")))

    leaked = sorted(p for p, t in {**code, **tests}.items() if SECRET.search(t))
    checks.append(Check("secrets", not leaked, f"credential-like text in {leaked}" if leaked else ""))

    syntax = _syntax(code)
    checks.append(Check("syntax", not syntax, "; ".join(syntax)))

    if not all(c.passed for c in checks):
        return _report(checks, skipped="sandbox checks skipped: fix the problems above first")

    ws = repo.materialize({**base, **tests, **code})
    try:
        mark = sandbox.marker()
        res = sandbox.run(ws, _pytest_script(mark, coverage=True), timeout_s)
        py = [p for p in code if p.endswith(".py")]
        files = " ".join(py)
        static = None
        if py:
            static_script = (
                # Output may lack a final newline, so print one before each marker.
                f"echo '{mark} RUFF'; ruff check --isolated --no-cache --output-format json {files} 2>&1; "
                f"code=$?; echo; echo '{mark} RUFF_EXIT'; echo $code; "
                f"echo '{mark} MYPY'; mypy --ignore-missing-imports --no-error-summary --cache-dir /tmp/mypy "
                f"--follow-imports silent {files} 2>&1; code=$?; echo; echo '{mark} MYPY_EXIT'; echo $code; "
                f"echo '{mark} BANDIT'; bandit -q -f json {files} 2>/dev/null; echo; "
            )
            static = sandbox.run(ws, static_script, timeout_s)
    finally:
        repo.remove(ws)

    # Tests
    parts = sandbox.sections(res.stdout, mark)
    exit_code = int((parts.get("EXIT") or "-1").strip() or -1)
    cases = _junit(parts.get("JUNIT", ""))
    failed = [c for c in cases if c["outcome"] in ("failed", "error")]
    checks.append(Check("tests", exit_code == 0 and not res.timed_out and bool(cases),
                        "timed out" if res.timed_out else
                        ("" if exit_code == 0 else parts.get("PYTEST", "")[-3000:]),
                        {"total": len(cases), "failed": [f"{c['name']}: {c['message'][:300]}" for c in failed]}))

    # Coverage of changed lines, and which test ran which changed line (for traceability)
    cov_data = {}
    try:
        cov_data = json.loads(parts.get("COVERAGE", "") or "{}")
    except json.JSONDecodeError:
        pass
    executable = covered = 0
    by_test: dict[str, dict[str, set[int]]] = {}
    uncovered: dict[str, list[int]] = {}
    for path, new in code.items():
        if not path.endswith(".py"):
            continue
        changed = repo.changed_lines(base.get(path), new)
        info = cov_data.get("files", {}).get(path, {})
        ran, missing = set(info.get("executed_lines", [])), set(info.get("missing_lines", []))
        exe = changed & (ran | missing)
        executable += len(exe)
        covered += len(exe & ran)
        if exe - ran:
            uncovered[path] = sorted(exe - ran)
        for line, ctxs in (info.get("contexts") or {}).items():
            if int(line) in changed:
                for ctx in ctxs:
                    fn = ctx.split("|")[0].split(".")[-1]
                    if fn:
                        by_test.setdefault(fn, {}).setdefault(path, set()).add(int(line))
    ratio = covered / executable if executable else 1.0
    checks.append(Check("coverage", ratio >= COVERAGE_MIN and bool(cov_data or not executable),
                        "" if ratio >= COVERAGE_MIN else
                        f"{ratio:.0%} of changed lines run by tests (need {COVERAGE_MIN:.0%}); "
                        f"not run: {json.dumps(uncovered)}",
                        {"changed_lines_covered": round(ratio, 3), "changed_lines": executable}))

    if static is not None:
        sp = sandbox.sections(static.stdout, mark)
        ruff_ok = (sp.get("RUFF_EXIT", "1").strip() == "0")
        checks.append(Check("lint", ruff_ok, "" if ruff_ok else _ruff_summary(sp.get("RUFF", ""))))
        mypy_ok = (sp.get("MYPY_EXIT", "1").strip() == "0")
        checks.append(Check("types", mypy_ok, "" if mypy_ok else sp.get("MYPY", "")[-2500:]))
        high = _bandit_high(sp.get("BANDIT", ""))
        checks.append(Check("security", not high, "; ".join(high)))

    report = _report(checks)
    report["traceability"] = {t: {p: sorted(ls) for p, ls in files_.items()} for t, files_ in by_test.items()}
    report["tests_run"] = len(cases)
    return report


def _ruff_summary(text: str) -> str:
    try:
        items = json.loads(text.strip() or "[]")
    except json.JSONDecodeError:
        return text[-2000:]
    return "; ".join(f"{i['filename'].split('/workspace/')[-1]}:{i['location']['row']} {i['code']} {i['message']}"
                     for i in items[:30])


def _bandit_high(text: str) -> list[str]:
    try:
        data = json.loads(text.strip() or "{}")
    except json.JSONDecodeError:
        return []
    return [f"{r['filename'].split('/workspace/')[-1]}:{r['line_number']} {r['test_id']} {r['issue_text']}"
            for r in data.get("results", []) if r.get("issue_severity") == "HIGH"]


def _report(checks: list[Check], skipped: str = "") -> dict:
    failed = [c.name for c in checks if not c.passed]
    return {"passed": not failed, "failed": failed, "checks": [c.as_dict() for c in checks],
            "summary": "all gates passed" if not failed else f"failed: {', '.join(failed)}",
            **({"skipped": skipped} if skipped else {})}
