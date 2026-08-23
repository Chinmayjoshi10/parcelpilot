"""Proactive issue detection.

The reactive half of this system answers questions. This half asks them --
surfacing what operations should be looking at before a customer complains.

Every detector here is **deterministic and cited**. No model runs. That is not
a shortcut: a dashboard that says "3 SLA breaches" is only actionable if the
number is reproducible and each row can be traced to the clause that defines
the breach. A model-generated dashboard is a dashboard nobody can act on,
because nobody can tell whether it is right.

Six detectors, each keyed to something real in the operational data:

* `sla_breach`            -- open tickets past their contracted first-response
                             target, resolved per account
* `credit_eligible`       -- money owed right now, from the same rule engine
                             that answers questions
* `pickup_overdue`        -- leading indicator: BOOKED past its window
* `recurring_issue`       -- the same failure across accounts, correlated with
                             a known issue
* `stale_answer`          -- a historical resolution that contradicts current
                             policy
* `unapproved_action`     -- approvals sitting in the ledger, expiring

Detectors run on a `scoped()` connection, so an operations user sees the whole
tenant and a customer sees only their own account -- the same dashboard code
serves both, with no role branching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from psycopg import Connection

from agentcore.db.engine import fetch_all
from agentcore.logging import get_logger
from agentcore.policy.pack import ResolvedPolicy, resolve_for_account
from agentcore.policy.rules import OrderFacts, failed_pickup_credit
from agentcore.types import Citation, Principal, Verdict

log = get_logger(__name__)


class Severity:
    """Ordering for the dashboard. Money and outages outrank hygiene."""

    P1 = "P1"
    P2 = "P2"
    P3 = "P3"

    ORDER = {P1: 0, P2: 1, P3: 2}


@dataclass
class Issue:
    kind: str
    severity: str
    title: str
    detail: str
    account_id: str | None = None
    subject_id: str | None = None
    #: Populated wherever a policy clause defines the threshold being breached.
    #: A breach without a citation is an opinion.
    citation: Citation | None = None
    #: The numbers behind the row, so a human can re-check the arithmetic.
    metrics: dict[str, Any] = field(default_factory=dict)
    #: What operations should do next, when there is an obvious next step.
    suggested_action: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "account_id": self.account_id,
            "subject_id": self.subject_id,
            "citation": (
                {
                    "chunk_id": str(self.citation.chunk_id),
                    "document_id": str(self.citation.document_id),
                    "quote": self.citation.quote,
                }
                if self.citation
                else None
            ),
            "metrics": self.metrics,
            "suggested_action": self.suggested_action,
        }


@dataclass
class Dashboard:
    generated_at: datetime
    as_of: datetime
    issues: list[Issue] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "as_of": self.as_of.isoformat(),
            "counts": self.counts,
            "issues": [issue.as_dict() for issue in self.issues],
        }


def detect(
    conn: Connection,
    principal: Principal,
    *,
    now: datetime | None = None,
) -> Dashboard:
    """Run every detector and return a severity-ordered dashboard."""
    as_of = now or datetime.now(UTC)
    issues: list[Issue] = []

    # Policy is resolved per account and cached across detectors: several of
    # them need the same account's parameters, and each resolution validates the
    # pack against the corpus.
    cache: dict[str | None, ResolvedPolicy] = {}

    def policy_for(account_id: str | None) -> ResolvedPolicy:
        if account_id not in cache:
            cache[account_id] = resolve_for_account(principal.tenant_id, account_id)
        return cache[account_id]

    issues.extend(_sla_breaches(conn, as_of, policy_for))
    issues.extend(_credit_eligible(conn, as_of, policy_for))
    issues.extend(_pickup_overdue(conn, as_of))
    issues.extend(_recurring_issues(conn, policy_for))
    issues.extend(_stale_answers(conn, policy_for))
    issues.extend(_unapproved_actions(conn, as_of))

    issues.sort(key=lambda i: (Severity.ORDER.get(i.severity, 9), i.kind, i.subject_id or ""))

    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.kind] = counts.get(issue.kind, 0) + 1
        counts[issue.severity] = counts.get(issue.severity, 0) + 1

    log.info("dashboard_generated", issues=len(issues), **counts)
    return Dashboard(
        generated_at=datetime.now(UTC), as_of=as_of, issues=issues, counts=counts
    )


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def _sla_breaches(conn: Connection, as_of: datetime, policy_for) -> list[Issue]:
    """Open tickets past their contracted first-response target.

    The target is resolved per account, so Northstar's tickets are measured
    against their contracted 15 minutes rather than the 30-minute default --
    which is the difference between a dashboard that reflects the contracts and
    one that reflects an average.
    """
    rows = fetch_all(
        conn,
        """
        SELECT t.ticket_id, t.account_id, t.subject, t.created_at,
               t.last_customer_message_at, t.assigned_to, a.plan, a.account_name
        FROM tickets t
        JOIN accounts a ON a.tenant_id = t.tenant_id AND a.account_id = t.account_id
        WHERE t.status = 'open'
        ORDER BY t.created_at
        """,
    )

    issues: list[Issue] = []
    for row in rows:
        created = row["created_at"]
        if created is None:
            continue
        policy = policy_for(row["account_id"])
        try:
            target = policy.get("sla.p1_response_minutes")
        except Exception:  # noqa: BLE001 - a missing target is not a breach
            continue

        elapsed = (as_of - created).total_seconds() / 60.0
        limit = float(target.value)
        if elapsed <= limit:
            continue

        clock = policy.parameters.get("sla.p1_clock")
        approximated = clock is not None and clock.value == "business_hours"

        detail = (
            f"Open for {elapsed:.0f} minutes against a {limit:.0f}-minute "
            f"first-response target for {row['account_name']}"
        )
        if approximated:
            # Stated plainly rather than buried: the number is an approximation,
            # and pretending otherwise on a compliance dashboard is worse than
            # showing an imperfect figure honestly.
            detail += (
                ". NOTE: this target is stated in business hours in the "
                "agreement and is approximated here as wall-clock time"
            )

        issues.append(
            Issue(
                kind="sla_breach",
                severity=Severity.P1,
                title=f"{row['ticket_id']} past first-response target",
                detail=detail,
                account_id=row["account_id"],
                subject_id=row["ticket_id"],
                citation=target.citation(),
                metrics={
                    "elapsed_minutes": round(elapsed, 1),
                    "target_minutes": limit,
                    "over_by_minutes": round(elapsed - limit, 1),
                    "target_from": target.scope,
                    "business_hours_approximated": approximated,
                    "assigned_to": row["assigned_to"],
                },
                suggested_action={
                    "action_type": "escalate_ticket",
                    "payload": {"ticket_id": row["ticket_id"], "priority": "P1"},
                    "summary": (
                        f"Escalate {row['ticket_id']} ({row['subject']}) -- "
                        f"{elapsed:.0f} min against a {limit:.0f} min target"
                    ),
                },
            )
        )
    return issues


def _credit_eligible(conn: Connection, as_of: datetime, policy_for) -> list[Issue]:
    """Money owed right now.

    Runs the same `failed_pickup_credit` rule that answers a customer's
    question, so the dashboard and the chat cannot disagree. If they could, one
    of them would be wrong and nobody would know which.
    """
    rows = fetch_all(
        conn,
        """
        SELECT * FROM orders
        WHERE carrier_fault = true AND customer_fault = false
        ORDER BY pickup_window_end
        """,
    )

    issues: list[Issue] = []
    for row in rows:
        order = OrderFacts.from_row(row)
        policy = policy_for(order.account_id)
        decision = failed_pickup_credit(order, policy, now=as_of)
        if decision.verdict is not Verdict.ELIGIBLE:
            continue

        amount = decision.inputs.get("credit_amount")
        currency = order.currency or "INR"
        monthly_cap = decision.inputs.get("monthly_cap")

        payload: dict[str, Any] = {
            "order_id": order.order_id,
            "amount": str(amount),
            "currency": currency,
            "reason": f"failed-pickup service credit for {order.order_id}",
        }
        if monthly_cap is not None:
            # Passed through so execution enforces the aggregate cap against
            # credits already issued this month.
            payload["monthly_cap"] = monthly_cap

        issues.append(
            Issue(
                kind="credit_eligible",
                severity=Severity.P1,
                title=f"{order.order_id} owes a {currency} {amount} service credit",
                detail=decision.explanation,
                account_id=order.account_id,
                subject_id=order.order_id,
                citation=decision.citation,
                metrics={
                    "amount": str(amount),
                    "currency": currency,
                    "delay_hours": decision.inputs.get("delay_hours"),
                    "threshold_hours": decision.inputs.get("delay_threshold_hours"),
                    "threshold_from": decision.inputs.get("delay_threshold_source"),
                    "requires_manager_approval": decision.inputs.get(
                        "requires_manager_approval"
                    ),
                    # From the row, not OrderFacts: that struct is deliberately
                    # limited to fields a rule may read, and a display label is
                    # not one of them.
                    "carrier": row["carrier"],
                },
                suggested_action={
                    "action_type": "issue_service_credit",
                    "payload": payload,
                    "summary": (
                        f"Issue {currency} {amount} credit for {order.order_id} "
                        f"({row['carrier']} missed the pickup window by "
                        f"{decision.inputs.get('delay_hours')}h)"
                    ),
                },
            )
        )
    return issues


def _pickup_overdue(conn: Connection, as_of: datetime) -> list[Issue]:
    """BOOKED orders past their pickup window. A leading indicator.

    Deliberately separate from `credit_eligible`: an order can be late without
    carrier fault being established, and the SOP is explicit that a credit must
    not be promised while fault is unknown. This says "look at it", not "pay".
    """
    rows = fetch_all(
        conn,
        """
        SELECT order_id, account_id, carrier, pickup_window_end, carrier_fault,
               customer_fault, shipment_fee, currency
        FROM orders
        WHERE status = 'BOOKED' AND pickup_actual_at IS NULL
              AND pickup_window_end < %s
        ORDER BY pickup_window_end
        """,
        (as_of,),
    )

    issues: list[Issue] = []
    for row in rows:
        overdue_hours = (as_of - row["pickup_window_end"]).total_seconds() / 3600.0
        fault_known = row["carrier_fault"] or row["customer_fault"]
        issues.append(
            Issue(
                kind="pickup_overdue",
                severity=Severity.P2 if fault_known else Severity.P1,
                title=f"{row['order_id']} not collected, {overdue_hours:.1f}h past window",
                detail=(
                    f"{row['carrier']} has not collected {row['order_id']}. "
                    + (
                        "Fault is attributed, so credit eligibility is decided."
                        if fault_known
                        else "Fault is NOT yet attributed, so no credit can be "
                        "promised until it is established."
                    )
                ),
                account_id=row["account_id"],
                subject_id=row["order_id"],
                metrics={
                    "overdue_hours": round(overdue_hours, 1),
                    "carrier": row["carrier"],
                    "fault_attributed": bool(fault_known),
                },
            )
        )
    return issues


#: Words too common to indicate a shared root cause.
_STOPWORDS = frozenset(
    [
        "the", "a", "an", "and", "or", "for", "of", "to", "in", "on", "is", "are", "was",
        "were", "be", "been", "with", "without", "how", "do", "does", "did", "we", "our", "my",
        "me", "you", "your", "it", "its", "this", "that", "then", "than", "at", "by", "from",
        "as", "not", "no", "yes", "can", "could", "should", "would"
    ]
)


def _recurring_issues(conn: Connection, policy_for) -> list[Issue]:
    """The same failure reported by more than one account.

    Token-overlap clustering rather than embeddings: at this volume it is
    exact, explainable and instant, and "why were these grouped" has a literal
    answer (the shared terms) instead of a cosine score.
    """
    rows = fetch_all(
        conn,
        """
        SELECT ticket_id, account_id, subject, description, status, created_at
        FROM tickets ORDER BY created_at
        """,
    )

    def tokens(text: str) -> set[str]:
        words = re.findall(r"[a-z]{4,}", (text or "").lower())
        return {w for w in words if w not in _STOPWORDS}

    clusters: list[dict[str, Any]] = []
    for row in rows:
        signature = tokens(row["subject"])
        placed = False
        for cluster in clusters:
            shared = signature & cluster["tokens"]
            # Two shared significant terms is a deliberately conservative bar:
            # one is coincidence ("upload"), three would miss real pairs.
            if len(shared) >= 2:
                cluster["tickets"].append(row)
                cluster["shared"] |= shared
                placed = True
                break
        if not placed:
            clusters.append(
                {"tokens": signature, "tickets": [row], "shared": set()}
            )

    issues: list[Issue] = []
    for cluster in clusters:
        tickets = cluster["tickets"]
        accounts = {t["account_id"] for t in tickets}
        if len(tickets) < 2:
            continue

        shared = ", ".join(sorted(cluster["shared"])[:5])
        open_count = sum(1 for t in tickets if t["status"] == "open")

        # A known-issue correlation, where the policy pack knows the real limit.
        citation = None
        extra = ""
        if "upload" in cluster["shared"] or "bulk" in cluster["shared"]:
            policy = policy_for(None)
            known = policy.parameters.get("product.bulk_upload_known_issue_rows")
            limit = policy.parameters.get("product.bulk_upload_max_rows")
            if known and limit:
                citation = known.citation()
                extra = (
                    f" Correlates with the known issue above ~{known.value} rows; "
                    f"the supported product limit is {limit.value} rows, so this is "
                    "a defect rather than a plan limitation."
                )

        issues.append(
            Issue(
                kind="recurring_issue",
                severity=Severity.P2 if open_count else Severity.P3,
                title=f"{len(tickets)} tickets across {len(accounts)} accounts: {shared}",
                detail=(
                    f"{', '.join(t['ticket_id'] for t in tickets)} share the terms "
                    f"[{shared}]." + extra
                ),
                account_id=None if len(accounts) > 1 else next(iter(accounts)),
                subject_id=",".join(t["ticket_id"] for t in tickets),
                citation=citation,
                metrics={
                    "ticket_count": len(tickets),
                    "account_count": len(accounts),
                    "open_count": open_count,
                    "shared_terms": sorted(cluster["shared"]),
                },
            )
        )
    return issues


def _stale_answers(conn: Connection, policy_for) -> list[Issue]:
    """Historical resolutions that contradict current policy.

    This is the detector that closes the loop on the dataset's own trap. A past
    answer is `context_only` and can never ground a claim -- but if it is still
    sitting in the ticket history, an agent may repeat it. So it is checked
    against the operative parameters and flagged when it disagrees.
    """
    rows = fetch_all(
        conn,
        """
        SELECT ticket_id, account_id, subject, historical_resolution
        FROM tickets
        WHERE historical_resolution IS NOT NULL AND historical_resolution <> ''
        ORDER BY ticket_id
        """,
    )

    issues: list[Issue] = []
    for row in rows:
        text = row["historical_resolution"]
        numbers = {int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", text)}
        policy = policy_for(row["account_id"])

        # Contradiction 1: a fee was quoted to an account whose agreement waives it.
        waived = policy.parameters.get("cancellation.fee_waived")
        fee = policy.parameters.get("cancellation.fee_amount")
        if (
            waived is not None
            and bool(waived.value)
            and fee is not None
            and int(fee.value) in numbers
        ):
            issues.append(
                Issue(
                    kind="stale_answer",
                    severity=Severity.P1,
                    title=f"{row['ticket_id']}: past answer contradicts the agreement",
                    detail=(
                        f"A previous resolution quoted a {fee.unit} {fee.value} "
                        f"cancellation fee, but this account's agreement waives the "
                        f"fee entirely. Recorded answer: “{text}”"
                    ),
                    account_id=row["account_id"],
                    subject_id=row["ticket_id"],
                    citation=waived.citation(),
                    metrics={"quoted_numbers": sorted(numbers)},
                )
            )
            continue

        # Contradiction 2: a known-issue threshold reported as a product limit.
        known = policy.parameters.get("product.bulk_upload_known_issue_rows")
        limit = policy.parameters.get("product.bulk_upload_max_rows")
        if (
            known is not None
            and limit is not None
            and int(known.value) in numbers
            and int(limit.value) not in numbers
            and re.search(r"row|upload|csv", text, re.IGNORECASE)
        ):
            issues.append(
                Issue(
                    kind="stale_answer",
                    severity=Severity.P2,
                    title=f"{row['ticket_id']}: known issue reported as a plan limit",
                    detail=(
                        f"A previous resolution described {known.value} rows as the "
                        f"supported maximum. The supported limit is {limit.value}; "
                        f"{known.value} is where a known defect begins. "
                        f"Recorded answer: “{text}”"
                    ),
                    account_id=row["account_id"],
                    subject_id=row["ticket_id"],
                    citation=limit.citation(),
                    metrics={"quoted_numbers": sorted(numbers)},
                )
            )
    return issues


def _unapproved_actions(conn: Connection, as_of: datetime) -> list[Issue]:
    """Approvals waiting on a human, and how long they have left."""
    rows = fetch_all(
        conn,
        """
        SELECT action_id, account_id, action_type, summary, prepared_by,
               prepared_at, expires_at
        FROM pending_actions
        WHERE status = 'pending' AND expires_at > %s
        ORDER BY expires_at
        """,
        (as_of,),
    )
    return [
        Issue(
            kind="unapproved_action",
            severity=Severity.P2,
            title=f"Awaiting approval: {row['action_type']}",
            detail=row["summary"],
            account_id=row["account_id"],
            subject_id=str(row["action_id"]),
            metrics={
                "minutes_remaining": round(
                    (row["expires_at"] - as_of).total_seconds() / 60.0, 1
                ),
                "prepared_by": row["prepared_by"],
            },
        )
        for row in rows
    ]


def credit_exposure(conn: Connection, as_of: datetime, policy_for) -> Decimal:
    """Total currently-owed credit. Used for the dashboard headline figure."""
    total = Decimal("0")
    for issue in _credit_eligible(conn, as_of, policy_for):
        total += Decimal(str(issue.metrics["amount"]))
    return total
