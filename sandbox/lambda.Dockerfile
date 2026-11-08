# The sandbox and the shop preview as Lambda functions (build from the repo root):
#   docker build -f sandbox/lambda.Dockerfile -t agentops-sandbox-lambda .
# Same tools and versions as sandbox/Dockerfile, the image the local Docker sandbox uses.
# One image, two handlers: lambda_sandbox.handler (default) and lambda_preview.handler.

FROM public.ecr.aws/lambda/python:3.13
RUN pip install --no-cache-dir "pytest==8.4.2" "coverage==7.10.6" "ruff==0.13.0" "mypy==1.18.1" "bandit==1.8.6" \
        "fastapi==0.162.0" "httpx==0.28.1" "uvicorn==0.54.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# The trusted coverage config, pointed at where lambda_sandbox.py unpacks the workspace.
COPY sandbox/coveragerc /opt/agentops/coveragerc
RUN python3 -c "import pathlib; p = pathlib.Path('/opt/agentops/coveragerc'); p.write_text(p.read_text().replace('/workspace', '/tmp/workspace'))"
# The original example repos, so the preview can open the shop as it is before any run.
COPY sdlc/examples /opt/agentops/examples
COPY sandbox/lambda_sandbox.py sandbox/lambda_preview.py ${LAMBDA_TASK_ROOT}/
CMD ["lambda_sandbox.handler"]
