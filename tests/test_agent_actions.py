"""The agent's state-changing tool.

The assessment requires that the agent can *prepare* an action and ask for
confirmation. These tests assert the three properties that make that safe:

1. Preparing changes nothing in the world.
2. The payload is assembled from records the run actually retrieved, never from
   model text -- so the model cannot invent an order id, an amount or an account.
3. Authorisation is enforced in the tool, not in the prompt.

The model is a fixture, so "what if it asks to credit its own account" is an
ordinary test case rather than a hope.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentcore.db import engine
from agentcore.llm.base import Completion
from agentcore.orchestrator.engine import Orchestrator
from agentcore.settings import load_config
from agentcore.types import Principal, Query, Role, RunStatus, TokenUsage

CONFIG = load_config()

# Tests that hand the orchestrator a scripted routing response are exercising the
# LLM planner, so they must ask for it. The shipped default is the deterministic
# fast planner (no round trip), which ignores a scripted route entirely -- and a
# test whose script is silently unused passes for the wrong reason.
CONFIG_LLM_ROUTER = CONFIG.model_copy(
    update={"agent": CONFIG.agent.model_copy(update={"router": "llm"})}
)
TENANT = CONFIG.tenant.id

_OPEN = "<<<UNTRUSTED_DATA id="
_CLOSE = "<<<END_UNTRUSTED_DATA id="


class ScriptedLLM:
    """A model whose routing plan and synthesis output each test dictates."""

    def __init__(self, route: dict[str, Any], synthesise) -> None:
        self._route = route
        self._synthesise = synthesise
        self.prompts: list[str] = []
        self.calls = 0

    @property
    def routing_model(self) -> str:
        return "scripted-routing"

    @property
    def synthesis_model(self) -> str:
        return "scripted-synthesis"

    async def complete_json(self, *, system, user, schema, model=None, **_):
        self.calls += 1
        self.prompts.append(user)
        payload = (
            self._route
            if model == self.routing_model
            else self._synthesise(user, self.calls)
        )
        return Completion(
            text=json.dumps(payload),
            data=payload,
            usage=TokenUsage(prompt_tokens=100, completion_tokens=50),
            model=model or "scripted",
        )

    async def complete_text(self, **_):  # pragma: no cover
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def _sources(prompt: str) -> list[tuple[str, str]]:
    """Parse the source blocks back out of a prompt.

    The scripted model quotes from what it was actually shown, exactly as a real
    model would, rather than from corpus text hardcoded here.
    """
    found: list[tuple[str, str]] = []
    cursor = 0
    while True:
        start = prompt.find(_OPEN, cursor)
        if start < 0:
            return found
        header_end = prompt.find(">>>", start)
        if header_end < 0:
            return found
        identifier = prompt[start + len(_OPEN) : header_end].split()[0]
        body_start = header_end + 3
        terminator = _CLOSE + identifier + ">>>"
        body_end = prompt.find(terminator, body_start)
        if body_end < 0:
            return found
        if identifier != "question":
            found.append((identifier, prompt[body_start:body_end].strip()))
        cursor = body_end + len(terminator)


def _cite(prompt: str, needle: str) -> dict[str, str]:
    """A verbatim citation, exact by construction (a slice of the shown text)."""
    chunk_id, body = next(
        (c, b) for c, b in _sources(prompt) if needle.lower() in b.lower()
    )
    index = body.lower().find(needle.lower())
    return {"chunk_id": chunk_id, "quote": body[max(0, index - 20) : index + 90]}


def _principal(account_id: str | None, role: Role = Role.CUSTOMER) -> Principal:
    return Principal(
        tenant_id=TENANT, user_id="agent-test", role=role, account_id=account_id
    )


async def _run(llm, question: str, principal: Principal):
    orchestrator = Orchestrator(CONFIG_LLM_ROUTER, llm, embedder=None)
    with engine.scoped(principal) as conn:
        return await orchestrator.run(conn, principal, Query(text=question))


def _step(response, label: str):
    return next(
        (
            s
            for s in response.steps
            if s.kind.value == "tool_result" and s.label == label
        ),
        None,
    )


def _discard(action_id) -> None:
    """Remove a prepared action so tests do not accumulate ledger rows."""
    if action_id is None:
        return
    with engine.admin() as conn:
        conn.execute("DELETE FROM pending_actions WHERE action_id = %s", (action_id,))
        conn.commit()


@pytest.fixture(scope="module", autouse=True)
def _index_present():
    with engine.admin() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM index_versions "
            "WHERE tenant_id = %s AND status = 'active'",
            (TENANT,),
        ).fetchone()
    if not row or not row["n"]:
        pytest.skip("run `parcelpilot ingest run` before this suite")


class TestAgentPreparesActions:
    async def test_escalation_is_prepared_but_not_executed(self):
        """The requirement's own example: "escalate this ticket"."""
        route = {
            "reasoning": "user asked us to escalate",
            "out_of_scope": False,
            "tools": [
                {"tool": "data_query", "record_id": "TKT-501"},
                {
                    "tool": "prepare_action",
                    "action_type": "escalate_ticket",
                    "record_id": "TKT-501",
                    "reason": "all shipment creation failing, past SLA",
                },
                {"tool": "doc_search", "search_query": "P1 severity critical outage"},
            ],
        }

        def synthesise(prompt, _call):
            # The model is told an action is pending and instructed not to claim
            # it is done.
            assert "ACTION PREPARED, AWAITING HUMAN CONFIRMATION" in prompt
            return {
                "claims": [
                    {
                        "text": "This is a P1 and an escalation has been prepared.",
                        "citations": [_cite(prompt, "P1")],
                    }
                ],
                "insufficient_evidence": False,
            }

        response = await _run(
            ScriptedLLM(route, synthesise), "Please escalate TKT-501", _principal("ACCT-001")
        )
        try:
            assert response.pending_action_id is not None
            # Not "completed": something is waiting on a person.
            assert response.status is RunStatus.AWAITING_CONFIRMATION

            with engine.admin() as conn:
                action = conn.execute(
                    """
                    SELECT status, action_type, account_id, payload, origin, run_id
                    FROM pending_actions WHERE action_id = %s
                    """,
                    (response.pending_action_id,),
                ).fetchone()
                ticket = conn.execute(
                    "SELECT status FROM tickets WHERE ticket_id = 'TKT-501'"
                ).fetchone()

            assert action["status"] == "pending"
            assert action["action_type"] == "escalate_ticket"
            assert action["account_id"] == "ACCT-001"
            assert action["payload"]["ticket_id"] == "TKT-501"
            # Attributed to the run that proposed it, not to an operator.
            assert action["origin"] == "agent"
            assert action["run_id"] is not None
            # The decisive assertion: the world is unchanged.
            assert ticket["status"] == "open"
        finally:
            _discard(response.pending_action_id)

    async def test_credit_amount_comes_from_the_rule_engine(self):
        """The model picks the action type; the amount is computed.

        With no eligible decision in the plan there is no amount that would not
        be invented, so nothing is prepared.
        """
        base_tools = [
            {"tool": "data_query", "record_id": "ORD-2002"},
            {
                "tool": "prepare_action",
                "action_type": "issue_service_credit",
                "record_id": "ORD-2002",
                "reason": "carrier fault, pickup missed",
            },
            {"tool": "doc_search", "search_query": "service credit"},
        ]
        def answer(prompt, _call):
            """A grounded answer, so the prepared action survives.

            A refusing run withdraws its own proposal (see
            `test_a_refused_run_withdraws_its_proposal`), so this case has to
            answer properly for the amount to be observable.
            """
            return {
                "claims": [
                    {
                        "text": "A service credit is due for this failed pickup.",
                        "citations": [_cite(prompt, "service credit")],
                    }
                ],
                "insufficient_evidence": False,
            }

        without = await _run(
            ScriptedLLM(
                {"reasoning": "c", "out_of_scope": False, "tools": base_tools}, answer
            ),
            "Credit ORD-2002",
            _principal(None, Role.OPERATIONS_ADMIN),
        )
        assert without.pending_action_id is None

        with_decision = [
            base_tools[0],
            {
                "tool": "policy_decide",
                "record_id": "ORD-2002",
                "rule": "failed_pickup_credit",
            },
            *base_tools[1:],
        ]
        with_it = await _run(
            ScriptedLLM(
                {"reasoning": "c", "out_of_scope": False, "tools": with_decision}, answer
            ),
            "Credit ORD-2002",
            _principal(None, Role.OPERATIONS_ADMIN),
        )
        try:
            assert with_it.pending_action_id is not None
            with engine.admin() as conn:
                action = conn.execute(
                    "SELECT payload, justification FROM pending_actions "
                    "WHERE action_id = %s",
                    (with_it.pending_action_id,),
                ).fetchone()

            # LumenWorks' contracted fixed 300, not the default min(500, 10%) = 240.
            assert action["payload"]["amount"] == "300"
            # And it carries the clause that justifies it, for the approver.
            assert action["justification"]
        finally:
            _discard(with_it.pending_action_id)


