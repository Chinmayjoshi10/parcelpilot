"""Operations dashboard: proactive issue detection.

Every row is produced by a deterministic detector and carries the clause that
defines the threshold it breached. No model runs here. A dashboard saying "3 SLA
breaches" is only actionable if the number is reproducible and each row traces
to a policy clause -- otherwise nobody can tell whether it is right, and nobody
acts on it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from agentcore.analytics.issues import detect
from agentcore.db.engine import fetch_all, fetch_one
from agentcore.logging import get_logger
from app.deps import CurrentPrincipal, ReadOnlyConn, StaffPrincipal

log = get_logger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("")
async def dashboard(
    conn: ReadOnlyConn,
    who: CurrentPrincipal,
    as_of: str | None = Query(default=None, description="ISO timestamp; snapshot demos"),
) -> dict[str, Any]:
    """The issue matrix.

    Available to customers as well as staff -- RLS narrows it to their own
    account, so the same code serves both with no role branching. A customer
    seeing "you are owed a credit" before they have to ask is the product
    working, not a leak.
    """
    reference = None
    if as_of:
        try:
            reference = datetime.fromisoformat(as_of)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="as_of must be an ISO 8601 timestamp",
            ) from exc

    return detect(conn, who, now=reference).as_dict()


@router.get("/summary")
async def summary(conn: ReadOnlyConn, who: StaffPrincipal) -> dict[str, Any]:
    """Headline counters for the console header.

    Deliberately cheap: counts and sums only, no detector logic, so it can be
    polled without re-running policy resolution.
    """
    accounts = fetch_one(conn, "SELECT count(*) AS n FROM accounts")
    open_tickets = fetch_one(
        conn, "SELECT count(*) AS n FROM tickets WHERE status = 'open'"
    )
    orders = fetch_one(conn, "SELECT count(*) AS n FROM orders")
    unpicked = fetch_one(
        conn,
        """
        SELECT count(*) AS n FROM orders
        WHERE status = 'BOOKED' AND pickup_actual_at IS NULL
              AND pickup_window_end < now()
        """,
    )
    pending_actions = fetch_one(
        conn,
        """
        SELECT count(*) AS n FROM pending_actions
        WHERE status = 'pending' AND expires_at > now()
        """,
    )
    credits = fetch_one(
        conn,
        """
        SELECT coalesce(sum(amount), 0) AS total, count(*) AS n
        FROM service_credits WHERE issued_at >= date_trunc('month', now())
        """,
    )
    index = fetch_one(
        conn,
        """
        SELECT index_version_id, document_count, chunk_count, embedded_count,
               embedding_model, activated_at
        FROM index_versions WHERE status = 'active'
        """,
    )

    return {
        "accounts": accounts["n"],
        "open_tickets": open_tickets["n"],
        "orders": orders["n"],
        "pickups_overdue": unpicked["n"],
        "actions_pending": pending_actions["n"],
        "credits_this_month": {
            "count": credits["n"],
            "total": str(credits["total"]),
        },
        # Surfaced in the UI so a demo viewer can see which corpus version
        # produced the answers they are looking at.
        "active_index": index,
    }


@router.get("/accounts")
async def accounts(conn: ReadOnlyConn, who: StaffPrincipal) -> list[dict[str, Any]]:
    """Per-account operational rollup, including whether a contract governs it."""
    return fetch_all(
        conn,
        """
        SELECT a.account_id, a.account_name, a.plan, a.status, a.csm,
               a.contract_file IS NOT NULL AS has_agreement,
               (SELECT count(*) FROM tickets t
                 WHERE t.tenant_id = a.tenant_id AND t.account_id = a.account_id
                   AND t.status = 'open') AS open_tickets,
               (SELECT count(*) FROM orders o
                 WHERE o.tenant_id = a.tenant_id AND o.account_id = a.account_id)
                 AS orders,
               (SELECT coalesce(sum(sc.amount), 0) FROM service_credits sc
                 WHERE sc.tenant_id = a.tenant_id AND sc.account_id = a.account_id
                   AND sc.issued_at >= date_trunc('month', now()))
                 AS credits_this_month
        FROM accounts a
        ORDER BY a.account_id
        """,
    )


@router.get("/sources")
async def sources(conn: ReadOnlyConn, who: CurrentPrincipal) -> list[dict[str, Any]]:
    """The corpus as the caller can see it, with its trust metadata.

    Useful in a demo: switch to a customer and their sibling's agreement simply
    is not in the list. That is RLS, not a filtered view.
    """
    return fetch_all(
        conn,
        """
        SELECT d.filename, d.title, d.source_class, d.authority, d.eligibility,
               d.freshness, d.policy_family, d.version_label, d.owner_account_id,
               d.page_count,
               (SELECT count(*) FROM chunks c WHERE c.document_id = d.document_id)
                 AS chunks
        FROM documents d
        JOIN index_versions iv ON iv.index_version_id = d.index_version_id
        WHERE iv.status = 'active'
        ORDER BY d.authority DESC, d.filename
        """,
    )
