"""Server-authored result tables.

WHY THIS EXISTS.

Asked "show me all open P1 tickets across accounts", the system answered with the
*definition* of a P1 incident and the first-response targets — every sentence
correctly cited, and none of it what was asked. The tickets were never named.

The cause is the citation contract meeting a question it does not fit. Every claim
must carry a verbatim quote from a document, and a database row has no quote. So
the model, asked for records, fills the answer with the only material it *can*
cite: the policy text. The requirement that makes every answer trustworthy was
actively distorting this one.

The fix is not a better prompt. It is to stop asking a citation-bound component to
narrate data it cannot cite. A row needs no source because it **is** the source —
quoting a record against itself is circular — so rows travel beside the answer as
a table this module builds, deterministically, from what the tools already
returned. The model is then told the table is displayed separately and to make
claims only about *rules*, which is the thing it can actually ground.

Same shape as `runs.action_notice`: a fact the system knows about itself, carried
next to the answer rather than pushed through the model.

The formatting lives here rather than in the frontend because the units are known
here. `elapsed_minutes: 30.0` becomes "30 min" once, on the server, instead of
every client re-deciding whether that field is minutes or hours.
"""

from __future__ import annotations

from typing import Any

from agentcore.types import DataTable

#: Findings whose metrics are worth a column, in the order a reader scans them.
#: A kind absent from here still appears in the answer's prose; it just has no
#: table, which is correct for something like `recurring_issue` whose subject is
#: a cluster rather than one record.
_SPECS: dict[str, dict[str, Any]] = {
    "sla_breach": {
        # `{n}` is the count and `{of}` the cohort total when one is known. The
        # title carries the summary because a summary sentence would be DERIVED
        # from the rows and so has no quote -- the same wall that made the model
        # answer with policy definitions. A count in a heading needs no citation.
        "suffix": "past their first-response target",
        "columns": ["Ticket", "Account", "Severity", "Target", "Elapsed", "Over by", "Owner"],
    },
    "credit_eligible": {
        "suffix": "owing a service credit",
        "columns": ["Order", "Account", "Severity", "Amount", "Delay", "Threshold", "Carrier"],
    },
    "pickup_overdue": {
        "suffix": "not collected",
        "columns": ["Order", "Account", "Severity", "Past window", "Carrier", "Fault"],
    },
    "stale_answer": {
        "suffix": "where a past answer contradicts current policy",
        "columns": ["Ticket", "Account", "Severity", "What was said"],
    },
}


#: Whether a kind's subject is a ticket or an order, for the heading's noun.
_TICKET_KINDS = frozenset({"sla_breach", "stale_answer", "recurring_issue"})


def _minutes(value: Any) -> str:
    """Minutes as a reader says them: 30 min, or 2h 30m past an hour."""
    try:
        total = float(value)
    except (TypeError, ValueError):
        return "—"
    if total < 60:
        return f"{total:g} min"
    hours, minutes = divmod(int(round(total)), 60)
    return f"{hours}h" if not minutes else f"{hours}h {minutes}m"


def _hours(value: Any) -> str:
    try:
        return f"{float(value):g}h"
    except (TypeError, ValueError):
        return "—"


def _account(issue: Any, names: dict[str, str]) -> str:
    """Company name where known, falling back to the id.

    The name matters: `ACCT-004` is how the database identifies a customer and
    "Axis Labs" is how a person does. Names come from the cohort query when one
    ran, because findings carry only the id.
    """
    account_id = issue.account_id or ""
    name = names.get(account_id)
    return f"{name} ({account_id})" if name else (account_id or "—")