class TestAgentActionAuthorisation:
    async def test_customer_cannot_have_the_agent_credit_their_account(self):
        """Enforced in the tool, not the prompt.

        A customer asking for money gets an explanation; the run still answers.
        """
        route = {
            "reasoning": "user asked for money",
            "out_of_scope": False,
            "tools": [
                {"tool": "data_query", "record_id": "ORD-2002"},
                {
                    "tool": "prepare_action",
                    "action_type": "issue_service_credit",
                    "record_id": "ORD-2002",
                    "reason": "I want my credit now",
                },
                {"tool": "doc_search", "search_query": "service credit eligibility"},
            ],
        }

        def synthesise(prompt, _call):
            # Nothing was prepared, so the prompt must not say otherwise.
            assert "ACTION PREPARED" not in prompt
            return {"claims": [], "insufficient_evidence": True}

        response = await _run(
            ScriptedLLM(route, synthesise),
            "Issue me the credit for ORD-2002",
            _principal("ACCT-002"),
        )

        assert response.pending_action_id is None
        step = _step(response, "prepare_action")
        assert step is not None, "the tool should have run and refused"
        assert "error" in step.detail

    async def test_action_on_an_invisible_record_prepares_nothing(self):
        """ACCT-001 asking to escalate ACCT-002's ticket.

        RLS hides the record, so no payload can be built -- and the message does
        not distinguish "absent" from "not yours".
        """
        route = {
            "reasoning": "escalate",
            "out_of_scope": False,
            "tools": [
                {
                    "tool": "prepare_action",
                    "action_type": "escalate_ticket",
                    "record_id": "TKT-502",
                    "reason": "please escalate",
                },
                {"tool": "doc_search", "search_query": "escalation procedure"},
            ],
        }
        response = await _run(
            ScriptedLLM(
                route, lambda *_: {"claims": [], "insufficient_evidence": True}
            ),
            "Escalate TKT-502",
            _principal("ACCT-001"),
        )

        assert response.pending_action_id is None
        step = _step(response, "prepare_action")
        assert "no such record in scope" in step.detail["error"]

    async def test_unknown_action_type_is_rejected(self):
        route = {
            "reasoning": "do something",
            "out_of_scope": False,
            "tools": [
                {
                    "tool": "prepare_action",
                    "action_type": "delete_everything",
                    "record_id": "TKT-501",
                },
                {"tool": "doc_search", "search_query": "policy"},
            ],
        }
        response = await _run(
            ScriptedLLM(
                route, lambda *_: {"claims": [], "insufficient_evidence": True}
            ),
            "Delete everything",
            _principal("ACCT-001"),
        )
        assert response.pending_action_id is None
        step = _step(response, "prepare_action")
        assert "unknown action_type" in step.detail["error"]


