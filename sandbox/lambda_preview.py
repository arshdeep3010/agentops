"""The "open the shop" preview as a Lambda function with its own public URL.

Locally the preview is a Docker container on 127.0.0.1:8830 (sdlc/preview.py). On AWS it's this
function: same image as the sandbox, same VPC with no internet route. Its role can read one
thing: the merged code the worker copied to S3 after a person approved the merge
(s3://<bucket>/previews/<run id>.json). The original shop is baked into the image.

Open  <url>/?run=original  or  <url>/?run=<run id>: that sets a cookie and the shop opens at /,
so the shop's own absolute paths (/api/cart, ...) work unchanged. State (the cart) lives in memory,
as in the local preview, so it resets when Lambda starts a fresh copy of the function.
"""

import base64
import json
import re
import sys
from pathlib import Path

ORIGINALS = Path("/opt/agentops/examples")
APPS = {"shoplite": "shoplite.main:app"}  # same as sdlc/preview.py
COOKIE = "agentops_preview"
RUN = re.compile(r"^(original|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")

_loaded: dict = {}  # {"key", "root", "client"}: one shop at a time per function copy


def _page(status: int, text: str) -> dict:
    html = (f"<!doctype html><meta charset=utf-8><title>Preview</title>"
            f"<body style='font:16px system-ui;margin:3rem'><p>{text}</p></body>")
    return {"statusCode": status, "headers": {"content-type": "text/html; charset=utf-8"}, "body": html}


def _fetch(key: str) -> tuple[Path, str] | None:
    """(code folder, app) for a run, or None if nothing was merged for it."""
    if key == "original":
        return ORIGINALS / "shoplite", APPS["shoplite"]
    import os

    import boto3

    s3 = boto3.client("s3")
    try:
        obj = s3.get_object(Bucket=os.environ["PREVIEW_BUCKET"], Key=f"previews/{key}.json")
    except s3.exceptions.NoSuchKey:
        return None
    except Exception as exc:  # noqa: BLE001 - AccessDenied for a missing key, without ListBucket
        if "AccessDenied" in str(exc) or "NoSuchKey" in str(exc):
            return None
        raise
    data = json.loads(obj["Body"].read())
    app = APPS.get(data.get("example", ""))
    if not app:
        return None
    root = Path("/tmp/preview") / key
    for rel, content in data["files"].items():
        if rel.startswith("/") or ".." in rel.split("/"):
            continue
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root, app


def _client(key: str):
    if _loaded.get("key") == key:
        return _loaded["client"]
    found = _fetch(key)
    if not found:
        return None
    root, app_ref = found
    # Unload the previous shop: both define the same package name.
    old = _loaded.get("root")
    if old:
        for name, mod in list(sys.modules.items()):
            if str(getattr(mod, "__file__", "") or "").startswith(str(old)):
                del sys.modules[name]
        sys.path.remove(str(old))
    sys.path.insert(0, str(root))
    import importlib

    from fastapi.testclient import TestClient

    module, attr = app_ref.split(":")
    app = getattr(importlib.import_module(module), attr)
    _loaded.update(key=key, root=root, client=TestClient(app, raise_server_exceptions=False))
    return _loaded["client"]


def handler(event, context) -> dict:
    http = event.get("requestContext", {}).get("http", {})
    method, path = http.get("method", "GET"), event.get("rawPath", "/")
    query = event.get("queryStringParameters") or {}
    if "run" in query:
        run = query["run"].lower()
        if not RUN.match(run):
            return _page(400, "That isn't a run id.")
        return {"statusCode": 302, "headers": {"location": "/", "cache-control": "no-store"},
                "cookies": [f"{COOKIE}={run}; Path=/; Secure; HttpOnly; SameSite=Lax"], "body": ""}
    cookies = dict(c.split("=", 1) for c in event.get("cookies", []) if "=" in c)
    key = cookies.get(COOKIE, "original")
    if not RUN.match(key):
        key = "original"
    client = _client(key)
    if client is None:
        return _page(404, "Nothing was merged for this run yet. Approve the merge first.")

    body = event.get("body") or ""
    content = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    headers = {k: v for k, v in (event.get("headers") or {}).items() if k in ("content-type", "accept")}
    url = path + ("?" + event["rawQueryString"] if event.get("rawQueryString") else "")
    r = client.request(method, url, headers=headers, content=content)
    out_headers = {k: v for k, v in r.headers.items() if k in ("content-type", "location")}
    out_headers["cache-control"] = "no-store"
    return {"statusCode": r.status_code, "headers": out_headers,
            "body": base64.b64encode(r.content).decode(), "isBase64Encoded": True}
