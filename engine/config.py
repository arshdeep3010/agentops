from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """All configuration comes from environment variables (or the project's .env file)."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str = "postgresql://agentops:agentops@localhost:54329/agentops"

    # Model calls. "fake" = deterministic offline replies (tests, demo without keys).
    llm_mode: str = "fake"
    llm_base_url: str = "https://integrate.api.nvidia.com/v1"
    llm_api_key: str = ""
    llm_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    llm_timeout_s: float = 300.0     # long replies (whole files) can take minutes on the free tier
    llm_max_tokens: int = 8192       # output limit per reply; doubled (up to 16384) if a reply is cut off
    # ₹ per 1M tokens (input, output). The NVIDIA free tier costs nothing.
    llm_price_in_per_m: float = 0.0
    llm_price_out_per_m: float = 0.0

    # Engine
    lease_seconds: float = 30.0      # how long a claimed step is ours without a heartbeat
    poll_seconds: float = 0.25       # idle wait between claim attempts
    backoff_base_s: float = 1.0      # retry delay = base * 2^(attempt-1)

    # Dev / demo: the API starts and supervises this many worker processes.
    spawn_workers: int = 0
    restart_killed_after_s: float = 4.0

    # Database connections kept open per process. 0 on Lambda: a frozen function holds nothing.
    db_pool_min: int = 1
    # One LISTEN connection wakes live streams at once. Off on Lambda (streams poll every 2 s).
    event_listen: bool = True

    # ---- Where things run. Local defaults use Docker; on AWS (see infra/) they are Lambda functions.
    sandbox: str = "docker"          # docker | lambda: where the gates run AI-written code
    sandbox_function: str = ""       # Lambda function name when sandbox=lambda
    preview: str = "docker"          # docker | lambda | off: how "open the shop" works
    preview_url: str = ""            # the preview function's URL when preview=lambda
    preview_bucket: str = ""         # S3 bucket the merged code is copied to for the preview
    worker_kick: str = "none"        # none | lambda: wake a worker function when there's new work
    worker_function: str = ""        # Lambda function name when worker_kick=lambda
    results_dir: str = ""            # where PR.md, the patch and merged code go (default results/sdlc)

    # ---- Limits for a public deployment (0 / empty = no limit, the local default)
    max_runs_per_day: int = 0        # runs started in the last 24 h, across everyone
    allowed_workflows: str = ""      # comma-separated workflow names the API may start
    stream_max_s: float = 0          # end a live stream after this long; the browser reconnects

    # On AWS, secrets (DATABASE_URL, LLM_API_KEY) live in SSM Parameter Store under this prefix.
    ssm_prefix: str = ""


def _load_ssm(prefix: str) -> None:
    """Copy SecureString parameters under `prefix` into the environment (names upper-cased).
    Values set in the environment already win, so a local run never reaches AWS."""
    import os

    import boto3  # only needed on AWS

    ssm = boto3.client("ssm")
    for page in ssm.get_paginator("get_parameters_by_path").paginate(Path=prefix, WithDecryption=True):
        for p in page["Parameters"]:
            name = p["Name"].rsplit("/", 1)[-1].upper()
            if p["Value"] and p["Value"] != "set-me":
                os.environ.setdefault(name, p["Value"])


@lru_cache
def get_settings() -> Settings:
    import os

    if os.environ.get("SSM_PREFIX"):
        _load_ssm(os.environ["SSM_PREFIX"])
    return Settings()
