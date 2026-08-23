"""The action ledger: preparing, confirming and executing state changes.

The whole design exists to answer one question safely: *how does an AI system
change something in the real world without anyone being able to make it do so
by accident, twice, or without approval?*

Four properties, each enforced structurally rather than by discipline:

1. **The client never holds the payload.** `prepare` returns an `action_id` and
   a human-readable summary. Confirmation sends back only that id. There is no
   request shape in which a caller can alter what executes, because the effect
   was frozen server-side at preparation time.

2. **Execution is exactly-once.** The status transition to `confirmed` is a
   conditional UPDATE (`WHERE status = 'pending'`). Two concurrent confirms
   race, one updates zero rows, and the loser reports the existing terminal
   state instead of executing again. `idempotency_key` is UNIQUE as a second
   line of defence.

3. **Approval is re-authorised at confirm time.** A role is checked when the
   action is prepared *and* again when it runs. Permissions change; a stale
   approval must not carry an authority the approver no longer has.

4. **Expiry is enforced in the same statement as execution.** Not a check
   followed by a use -- there is no window between them.

`payload_sha256` is verified before executing. The client cannot tamper (it
never sees the payload), so a mismatch means the row itself was altered, which
is a reason to refuse rather than to proceed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection

from agentcore.db.engine import fetch_all, fetch_one
from agentcore.errors import (
    ActionAlreadySettled,
    ActionError,
    ActionExpired,
    ActionPayloadTampered,
    AuthorizationError,
    RunNotVisible,
)
from agentcore.logging import get_logger
from agentcore.types import ActionStatus, ActionType, Citation, Principal, Role

log = get_logger(__name__)

#: Which roles may *prepare* each action type. Preparation is cheap and
#: reversible; anyone who can ask a question can propose an escalation.
_PREPARE_ROLES: dict[ActionType, frozenset[Role]] = {
    ActionType.ESCALATE_TICKET: frozenset(Role),
    ActionType.CREATE_FOLLOW_UP: frozenset(Role),
    # Money and operational state are staff-proposed only: a customer should
    # not be able to queue a credit against their own account for approval.
    ActionType.ISSUE_SERVICE_CREDIT: frozenset(
        {Role.SUPPORT_AGENT, Role.OPERATIONS_ADMIN}
    ),
    ActionType.UPDATE_ORDER_STATUS: frozenset(
        {Role.SUPPORT_AGENT, Role.OPERATIONS_ADMIN}
    ),
}

#: Which roles may *commit*. Deliberately narrower than preparation for
#: everything: committing is the irreversible half.
_CONFIRM_ROLES: dict[ActionType, frozenset[Role]] = {
    ActionType.ESCALATE_TICKET: frozenset({Role.SUPPORT_AGENT, Role.OPERATIONS_ADMIN}),
    ActionType.CREATE_FOLLOW_UP: frozenset({Role.SUPPORT_AGENT, Role.OPERATIONS_ADMIN}),
    ActionType.ISSUE_SERVICE_CREDIT: frozenset({Role.OPERATIONS_ADMIN}),
    ActionType.UPDATE_ORDER_STATUS: frozenset({Role.OPERATIONS_ADMIN}),
}


def payload_digest(
    tenant_id: str, account_id: str, action_type: ActionType, payload: dict[str, Any]
) -> str:
    """Digest over the whole effect, not just the payload.

    Tenant, account and type are included so a payload cannot be replayed
    against a different account by editing one column.
    `sort_keys` makes it stable across dict ordering.
    """
    canonical = json.dumps(
        {
            "tenant_id": tenant_id,
            "account_id": account_id,
            "action_type": action_type.value,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ActionView:
    """What a caller is allowed to know about a pending action.

    Carries the summary and the justification (so a human can see the evidence)
    but the payload is included only for staff -- a customer confirming an
    escalation does not need the internal field names it will set.
    """

    action_id: UUID
    action_type: ActionType
    status: ActionStatus
    account_id: str
    summary: str
    expires_at: datetime
    prepared_by: str
    justification: list[dict[str, Any]]
    payload: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "action_id": str(self.action_id),
            "action_type": self.action_type.value,
            "status": self.status.value,
            "account_id": self.account_id,
            "summary": self.summary,
            "expires_at": self.expires_at.isoformat(),
            "prepared_by": self.prepared_by,
            "justification": self.justification,
        }
        if self.payload is not None:
            out["payload"] = self.payload
        if self.result is not None:
            out["result"] = self.result
        if self.error is not None:
            out["error"] = self.error
        return out


def _view(row: dict[str, Any], principal: Principal) -> ActionView:
    return ActionView(
        action_id=row["action_id"],
        action_type=ActionType(row["action_type"]),
        status=ActionStatus(row["status"]),
        account_id=row["account_id"],
        summary=row["summary"],
        expires_at=row["expires_at"],
        prepared_by=row["prepared_by"],
        justification=row["justification"] or [],
        payload=row["payload"] if principal.is_staff else None,
        result=row.get("result"),
        error=row.get("error"),
    )


# ---------------------------------------------------------------------------
# Prepare
# ---------------------------------------------------------------------------


def prepare(
    conn: Connection,
    principal: Principal,
    *,
    action_type: ActionType,
    run_id: UUID | None = None,
    account_id: str,
    payload: dict[str, Any],
    summary: str,
    justification: list[Citation] | None = None,
    ttl_seconds: int = 900,
) -> ActionView:
    """Record a proposed state change. Nothing happens yet.

    The returned view is what a confirmation drawer renders. `summary` must be
    written for a human deciding whether to approve -- it is the only thing most
    approvers will read, so it carries the real weight.
    """
    allowed = _PREPARE_ROLES.get(action_type, frozenset())
    if principal.role not in allowed:
        raise AuthorizationError(
            f"role {principal.role.value} may not prepare {action_type.value}",
            action_type=action_type.value,
        )

    # A customer may only ever act on their own account. Staff are already
    # tenant-scoped by RLS; this stops a customer naming someone else's account
    # in a payload.
    if principal.role is Role.CUSTOMER and account_id != principal.account_id:
        raise AuthorizationError("cannot prepare an action for another account")

    # An action proposed during a run cites that run; one proposed from the
    # operations console has no run behind it and says so, rather than being
    # attributed to a synthetic one.
    origin = "agent" if run_id is not None else "operator"

    digest = payload_digest(principal.tenant_id, account_id, action_type, payload)
    # Derived from the digest, so re-preparing an identical effect for the same
    # origin collides instead of queueing a duplicate for approval.
    idempotency_key = f"{run_id or 'operator'}:{action_type.value}:{digest[:32]}"
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

    row = fetch_one(
        conn,
        """
        INSERT INTO pending_actions (
            tenant_id, run_id, account_id, action_type, payload, summary,
            payload_sha256, idempotency_key, status, justification,
            prepared_by, expires_at, origin
        ) VALUES (
            %(tenant)s, %(run)s, %(account)s, %(type)s, %(payload)s, %(summary)s,
            %(digest)s, %(key)s, 'pending', %(justification)s, %(by)s, %(expires)s,
            %(origin)s
        )
        ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
        RETURNING *
        """,
        {
            "tenant": principal.tenant_id,
            "run": run_id,
            "account": account_id,
            "type": action_type.value,
            "payload": json.dumps(payload, default=str),
            "summary": summary,
            "digest": digest,
            "key": idempotency_key,
            "justification": json.dumps(
                [c.model_dump(mode="json") for c in (justification or [])]
            ),
            "by": principal.user_id,
            "expires": expires_at,
            "origin": origin,
        },
    )

    if row is None:
        # Already prepared for this run with an identical effect: return the
        # existing one rather than a second approval for the same thing.
        row = fetch_one(
            conn,
            "SELECT * FROM pending_actions WHERE tenant_id = %s AND idempotency_key = %s",
            (principal.tenant_id, idempotency_key),
        )
        if row is None:  # pragma: no cover - would mean RLS hid our own insert
            raise ActionError("could not prepare or retrieve the action")
        log.info("action_prepare_deduplicated", action_id=str(row["action_id"]))

    conn.commit()
    log.info(
        "action_prepared",
        action_id=str(row["action_id"]),
        action_type=action_type.value,
        account_id=account_id,
        origin=origin,
        expires_at=expires_at.isoformat(),
    )
    return _view(row, principal)


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------


def confirm(conn: Connection, principal: Principal, action_id: UUID) -> ActionView:
    """Approve and execute, exactly once.

    Everything happens in one transaction: claim the row, verify the digest,
    apply the effect, record the result, append the audit entry. A failure at
    any point rolls the whole thing back, so a half-applied action cannot exist.
    """
    row = fetch_one(
        conn,
        "SELECT * FROM pending_actions WHERE action_id = %s",
        (action_id,),
    )
    if row is None:
        # Absent, or hidden by RLS. Indistinguishable on purpose: confirming
        # that an action exists on another account is itself a disclosure.
        raise ActionError("no such pending action")

    action_type = ActionType(row["action_type"])

    # Re-authorised now, not trusted from preparation time: roles change, and a
    # stale approval must not carry authority its approver no longer has.
    if principal.role not in _CONFIRM_ROLES.get(action_type, frozenset()):
        raise AuthorizationError(
            f"role {principal.role.value} may not confirm {action_type.value}",
            action_type=action_type.value,
        )

    # Claim the action. Expiry is part of the predicate, so there is no gap
    # between checking it and acting on it.
    claimed = fetch_one(
        conn,
        """
        UPDATE pending_actions
        SET status = 'confirmed', settled_by = %s, settled_at = now()
        WHERE action_id = %s AND status = 'pending' AND expires_at > now()
        RETURNING *
        """,
        (principal.user_id, action_id),
    )

    if claimed is None:
        # Lost the race, already settled, or expired. Report the terminal state
        # rather than executing a second time.
        current = ActionStatus(row["status"])
        if current is ActionStatus.PENDING:
            _expire(conn, action_id)
            raise ActionExpired(
                "this approval has expired; ask again to get a fresh one",
                action_id=str(action_id),
            )
        raise ActionAlreadySettled(
            f"this action is already {current.value}", status=current.value
        )

    if claimed["payload_sha256"] != payload_digest(
        claimed["tenant_id"],
        claimed["account_id"],
        action_type,
        claimed["payload"],
    ):
        # The client never sees the payload, so a mismatch is not a client bug --
        # it means the stored row was altered. Refuse and leave it visible.
        conn.rollback()
        _fail(conn, action_id, "payload digest mismatch")
        raise ActionPayloadTampered(
            "the stored action payload does not match its digest; refusing to execute",
            action_id=str(action_id),
        )

    try:
        result = _EXECUTORS[action_type](conn, principal, claimed)
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        conn.rollback()
        _fail(conn, action_id, str(exc))
        raise ActionError(f"executing {action_type.value} failed: {exc}") from exc

    conn.execute(
        """
        UPDATE pending_actions SET status = 'executed', result = %s
        WHERE action_id = %s
        """,
        (json.dumps(result, default=str), action_id),
    )
    _audit(
        conn,
        principal,
        event=f"action.{action_type.value}",
        account_id=claimed["account_id"],
        subject_id=str(action_id),
        run_id=claimed["run_id"],
        detail={"summary": claimed["summary"], "result": result},
    )
    conn.commit()

    log.info("action_executed", action_id=str(action_id), action_type=action_type.value)
    final = fetch_one(conn, "SELECT * FROM pending_actions WHERE action_id = %s", (action_id,))
    assert final is not None
    return _view(final, principal)


def reject(
    conn: Connection, principal: Principal, action_id: UUID, reason: str | None = None
) -> ActionView:
    """Decline an action. Recorded, because a rejection is a decision too."""
    row = fetch_one(
        conn,
        """
        UPDATE pending_actions
        SET status = 'rejected', settled_by = %s, settled_at = now(), error = %s
        WHERE action_id = %s AND status = 'pending'
        RETURNING *
        """,
        (principal.user_id, reason, action_id),
    )
    if row is None:
        existing = fetch_one(
            conn, "SELECT status FROM pending_actions WHERE action_id = %s", (action_id,)
        )
        if existing is None:
            raise ActionError("no such pending action")
        raise ActionAlreadySettled(
            f"this action is already {existing['status']}", status=existing["status"]
        )

    _audit(
        conn,
        principal,
        event="action.rejected",
        account_id=row["account_id"],
        subject_id=str(action_id),
        run_id=row["run_id"],
        detail={"reason": reason, "summary": row["summary"]},
    )
    conn.commit()
    log.info("action_rejected", action_id=str(action_id))
    return _view(row, principal)


def list_pending(conn: Connection, principal: Principal) -> list[ActionView]:
    """Everything awaiting a decision, within the caller's scope."""
    rows = fetch_all(
        conn,
        """
        SELECT * FROM pending_actions
        WHERE status = 'pending' AND expires_at > now()
        ORDER BY prepared_at DESC
        LIMIT 100
        """,
    )
    return [_view(row, principal) for row in rows]


