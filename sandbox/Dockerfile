# The only place AI-written code ever runs. Tools are baked in; containers get no network.
# The web packages are here for example repos like ShopLite (FastAPI apps tested through TestClient).
FROM python:3.13-slim
RUN pip install --no-cache-dir "pytest==8.4.2" "coverage==7.10.6" "ruff==0.13.0" "mypy==1.18.1" "bandit==1.8.6" \
        "fastapi==0.162.0" "httpx==0.28.1" "uvicorn==0.54.0" \
    && useradd --uid 1000 --create-home sandbox
COPY coveragerc /opt/agentops/coveragerc
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
USER sandbox
WORKDIR /workspace
