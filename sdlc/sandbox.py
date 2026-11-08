"""Run commands against a workspace inside a locked-down sandbox.

AI-written code never runs where the engine runs. Two backends, chosen by SANDBOX:

- docker (local): each call gets a fresh container: no network, read-only root and workspace,
  512 MB memory, 1 CPU, 256 processes, non-root, no Linux capabilities, a hard wall-clock timeout.
  Tools write only to a small /tmp.
- lambda (AWS): each call goes to a separate Lambda function (sandbox/lambda_sandbox.py) that sits
  in a VPC with no internet route, has no permissions and no secrets, and runs the script as a
  non-root user with a timeout. The workspace files travel in the request.
"""

import secrets
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

IMAGE = "agentops-sandbox:2"


class SandboxUnavailable(RuntimeError):
    """The sandbox can't be used at all (no image, input too big): retrying won't help."""


class SandboxFailed(RuntimeError):
    """One call failed (an AWS hiccup): the step is retried."""


@dataclass
class Result:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    ms: int


def _backend() -> str:
    from engine.config import get_settings

    s = get_settings()
    return "lambda" if s.sandbox == "lambda" and s.sandbox_function else "docker"


def available(image: str = IMAGE) -> bool:
    if _backend() == "lambda":
        return True
    if not shutil.which("docker"):
        return False
    try:
        r = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def run(workspace: Path, script: str, timeout_s: float = 120, image: str = IMAGE) -> Result:
    """Run a shell script in the sandbox, in the workspace folder (read-only)."""
    if _backend() == "lambda":
        return _run_lambda(workspace, script, timeout_s)
    if not available(image):
        raise SandboxUnavailable(f"Docker image {image} not found. Build it: docker build -t {image} sandbox")
    name = f"agentops-sbx-{uuid.uuid4().hex[:12]}"
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "--network", "none",
        "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "256",
        "--read-only", "--tmpfs", "/tmp:rw,size=128m,exec",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", "1000:1000",
        "-v", f"{workspace.resolve()}:/workspace:ro", "-w", "/workspace",
        "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp", "-e", "PYTHONPATH=/workspace",
        image, "sh", "-c", script,
    ]
    started = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        return Result(124, _text(exc.stdout), _text(exc.stderr), True, int((time.monotonic() - started) * 1000))
    return Result(p.returncode, p.stdout, p.stderr, False, int((time.monotonic() - started) * 1000))


MAX_PAYLOAD_BYTES = 5_000_000  # Lambda takes 6 MB per request


def _run_lambda(workspace: Path, script: str, timeout_s: float) -> Result:
    from engine import aws
    from engine.config import get_settings

    files = {f.relative_to(workspace).as_posix(): f.read_text(encoding="utf-8")
             for f in sorted(workspace.rglob("*")) if f.is_file()}
    if sum(len(v) for v in files.values()) > MAX_PAYLOAD_BYTES:
        raise SandboxUnavailable("workspace too large to send to the sandbox function")
    started = time.monotonic()
    try:
        out = aws.invoke(get_settings().sandbox_function,
                         {"files": files, "script": script, "timeout_s": timeout_s})
    except Exception as exc:  # noqa: BLE001 - a step retry is the right answer to an AWS blip
        raise SandboxFailed(f"sandbox function failed: {exc}") from exc
    return Result(int(out["exit_code"]), out.get("stdout", ""), out.get("stderr", ""),
                  bool(out.get("timed_out")), out.get("ms", int((time.monotonic() - started) * 1000)))


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def marker() -> str:
    """A fresh random marker per run, so output printed by generated code can't pose as ours."""
    return "@@" + secrets.token_hex(8)


def sections(stdout: str, mark: str) -> dict[str, str]:
    """Split `<mark> NAME` delimited output into {NAME: text}."""
    parts: dict[str, str] = {}
    current, buf = None, []
    for line in stdout.splitlines():
        if line.startswith(mark + " "):
            if current:
                parts[current] = "\n".join(buf)
            current, buf = line[len(mark) + 1:].strip(), []
        elif current:
            buf.append(line)
    if current:
        parts[current] = "\n".join(buf)
    return parts
