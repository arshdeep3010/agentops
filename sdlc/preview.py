"""Run a web example so a person can click around it: the original repo, or what a run merged.

One preview at a time, in the sandbox image, on http://127.0.0.1:8830. It's locked down like the
gates (read-only code, non-root, no capabilities, memory/CPU/process limits) and stops itself after
15 minutes. Unlike the gates it needs a network so your browser can reach it: the port is bound to
127.0.0.1 only, but the container can make outbound requests. That's why it only runs code a person
has approved for merge (or the original example), never code that is still in review.

On AWS (PREVIEW=lambda) the preview is a Lambda function with its own URL instead: see
sandbox/lambda_preview.py. The worker copies merged code to S3 at merge time; this module only
builds the link.
"""

import subprocess
import time
import urllib.request
from pathlib import Path

from sdlc import sandbox

NAME = "agentops-preview"
PORT = 8830
LIFETIME_S = 15 * 60
# Examples that are web apps, and the ASGI app to serve.
APPS = {"shoplite": "shoplite.main:app"}


class PreviewError(RuntimeError):
    pass


def url() -> str:
    return f"http://127.0.0.1:{PORT}"


def stop() -> None:
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True, timeout=30)


def link(run: str, example: str) -> str | None:
    """The preview URL when previews are a Lambda function, else None (use Docker)."""
    from engine.config import get_settings

    s = get_settings()
    if s.preview == "off":
        raise PreviewError("previews are turned off on this server")
    if s.preview != "lambda":
        return None
    if example not in APPS:
        raise PreviewError(f"{example!r} isn't a web app, so there is nothing to open")
    if not s.preview_url:
        raise PreviewError("PREVIEW_URL isn't set")
    return f"{s.preview_url.rstrip('/')}/?run={run}"


def start(code_dir: Path, example: str, wait_s: float = 25) -> str:
    app = APPS.get(example)
    if not app:
        raise PreviewError(f"{example!r} isn't a web app, so there is nothing to open")
    if not (code_dir / (app.split(":")[0].replace(".", "/") + ".py")).is_file():
        raise PreviewError(f"no {app.split(':')[0]} module in {code_dir.name}")
    if not sandbox.available():
        raise PreviewError(f"Docker image {sandbox.IMAGE} not found. Build it: docker build -t {sandbox.IMAGE} sandbox")
    stop()
    cmd = [
        "docker", "run", "-d", "--rm", "--name", NAME,
        "-p", f"127.0.0.1:{PORT}:8000",
        "--memory", "256m", "--memory-swap", "256m", "--cpus", "1", "--pids-limit", "128",
        "--read-only", "--tmpfs", "/tmp:rw,size=32m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "1000:1000",
        "-v", f"{code_dir.resolve()}:/app:ro", "-w", "/app",
        "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp", "-e", "PYTHONPATH=/app",
        sandbox.IMAGE, "timeout", str(LIFETIME_S),
        "python", "-m", "uvicorn", app, "--host", "0.0.0.0", "--port", "8000",
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise PreviewError(f"could not start the preview: {p.stderr.strip()[-500:]}")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url() + "/", timeout=2) as r:  # noqa: S310 - our own fixed localhost URL
                if r.status == 200:
                    return url()
        except OSError:
            time.sleep(0.5)
    logs = subprocess.run(["docker", "logs", "--tail", "20", NAME], capture_output=True, text=True).stdout
    stop()
    raise PreviewError(f"the shop didn't start within {wait_s:.0f} s. {logs.strip()[-800:]}")
