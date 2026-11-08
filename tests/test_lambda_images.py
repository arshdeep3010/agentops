"""The Lambda sandbox and preview images, run locally through the Lambda emulator (RIE) that the
AWS base image ships with. Skipped unless the containers are up:

  docker build -f sandbox/lambda.Dockerfile -t agentops-sandbox-lambda .
  docker run -d --rm --user 1000:1000 -e AWS_SECRET_ACCESS_KEY=should-not-reach-the-script \
      -p 9101:8080 agentops-sandbox-lambda
  docker run -d --rm --user 1000:1000 -p 9102:8080 agentops-sandbox-lambda lambda_preview.handler
  SANDBOX_RIE=http://localhost:9101 PREVIEW_RIE=http://localhost:9102 pytest tests/test_lambda_images.py

CI does exactly this (.github/workflows/ci.yml, job lambda-images).
"""

import base64
import json
import os
import urllib.request

import pytest

from engine import aws, config
from sdlc import gates, repo, sandbox

SANDBOX_RIE = os.environ.get("SANDBOX_RIE", "")
PREVIEW_RIE = os.environ.get("PREVIEW_RIE", "")
INVOKE = "/2015-03-31/functions/function/invocations"


@pytest.fixture
def lambda_sandbox(monkeypatch):
    if not SANDBOX_RIE:
        pytest.skip("set SANDBOX_RIE to the sandbox image's emulator URL")
    # The real code path: sandbox.run -> engine.aws.invoke -> boto3 -> the emulator.
    for k, v in {"SANDBOX": "lambda", "SANDBOX_FUNCTION": "function", "AWS_ENDPOINT_URL_LAMBDA": SANDBOX_RIE,
                 "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                 "AWS_DEFAULT_REGION": "ap-south-1"}.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()
    aws._client.cache_clear()
    yield
    config.get_settings.cache_clear()
    aws._client.cache_clear()


def _preview(event: dict) -> dict:
    if not PREVIEW_RIE:
        pytest.skip("set PREVIEW_RIE to the preview image's emulator URL")
    req = urllib.request.Request(PREVIEW_RIE + INVOKE, data=json.dumps(event).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - local emulator
        return json.loads(r.read())


def test_gate_script_runs_tests_and_coverage_in_the_lambda_sandbox(lambda_sandbox):
    files = repo.load(repo.example_dir("shoplite"))
    ws = repo.materialize(files)
    try:
        mark = sandbox.marker()
        res = sandbox.run(ws, gates._pytest_script(mark, coverage=True), timeout_s=120)
    finally:
        repo.remove(ws)
    parts = sandbox.sections(res.stdout, mark)
    assert parts["EXIT"].strip() == "0", parts.get("PYTEST")
    cases = gates._junit(parts["JUNIT"])
    assert cases and all(c["outcome"] == "passed" for c in cases)
    # Coverage paths come back repo-relative, which is what the coverage gate looks up.
    cov = json.loads(parts["COVERAGE"])
    assert "shoplite/cart.py" in cov["files"]


def test_lambda_sandbox_scrubs_the_environment_and_enforces_the_timeout(lambda_sandbox, tmp_path):
    (tmp_path / "x.txt").write_text("hi")
    res = sandbox.run(tmp_path, "env; cat x.txt; echo nope > x.txt", timeout_s=30)
    assert "hi" in res.stdout
    assert "AWS_" not in res.stdout and "should-not-reach" not in res.stdout
    assert res.exit_code != 0  # the workspace is read-only (Lambda runs as non-root: so does --user here)
    slow = sandbox.run(tmp_path, "sleep 30", timeout_s=2)
    assert slow.timed_out and slow.exit_code == 124 and slow.ms < 10_000


def test_preview_serves_the_original_shop():
    hop = _preview({"rawPath": "/", "queryStringParameters": {"run": "original"},
                    "requestContext": {"http": {"method": "GET"}}})
    assert hop["statusCode"] == 302 and hop["cookies"][0].startswith("agentops_preview=original")
    r = _preview({"rawPath": "/api/products", "rawQueryString": "", "cookies": ["agentops_preview=original"],
                  "requestContext": {"http": {"method": "GET"}}})
    assert r["statusCode"] == 200
    products = json.loads(base64.b64decode(r["body"]))
    assert {"id", "name", "price"} <= set(products[0])
    bad = _preview({"rawPath": "/", "queryStringParameters": {"run": "../etc"},
                    "requestContext": {"http": {"method": "GET"}}})
    assert bad["statusCode"] == 400
