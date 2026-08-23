"""FastAPI application.

Three things here are deliberate and worth reading.

**Readiness is not liveness.** `/health/live` says the process is up.
`/health/ready` says it should receive traffic -- migrations applied, an index
pinned, UTF8 encoding. A server that is up but has no active index cannot answer
anything, and routing traffic to it produces confusing refusals rather than an
obvious outage.

**Startup fails loudly.** If the schema revision is unknown or no index is
active, the app logs it at error level rather than starting quietly and failing
per-request. Ingestion is a separate command by design, so a missing index is an
operator mistake and should look like one.

**Errors are translated once, centrally.** Every `ParcelPilotError` declares
whether its message is user-safe. The handler shows the safe ones and
substitutes a generic message for the rest, so an internal message cannot reach
a customer's browser by being raised somewhere new.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agentcore.db import engine
from agentcore.errors import ParcelPilotError
from agentcore.logging import configure_logging, get_logger
from agentcore.settings import get_settings, load_config
from app.routers import actions, auth, chat, dashboard

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    config = load_config()

    state = engine.healthcheck(config.tenant.id)
    if not state.get("ready"):
        # Not fatal -- /health/ready reports it and a load balancer keeps this
        # replica out of rotation -- but loud, because the usual cause is
        # forgetting to run `parcelpilot ingest run`.
        log.error(
            "startup_not_ready",
            hint="run `parcelpilot db migrate` and `parcelpilot ingest run`",
            **{k: v for k, v in state.items() if k != "active_index"},
        )
    else:
        log.info(
            "startup_ready",
            schema_version=state["schema_version"],
            index_version=(state["active_index"] or {}).get("index_version_id"),
            embedded=(state["active_index"] or {}).get("embedded_count"),
        )

    yield

    engine.close_pool()
    log.info("shutdown_complete")


app = FastAPI(
    title="ParcelPilot Agentic Intelligence API",
    version="0.2.0",
    description=(
        "Cited, verifiable reasoning over heterogeneous documents and "
        "operational data, with tenancy enforced in the database."
    ),
    lifespan=lifespan,
)

# The frontend is served separately in development. Explicit origins rather than
# "*": with credentials in play, a wildcard is both invalid and a bad habit.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    # So a browser EventSource can read the resumption id.
    expose_headers=["Last-Event-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Tag every request with an id and log its outcome.

    The id goes back in a response header so a user reporting a problem can
    quote it and land directly on the relevant log lines.
    """
    request_id = str(uuid4())
    started = time.monotonic()
    response = await call_next(request)
    duration_ms = int((time.monotonic() - started) * 1000)

    # Streams are long-lived; logging their duration on completion would report
    # how long someone watched, not how long the work took.
    if not request.url.path.endswith("/stream"):
        log.info(
            "request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(ParcelPilotError)
async def domain_error_handler(request: Request, exc: ParcelPilotError) -> JSONResponse:
    """One place where domain errors become responses.

    `public_message()` is the safety valve: a new exception type is private by
    default, so adding one cannot accidentally start leaking internals.
    """
    log.warning(
        "domain_error",
        error_type=type(exc).__name__,
        path=request.url.path,
        message=exc.message,
        **{k: str(v)[:200] for k, v in exc.context.items()},
    )
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.public_message()}
    )


app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(dashboard.router)
app.include_router(actions.router)


@app.get("/health/live", tags=["health"])
async def live() -> dict[str, str]:
    """Is the process running. Nothing more -- it must not touch the database,
    or a database blip would trigger a restart loop instead of a failover."""
    return {"status": "alive"}


@app.get("/health/ready", tags=["health"])
async def ready() -> JSONResponse:
    """Should this replica receive traffic.

    503 when not, so an orchestrator withholds traffic rather than sending it to
    a replica that will refuse every question.
    """
    state = engine.healthcheck()
    code = 200 if state.get("ready") else 503
    # jsonable_encoder because the payload carries timestamps: JSONResponse
    # serialises with plain json.dumps, which cannot encode a datetime, and a
    # readiness probe that 500s is worse than one that reports unready.
    return JSONResponse(status_code=code, content=jsonable_encoder(state))


@app.get("/api/meta", tags=["health"])
async def meta() -> dict[str, Any]:
    """What this deployment is configured to do.

    Exposed so the UI can show the active model, index version and degraded
    modes. A demo viewer should be able to see that retrieval is lexical-only,
    rather than wondering why answers feel thin.
    """
    settings = get_settings()
    config = load_config()
    state = engine.healthcheck(config.tenant.id)
    return {
        "tenant": config.tenant.model_dump(),
        "provider": settings.llm_provider,
        "routing_model": settings.llm_routing_model,
        "synthesis_model": settings.llm_synthesis_model,
        "embedding_model": (
            settings.embedding_model if settings.embeddings_enabled else None
        ),
        "retrieval_mode": (
            config.retrieval.mode if settings.embeddings_enabled else "lexical_only"
        ),
        "citation_validation": config.agent.citation_validation.model_dump(),
        "active_index": state.get("active_index"),
        "ready": state.get("ready"),
    }
