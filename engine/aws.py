"""The few AWS calls AgentOps makes when it runs on Lambda (see infra/). boto3 is imported lazily,
so local runs and the tests never need it."""

import json
import logging
from functools import lru_cache

log = logging.getLogger("agentops.aws")


@lru_cache
def _client(service: str, read_timeout: int = 900, attempts: int = 2):
    import boto3
    from botocore.config import Config

    # Gates can run for minutes; the default 60 s read timeout would cut them off.
    return boto3.client(service, config=Config(read_timeout=read_timeout, connect_timeout=5,
                                               retries={"total_max_attempts": attempts, "mode": "standard"}))


class InvokeError(RuntimeError):
    pass


def invoke(function: str, payload: dict) -> dict:
    """Call a Lambda function and wait for its JSON reply."""
    resp = _client("lambda").invoke(FunctionName=function, Payload=json.dumps(payload).encode())
    body = resp["Payload"].read()
    if resp.get("FunctionError"):
        raise InvokeError(f"{function} failed: {body[:500].decode('utf-8', 'replace')}")
    return json.loads(body or b"{}")


def invoke_async(function: str, payload: dict) -> None:
    """Queue an event for a function and return at once. Done inline, not in a thread: Lambda
    freezes a function as soon as it replies, so a background thread might never send it.
    Never raises: a missed kick is picked up by the next one or by the scheduled sweep."""
    try:
        _client("lambda", read_timeout=5, attempts=1).invoke(
            FunctionName=function, InvocationType="Event", Payload=json.dumps(payload).encode())
    except Exception:  # noqa: BLE001
        log.warning("could not invoke %s", function, exc_info=True)


def put_json(bucket: str, key: str, value) -> None:
    _client("s3").put_object(Bucket=bucket, Key=key, Body=json.dumps(value).encode(),
                             ContentType="application/json")
