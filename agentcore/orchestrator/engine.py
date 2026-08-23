"""The agent loop.

Shape: **route -> execute -> synthesise -> validate**, with hard bounds on
steps, tokens and wall clock, and every step appended to the durable run log as
it happens.

Two choices are worth stating plainly.

**The model chooses tools; it never computes outcomes.** Routing is a genuine
model decision -- which is what makes this agentic rather than a fixed pipeline --
but a fee, a threshold or an eligibility verdict comes from
`agentcore/policy/rules.py`, and the model's job is to state and explain it. The
routing step is explicitly told to prefer `policy_decide` and never to do the
arithmetic itself.

**Validation is a gate, not a warning.** An answer whose citations fail is
regenerated once, and then refused. No path returns unvalidated claims, and the
refusal always offers a human.

Everything here writes to `run_steps` as it goes rather than accumulating in
memory, so a dropped connection loses nothing: the transport tails the log with
a cursor, and the reasoning trace survives as an auditable artifact.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import Connection

from agentcore.db.engine import fetch_all, fetch_one
from agentcore.errors import BudgetExhausted, ParcelPilotError, ProviderError
from agentcore.llm.base import LLM, Embedder, TokenBudget, estimate_tokens
from agentcore.logging import bind_run, get_logger
from agentcore.orchestrator import router as fast_router
from agentcore.orchestrator import tables as result_tables
from agentcore.orchestrator.prompts import (
    ROUTING_SCHEMA,
    ROUTING_SYSTEM,
    SYNTHESIS_SYSTEM,
    refusal_message,
    routing_user_prompt,
    synthesis_user_prompt,
)
from agentcore.policy.pack import ResolvedPolicy, resolve_for_account
from agentcore.policy.rules import OrderFacts, cancellation_fee, failed_pickup_credit
from agentcore.retrieval.hybrid import RetrievalResult, detect_superseded, retrieve
from agentcore.settings import EngineConfig
from agentcore.tools import actions
from agentcore.tools.actions import ActionView
from agentcore.trust.validator import (
    ANSWER_SCHEMA,
    parse_answer,
    render_prose,
    validate_claims,
)
from agentcore.types import (
    ActionType,
    Answer,
    ConflictNote,
    EngineResponse,
    PolicyDecision,
    Principal,
    Query,
    Refusal,
    RefusalReason,
    RetrievedChunk,
    RunStatus,
    StepKind,
    TokenUsage,
    Verdict,
)

log = get_logger(__name__)

#: Query templates the agent may run. The entire allowed surface: there is no
#: path from a model-generated string to the database, and a tool asking for an
#: unlisted template is an error rather than a query.
RECORD_TEMPLATES: dict[str, str] = {
    "order": "SELECT * FROM orders WHERE order_id = %(id)s",
    "ticket": """
        SELECT ticket_id, account_id, created_at, status, subject, description,
               channel, assigned_to, last_customer_message_at
        FROM tickets WHERE ticket_id = %(id)s
    """,
    "account": """
        SELECT account_id, account_name, plan, status, csm, premium_support
        FROM accounts WHERE account_id = %(id)s
    """,
}


#: Cohort queries, for questions that name no single record.
#:
#: "Show me all open P1 tickets across accounts" has no id to look up, so the
#: record templates above cannot serve it -- and the answer lives in a table, not
#: a PDF, so document search finds nothing citable and the run refuses. Both
#: headline internal workflows failed that way while the dashboard computed the
#: same answers correctly one tab away.
#:
#: These are still fixed templates: there is no path from a model-generated
#: string to the database. Filters are bound parameters, and every row comes back
#: through the SCOPED connection -- so a customer running this sees only their
#: own tickets, and the same SQL is safe for both audiences.
COHORT_TEMPLATES: dict[str, str] = {
    "open_tickets": """
        SELECT t.ticket_id, t.account_id, a.account_name, t.status, t.subject,
               t.created_at, t.last_customer_message_at, t.assigned_to
        FROM tickets t
        JOIN accounts a ON a.tenant_id = t.tenant_id AND a.account_id = t.account_id
        WHERE t.status <> 'closed'
          AND (%(account_id)s::text IS NULL OR t.account_id = %(account_id)s::text)
        ORDER BY t.created_at
        LIMIT 50
    """,
    "tickets_by_account": """
        SELECT t.ticket_id, t.account_id, a.account_name, t.status, t.subject,
               t.created_at, t.historical_resolution IS NOT NULL AS has_history
        FROM tickets t
        JOIN accounts a ON a.tenant_id = t.tenant_id AND a.account_id = t.account_id
        WHERE (%(account_id)s::text IS NULL OR t.account_id = %(account_id)s::text)
        ORDER BY t.created_at DESC
        LIMIT 50
    """,
}


#: Record ids come from the router's recogniser rather than a second copy of the
#: pattern. Two copies drift: the router learned to read "ord 2001" while this
#: file still demanded "ORD-2001", which would let a run stage an action against
#: a record the halt condition had never checked. A guard and the planner that
#: feeds it have to agree on what a record id is.


#: Retrieval queries that surface the clause justifying each action type. Used
#: when the router proposes an action without asking for policy context.
_JUSTIFICATION_QUERIES: dict[str, str] = {
    "escalate_ticket": "severity definitions P1 critical escalation first response target",
    "issue_service_credit": "failed pickup service credit eligibility approval",
    "update_order_status": "order cancellation status picked up return to origin",
    "create_follow_up": "approval and uncertainty request verification",
}


#: Plain-language versions of the reasons an action does not get staged. Keyed
#: on a fragment of the internal error, because the internal wording belongs in
#: the run log and not in front of a customer.
_ACTION_NOTICES: tuple[tuple[str, str], ...] = (
    (
        "may not prepare",
        "I have not raised this — only a support agent can, so nothing is "
        "queued. Ask them and they can raise it from what is above.",
    ),
    (
        "no such record in scope",
        "I have not raised this, because I cannot see the record it refers to. "
        "Nothing is queued.",
    ),
    (
        "refusing to guess",
        "I have not raised this, because more than one record matched and "
        "choosing between them is not a guess I should make. Name the one you "
        "mean and I will try again. Nothing is queued.",
    ),
)


def _action_notice(action_error: str | None) -> str | None:
    """Turn an internal action failure into something a person should read.

    Deliberately not a model claim. See `EngineResponse.action_notice` for why
    asking the model to say this could never work: every claim needs a verbatim
    source quote, and no clause in the corpus says "you are not authorised".
    """
    if not action_error:
        return None
    for fragment, notice in _ACTION_NOTICES:
        if fragment in action_error:
            return notice
    # Unrecognised cause: still say the thing that matters, which is that the
    # action did NOT happen. Silence here is what let a customer believe a
    # credit was queued.
    return (
        "I have not raised this, so nothing is queued. A support agent can "
        "action it for you."
    )


def _justification_query(requested: list[dict[str, Any]], query: Query) -> str:
    """The retrieval query that should accompany a proposed action."""
    action = next(
        (c for c in requested if c.get("tool") == "prepare_action"), {}
    )
    return _JUSTIFICATION_QUERIES.get(
        str(action.get("action_type") or ""), query.text
    )


@dataclass
class _RunState:
    """Mutable bookkeeping for one run."""

    run_id: UUID
    seq: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    started: float = field(default_factory=time.monotonic)
    #: Set when the agent proposes a state change. Held on the run state rather
    #: than as a local, so EVERY refusal path withdraws it automatically --
    #: including the provider-error path, which previously left an approval
    #: sitting in the queue after the run failed to explain it.
    prepared_action: ActionView | None = None

    def add_usage(self, other: TokenUsage) -> None:
        self.usage = TokenUsage(
            prompt_tokens=self.usage.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.usage.completion_tokens + other.completion_tokens,
        )


class Orchestrator:
    def __init__(
        self,
        config: EngineConfig,
        llm: LLM,
        *,
        embedder: Embedder | None = None,
    ) -> None:
        self._config = config
        self._llm = llm
        self._embedder = embedder

    # -- public ------------------------------------------------------------

    def create_run(self, conn: Connection, principal: Principal, query: Query) -> UUID:
        """Register a run and return its id, without executing it.

        Split out from `run` for the HTTP transport: the API creates the run
        synchronously so it can hand the caller a real id to stream, then
        executes in the background. A client should never have to poll for a run
        that might not exist yet.
        """
        return self._create_run(conn, principal, query)

    async def run(
        self,
        conn: Connection,
        principal: Principal,
        query: Query,
        *,
        now: datetime | None = None,
    ) -> EngineResponse:
        """Create a run and answer it. The synchronous path (CLI, tests)."""
        return await self.resume_run(
            conn, principal, query, self._create_run(conn, principal, query), now=now
        )

    async def resume_run(
        self,
        conn: Connection,
        principal: Principal,
        query: Query,
        run_id: UUID,
        *,
        now: datetime | None = None,
    ) -> EngineResponse:
        """Execute an already-created run on an already-scoped connection.

        `conn` must come from `scoped()`. Every record and chunk this loop sees
        is filtered by RLS, so the agent physically cannot reach another
        account's data regardless of what it decides to look up.
        """
        state = _RunState(run_id=run_id)
        budget = TokenBudget(self._config.agent.token_budget)
        # Precedence: an explicit `now` (staff "as of", or a test) beats the
        # configured snapshot, which beats the wall clock.
        #
        # Without the middle term a customer got wall-clock arithmetic against a
        # fixed dataset: "171.1 hours past the 4-hour threshold" for the same
        # pickup the dashboard reported as 4.5 hours late. Staff had an "as of"
        # field and customers had nothing, so only the unprivileged path was
        # wrong -- and the credit amount stayed correct, which is what let it go
        # unnoticed. `data.snapshot_at` is server-side config precisely because a
        # customer must not be able to choose what time it is.
        reference_time = (
            now or self._config.data.snapshot_at or datetime.now().astimezone()
        )

        with bind_run(
            run_id=state.run_id,
            tenant_id=principal.tenant_id,
            principal_role=principal.role.value,
            account_id=principal.account_id,
        ):
            try:
                return await self._execute(
                    conn, principal, query, state, budget, reference_time
                )
            except BudgetExhausted as exc:
                return self._refuse(
                    conn, state, RefusalReason.BUDGET_EXHAUSTED, exc.message
                )
            except ProviderError as exc:
                # The model is unavailable. Retrieval and policy may still have
                # produced something useful, but an unsynthesised answer is not
                # an answer -- so refuse honestly and offer escalation.
                log.error("provider_unavailable", error=exc.message)
                return self._refuse(
                    conn,
                    state,
                    RefusalReason.UPSTREAM_UNAVAILABLE,
                    "The reasoning service is unavailable.",
                )
            except ParcelPilotError as exc:
                self._mark_failed(conn, state, exc.message)
                raise
            except Exception as exc:  # noqa: BLE001 - see below
                # A catch-all, because the alternative is worse than a broad
                # except. Any exception type this block does not name propagates
                # with the run still marked `running`, and nothing ever moves it:
                # the SSE stream holds until its 300-second ceiling and the UI
                # spins forever. A crash the user can see beats a crash that
                # looks like slowness.
                log.exception("run_crashed")
                self._mark_failed(conn, state, f"{type(exc).__name__}: {exc}")
                raise

    def _mark_failed(self, conn: Connection, state: _RunState, message: str) -> None:
        """Record a run as failed, on a connection that may be unusable.

        THE BUG THIS FIXES. A malformed query raised `RepositoryError`, the
        handler above caught it and called `_step` then `_finish` to record the
        failure — and both of those silently failed too, because PostgreSQL had
        already aborted the transaction. Every statement after an error in the
        same transaction raises `current transaction is aborted`. So the status
        stayed `running` forever, the stream held open, and a hard SQL error
        presented as an infinitely spinning UI.

        The error handler could not report the error. Rolling back first is what
        makes the connection usable again — and `ScopedConnection` re-binds the
        RLS scope on rollback, so the writes below are still correctly scoped.
        """
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 - nothing better to do than continue
            log.warning("rollback_failed_before_marking_run_failed")
        try:
            self._step(conn, state, StepKind.ERROR, "failed", {"error": message})
            self._finish(conn, state, RunStatus.FAILED, error=message)
        except Exception:  # noqa: BLE001 - last resort
            # If even this fails the run is unrecoverable, but say so in the log
            # rather than leaving a silent `running` row behind.
            log.exception("could_not_mark_run_failed", run_id=str(state.run_id))

    # -- pipeline ----------------------------------------------------------

    async def _execute(
        self,
        conn: Connection,
        principal: Principal,
        query: Query,
        state: _RunState,
        budget: TokenBudget,
        now: datetime,
    ) -> EngineResponse:
        self._set_status(conn, state, RunStatus.RUNNING)

        # --- 0. is this question even about you? ---
        #
        # The sibling guard further down catches a question naming another
        # account's RECORD, because the scoped lookup returns nothing. It cannot
        # catch a question naming another COMPANY, because there is no id:
        #
        #     Q (as ACCT-001): "What cancellation terms does LumenWorks have?"
        #     A: "For Northstar Logistics (ACCT-001), any booked shipment can be
        #         cancelled before pickup without a cancellation fee..."
        #
        # Nothing leaked -- LumenWorks' agreement was never read -- but the reply
        # describes one company's contract in answer to a question about
        # another's, and an earlier build merged the identities outright.
        #
        # Checked first because it costs one indexed query and saves a retrieval
        # plus a synthesis call. `app_names_foreign_account` returns a single
        # boolean and never a row, so the request path still has no tenant-wide
        # read on `accounts`; staff can see every account and so never trip it.
        if not principal.is_staff:
            names_other = fetch_one(
                conn, "SELECT app_names_foreign_account(%s) AS x", (query.text,)
            )
            if names_other and names_other["x"]:
                return self._refuse(
                    conn,
                    state,
                    RefusalReason.OUT_OF_SCOPE,
                    # Worded identically whether the company named is a customer
                    # here or does not exist at all, so the bit the function
                    # returned never reaches the user. It only decides to stop.
                    "That question is about another company, and I can only "
                    "answer about your own account. Ask me the same thing about "
                    "your account and I will answer it in full.",
                    detail={"reason": "question names an account outside scope"},
                )

        # --- 1. route ---
        step_started = time.monotonic()
        plan = await self._route(principal, query.text, budget, state)
        self._step(
            conn,
            state,
            StepKind.DECOMPOSE,
            "Decompose",
            {
                "reasoning": plan.get("reasoning", ""),
                "tools": [t.get("tool") for t in plan.get("tools", [])],
                "out_of_scope": plan.get("out_of_scope", False),
            },
            duration_ms=int((time.monotonic() - step_started) * 1000),
        )

        if plan.get("out_of_scope"):
            return self._refuse(
                conn,
                state,
                RefusalReason.OUT_OF_SCOPE,
                "That question is outside what I can help with.",
            )

        # --- 2. execute tools ---
        # Deduplicated and capped: a routing step that returns the same tool
        # eight times must not turn into eight round trips.
        requested = plan.get("tools") or []
        if not requested:
            requested = [{"tool": "doc_search", "search_query": query.text}]

        # An action must arrive with cited justification, so retrieval is
        # guaranteed rather than hoped for. Observed in practice: asked to
        # escalate a ticket, the router chose data_query + prepare_action and no
        # doc_search -- leaving nothing citable, so the run refused and withdrew
        # its own proposal. Correct behaviour, useless outcome. An approver
        # should see the clause that justifies the action, so the plan is
        # completed here rather than left to the model's discretion.
        if any(c.get("tool") == "prepare_action" for c in requested) and not any(
            c.get("tool") == "doc_search" for c in requested
        ):
            requested = [
                *requested,
                {"tool": "doc_search", "search_query": _justification_query(requested, query)},
            ]

        retrieval = RetrievalResult()
        decisions: list[PolicyDecision] = []
        records: list[dict[str, Any]] = []
        #: Findings from the deterministic detectors, when the question was about
        #: a cohort rather than a record. Carried separately from `decisions`
        #: because an Issue describes a population and a PolicyDecision describes
        #: one subject, and blurring them would let the model attribute a
        #: tenant-wide count to a single order.
        issues: list[Any] = []
        cohort_rows: list[dict[str, Any]] = []
        policy: ResolvedPolicy | None = None
        prepared_action: ActionView | None = None
        action_error: str | None = None
        seen: set[str] = set()
        # Record ids the question named, split by whether the SCOPED read could
        # see them. A named id that resolved to nothing must stop the run, not
        # decorate it -- see the guard after this loop.
        resolved_ids: set[str] = set()
        invisible_ids: set[str] = set()

        for call in requested[: self._config.agent.max_steps]:
            self._check_deadline(state)
            tool = call.get("tool")
            fingerprint = json.dumps(call, sort_keys=True, default=str)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)

            step_started = time.monotonic()
            if tool == "doc_search":
                search = (call.get("search_query") or query.text).strip()
                result = await retrieve(
                    conn,
                    principal,
                    search,
                    self._config.retrieval,
                    embedder=self._embedder,
                )
                retrieval = self._merge_retrieval(retrieval, result)
                detail = {"query": search, **result.as_log()}

            elif tool == "policy_decide":
                decision, policy = self._decide(
                    conn, principal, call, now, existing_policy=policy
                )
                if decision is not None:
                    decisions.append(decision)
                    detail = {
                        "rule": decision.rule_id,
                        "verdict": decision.verdict.value,
                        "record_id": call.get("record_id"),
                    }
                else:
                    detail = {
                        "record_id": call.get("record_id"),
                        "error": "record not found or not visible",
                    }

            elif tool in ("data_query", "ticket_history"):
                requested_id = call.get("record_id")
                found = self._lookup(conn, requested_id)
                records.extend(found)
                detail = {"record_id": requested_id, "rows": len(found)}
                # A named record that resolved to nothing is tracked, not merely
                # logged. See `_invisible_records` below for why.
                if requested_id:
                    (resolved_ids if found else invisible_ids).add(str(requested_id))

            elif tool == "cohort_query":
                # A question about a SET of records rather than one named record.
                rows, detail = self._cohort(conn, principal, call)
                records.extend(rows)
                # Held separately as well: these rows carry account NAMES, which
                # findings do not, and "Axis Labs (ACCT-004)" reads better in a
                # table than the id alone.
                cohort_rows.extend(rows)

            elif tool == "issue_scan":
                # The deterministic detectors the proactive dashboard already
                # runs, reachable from chat. They return a `Citation` for every
                # threshold they apply, which is what lets an answer built on one
                # pass the same validation as any other -- so this is the
                # `policy_decide` pattern, applied to cohorts.
                found, detail = self._scan_issues(conn, principal, call, now)
                issues.extend(found)

            elif tool == "prepare_action":
                # The state-changing tool. It PREPARES only: the ledger row is
                # written, nothing is executed, and a human must confirm. The
                # model can therefore never cause an effect, only propose one.
                prepared, detail = self._prepare_action(
                    conn, principal, call, decisions, records, state.run_id,
                    question=query.text,
                )
                if prepared is not None:
                    prepared_action = prepared
                    state.prepared_action = prepared
                else:
                    # Kept so synthesis can say the action did NOT happen. Left
                    # out, the model inferred "prepared" from an ALLOWED verdict
                    # plus an imperative question and told a customer their
                    # credit was queued when the ledger was empty.
                    action_error = str(detail.get("error") or "it could not be prepared")

            else:
                detail = {"error": f"unknown tool {tool!r}"}

            self._step(
                conn,
                state,
                StepKind.TOOL_RESULT,
                str(tool),
                detail,
                duration_ms=int((time.monotonic() - step_started) * 1000),
            )

        # An invisible record terminates the run.
        #
        # THE BUG THIS CLOSES. Asked "what is the cancellation fee on ORD-2001?"
        # as ACCT-001, row-level security did its job perfectly -- the scoped
        # read returned zero rows and the rule engine reported "record not found
        # or not visible". The run then carried on to `doc_search`, synthesised
        # from the generic policy documents as though they described that order,
        # and the citation validator passed it because the quotes were real:
        #
        #     "There is no cancellation fee for ORD-2001 because your agreement
        #      with Northstar Logistics allows cancellation..."
        #
        # Confident, cited, and about a record belonging to another company. A
        # second phrasing merged the two identities outright ("LumenWorks, as
        # Northstar Logistics, can cancel..."). No data leaked -- nothing of
        # ORD-2001 was ever read -- but the answer is worse than a leak in one
        # respect: a customer could act on it.
        #
        # The failure was never in the retrieval or the validator. It was that
        # "I could not see this record" arrived as a tool result rather than as a
        # halt condition. A citation validator proves an answer is GROUNDED, not
        # that it is ABOUT THE RIGHT RECORD; those are different properties and
        # only one of them was being checked.
        #
        # One reason code covers "not yours" and "does not exist" deliberately.
        # Separate messages would confirm which ids are real, turning an honest
        # refusal into an enumeration oracle.
        if invisible_ids:
            unseen = sorted(invisible_ids)
            names = ", ".join(unseen)
            plural = "those records" if len(unseen) > 1 else "that record"
            return self._refuse(
                conn,
                state,
                RefusalReason.RECORD_NOT_FOUND,
                f"I cannot find {names} on your account, so I will not answer "
                f"questions about it -- a confident answer about the wrong "
                f"record is worse than none. If you believe {plural} should be "
                f"visible to you, a support agent can check.",
                detail={"invisible": unseen, "resolved": sorted(resolved_ids)},
            )

        # A policy decision's clause must be citable.
        #
        # The rule engine hands back a citation resolved by `policy validate`
        # against the live index -- a real chunk of a real groundable document.
        # But if the router chose only policy_decide, that chunk is not in the
        # retrieval result, so validation would reject the very clause the
        # engine told the model to quote, and every policy answer would refuse.
        # Admitting it here keeps one validation path rather than exempting
        # policy citations from checking.
        # The records the answer is about, formatted server-side and carried
        # beside it. See `agentcore/orchestrator/tables.py` for why this is not
        # the model's job: a row is its own source, so it needs no citation, and
        # asking a citation-bound component to narrate one made it answer with
        # policy definitions instead of naming a single ticket.
        # The plain listing comes FIRST, because it is what was asked. Findings
        # follow as "and here is what needs attention".
        #
        # Showing only findings was wrong for "show all tickets": it returned the
        # three breached tickets and silently dropped the other two open ones, so
        # a request for everything answered with a subset. A table that quietly
        # filters is worse than no table -- the reader has no way to know it did.
        answer_tables = []
        if cohort_rows:
            listing = result_tables.from_cohort(cohort_rows)
            if listing is not None:
                answer_tables.append(listing)
        answer_tables.extend(result_tables.from_findings(issues, cohort_rows))

        # Detector findings carry clauses too, for the same reason and with the
        # same consequence if they are left out.
        retrieval = self._admit_decision_clauses(
            conn, retrieval, [*decisions, *issues]
        )

        self._persist_candidates(conn, state, principal, retrieval)

        # A policy decision or a detector finding carries its own clause, so
        # either can answer even when free-text retrieval found nothing citable.
        # This is exactly why the ops questions used to refuse: their answers
        # live in a table, so `doc_search` returned nothing groundable and the
        # run stopped here with `low_confidence` while the dashboard had the
        # answer all along.
        if not retrieval.groundable and not decisions and not issues:
            return self._refuse(
                conn,
                state,
                RefusalReason.NO_ELIGIBLE_SOURCE,
                "I could not find a current, citable source covering that.",
            )

        # --- 3. record any conflict the retrieval exposed ---
        conflicts: list[ConflictNote] = []
        # Keyed by the pair of documents, not by chunk: two retrieved chunks of
        # the deprecated policy describe ONE supersession, and reporting it
        # twice makes the answer look confused.
        seen_supersessions: set[tuple[Any, Any]] = set()
        for stale, current in detect_superseded(retrieval):
            key = (stale.document.document_id, current.document.document_id)
            if key in seen_supersessions:
                continue
            seen_supersessions.add(key)
            note = ConflictNote(
                rule_id="current_beats_deprecated",
                winning_document_id=current.document.document_id,
                losing_document_ids=[stale.document.document_id],
                explanation=(
                    f"{current.document.title} ({current.document.version_label}) "
                    f"supersedes {stale.document.title} "
                    f"({stale.document.version_label}); only the current version is "
                    "operative."
                ),
            )
            conflicts.append(note)
            self._step(conn, state, StepKind.CONFLICT, "Superseded source", {
                "winning": current.document.filename,
                "losing": stale.document.filename,
            })
        for decision in decisions:
            conflicts.extend(decision.conflicts)

        # --- 4. synthesise and validate ---
        attempts = self._config.agent.citation_validation.max_regenerations + 1
        last_outcome = None

        for attempt in range(1, attempts + 1):
            self._check_deadline(state)
            step_started = time.monotonic()

            completion = await self._synthesise(
                principal,
                query.text,
                retrieval,
                decisions,
                records,
                policy,
                budget,
                state,
                prepared_action=prepared_action,
                action_error=action_error,
                tables=answer_tables,
            )
            payload = completion.data or {}
            claims, insufficient = parse_answer(payload)

            self._step(
                conn,
                state,
                StepKind.SYNTHESIZE,
                f"Synthesize (attempt {attempt})",
                {
                    "claims": len(claims),
                    "insufficient_evidence": insufficient,
                    "model": completion.model,
                    "tokens": completion.usage.total,
                },
                duration_ms=int((time.monotonic() - step_started) * 1000),
            )

            # No claims is not an answer, whatever the model said about its
            # evidence.
            #
            # The condition used to be `insufficient and not claims`, so a
            # response with ZERO claims and `insufficient_evidence: false` fell
            # through to validation. Validating nothing rejects nothing, so
            # `outcome.ok` was false with an empty `reasons` list, and the run
            # refused as CITATION_VALIDATION_FAILED — telling the user "I could
            # not produce an answer whose every statement traces to a source"
            # when there were no statements and nothing had been rejected. The
            # empty reasons list was the tell.
            #
            # That is the same defect class as the fabricated action state: the
            # system asserting something untrue about its own workings. A wrong
            # reason is worse than a vague one, because it sends whoever reads
            # the trace after the wrong problem — I went looking for a citation
            # bug that did not exist.
            #
            # It is also intermittent: the same question answered with two claims
            # on the previous attempt. So this is a real robustness hole, not a
            # property of one phrasing.
            if not claims:
                if not insufficient:
                    log.warning(
                        "model_returned_no_claims_but_claimed_sufficient_evidence",
                        attempt=attempt,
                    )
                    if attempt < attempts:
                        # Contradictory response. Worth one retry before refusing,
                        # because the same prompt produced a good answer moments
                        # earlier.
                        continue
                # A table with no claims is an ANSWER, not a refusal.
                #
                # The run produced a correct three-row table -- ticket, account,
                # contracted target, elapsed, over by -- and no claims, because
                # there is no document sentence to quote for "TKT-501 is 15
                # minutes over its target". Treating that as a refusal presented
                # a complete answer as a failure.
                if answer_tables:
                    answer = Answer(
                        conflicts=conflicts, prose="", is_table_only=True
                    )
                    self._finish(
                        conn,
                        state,
                        RunStatus.COMPLETED,
                        answer=answer,
                        index_version_id=retrieval.index_version_id,
                        tables=answer_tables,
                    )
                    log.info("answered_with_table_only", tables=len(answer_tables))
                    return EngineResponse(
                        run_id=state.run_id,
                        status=RunStatus.COMPLETED,
                        answer=answer,
                        steps=self._steps(conn, state.run_id),
                        usage=state.usage,
                        index_version=retrieval.index_version_id,
                        tables=answer_tables,
                    )

                return self._refuse(
                    conn,
                    state,
                    RefusalReason.LOW_CONFIDENCE,
                    "The available sources do not answer that.",
                )

            # Validated against the groundable channel only. Conflict and
            # context chunks were in the prompt, so the model can attempt to
            # cite them -- and must fail if it does.
            outcome = validate_claims(
                claims,
                retrieval.groundable,
                require_verbatim=(
                    self._config.agent.citation_validation.require_verbatim_span
                ),
            )
            last_outcome = outcome

            self._step(
                conn,
                state,
                StepKind.VALIDATE,
                f"Validate citations (attempt {attempt})",
                {
                    **outcome.as_log(),
                    "rejected": [r.as_dict() for r in outcome.rejected[:5]],
                },
            )

            # Partial acceptance, on the last attempt only.
            #
            # WHY. "Is TKT-501 an SLA breach?" produced three claims. Two
            # validated. The third quoted `P1\nEnterprise\n30 minutes, 24x7` --
            # a row-and-column intersection of the SLA table, which the PDF
            # flattens COLUMN-MAJOR:
            #
            #     Plan / P1 / P2 / P3 / Enterprise / 30 minutes, 24x7 / 2 hours
            #
            # The model reconstructed the table's MEANING correctly and then
            # could not cite it, because those tokens are not contiguous in the
            # text. Verbatim span validation is structurally incompatible with a
            # flattened table, and no amount of whitespace normalisation helps --
            # the words genuinely are not adjacent. The real fix is at ingestion
            # (emit tables row-wise so a citable span exists); until then,
            # refusing an answer that carries two independently verified claims
            # because a third hit that seam is over-strict.
            #
            # THE RISK, and it is not hypothetical -- the eval caught it. If the
            # model wrote "X applies" then "but Y overrides X", keeping only the
            # first is actively wrong. Guarding on "the lead claim survived" was
            # not enough: on the flagship question one of three claims validated,
            # the answer shipped as "you can cancel without a fee", and it
            # silently lost the clause showing the INR 250 default it overrides.
            # An answer that hides its own conflict is worse than a refusal, and
            # `answer-northstar-no-fee` went red for exactly that reason.
            #
            # So two conditions, not one: the lead claim must survive (rule 9
            # makes claim one the outcome) AND a strict majority must survive.
            # Losing one supporting clause of three is a thinner answer; losing
            # two of three is a different answer. The drop is recorded as its own
            # step either way, so the trace never hides it.
            kept, total = len(outcome.claims), len(claims)
            partial = (
                not outcome.ok
                and attempt >= attempts
                and outcome.claims
                and claims
                and outcome.claims[0].text == claims[0].get("text")
                and kept * 2 > total
            )
            if partial:
                self._step(
                    conn,
                    state,
                    StepKind.VALIDATE,
                    "Partial answer: unverifiable claims dropped",
                    {
                        "kept": kept,
                        "of": total,
                        "dropped": len(outcome.rejected),
                        "why": (
                            "the lead claim verified; the rest could not be "
                            "traced to a contiguous span and were removed"
                        ),
                    },
                )
                log.info(
                    "answer_partially_accepted",
                    kept=kept,
                    of=total,
                    dropped=len(outcome.rejected),
                )

            if outcome.ok or partial:
                prose, ordered = render_prose(outcome.claims)
                answer = Answer(
                    claims=outcome.claims, conflicts=conflicts, prose=prose
                )
                # A run with a prepared action is not "completed": something is
                # waiting on a person. The status says so, so a queue of
                # awaiting-confirmation runs is a query rather than a scan.
                final_status = (
                    RunStatus.AWAITING_CONFIRMATION
                    if prepared_action is not None
                    else RunStatus.COMPLETED
                )
                self._finish(
                    conn,
                    state,
                    final_status,
                    answer=answer,
                    index_version_id=retrieval.index_version_id,
                    action_notice=_action_notice(action_error),
                    tables=answer_tables,
                )
                return EngineResponse(
                    run_id=state.run_id,
                    status=final_status,
                    answer=answer,
                    pending_action_id=(
                        prepared_action.action_id if prepared_action else None
                    ),
                    steps=self._steps(conn, state.run_id),
                    usage=state.usage,
                    index_version=retrieval.index_version_id,
                    action_notice=_action_notice(action_error),
                    tables=answer_tables,
                )

            if attempt < attempts:
                log.warning("regenerating_after_validation_failure", **outcome.as_log())

        # Validation failed every attempt. Refuse rather than show claims the
        # model believed alongside ones it invented.
        reasons = sorted({r.reason for r in (last_outcome.rejected if last_outcome else [])})

        # ...but the rows survive the prose.
        #
        # "Show me all open P1 tickets across accounts" produced three correct
        # tables and, about one run in three, prose whose citations would not
        # validate. The whole answer was then discarded -- including the records
        # the question actually asked for, which are computed by the rule engine
        # from rows the caller is allowed to see and carry no citations because
        # a row IS its own source (see orchestrator/tables.py).
        #
        # So a citation failure is scoped to the thing it judges. It says the
        # PROSE could not be grounded; it says nothing about arithmetic over
        # typed columns. Throwing the tables away made an unverifiable sentence
        # suppress verified data, which is the opposite of the trade this system
        # is meant to make.
        #
        # This is the same shape as the table-only path above, reached from the
        # other direction: there the model produced no claims, here it produced
        # claims that did not hold up. Either way the tables stand on their own.
        if answer_tables:
            answer = Answer(conflicts=conflicts, prose="", is_table_only=True)
            self._step(
                conn,
                state,
                StepKind.VALIDATE,
                "Prose dropped; the table is shown on its own",
                {"reasons": reasons},
            )
            self._finish(
                conn,
                state,
                RunStatus.COMPLETED,
                answer=answer,
                index_version_id=retrieval.index_version_id,
                tables=answer_tables,
            )
            log.info("table_survived_validation_failure", tables=len(answer_tables))
            return EngineResponse(
                run_id=state.run_id,
                status=RunStatus.COMPLETED,
                answer=answer,
                steps=self._steps(conn, state.run_id),
                usage=state.usage,
                index_version=retrieval.index_version_id,
                tables=answer_tables,
            )

        return self._refuse(
            conn,
            state,
            RefusalReason.CITATION_VALIDATION_FAILED,
            "I could not produce an answer whose every statement traces to a source.",
            detail={"reasons": reasons},
        )

    # -- steps -------------------------------------------------------------

    async def _route(
        self, principal: Principal, question: str, budget: TokenBudget, state: _RunState
    ) -> dict[str, Any]:
        """Plan the tools for this question.

        The fast planner is the default. Routing is classification, the domain
        has explicit signals (record-id shapes, a closed set of verbs), and the
        model round trip cost ~2.6s of a ~8s answer. Deterministic planning also
        makes tool selection reproducible, which matters more here than nuance.
        """
        if self._config.agent.router == "fast":
            return fast_router.plan(question)

        prompt = routing_user_prompt(question, principal)
        budget.reserve(estimate_tokens(ROUTING_SYSTEM + prompt))
        completion = await self._llm.complete_json(
            system=ROUTING_SYSTEM,
            user=prompt,
            schema=ROUTING_SCHEMA,
            model=self._llm.routing_model,
            # Headroom plus no thinking: routing is classification, and the
            # failure mode of too small a ceiling is an empty response that
            # looks like a provider outage.
            max_output_tokens=1024,
            thinking_budget=0,
        )
        budget.record(completion.usage)
        state.add_usage(completion.usage)
        return completion.data or {}

    async def _synthesise(
        self,
        principal: Principal,
        question: str,
        retrieval: RetrievalResult,
        decisions: list[PolicyDecision],
        records: list[dict[str, Any]],
        policy: ResolvedPolicy | None,
        budget: TokenBudget,
        state: _RunState,
        prepared_action: ActionView | None = None,
        action_error: str | None = None,
        issues: list[Any] | None = None,
        tables: list[Any] | None = None,
    ):
        prompt = synthesis_user_prompt(
            question,
            principal,
            retrieval,
            decisions=decisions,
            records=records,
            policy=policy,
            issues=issues,
            prepared_action=prepared_action.summary if prepared_action else None,
            action_error=action_error,
        )
        budget.reserve(estimate_tokens(SYNTHESIS_SYSTEM + prompt))
        completion = await self._llm.complete_json(
            system=SYNTHESIS_SYSTEM,
            user=prompt,
            schema=ANSWER_SCHEMA,
            model=self._llm.synthesis_model,
            # Generous on purpose. The 2.5 pro model spends part of this budget
            # on internal reasoning, and running out produces MAX_TOKENS with
            # incomplete JSON -- a hard failure, not a shorter answer. Observed
            # at 3072 on a normal question, so the ceiling is well clear of it.
            max_output_tokens=8192,
            thinking_budget=self._config.agent.synthesis_thinking_budget,
        )
        budget.record(completion.usage)
        state.add_usage(completion.usage)
        return completion

    # -- cohort tools ------------------------------------------------------

    def _cohort(
        self, conn: Connection, principal: Principal, call: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Run a named cohort query: a question about a SET, not a record.

        Row-level security does the scoping, which is what makes one template
        safe for both audiences: staff see the tenant, a customer sees their own
        account, and neither needs a different query. The account filter here is
        a NARROWING convenience for "…for Northstar", never the security
        boundary -- passing someone else's id simply returns nothing.
        """
        name = str(call.get("cohort") or "open_tickets")
        sql = COHORT_TEMPLATES.get(name)
        if sql is None:
            return [], {"error": f"unknown cohort {name!r}"}

        account_id = call.get("account_id") or None
        rows = fetch_all(conn, sql, {"account_id": account_id})
        log.info("cohort_query", cohort=name, rows=len(rows), account_id=account_id)
        return rows, {
            "cohort": name,
            "rows": len(rows),
            "account_id": account_id,
            "accounts": sorted({str(r.get("account_id")) for r in rows}),
        }

    def _scan_issues(
        self,
        conn: Connection,
        principal: Principal,
        call: dict[str, Any],
        now: datetime,
    ) -> tuple[list[Any], dict[str, Any]]:
        """Run the proactive detectors and return the findings.

        WHY THIS TOOL EXISTS. "Is TKT-501 an SLA breach for Northstar?" and "show
        me all open P1 tickets" both refused with `low_confidence`, while the
        dashboard answered both correctly from the same database one tab away.
        The router could only plan `doc_search`, which found nothing citable --
        because the answer lives in a table, not a PDF. The logic was written and
        tested; it simply was not reachable from chat.

        Every `Issue` carries the clause defining the threshold it applied, so a
        finding grounds an answer exactly the way a policy decision does. That is
        the whole reason this integrates cleanly rather than needing a validation
        exemption: a breach without a citation is an opinion, and the detectors
        already refused to produce one.
        """
        from agentcore.analytics import issues as analytics

        wanted = call.get("kinds")
        subject = (call.get("record_id") or "").strip().upper() or None

        dashboard = analytics.detect(conn, principal, now=now)
        found = dashboard.issues

        # Narrow AFTER detection rather than before: the detectors resolve policy
        # per account and cache it, so running the full set and filtering is
        # cheaper than it looks and keeps one code path shared with the
        # dashboard. A divergence here would mean chat and dashboard could
        # disagree about the same ticket, which is worse than a few extra rows.
        if wanted:
            keep = {str(k) for k in wanted}
            found = [i for i in found if i.kind in keep]
        if subject:
            found = [i for i in found if (i.subject_id or "").upper() == subject]

        log.info(
            "issue_scan",
            scanned=len(dashboard.issues),
            returned=len(found),
            subject=subject,
            kinds=wanted,
        )
        return found, {
            "scanned": len(dashboard.issues),
            "findings": len(found),
            "kinds": sorted({i.kind for i in found}),
            "severities": sorted({i.severity for i in found}),
            "record_id": subject,
        }

    def _decide(
        self,
        conn: Connection,
        principal: Principal,
        call: dict[str, Any],
        now: datetime,
        *,
        existing_policy: ResolvedPolicy | None,
    ) -> tuple[PolicyDecision | None, ResolvedPolicy | None]:
        record_id = (call.get("record_id") or "").strip()
        if not record_id:
            return None, existing_policy

        row = fetch_one(conn, RECORD_TEMPLATES["order"], {"id": record_id})
        if row is None:
            # Either it does not exist or RLS hid it. Deliberately
            # indistinguishable: confirming existence would leak that another
            # account has this order.
            return None, existing_policy

        order = OrderFacts.from_row(row)
        policy = existing_policy or resolve_for_account(
            principal.tenant_id, order.account_id
        )

        rule = call.get("rule") or "cancellation_fee"
        if rule == "failed_pickup_credit":
            return failed_pickup_credit(order, policy, now=now), policy
        return cancellation_fee(order, policy, now=now), policy

    def _prepare_action(
        self,
        conn: Connection,
        principal: Principal,
        call: dict[str, Any],
        decisions: list[PolicyDecision],
        records: list[dict[str, Any]],
        run_id: UUID,
        question: str = "",
    ) -> tuple[ActionView | None, dict[str, Any]]:
        """Propose a state change. Writes a ledger row; executes nothing.

        This is the agent's only route to the real world, and it is a proposal.
        The model chooses the action *type* and writes the summary a human will
        read; it does not choose the payload freely -- the payload is assembled
        here from records the run actually retrieved, so the model cannot invent
        an order id, an amount, or an account.

        That distinction is the whole safety argument. A model that could
        construct the payload could escalate someone else's ticket by guessing an
        id; here an unknown id simply produces no action.
        """
        raw_type = (call.get("action_type") or "").strip()
        try:
            action_type = ActionType(raw_type)
        except ValueError:
            return None, {"error": f"unknown action_type {raw_type!r}"}

        record_id = (call.get("record_id") or "").strip().upper()
        reason = (call.get("reason") or "").strip()

        # Fall back to the record the plan already looked up. Observed: the
        # router chose prepare_action WITHOUT a record_id even though a sibling
        # data_query in the same plan had just fetched TKT-501, so the action
        # silently prepared nothing.
        #
        # This resolves only among records the run legitimately retrieved, so it
        # cannot invent an id. Ambiguity is refused rather than guessed: choosing
        # which of several tickets to escalate is not an inference this system
        # gets to make.
        # Recover the id from the question itself when the router omitted it.
        # Deterministic, and it cannot fabricate: the pattern only matches ids
        # the user actually typed, and the lookup below is RLS-scoped.
        if not record_id:
            found = fast_router.find_record_ids(question or "")
            if len(found) == 1:
                record_id = found[0]

        if not record_id:
            if len(records) == 1:
                only = records[0]
                record_id = str(
                    only.get("ticket_id") or only.get("order_id") or ""
                ).upper()
            elif len(records) > 1:
                return None, {
                    "error": (
                        f"{len(records)} records in scope and no record_id given; "
                        "refusing to guess which one to act on"
                    )
                }

        # Resolve the subject through the scoped connection: RLS decides whether
        # it exists for this caller. An invisible record yields no action, and
        # the message does not distinguish "absent" from "not yours".
        subject = next(
            (
                r
                for r in records
                if record_id
                and record_id in {str(r.get("ticket_id")), str(r.get("order_id"))}
            ),
            None,
        )
        if subject is None and record_id:
            subject = self._lookup(conn, record_id)
            subject = subject[0] if subject else None

        if subject is None:
            return None, {
                "record_id": record_id or None,
                "error": "no such record in scope; nothing prepared",
            }

        account_id = subject.get("account_id")
        if not account_id:
            return None, {"error": "record has no account; nothing prepared"}

        payload, summary = self._action_payload(
            action_type, subject, decisions, reason
        )
        if payload is None:
            return None, {"error": f"cannot build a payload for {action_type.value}"}

        # Citations from the decisions that justify it, so an approver can see
        # the clause behind the proposal rather than taking the summary on trust.
        justification = [d.citation for d in decisions if d.citation is not None]

        try:
            view = actions.prepare(
                conn,
                principal,
                run_id=run_id,
                action_type=action_type,
                account_id=account_id,
                payload=payload,
                summary=summary,
                justification=justification,
                ttl_seconds=self._config.security.action_ledger.ttl_seconds,
            )
        except ParcelPilotError as exc:
            # An authorisation failure here is a legitimate outcome, not a crash:
            # a customer asking to issue themselves a credit gets an explanation.
            return None, {"error": exc.message, "action_type": action_type.value}

        return view, {
            "action_type": action_type.value,
            "action_id": str(view.action_id),
            "account_id": account_id,
            "requires_confirmation": True,
        }

    def _action_payload(
        self,
        action_type: ActionType,
        subject: dict[str, Any],
        decisions: list[PolicyDecision],
        reason: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """Assemble the payload from retrieved records, never from model text.

        Amounts come from the deterministic decision, not from the model's
        opinion of what is owed.
        """
        if action_type is ActionType.ESCALATE_TICKET:
            ticket_id = subject.get("ticket_id")
            if not ticket_id:
                return None, ""
            return (
                {"ticket_id": ticket_id, "priority": "P1"},
                f"Escalate {ticket_id}"
                + (f" — {reason}" if reason else "")
                + f" (subject: {subject.get('subject') or 'n/a'})",
            )

        if action_type is ActionType.ISSUE_SERVICE_CREDIT:
            order_id = subject.get("order_id")
            credit = next(
                (
                    d
                    for d in decisions
                    if d.rule_id == "failed_pickup_service_credit"
                    and d.verdict is Verdict.ELIGIBLE
                ),
                None,
            )
            if not order_id or credit is None:
                # No eligible decision means no amount that is not invented.
                return None, ""
            amount = credit.inputs.get("credit_amount")
            payload: dict[str, Any] = {
                "order_id": order_id,
                "amount": str(amount),
                "currency": subject.get("currency") or "INR",
                "reason": reason or f"failed-pickup service credit for {order_id}",
            }
            if credit.inputs.get("monthly_cap") is not None:
                payload["monthly_cap"] = credit.inputs["monthly_cap"]
            return (
                payload,
                f"Issue {payload['currency']} {amount} service credit for "
                f"{order_id} — {credit.explanation[:160]}",
            )

        if action_type is ActionType.UPDATE_ORDER_STATUS:
            order_id = subject.get("order_id")
            if not order_id:
                return None, ""
            return (
                {"order_id": order_id, "status": "CANCELLED"},
                f"Cancel {order_id}" + (f" — {reason}" if reason else ""),
            )

        if action_type is ActionType.CREATE_FOLLOW_UP:
            subject_id = subject.get("ticket_id") or subject.get("order_id")
            return (
                {
                    "subject": reason or f"Follow up on {subject_id}",
                    "body": f"Raised from a support conversation about {subject_id}.",
                },
                f"Create a follow-up task for {subject_id}"
                + (f" — {reason}" if reason else ""),
            )

        return None, ""

    def _lookup(self, conn: Connection, record_id: str | None) -> list[dict[str, Any]]:
        """Resolve an identifier through the template registry.

        The record's shape is inferred from its prefix, not from anything the
        model wrote: the model supplies only an identifier, which travels as a
        bound parameter.
        """
        identifier = (record_id or "").strip().upper()
        if not identifier:
            return []
        if identifier.startswith("ORD-"):
            template = RECORD_TEMPLATES["order"]
        elif identifier.startswith("TKT-"):
            template = RECORD_TEMPLATES["ticket"]
        elif identifier.startswith("ACCT-"):
            template = RECORD_TEMPLATES["account"]
        else:
            return []
        return fetch_all(conn, template, {"id": identifier})

    def _admit_decision_clauses(
        self,
        conn: Connection,
        retrieval: RetrievalResult,
        cited: list[Any],
    ) -> RetrievalResult:
        """Add each operative clause to the groundable channel.

        Takes anything carrying a `.citation`: a `PolicyDecision` (one subject) or
        an `Issue` from the proactive detectors (a cohort). Both produce a clause
        the deterministic layer actually applied, and both must be citable or the
        validator rejects the very quote the engine told the model to use.

        Fetched through the scoped connection, so RLS still applies: a clause
        from a contract the caller cannot see would come back empty rather than
        being smuggled in by the policy path.

        `fused_score` is 1.0 -- above any RRF score, which tops out near 1/61 --
        because a clause the deterministic engine actually relied on is the most
        relevant thing in the run by definition.
        """
        wanted = {
            c.citation.chunk_id: c.citation
            for c in cited
            if getattr(c, "citation", None) is not None
        }
        already = {c.chunk.chunk_id for c in retrieval.groundable}
        missing = [cid for cid in wanted if cid not in already]
        if not missing:
            return retrieval

        rows = fetch_all(
            conn,
            """
            SELECT c.chunk_id, c.document_id, c.tenant_id, c.ordinal, c.page_from,
                   c.page_to, c.section_path, c.text, c.index_version_id,
                   d.filename, d.title, d.source_class, d.authority, d.eligibility,
                   d.freshness, d.owner_account_id, d.policy_family, d.version_label,
                   d.effective_from, d.effective_to, d.content_sha256, d.page_count
            FROM chunks c
            JOIN documents d ON d.document_id = c.document_id
            WHERE c.chunk_id = ANY(%s) AND d.eligibility = 'groundable'
            """,
            (missing,),
        )
        if not rows:
            return retrieval

        from agentcore.retrieval.hybrid import _to_retrieved

        admitted = []
        for row in rows:
            chunk, document = _to_retrieved(row)
            admitted.append(
                RetrievedChunk(
                    chunk=chunk, document=document, fused_score=1.0, selected=True
                )
            )

        log.info("decision_clauses_admitted", count=len(admitted))
        return RetrievalResult(
            groundable=admitted + retrieval.groundable,
            conflict=retrieval.conflict,
            context=retrieval.context,
            candidates=admitted + retrieval.candidates,
            lexical_hits=retrieval.lexical_hits,
            dense_hits=retrieval.dense_hits,
            dense_available=retrieval.dense_available,
            index_version_id=(
                retrieval.index_version_id or rows[0]["index_version_id"]
            ),
        )

    @staticmethod
    def _merge_retrieval(base: RetrievalResult, new: RetrievalResult) -> RetrievalResult:
        """Union two retrievals, keeping the best score per chunk.

        Multiple doc_search calls are normal for a compound question; the
        channels must stay disjoint and free of duplicates.
        """
        def merge(a, b):
            best: dict[Any, Any] = {}
            for chunk in list(a) + list(b):
                key = chunk.chunk.chunk_id
                if key not in best or chunk.fused_score > best[key].fused_score:
                    best[key] = chunk
            return sorted(best.values(), key=lambda c: -c.fused_score)

        return RetrievalResult(
            groundable=merge(base.groundable, new.groundable),
            conflict=merge(base.conflict, new.conflict),
            context=merge(base.context, new.context),
            candidates=merge(base.candidates, new.candidates),
            lexical_hits=base.lexical_hits + new.lexical_hits,
            dense_hits=base.dense_hits + new.dense_hits,
            dense_available=base.dense_available or new.dense_available,
            index_version_id=new.index_version_id or base.index_version_id,
        )

    # -- bounds ------------------------------------------------------------

    def _check_deadline(self, state: _RunState) -> None:
        elapsed = time.monotonic() - state.started
        if elapsed > self._config.agent.wall_clock_seconds:
            raise BudgetExhausted(
                "this question took too long to answer",
                elapsed_seconds=round(elapsed, 1),
                limit=self._config.agent.wall_clock_seconds,
            )

    # -- persistence -------------------------------------------------------

    def _create_run(
        self, conn: Connection, principal: Principal, query: Query
    ) -> UUID:
        row = fetch_one(
            conn,
            """
            INSERT INTO runs (tenant_id, conversation_id, account_id, user_id,
                              role, query, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending')
            RETURNING run_id
            """,
            (
                principal.tenant_id,
                query.conversation_id,
                principal.account_id,
                principal.user_id,
                principal.role.value,
                query.text,
            ),
        )
        assert row is not None
        conn.commit()
        return row["run_id"]

    def _step(
        self,
        conn: Connection,
        state: _RunState,
        kind: StepKind,
        label: str,
        detail: dict[str, Any] | None = None,
        *,
        duration_ms: int = 0,
    ) -> None:
        """Append to the run log and commit immediately.

        Committed per step, not batched: the point of the log is that a client
        can tail it *while the run is in flight*, and an uncommitted row is
        invisible to another connection.
        """
        state.seq += 1
        conn.execute(
            """
            INSERT INTO run_steps (run_id, seq, tenant_id, account_id, kind,
                                   label, detail, duration_ms)
            SELECT %s, %s, tenant_id, account_id, %s, %s, %s, %s
            FROM runs WHERE run_id = %s
            """,
            (
                state.run_id,
                state.seq,
                kind.value,
                label,
                json.dumps(detail or {}, default=str),
                duration_ms,
                state.run_id,
            ),
        )
        conn.commit()

    def _persist_candidates(
        self,
        conn: Connection,
        state: _RunState,
        principal: Principal,
        retrieval: RetrievalResult,
    ) -> None:
        """Record what was CONSIDERED, not only what was cited.

        Without this a retrieval miss is invisible afterwards, and "why didn't
        it find the contract clause" is the most common real question about a
        wrong answer.
        """
        if not self._config.observability.persist_retrieval_candidates:
            return
        if not retrieval.candidates:
            return

        selected = {c.chunk.chunk_id for c in retrieval.groundable}
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO retrieval_candidates (
                    run_id, tenant_id, account_id, chunk_id, document_id,
                    lexical_rank, lexical_score, dense_rank, dense_score,
                    fused_score, rerank_score, selected
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, chunk_id) DO NOTHING
                """,
                [
                    (
                        state.run_id,
                        principal.tenant_id,
                        principal.account_id,
                        c.chunk.chunk_id,
                        c.chunk.document_id,
                        c.lexical_rank,
                        c.lexical_score,
                        c.dense_rank,
                        c.dense_score,
                        c.fused_score,
                        c.rerank_score,
                        c.chunk.chunk_id in selected,
                    )
                    for c in retrieval.candidates
                ],
            )
        conn.commit()

    def _set_status(self, conn: Connection, state: _RunState, status: RunStatus) -> None:
        conn.execute(
            "UPDATE runs SET status = %s WHERE run_id = %s",
            (status.value, state.run_id),
        )
        conn.commit()

    def _finish(
        self,
        conn: Connection,
        state: _RunState,
        status: RunStatus,
        *,
        answer: Answer | None = None,
        error: str | None = None,
        index_version_id: int | None = None,
        action_notice: str | None = None,
        tables: list[Any] | None = None,
    ) -> None:
        # index_version_id is recorded because reproducibility depends on it:
        # without it, "why did it answer that in August" cannot be replayed once
        # the next ingest has replaced the active version.
        conn.execute(
            """
            UPDATE runs SET status = %s, finished_at = now(), answer_json = %s,
                   refusal_reason = %s, error = %s, action_notice = %s,
                   tables_json = %s,
                   prompt_tokens = %s, completion_tokens = %s,
                   index_version_id = coalesce(%s, index_version_id)
            WHERE run_id = %s
            """,
            (
                status.value,
                json.dumps(answer.model_dump(mode="json")) if answer else None,
                answer.refusal.reason.value if answer and answer.refusal else None,
                error,
                action_notice,
                # Persisted rather than recomputed on read, so a replayed run
                # shows the rows as they WERE. Recomputing would show today's
                # data under yesterday's answer, which is the opposite of an
                # auditable log.
                json.dumps([t.model_dump(mode="json") for t in tables])
                if tables
                else None,
                state.usage.prompt_tokens,
                state.usage.completion_tokens,
                index_version_id,
                state.run_id,
            ),
        )
        conn.commit()

    def _steps(self, conn: Connection, run_id: UUID) -> list:
        from agentcore.types import RunStep

        rows = fetch_all(
            conn,
            """
            SELECT run_id, seq, kind, label, detail, started_at, duration_ms
            FROM run_steps WHERE run_id = %s ORDER BY seq
            """,
            (run_id,),
        )
        return [RunStep(**row) for row in rows]

    def _discard_prepared(
        self, conn: Connection, prepared: ActionView | None
    ) -> None:
        """Withdraw a prepared action when the run cannot justify it.

        If synthesis failed, or citations did not validate, or the evidence was
        insufficient, then the engine has no grounded explanation for the action
        it proposed. Leaving it in the queue would present an approver with a
        summary and nothing to check it against -- which is worse than no
        proposal at all, because it invites approval on trust.
        """
        if prepared is None:
            return
        conn.execute(
            """
            UPDATE pending_actions SET status = 'rejected',
                   error = 'withdrawn: the run could not produce a validated answer'
            WHERE action_id = %s AND status = 'pending'
            """,
            (prepared.action_id,),
        )
        conn.commit()
        log.info("prepared_action_withdrawn", action_id=str(prepared.action_id))

    def _refuse(
        self,
        conn: Connection,
        state: _RunState,
        reason: RefusalReason,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        tables: list[Any] | None = None,
    ) -> EngineResponse:
        # Any refusal withdraws a proposal the run could not justify. Done here
        # rather than at each call site so a new refusal path cannot omit it.
        self._discard_prepared(conn, state.prepared_action)

        answer = Answer(
            refusal=Refusal(
                reason=reason, message=refusal_message(message), escalation_offered=True
            )
        )
        self._step(
            conn,
            state,
            StepKind.REFUSE,
            reason.value,
            {"message": message, **(detail or {})},
        )
        self._finish(
            conn, state, RunStatus.COMPLETED, answer=answer, tables=tables
        )
        log.info("run_refused", reason=reason.value)
        return EngineResponse(
            run_id=state.run_id,
            status=RunStatus.COMPLETED,
            answer=answer,
            steps=self._steps(conn, state.run_id),
            usage=state.usage,
            tables=tables or [],
        )


def summarise(response: EngineResponse) -> dict[str, Any]:
    """Compact view for CLI output and logs.

    `citations` is re-derived through `render_prose`, so the list and the [n]
    markers in the prose come from ONE numbering. Deriving them separately gave
    a display where the text cited [2] and [3] but only [1] was listed -- the
    same clause quoted at two different spans counts as two citations, and the
    printer had deduped by chunk.
    """
    answer = response.answer
    ordered_citations: list[dict[str, Any]] = []
    if answer and answer.claims:
        _prose, ordered = render_prose(answer.claims)
        ordered_citations = [
            {
                "n": index,
                "chunk_id": str(citation.chunk_id),
                "document_id": str(citation.document_id),
                "quote": citation.quote,
            }
            for index, citation in enumerate(ordered, start=1)
        ]
    return {
        "run_id": str(response.run_id),
        "status": response.status.value,
        # Present when the agent prepared a state change. The client confirms by
        # id alone; it never receives the payload.
        "pending_action_id": (
            str(response.pending_action_id) if response.pending_action_id else None
        ),
        "awaiting_confirmation": response.pending_action_id is not None,
        "action_notice": response.action_notice,
        "tables": [t.model_dump(mode="json") for t in response.tables],
        "refused": bool(answer and answer.is_refusal),
        "refusal_reason": (
            answer.refusal.reason.value if answer and answer.refusal else None
        ),
        "prose": answer.prose if answer else "",
        "claims": [
            {
                "text": claim.text,
                "citations": [
                    {"chunk_id": str(c.chunk_id), "quote": c.quote} for c in claim.citations
                ],
            }
            for claim in (answer.claims if answer else [])
        ],
        "citations": ordered_citations,
        # Deduplicated: a policy family with several retrieved chunks produced
        # the same "v3 supersedes v2" note once per chunk.
        "conflicts": list(
            dict.fromkeys(c.explanation for c in (answer.conflicts if answer else []))
        ),
        "steps": [
            {"seq": s.seq, "kind": s.kind.value, "label": s.label, "ms": s.duration_ms}
            for s in response.steps
        ],
        "tokens": response.usage.total,
        "index_version": response.index_version,
    }


__all__ = ["Orchestrator", "summarise", "Verdict"]
