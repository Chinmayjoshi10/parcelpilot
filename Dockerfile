# ===========================================================================
# Runtime image for the API.
#
# Ingestion is NOT run here. It is a separate command against the same image,
# because a container that ingests on boot cannot be scaled: N replicas would
# race to build the index. The server loads a pinned index version and never
# mutates it.
# ===========================================================================
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

# Non-root. The app needs no write access to its own code.
RUN useradd --create-home --uid 10001 parcelpilot \
    && chown -R parcelpilot:parcelpilot /app
USER parcelpilot

EXPOSE 8000

# Readiness, not liveness: a replica with no active index should not receive
# traffic even though the process is healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready').status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