class TestWithdrawal:
    async def test_a_refused_run_withdraws_its_proposal(self):
        """An approval whose justification failed validation must not survive.

        If the engine cannot produce a grounded answer, an approver would see a
        summary with nothing to check it against -- which invites approval on
        trust. So the proposal is withdrawn rather than left queued.
        """
        route = {
            "reasoning": "escalate",
            "out_of_scope": False,
            "tools": [
                {"tool": "data_query", "record_id": "TKT-501"},
                {
                    "tool": "prepare_action",
                    "action_type": "escalate_ticket",
                    "record_id": "TKT-501",
                    "reason": "past SLA",
                },
                {"tool": "doc_search", "search_query": "escalation"},
            ],
        }
        response = await _run(
            ScriptedLLM(
                route, lambda *_: {"claims": [], "insufficient_evidence": True}
            ),
            "Escalate TKT-501",
            _principal("ACCT-001"),
        )

        assert response.answer.is_refusal
        assert response.pending_action_id is None

        # The row exists for audit, but it is settled -- not awaiting anyone.
        with engine.admin() as conn:
            rows = conn.execute(
                """
                SELECT status, error FROM pending_actions
                WHERE run_id = %s AND action_type = 'escalate_ticket'
                """,
                (response.run_id,),
            ).fetchall()
        assert rows, "the proposal should be recorded, not vanished"
        assert all(r["status"] == "rejected" for r in rows)
        assert all("withdrawn" in (r["error"] or "") for r in rows)

        with engine.admin() as conn:
            conn.execute(
                "DELETE FROM pending_actions WHERE run_id = %s", (response.run_id,)
            )
            conn.commit()
