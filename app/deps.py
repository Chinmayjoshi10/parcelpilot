"""Request-scoped dependencies.

Two rules hold this layer together:

**A Principal comes only from a verified token.** No endpoint accepts
`account_id` from a path, query or body to decide *what it can see*. That is the
single most common multi-tenant leak: an endpoint that takes `?account_id=` "for
convenience" and trusts it.

**Errors are translated, never leaked.** Every `ParcelPilotError` declares
whether its message is safe to show a caller. The handler shows the safe ones
and substitutes a generic message for the rest, so a psycopg error naming
internal columns never reaches a customer's browser.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from psycopg import Connection

from agentcore.db import engine
from agentcore.errors import AuthenticationError, ParcelPilotError
from agentcore.llm.base import LLM, Embedder
from agentcore.llm.registry import get_embedder, get_llm
from agentcore.logging import get_logger
from agentcore.orchestrator.engine import Orchestrator
from agentcore.security.auth import verify_token
from agentcore.settings import EngineConfig, Settings, get_settings, load_config
from agentcore.types import Principal, Role

log = get_logger(__name__)


def settings() -> Settings:
    return get_settings()


def config() -> EngineConfig:
    return load_config()


def llm() -> LLM:
    return get_llm()


def embedder() -> Embedder | None:
    return get_embedder()


def orchestrator(
    cfg: Annotated[EngineConfig, Depends(config)],
    model: Annotated[LLM, Depends(llm)],
    embed: Annotated[Embedder | None, Depends(embedder)],
) -> Orchestrator:
    return Orchestrator(cfg, model, embedder=embed)


def principal(
    authorization: Annotated[str | None, Header()] = None,
    cfg: Annotated[Settings, Depends(settings)] = None,  # type: ignore[assignment]
) -> Principal:
    """Extract and verify the caller.

    Rejects anything that is not a well-formed `Bearer <token>`. The 401 carries
    `WWW-Authenticate` so a client knows to re-authenticate rather than retrying
    the same credential.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    try:
        return verify_token(cfg, token)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.public_message(),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def staff_only(who: Annotated[Principal, Depends(principal)]) -> Principal:
    """Guard for endpoints a customer must not reach at all.

    Distinct from RLS, which would simply return an empty result. For something
    like the operations dashboard, an empty page is a confusing answer -- 403 is
    the honest one.
    """
    if not who.is_staff:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this view is for internal users",
        )
    return who


def approver_only(who: Annotated[Principal, Depends(principal)]) -> Principal:
    if who.role is not Role.OPERATIONS_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only an operations admin may approve actions",
        )
    return who


def scoped_connection(
    who: Annotated[Principal, Depends(principal)],
) -> Iterator[Connection]:
    """A connection filtered to the caller, for the life of the request.

    The transaction commits on a clean response and rolls back on an exception,
    so a request that fails part-way cannot leave a partial write behind.
    """
    with engine.scoped(who) as conn:
        yield conn


def read_only_connection(
    who: Annotated[Principal, Depends(principal)],
) -> Iterator[Connection]:
    """For GETs. Postgres rejects writes outright.

    Turns "this handler should not mutate anything" from a review comment into
    an enforced property.
    """
    with engine.scoped(who, read_only=True) as conn:
        yield conn


def http_error(exc: ParcelPilotError) -> HTTPException:
    """Map a domain error onto a response without leaking internals."""
    return HTTPException(status_code=exc.status_code, detail=exc.public_message())


ScopedConn = Annotated[Connection, Depends(scoped_connection)]
ReadOnlyConn = Annotated[Connection, Depends(read_only_connection)]
CurrentPrincipal = Annotated[Principal, Depends(principal)]
StaffPrincipal = Annotated[Principal, Depends(staff_only)]
ApproverPrincipal = Annotated[Principal, Depends(approver_only)]
CurrentSettings = Annotated[Settings, Depends(settings)]
CurrentConfig = Annotated[EngineConfig, Depends(config)]
CurrentOrchestrator = Annotated[Orchestrator, Depends(orchestrator)]
