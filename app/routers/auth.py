"""Login and token issuance.

Demo-grade in one specific respect: there is no identity provider, so `/login`
accepts a role and an account rather than a password. Everything downstream of
the token -- signature, expiry, algorithm pinning, claim validation, scope
derivation -- is real, because those are the parts that would be a
vulnerability rather than a shortcut.

The available logins are read from the accounts table, not hardcoded, so the
demo's context switcher reflects what was actually ingested.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from agentcore.errors import AuthorizationError
from agentcore.security.auth import available_logins, issue_token, principal_for_login
from app.deps import (
    CurrentConfig,
    CurrentPrincipal,
    CurrentSettings,
    ReadOnlyConn,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    role: str
    account_id: str | None = None
    user_id: str | None = None


@router.get("/logins")
async def logins(cfg: CurrentConfig) -> list[dict[str, Any]]:
    """The identities this deployment can issue tokens for.

    Unauthenticated on purpose -- it is the login screen. It exposes account
    names, which in a real deployment would be a disclosure; there it would be
    replaced by an SSO redirect rather than a list.
    """
    from agentcore.db import engine

    with engine.admin() as conn:
        return available_logins(conn, cfg.tenant.id)


@router.post("/login")
async def login(
    body: LoginRequest, cfg: CurrentConfig, settings: CurrentSettings
) -> dict[str, Any]:
    """Issue a token for a demo identity.

    The requested account is validated against real data: without that, a token
    could be scoped to a non-existent account, producing a session that sees
    nothing and looks like a retrieval bug rather than a bad login.
    """
    from agentcore.db import engine

    try:
        with engine.admin() as conn:
            who = principal_for_login(
                conn, cfg.tenant.id, body.role, body.account_id, body.user_id
            )
    except AuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message
        ) from exc

    return issue_token(settings, who).as_dict()


@router.get("/me")
async def me(who: CurrentPrincipal, conn: ReadOnlyConn) -> dict[str, Any]:
    """Who the caller is, and what they can see.

    `visible_accounts` comes from an RLS-filtered query rather than from the
    role, so the UI shows the scope the *database* will actually enforce -- not
    the scope the application believes it granted. If those ever disagree, this
    is where it shows.
    """
    from agentcore.db.engine import fetch_all

    accounts = fetch_all(
        conn, "SELECT account_id, account_name, plan FROM accounts ORDER BY account_id"
    )
    return {
        "tenant_id": who.tenant_id,
        "user_id": who.user_id,
        "role": who.role.value,
        "account_id": who.account_id,
        "is_staff": who.is_staff,
        "may_execute_actions": who.may_execute_actions,
        "visible_accounts": accounts,
    }
