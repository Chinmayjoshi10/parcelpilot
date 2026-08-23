"""Token issuance and verification.

The only place a `Principal` is created from untrusted input. Everything below
this layer receives an already-verified Principal, which is what makes the
tenancy story hold: if a Principal could be constructed anywhere, RLS scope
would be a suggestion.

Scope is taken **only** from verified token claims, never from a request body
or query parameter. That sounds obvious and is the single most common way
multi-tenant systems leak: an endpoint that accepts `?account_id=` "for
convenience" and trusts it.

This is demo-grade in exactly one respect -- there is no identity provider, so
`issue_token` mints tokens for known accounts against a shared secret. What is
*not* demo-grade is the verification path: signature, expiry, algorithm
pinning, issuer/audience checks and claim validation are all real, because
those are the parts that would be a vulnerability rather than a shortcut.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from agentcore.db.engine import fetch_all, fetch_one
from agentcore.errors import AuthenticationError, AuthorizationError
from agentcore.logging import get_logger
from agentcore.settings import Settings
from agentcore.types import Principal, Role

log = get_logger(__name__)

#: Pinned. Accepting a list, or reading the algorithm from the token header, is
#: how `alg: none` and HS/RS confusion attacks work.
ALGORITHM = "HS256"
ISSUER = "parcelpilot"
AUDIENCE = "parcelpilot-api"


@dataclass(frozen=True)
class IssuedToken:
    token: str
    expires_at: datetime
    principal: Principal

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.token,
            "token_type": "bearer",
            "expires_at": self.expires_at.isoformat(),
            "principal": {
                "tenant_id": self.principal.tenant_id,
                "user_id": self.principal.user_id,
                "role": self.principal.role.value,
                "account_id": self.principal.account_id,
            },
        }


def issue_token(settings: Settings, principal: Principal) -> IssuedToken:
    """Mint a token for an already-established identity.

    Note what this does NOT do: authenticate. There is no password check,
    because there is no identity provider in this build. Wiring one in replaces
    this function and nothing else -- verification, scoping and every layer below
    are unchanged.
    """
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.jwt_ttl_seconds)

    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": principal.user_id,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "tenant_id": principal.tenant_id,
        "role": principal.role.value,
    }
    # Absent rather than null for staff: "no account claim" and "account claim
    # of null" should not be two spellings of the same thing.
    if principal.account_id:
        claims["account_id"] = principal.account_id

    token = jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)
    log.info(
        "token_issued",
        user_id=principal.user_id,
        role=principal.role.value,
        account_id=principal.account_id,
        expires_at=expires_at.isoformat(),
    )
    return IssuedToken(token=token, expires_at=expires_at, principal=principal)


def verify_token(settings: Settings, token: str) -> Principal:
    """Verify a bearer token and build the Principal it authorises.

    Every failure mode collapses to the same message. Distinguishing "expired"
    from "bad signature" from "wrong audience" tells an attacker which knob to
    turn; the detail goes to the log instead.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            # A list, so the pinned algorithm is enforced rather than read from
            # the attacker-controlled header.
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        log.info("token_rejected", reason="expired")
        raise AuthenticationError("your session has expired; sign in again") from exc
    except jwt.InvalidTokenError as exc:
        log.warning("token_rejected", reason=type(exc).__name__, detail=str(exc)[:200])
        raise AuthenticationError("invalid credentials") from exc

    tenant_id = claims.get("tenant_id")
    raw_role = claims.get("role")
    if not tenant_id or not raw_role:
        log.warning("token_rejected", reason="missing_scope_claims")
        raise AuthenticationError("invalid credentials")

    try:
        role = Role(raw_role)
    except ValueError as exc:
        log.warning("token_rejected", reason="unknown_role", role=str(raw_role)[:40])
        raise AuthenticationError("invalid credentials") from exc

    try:
        # Principal's own validator rejects a customer without an account, so a
        # token crafted to be unscoped cannot produce a usable Principal.
        return Principal(
            tenant_id=tenant_id,
            user_id=str(claims["sub"]),
            role=role,
            account_id=claims.get("account_id"),
        )
    except ValueError as exc:
        log.warning("token_rejected", reason="unscoped_principal")
        raise AuthenticationError("invalid credentials") from exc


# ---------------------------------------------------------------------------
# Demo login
# ---------------------------------------------------------------------------


def available_logins(conn, tenant_id: str) -> list[dict[str, Any]]:
    """The identities this deployment can issue tokens for.

    Drives the demo's context switcher. Real accounts come from the database
    rather than a hardcoded list, so the switcher reflects what was actually
    ingested.
    """
    rows = fetch_all(
        conn,
        """
        SELECT account_id, account_name, plan FROM accounts
        WHERE tenant_id = %s ORDER BY account_id
        """,
        (tenant_id,),
    )
    logins = [
        {
            "label": f"{row['account_name']} ({row['plan']})",
            "role": Role.CUSTOMER.value,
            "account_id": row["account_id"],
            "user_id": f"customer@{row['account_id'].lower()}",
        }
        for row in rows
    ]
    logins.append(
        {
            "label": "Support Agent (all accounts)",
            "role": Role.SUPPORT_AGENT.value,
            "account_id": None,
            "user_id": "agent@parcelpilot",
        }
    )
    logins.append(
        {
            "label": "Operations Admin (all accounts, may approve actions)",
            "role": Role.OPERATIONS_ADMIN.value,
            "account_id": None,
            "user_id": "ops@parcelpilot",
        }
    )
    return logins


def principal_for_login(
    conn, tenant_id: str, role: str, account_id: str | None, user_id: str | None = None
) -> Principal:
    """Build a Principal for a demo login, validating it against real data.

    A customer login is checked against the accounts table. Without that, the
    login endpoint would happily issue a token scoped to an account that does
    not exist -- which is not exploitable given RLS, but produces a session that
    silently sees nothing and looks like a bug in retrieval.
    """
    try:
        parsed_role = Role(role)
    except ValueError as exc:
        raise AuthorizationError(f"unknown role {role!r}") from exc

    if parsed_role is Role.CUSTOMER:
        if not account_id:
            raise AuthorizationError("a customer login requires an account_id")
        exists = fetch_one(
            conn,
            "SELECT 1 FROM accounts WHERE tenant_id = %s AND account_id = %s",
            (tenant_id, account_id),
        )
        if exists is None:
            raise AuthorizationError(f"unknown account {account_id}")
    else:
        # Staff are tenant-scoped, never account-scoped. Silently dropping any
        # account claim here prevents a "staff but pinned to one account"
        # half-state that no policy models.
        account_id = None

    return Principal(
        tenant_id=tenant_id,
        user_id=user_id or f"{parsed_role.value}@{tenant_id}",
        role=parsed_role,
        account_id=account_id,
    )