def get(conn: Connection, principal: Principal, action_id: UUID) -> ActionView:
    row = fetch_one(conn, "SELECT * FROM pending_actions WHERE action_id = %s", (action_id,))
    if row is None:
        raise ActionError("no such action")
    return _view(row, principal)


def expire_stale(conn: Connection) -> int:
    """Sweep expired approvals. Safe to run on a schedule.

    Expiry is already enforced at confirm time, so this is housekeeping for the
    dashboard rather than a correctness measure.
    """
    rows = fetch_all(
        conn,
        """
        UPDATE pending_actions SET status = 'expired'
        WHERE status = 'pending' AND expires_at <= now()
        RETURNING action_id
        """,
    )
    conn.commit()
    if rows:
        log.info("actions_expired", count=len(rows))
    return len(rows)


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------


def _escalate_ticket(
    conn: Connection, principal: Principal, action: dict[str, Any]
) -> dict[str, Any]:
    payload = action["payload"]
    ticket_id = payload["ticket_id"]
    priority = payload.get("priority", "P1")

    row = fetch_one(
        conn,
        """
        UPDATE tickets SET status = 'escalated'
        WHERE ticket_id = %s AND account_id = %s
        RETURNING ticket_id, status
        """,
        (ticket_id, action["account_id"]),
    )
    if row is None:
        # RLS-scoped, so this is "not yours or not there" -- either way the
        # action cannot proceed.
        raise ActionError(f"ticket {ticket_id} not found in scope")
    return {"ticket_id": row["ticket_id"], "status": row["status"], "priority": priority}


