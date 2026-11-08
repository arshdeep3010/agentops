"""The sandbox as a Lambda function: run one gate script against a workspace sent in the request.

What keeps AI-written code contained here (compare the Docker flags in sdlc/sandbox.py):
- the function sits in a VPC subnet with no route to the internet and a security group with
  no outbound rules (Terraform: infra/network.tf): no network;
- its IAM role can only write its own logs, and it has no secrets in its environment;
- the script runs as a child process with a scrubbed environment (no AWS credentials), its own
  process group (killed afterwards, so nothing lingers into the next request), a process limit,
  a file-size limit and a wall-clock timeout;
- the workspace is rebuilt from the request every time and made read-only (owner-read-only
  files: that stops accidents; the rebuild is what matters), and /tmp is wiped before and after.
Lambda runs every function in its own Firecracker micro-VM, as a non-root user.
"""

import os
import resource
import shutil
import signal
import subprocess
import time
from pathlib import Path, PurePosixPath

WORKSPACE = Path("/tmp/workspace")  # coveragerc in this image points here
MAX_OUT = 2_000_000  # characters of stdout/stderr returned (the reply limit is 6 MB)


def _clean_tmp() -> None:
    for p in Path("/tmp").iterdir():
        if p.is_dir() and not p.is_symlink():
            for d, _dirs, _names in os.walk(p):
                try:
                    os.chmod(d, 0o755)  # the workspace was made read-only
                except OSError:
                    pass
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def _write(files: dict[str, str]) -> None:
    WORKSPACE.mkdir()
    for rel, content in files.items():
        p = PurePosixPath(rel)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"unsafe path {rel!r}")
        target = WORKSPACE / p
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    # Read-only, like the :ro mount in Docker.
    for d, _dirs, names in os.walk(WORKSPACE):
        for n in names:
            os.chmod(os.path.join(d, n), 0o444)
        os.chmod(d, 0o555)


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def handler(event, context) -> dict:
    _clean_tmp()
    _write(event["files"])
    timeout_s = min(float(event.get("timeout_s", 120)), 280)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp", "LANG": "C.UTF-8",
           "PYTHONPATH": str(WORKSPACE), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
    started = time.monotonic()
    proc = subprocess.Popen(["/bin/sh", "-c", event["script"]], cwd=WORKSPACE, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                            preexec_fn=_limits)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc.pid)
        out, err = proc.communicate()
    _kill_group(proc.pid)  # background processes the script left behind
    try:
        return {"exit_code": 124 if timed_out else proc.returncode,
                "stdout": out.decode("utf-8", "replace")[-MAX_OUT:],
                "stderr": err.decode("utf-8", "replace")[-MAX_OUT:],
                "timed_out": timed_out, "ms": int((time.monotonic() - started) * 1000)}
    finally:
        _clean_tmp()
