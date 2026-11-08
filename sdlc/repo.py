"""Target repos as data: load a base repo, build a workspace from base + overlays, diff them.

A workspace is never edited in place. It's rebuilt from (base repo, approved tests, generated
code) every time a check runs, so a crashed or retried step always sees the same files.
"""

import difflib
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

EXAMPLES = Path(__file__).resolve().parent / "examples"
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
MAX_FILE_BYTES = 100_000
MAX_REPO_BYTES = 400_000

# Files generated code may never touch: they change how tests, tools or Python itself behave.
PROTECTED = re.compile(
    r"(^|/)(conftest\.py|pytest\.ini|pyproject\.toml|setup\.cfg|tox\.ini|\.coveragerc|setup\.py|"
    r"sitecustomize\.py|usercustomize\.py|requirements[^/]*\.txt|Pipfile(\.lock)?|poetry\.lock|"
    r"\.github/.*|[^/]*\.pth|\.env[^/]*)$"
)


class RepoError(ValueError):
    pass


def safe_path(path: str) -> str:
    """Normalise a repo-relative path and refuse anything that escapes the repo."""
    p = PurePosixPath(path.replace("\\", "/").strip())
    if not path.strip() or p.is_absolute() or ".." in p.parts or p.parts[0] in ("", "."):
        raise RepoError(f"unsafe path {path!r}")
    # Paths reach a shell inside the sandbox, so only plain characters are allowed.
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", str(p)):
        raise RepoError(f"path has characters that aren't allowed: {path!r}")
    if any(part in SKIP_DIRS for part in p.parts):
        raise RepoError(f"path inside an ignored folder: {path!r}")
    return str(p)


def is_protected(path: str) -> bool:
    return bool(PROTECTED.search(path))


def is_test_path(path: str) -> bool:
    return path.startswith("tests/")


def example_dir(name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", name or ""):
        raise RepoError("example names are lowercase letters, digits, - and _")
    path = EXAMPLES / name
    if not path.is_dir():
        raise RepoError(f"no example repo named {name!r}")
    return path


def load(base: Path) -> dict[str, str]:
    """Read a repo's text files as {relative path: content}, with size limits."""
    files: dict[str, str] = {}
    total = 0
    for f in sorted(base.rglob("*")):
        rel = f.relative_to(base).as_posix()
        if not f.is_file() or any(part in SKIP_DIRS for part in f.relative_to(base).parts):
            continue
        if f.name == "desktop.ini" or f.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        total += len(text)
        if total > MAX_REPO_BYTES:
            raise RepoError("repo is too large for this demo (400 KB of text)")
        files[rel] = text
    return files


def materialize(files: dict[str, str]) -> Path:
    """Write files into a fresh temp folder (the caller deletes it)."""
    root = Path(tempfile.mkdtemp(prefix="agentops-ws-"))
    # mkdtemp makes it 0700 for our user; the sandbox container runs as uid 1000 and must read it.
    # (Docker Desktop on Windows hides this; a Linux host doesn't.) It's mounted read-only anyway.
    root.chmod(0o755)
    for rel, content in files.items():
        target = root / safe_path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    return root


def remove(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def changed_lines(old: str | None, new: str) -> set[int]:
    """1-based line numbers in `new` that are added or changed compared with `old`."""
    new_lines = new.splitlines()
    if old is None:
        return set(range(1, len(new_lines) + 1))
    lines: set[int] = set()
    matcher = difflib.SequenceMatcher(a=old.splitlines(), b=new_lines, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            lines.update(range(j1 + 1, j2 + 1))
    return lines


def unified_diff(base: dict[str, str], changes: dict[str, str]) -> str:
    out = []
    for path in sorted(changes):
        old = base.get(path)
        out.extend(difflib.unified_diff(
            (old or "").splitlines(keepends=True), changes[path].splitlines(keepends=True),
            fromfile="/dev/null" if old is None else f"a/{path}", tofile=f"b/{path}"))
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
    return "".join(out)


def ranges(lines: set[int]) -> str:
    """{3,4,5,9} -> "3-5, 9"."""
    out, run = [], []
    for n in sorted(lines):
        if run and n == run[-1] + 1:
            run.append(n)
        else:
            if run:
                out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
            run = [n]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out)