def _update_order_status(
    conn: Connection, principal: Principal, action: dict[str, Any]
) -> dict[str, Any]:
    payload = action["payload"]
    row = fetch_one(
        conn,
        """
        UPDATE orders SET status = %s
        WHERE order_id = %s AND account_id = %s
        RETURNING order_id, status
        """,
        (payload["status"], payload["order_id"], action["account_id"]),
    )
    if row is None:
        raise ActionError(f"order {payload['order_id']} not found in scope")
    return {"order_id": row["order_id"], "status": row["status"]}


def _issue_service_credit(
    conn: Connection, principal: Principal, action: dict[str, Any]
) -> dict[str, Any]:
    payload = action["payload"]
    amount = payload["amount"]

    # The monthly aggregate cap from a customer agreement. The policy engine
    # reports the cap but cannot enforce it -- that needs this month's issued
    # total, which is a ledger question, answered here at the moment of
    # execution rather than at preparation time when it could go stale.
    cap = payload.get("monthly_cap")
    if cap is not None:
        issued = fetch_one(
            conn,
            """
            SELECT coalesce(sum(amount), 0) AS total FROM service_credits
            WHERE account_id = %s
              AND issued_at >= date_trunc('month', now())
            """,
            (action["account_id"],),
        )
        total = float(issued["total"]) if issued else 0.0
        if total + float(amount) > float(cap):
            raise ActionError(
                f"this credit would take the month to "
                f"{total + float(amount):.2f}, above the agreed cap of {cap}"
            )

    row = fetch_one(
        conn,
        """
        INSERT INTO service_credits (
            tenant_id, account_id, order_id, amount, currency, reason,
            action_id, run_id, issued_by
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING credit_id, amount, currency
        """,
        (
            action["tenant_id"],
            action["account_id"],
            payload.get("order_id"),
            amount,
            payload.get("currency", "INR"),
            payload.get("reason", "failed-pickup service credit"),
            action["action_id"],
            action["run_id"],
            principal.user_id,
        ),
    )
    assert row is not None
    return {
        "credit_id": str(row["credit_id"]),
        "amount": str(row["amount"]),
        "currency": row["currency"],
    }


