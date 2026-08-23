# ===========================================================================
# Runtime image for the API.
#
# Ingestion is NOT run here. It is a separate command against the same image,
# because a container that ingests on boot cannot be scaled: N replicas would
# race to build the index. The server loads a pinned index version and never
# mutates it.
# ===========================================================================
# ---------------------------------------------------------------------------
# Stage 1: build the console.
#
# The API serves these files, so they must exist in the image. Splitting them
# across two hosts would mean cross-origin requests on every call including the
# SSE stream, and a bearer token travelling to a different origin than the page
# that holds it. One origin removes that whole class of problem.
# ---------------------------------------------------------------------------
FROM node:20-slim AS web

WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM python:3.11-slim AS base

# No .pyc files, unbuffered logs so they reach the collector immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first: source changes then do not invalidate the install.
COPY pyproject.toml README.md ./
COPY agentcore/__init__.py agentcore/__init__.py
RUN pip install --no-cache-dir -e ".[vertex]"

COPY agentcore/ agentcore/
COPY app/ app/
COPY eval/ eval/
COPY config.yaml policy_pack.yaml ./
COPY data/ data/

# The built console, from stage 1. `app/main.py` mounts it when present and runs
# API-only when it is not, so a stripped image still works.
COPY --from=web /web/dist/ frontend/dist/

# Non-root. The app needs no write access to its own code.
RUN useradd --create-home --uid 10001 parcelpilot \
    && chown -R parcelpilot:parcelpilot /app
USER parcelpilot

# Managed platforms (Railway, Render, Cloud Run) assign the port at runtime and
# expect the process to read it from the environment. Defaulted so a plain
# `docker run -p 8000:8000` still works unchanged.
ENV PORT=8000
EXPOSE 8000

# Readiness, not liveness: a replica with no active index should not receive
# traffic even though the process is healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; p=os.environ.get('PORT','8000'); \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/health/ready').status == 200 else 1)"

# Shell form on purpose: $PORT must be expanded at container start, and the exec
# form would pass the literal string "${PORT}" to uvicorn.
CMD python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
