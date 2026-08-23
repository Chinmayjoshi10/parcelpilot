"""Structured logging with run-scoped context.

The point is reconstructability. Every LLM call, SQL statement, retrieval and
tool execution carries the `run_id` of the answer it contributed to, so months
later "why did it tell that customer there was no fee" is a query, not an
archaeology project.

`bind_run` uses contextvars rather than passing a logger down every call
signature, which means an inner layer cannot accidentally log without
provenance.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import structlog

_configured = False

#: Keys scrubbed from every event before it is emitted. Support conversations
#: are full of things that must not land in a log aggregator.
_REDACT_KEYS = frozenset(
    {
        "llm_api_key",
        "api_key",
        "authorization",
        "password",
        "jwt_secret",
        "token",
        "database_url",
        "admin_database_url",
    }
)

#: Fields truncated rather than redacted: useful for debugging, dangerous at
#: full length (a whole contract in a log line).
_TRUNCATE = {"query": 500, "text": 500, "prose": 500, "quote": 300}


def _scrub(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    for key in list(event):
        lowered = key.lower()
        if lowered in _REDACT_KEYS or lowered.endswith(("_secret", "_key", "_password")):
            event[key] = "***"
        elif key in _TRUNCATE and isinstance(event[key], str):
            limit = _TRUNCATE[key]
            if len(event[key]) > limit:
                event[key] = event[key][:limit] + f"...[+{len(event[key]) - limit} chars]"
    return event


def _stringify_uuids(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    for key, value in event.items():
        if isinstance(value, UUID):
            event[key] = str(value)
    return event


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Idempotent global logging setup. Safe to call from every entry point."""
    global _configured
    if _configured:
        return

    # stderr, not stdout. Command output on stdout must stay machine-readable:
    # with logs interleaved, `parcelpilot policy decide | jq` fails on the log
    # line, which makes every CLI command unpipeable.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    # Third-party loggers are noisy and, in httpx's case, log full URLs.
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer(colors=False)
        if fmt == "console"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _stringify_uuids,
            _scrub,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)


@contextmanager
def bind_run(
    *,
    run_id: UUID | str,
    tenant_id: str,
    principal_role: str | None = None,
    account_id: str | None = None,
    **extra: Any,
) -> Iterator[None]:
    """Attach run provenance to every log line emitted inside the block.

    `account_id` is included because a tenancy incident is diagnosed by
    comparing the scope a run *claimed* against the rows it touched.
    """
    tokens = structlog.contextvars.bind_contextvars(
        run_id=str(run_id),
        tenant_id=tenant_id,
        principal_role=principal_role,
        account_id=account_id,
        **extra,
    )
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