def _create_follow_up(
    conn: Connection, principal: Principal, action: dict[str, Any]
) -> dict[str, Any]:
    payload = action["payload"]
    row = fetch_one(
        conn,
        """
        INSERT INTO follow_ups (
            tenant_id, account_id, subject, body, due_at, assigned_to,
            action_id, run_id, created_by
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING follow_up_id, subject
        """,
        (
            action["tenant_id"],
            action["account_id"],
            payload["subject"],
            payload.get("body"),
            payload.get("due_at"),
            payload.get("assigned_to"),
            action["action_id"],
            action["run_id"],
            principal.user_id,
        ),
    )
    assert row is not None
    return {"follow_up_id": str(row["follow_up_id"]), "subject": row["subject"]}


_EXECUTORS = {
    ActionType.ESCALATE_TICKET: _escalate_ticket,
    ActionType.UPDATE_ORDER_STATUS: _update_order_status,
    ActionType.ISSUE_SERVICE_CREDIT: _issue_service_credit,
    ActionType.CREATE_FOLLOW_UP: _create_follow_up,
}


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------


def _fail(conn: Connection, action_id: UUID, error: str) -> None:
    """Record a failure in its own transaction, so the trail survives a rollback."""
    conn.execute(
        "UPDATE pending_actions SET status = 'failed', error = %s WHERE action_id = %s",
        (error[:500], action_id),
    )
    conn.commit()


