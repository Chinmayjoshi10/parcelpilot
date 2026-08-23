"""The confirmation gate, over HTTP.

Note what these endpoints do *not* accept. Confirming takes an `action_id` and
nothing else: no payload, no amount, no ticket id. The effect was frozen
server-side when the action was prepared, so there is no request shape in which
a caller can alter what executes.

That is the whole design. A confirmation endpoint that echoes back the action's
contents is a confirmation endpoint that can be tampered with.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from agentcore.errors import ParcelPilotError
from agentcore.logging import get_logger
from agentcore.tools import actions
from agentcore.types import ActionType
from app.deps import (
    ApproverPrincipal,
    CurrentConfig,
    CurrentPrincipal,
    ReadOnlyConn,
    ScopedConn,
    StaffPrincipal,
    http_error,
)

log = get_logger(__name__)
router = APIRouter(prefix="/api/actions", tags=["actions"])


class PrepareRequest(BaseModel):
    action_type: ActionType
    account_id: str
    payload: dict[str, Any]
    summary: str = Field(min_length=1, max_length=500)
    #: Present when an agent proposed this during a run. Absent for an action
    #: proposed from the operations console, which has no run behind it.
    run_id: UUID | None = None


class RejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.get("")
async def pending(conn: ReadOnlyConn, who: CurrentPrincipal) -> list[dict[str, Any]]:
    """Actions awaiting a decision, in the caller's scope."""
    return [view.as_dict() for view in actions.list_pending(conn, who)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def prepare(
    body: PrepareRequest,
    conn: ScopedConn,
    who: StaffPrincipal,
    cfg: CurrentConfig,
) -> dict[str, Any]:
    """Queue a state change for approval. Nothing happens yet.

    Staff-only: the dashboard's suggested actions are proposed by an operator,
    not self-served by a customer. Preparation still re-checks the role against
    the action type, so this guard is defence in depth rather than the only
    check.
    """
    try:
        view = actions.prepare(
            conn,
            who,
            run_id=body.run_id,
            action_type=body.action_type,
            account_id=body.account_id,
            payload=body.payload,
            summary=body.summary,
            ttl_seconds=cfg.security.action_ledger.ttl_seconds,
        )
    except ParcelPilotError as exc:
        raise http_error(exc) from exc
    return view.as_dict()


@router.get("/{action_id}")
async def get_action(
    action_id: UUID, conn: ReadOnlyConn, who: CurrentPrincipal
) -> dict[str, Any]:
    try:
        return actions.get(conn, who, action_id).as_dict()
    except ParcelPilotError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such action") from exc


@router.post("/{action_id}/confirm")
async def confirm(
    action_id: UUID, conn: ScopedConn, who: ApproverPrincipal
) -> dict[str, Any]:
    """Approve and execute, exactly once.

    Takes ONLY the id. The role is re-checked here, not trusted from
    preparation time -- permissions change, and a stale approval must not carry
    an authority its approver no longer has.

    A second confirm returns the existing terminal state (409) rather than
    executing again.
    """
    try:
        view = actions.confirm(conn, who, action_id)
    except ParcelPilotError as exc:
        raise http_error(exc) from exc
    return view.as_dict()


@router.post("/{action_id}/reject")
async def reject(
    action_id: UUID, body: RejectRequest, conn: ScopedConn, who: ApproverPrincipal
) -> dict[str, Any]:
    """Decline an action. Recorded, because a rejection is a decision too."""
    try:
        view = actions.reject(conn, who, action_id, body.reason)
    except ParcelPilotError as exc:
        raise http_error(exc) from exc
    return view.as_dict()


@router.get("/history/effects")
async def effects(conn: ReadOnlyConn, who: CurrentPrincipal) -> dict[str, Any]:
    """What confirmed actions actually did.

    Separate from the ledger on purpose: an approval that was never executed and
    an execution that was never approved are different incidents, and one table
    could not tell them apart.
    """
    from agentcore.db.engine import fetch_all

    return {
        "service_credits": fetch_all(
            conn,
            """
            SELECT credit_id, account_id, order_id, amount, currency, reason,
                   action_id, run_id, issued_by, issued_at
            FROM service_credits ORDER BY issued_at DESC LIMIT 50
            """,
        ),
        "follow_ups": fetch_all(
            conn,
            """
            SELECT follow_up_id, account_id, subject, status, due_at,
                   assigned_to, action_id, created_by, created_at
            FROM follow_ups ORDER BY created_at DESC LIMIT 50
            """,
        ),
    }


@router.get("/history/audit")
async def audit(conn: ReadOnlyConn, who: StaffPrincipal) -> list[dict[str, Any]]:
    """The immutable audit trail. Staff only.

    Append-only by database permission -- `UPDATE` and `DELETE` are revoked from
    the runtime role, so no code path can rewrite what is shown here.
    """
    from agentcore.db.engine import fetch_all

    return fetch_all(
        conn,
        """
        SELECT audit_id, account_id, actor, actor_role, event, subject_id,
               run_id, detail, occurred_at
        FROM audit_log ORDER BY occurred_at DESC LIMIT 100
        """,
    )
