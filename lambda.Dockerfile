# AgentOps on AWS Lambda: one image, two targets.
#   docker build -f lambda.Dockerfile --target api    -t agentops-api .
#   docker build -f lambda.Dockerfile --target worker -t agentops-worker .
# The sandbox and preview functions use sandbox/lambda.Dockerfile.

FROM public.ecr.aws/lambda/python:3.13 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RESULTS_DIR=/tmp/results
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt
COPY engine ${LAMBDA_TASK_ROOT}/engine
COPY workflows ${LAMBDA_TASK_ROOT}/workflows
COPY sdlc ${LAMBDA_TASK_ROOT}/sdlc
COPY api ${LAMBDA_TASK_ROOT}/api

# The worker: a plain Lambda handler that drains the queue (engine/lambda_worker.py).
FROM base AS worker
CMD ["engine.lambda_worker.handler"]

# The API: the same FastAPI app as locally, run by uvicorn. The Lambda Web Adapter turns
# Lambda requests into HTTP and streams the response back, so the live SSE stream works.
FROM base AS api
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter
ENV AWS_LWA_PORT=8000 AWS_LWA_INVOKE_MODE=response_stream AWS_LWA_READINESS_CHECK_PATH=/
WORKDIR ${LAMBDA_TASK_ROOT}
ENTRYPOINT ["python3", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