def _row(issue: Any, names: dict[str, str]) -> list[str]:
    m = issue.metrics or {}
    subject = issue.subject_id or "—"
    account = _account(issue, names)
    severity = issue.severity

    if issue.kind == "sla_breach":
        target = _minutes(m.get("target_minutes"))
        source = m.get("target_from")
        # "15 min (contract)" is the interesting part: it says the target came
        # from this customer's agreement rather than the plan default.
        if source and source != "default":
            target = f"{target} (contract)"
        return [
            subject,
            account,
            severity,
            target,
            _minutes(m.get("elapsed_minutes")),
            f"+{_minutes(m.get('over_by_minutes'))}",
            str(m.get("assigned_to") or "—"),
        ]

    if issue.kind == "credit_eligible":
        amount = f"{m.get('currency', '')} {m.get('amount', '')}".strip() or "—"
        threshold = _hours(m.get("threshold_hours"))
        if m.get("threshold_from") not in (None, "default"):
            threshold = f"{threshold} (contract)"
        return [
            subject, account, severity, amount,
            _hours(m.get("delay_hours")), threshold,
            str(m.get("carrier") or "—"),
        ]

    if issue.kind == "pickup_overdue":
        # Keys read from the detector, not guessed. My first attempt invented
        # `hours_past_window` and `carrier_fault`; the detector emits
        # `overdue_hours` and `fault_attributed`, so every cell rendered as an
        # em-dash — a table that looks like missing data when the data is there.
        return [
            subject,
            account,
            severity,
            _hours(m.get("overdue_hours")),
            str(m.get("carrier") or "—"),
            "attributed" if m.get("fault_attributed") else "not yet attributed",
        ]

    if issue.kind == "stale_answer":
        return [subject, account, severity, (issue.detail or "")[:110]]

    return [subject, account, severity]


def from_findings(issues: list[Any], cohort_rows: list[dict[str, Any]]) -> list[DataTable]:
    """One table per finding kind that has a spec, most severe first.

    Deliberately NOT one merged table. An SLA breach and an owed credit share no
    meaningful columns, and forcing them into one grid produces a row where half
    the cells are blank — which reads as missing data rather than as a different
    kind of finding.
    """
    names = {
        str(r.get("account_id")): str(r.get("account_name"))
        for r in cohort_rows
        if r.get("account_id") and r.get("account_name")
    }

    # Subjects actually in the cohort. "2 of 5 open tickets" was wrong for the
    # stale-answer findings, whose subjects (TKT-450, TKT-451) are CLOSED and so
    # were never among the five open ones. A denominator is only meaningful when
    # the findings are a subset of what it counts.
    cohort_subjects = {
        str(r.get("ticket_id") or r.get("order_id"))
        for r in cohort_rows
        if r.get("ticket_id") or r.get("order_id")
    }

    grouped: dict[str, list[Any]] = {}
    for issue in issues:
        if issue.kind in _SPECS:
            grouped.setdefault(issue.kind, []).append(issue)

    tables: list[DataTable] = []
    for kind, spec in _SPECS.items():
        found = grouped.get(kind)
        if not found:
            continue
        is_ticket = kind in _TICKET_KINDS
        noun = "ticket" if is_ticket else "order"
        count = len(found)
        plural = noun if count == 1 else f"{noun}s"
        # "3 of 5 open tickets past their target" reads; "3 tickets of 5 open
        # past their target" does not. The denominator only applies when the
        # cohort is the same kind of record as the finding.
        subset_of_cohort = bool(cohort_subjects) and all(
            str(i.subject_id) in cohort_subjects for i in found
        )
        if subset_of_cohort and is_ticket:
            lead = f"{count} of {len(cohort_rows)} open {plural}"
        else:
            lead = f"{count} {plural}"
        title = f"{lead} {spec['suffix']}"
        tables.append(
            DataTable(
                title=title,
                columns=spec["columns"],
                rows=[_row(i, names) for i in found],
                note=(
                    "Computed from the records this account may see. "
                    "Figures come from the rule engine, not from the model."
                ),
            )
        )
    return tables


def from_cohort(rows: list[dict[str, Any]]) -> DataTable | None:
    """A plain listing, for a cohort question that produced no findings.

    "Show me the open tickets" is a legitimate question with no threshold to
    breach, so there is nothing for a detector to find and nothing citable. It
    still deserves an answer.
    """
    if not rows:
        return None
    return DataTable(
        title=f"{len(rows)} matching record{'' if len(rows) == 1 else 's'}",
        columns=["Ticket", "Account", "Status", "Subject", "Owner"],
        rows=[
            [
                str(r.get("ticket_id") or r.get("order_id") or "—"),
                (
                    f"{r['account_name']} ({r['account_id']})"
                    if r.get("account_name")
                    else str(r.get("account_id") or "—")
                ),
                str(r.get("status") or "—"),
                str(r.get("subject") or "—")[:80],
                str(r.get("assigned_to") or "unassigned"),
            ]
            for r in rows[:50]
        ],
        note="Scoped by row-level security to the records this caller may see.",
    )