def _expire(conn: Connection, action_id: UUID) -> None:
    conn.execute(
        "UPDATE pending_actions SET status = 'expired' WHERE action_id = %s AND status = 'pending'",
        (action_id,),
    )
    conn.commit()


def _audit(
    conn: Connection,
    principal: Principal,
    *,
    event: str,
    account_id: str | None,
    subject_id: str | None,
    run_id: UUID | None,
    detail: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log (tenant_id, account_id, actor, actor_role, event,
                               subject_id, run_id, detail)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            principal.tenant_id,
            account_id,
            principal.user_id,
            principal.role.value,
            event,
            subject_id,
            run_id,
            json.dumps(detail, default=str),
        ),
    )


def new_idempotency_key() -> str:
    """For callers that need a key before an action exists."""
    return str(uuid4())


# ---------------------------------------------------------------------------
# Handing a question to a person
# ---------------------------------------------------------------------------


def request_handoff(
    conn: Connection,
    principal: Principal,
    *,
    run_id: UUID,
    question: str,
    reason: str | None = None,
) -> ActionView:
    """Queue a support follow-up because the agent could not answer.

    WHY THIS EXISTS. Every refusal carries `escalation_offered=True` and reads "I
    can pass this to a human support agent with everything gathered so far" --
    and until now there was no way to accept. An offer you cannot accept reads as
    a brush-off, and this system refuses often BY DESIGN, so a dead end at every
    refusal quietly undercuts the behaviour it is proudest of.

    WHY IT IS AN ORDINARY LEDGER ACTION. The first attempt executed the follow-up
    immediately, reasoning that asking a human for help grants the asker nothing
    and so needs no second approver. Two things said otherwise, and both were
    right:

    * `follow_ups.action_id` is NOT NULL. The schema refuses to hold a row that
      is not the effect of a ledger action -- the working agreement about single
      ownership of that table is enforced in Postgres, not just in review.
    * `_PREPARE_ROLES` already allows every role to PREPARE a follow-up while
      `_CONFIRM_ROLES` restricts committing to staff. The matrix had this case
      right before the feature existed.

    So a customer's request lands in the support queue as `awaiting_confirmation`
    and a support agent commits it. That is not a weaker outcome than executing
    directly -- it is what "passed to a human" actually means, and the customer
    gets told so.
    """
    run = fetch_one(
        conn,
        "SELECT run_id, account_id, query FROM runs WHERE run_id = %s",
        (run_id,),
    )
    if run is None:
        raise RunNotVisible("that conversation is not available")

    account_id = run["account_id"] or principal.account_id
    if not account_id:
        raise ActionError("a handoff needs an account to attach to")

    asked = (question or run["query"] or "").strip()
    return prepare(
        conn,
        principal,
        action_type=ActionType.CREATE_FOLLOW_UP,
        run_id=run_id,
        account_id=account_id,
        payload={
            "subject": f"Customer needs help: {asked[:120]}",
            "body": "\n".join(
                (
                    "Raised from the support console because the assistant could "
                    "not answer from the available sources.",
                    "",
                    f"Question: {asked}",
                    f"Reason the assistant declined: {reason or 'not recorded'}",
                    f"Run: {run_id}",
                )
            ),
            "assigned_to": "support",
        },
        summary=f"Follow up with the customer about: {asked[:100]}",
    )
