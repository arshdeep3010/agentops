"""Dev/demo only: start worker processes, kill one on request, restart it a few seconds later.

In production, workers are separate containers and the orchestrator restarts them; this
plays that role locally so the UI's "Kill worker" button does something real: a hard kill
(no cleanup, like a crash or an OOM), not a polite shutdown.
"""

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("agentops.supervisor")
ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Managed:
    id: str
    proc: subprocess.Popen | None = None
    killed_at: float | None = None
    kills: int = 0
    restarts: list[float] = field(default_factory=list)


class Supervisor:
    def __init__(self, count: int, env: dict | None = None, restart_after_s: float = 4.0):
        self.restart_after_s = restart_after_s
        self.env = {**os.environ, **(env or {}), "PYTHONIOENCODING": "utf-8"}
        self.workers = {f"w{i}": Managed(f"w{i}") for i in range(1, count + 1)}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def start(self) -> None:
        for m in self.workers.values():
            self._spawn(m)
        self._thread.start()

    def _spawn(self, m: Managed) -> None:
        m.proc = subprocess.Popen([sys.executable, "-m", "engine.worker", "--id", m.id], cwd=ROOT, env=self.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        m.killed_at = None
        log.info("started worker %s (pid %s)", m.id, m.proc.pid)

    def kill(self, worker_id: str) -> bool:
        with self._lock:
            m = self.workers.get(worker_id)
            if not m or not m.proc or m.proc.poll() is not None:
                return False
            m.proc.kill()  # SIGKILL / TerminateProcess: no chance to clean up
            m.proc.wait(timeout=5)
            m.killed_at = time.time()
            m.kills += 1
            log.info("killed worker %s", worker_id)
            return True

    def _watch(self) -> None:
        while not self._stop.wait(0.25):
            with self._lock:
                for m in self.workers.values():
                    dead = m.proc is None or m.proc.poll() is not None
                    if dead and (m.killed_at is None or time.time() - m.killed_at >= self.restart_after_s):
                        self._spawn(m)
                        m.restarts.append(time.time())

    def status(self) -> dict[str, dict]:
        out = {}
        for m in self.workers.values():
            alive = bool(m.proc and m.proc.poll() is None)
            restart_in = None
            if not alive and m.killed_at:
                restart_in = max(0.0, round(self.restart_after_s - (time.time() - m.killed_at), 1))
            out[m.id] = {"alive": alive, "pid": m.proc.pid if m.proc else None, "kills": m.kills,
                         "restart_in_s": restart_in}
        return out

    def stop(self) -> None:
        self._stop.set()
        for m in self.workers.values():
            if m.proc and m.proc.poll() is None:
                m.proc.terminate()
        for m in self.workers.values():
            if m.proc:
                try:
                    m.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    m.proc.kill()
