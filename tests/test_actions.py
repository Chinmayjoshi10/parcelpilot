"""The action ledger.

These are the tests that make it safe to let an AI system change something.
Each one is an attack or an accident: a replay, a tampered payload, an expired
approval, a customer approving their own credit, two operators clicking at once.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from agentcore.db import engine
from agentcore.db.engine import fetch_one
from agentcore.errors import (
    ActionAlreadySettled,
    ActionError,
    ActionExpired,
    ActionPayloadTampered,
    AuthorizationError,
)
from agentcore.settings import load_config
from agentcore.tools import actions
from agentcore.types import ActionStatus, ActionType, Principal, Role

TENANT = load_config().tenant.id


def _principal(role: Role, account_id: str | None = None) -> Principal:
    return Principal(
        tenant_id=TENANT,
        user_id=f"{role.value}@test",
        role=role,
        account_id=account_id,
    )


@pytest.fixture
def ops() -> Principal:
    return _principal(Role.OPERATIONS_ADMIN)


@pytest.fixture
def agent() -> Principal:
    return _principal(Role.SUPPORT_AGENT)


@pytest.fixture
def customer() -> Principal:
    return _principal(Role.CUSTOMER, "ACCT-002")


#: Test order, cleaned between tests.
TEST_ORDER = "ORD-2002"


@pytest.fixture(autouse=True)
def _clean():
    """Isolate each test from prior ledger state.

    Scoped by ORDER rather than by preparer. The first version filtered on
    `prepared_by LIKE '%@test'` and every test failed: a manual API call had
    left a real executed credit for the same order, and because the idempotency
    key is derived from the payload digest -- deliberately, so two clicks cannot
    queue two credits -- the tests deduplicated onto it.

    Deleted as the owner: the runtime role has no DELETE on the effect tables,
    which is itself asserted below.
    """
    def purge(conn):
        conn.execute("DELETE FROM service_credits WHERE order_id = %s", (TEST_ORDER,))
        conn.execute(
            "DELETE FROM pending_actions WHERE payload->>'order_id' = %s",
            (TEST_ORDER,),
        )
        conn.execute("DELETE FROM follow_ups WHERE created_by LIKE %s", ("%@test",))
        conn.execute("DELETE FROM audit_log WHERE actor LIKE %s", ("%@test",))
        conn.commit()

    with engine.admin() as conn:
        purge(conn)
    yield
    with engine.admin() as conn:
        purge(conn)


def _credit_payload(amount: str = "300", **extra) -> dict:
    return {
        "order_id": TEST_ORDER,
        "amount": amount,
        "currency": "INR",
        "reason": "failed-pickup service credit",
        **extra,
    }


def _prepare_credit(conn, who: Principal, **kwargs):
    return actions.prepare(
        conn,
        who,
        action_type=ActionType.ISSUE_SERVICE_CREDIT,
        account_id="ACCT-002",
        payload=kwargs.pop("payload", _credit_payload()),
        summary="Issue INR 300 credit for ORD-2002",
        **kwargs,
    )


class TestPreparation:
    def test_preparing_changes_nothing(self, ops):
        """The whole point of a two-phase gate."""
        with engine.scoped(ops) as conn:
            view = _prepare_credit(conn, ops)
            credit = fetch_one(
                conn,
                "SELECT count(*) AS n FROM service_credits WHERE action_id = %s",
                (view.action_id,),
            )
        assert view.status is ActionStatus.PENDING
        assert credit["n"] == 0

    def test_customer_cannot_prepare_a_credit(self, customer):
        """A customer must not be able to queue money against their own account
        for approval -- even though approving is separately gated."""
        with pytest.raises(AuthorizationError), engine.scoped(customer) as conn:
            _prepare_credit(conn, customer)

    def test_customer_cannot_prepare_for_another_account(self):
        who = _principal(Role.CUSTOMER, "ACCT-001")
        with pytest.raises(AuthorizationError), engine.scoped(who) as conn:
            actions.prepare(
                conn,
                who,
                action_type=ActionType.ESCALATE_TICKET,
                account_id="ACCT-002",
                payload={"ticket_id": "TKT-502"},
                summary="escalate someone else's ticket",
            )

    def test_identical_proposals_deduplicate(self, ops):
        """Two clicks on the same dashboard row must not queue two approvals.

        The idempotency key is derived from the payload digest, so an identical
        effect from the same origin collides.
        """
        with engine.scoped(ops) as conn:
            first = _prepare_credit(conn, ops)
            second = _prepare_credit(conn, ops)
        assert first.action_id == second.action_id

    def test_a_different_amount_is_a_different_action(self, ops):
        with engine.scoped(ops) as conn:
            first = _prepare_credit(conn, ops)
            second = _prepare_credit(conn, ops, payload=_credit_payload("500"))
        assert first.action_id != second.action_id

    def test_operator_initiated_actions_record_their_origin(self, ops):
        """A dashboard-initiated action has no run behind it.

        Recorded as origin='operator' with a null run_id rather than attributed
        to a synthetic run -- "which cited answer authorised this" and "which
        operator spotted it" are different questions.
        """
        with engine.scoped(ops) as conn:
            view = _prepare_credit(conn, ops)
            row = fetch_one(
                conn,
                "SELECT origin, run_id FROM pending_actions WHERE action_id = %s",
                (view.action_id,),
            )
        assert row["origin"] == "operator"
        assert row["run_id"] is None

    def test_payload_is_hidden_from_customers(self):
        """A customer approving an escalation does not need the internal field
        names it will set."""
        who = _principal(Role.CUSTOMER, "ACCT-002")
        with engine.scoped(who) as conn:
            view = actions.prepare(
                conn,
                who,
                action_type=ActionType.ESCALATE_TICKET,
                account_id="ACCT-002",
                payload={"ticket_id": "TKT-502", "priority": "P1"},
                summary="Escalate TKT-502",
            )
        assert view.payload is None
        assert "payload" not in view.as_dict()


class TestConfirmation:
    def test_confirming_executes_exactly_once(self, ops):
        with engine.scoped(ops) as conn:
            view = _prepare_credit(conn, ops)
            executed = actions.confirm(conn, ops, view.action_id)

            assert executed.status is ActionStatus.EXECUTED
            assert executed.result["currency"] == "INR"

            credit = fetch_one(
                conn,
                "SELECT amount, order_id, run_id FROM service_credits WHERE action_id = %s",
                (view.action_id,),
            )
            assert float(credit["amount"]) == 300.0
            assert credit["order_id"] == "ORD-2002"

            # A second confirm reports the terminal state instead of paying twice.
            with pytest.raises(ActionAlreadySettled):
                actions.confirm(conn, ops, view.action_id)

            count = fetch_one(
                conn, "SELECT count(*) AS n FROM service_credits WHERE action_id = %s",
                (view.action_id,),
            )
            assert count["n"] == 1

    def test_support_agent_cannot_confirm_a_credit(self, ops, agent):
        """Preparation and approval are separate privileges.

        An agent may propose money; only an operations admin commits it.
        """
        with engine.scoped(ops) as conn:
            view = _prepare_credit(conn, ops)
        with pytest.raises(AuthorizationError), engine.scoped(agent) as conn:
            actions.confirm(conn, agent, view.action_id)

    def test_customer_cannot_confirm(self, ops, customer):
        with engine.scoped(ops) as conn:
            view = _prepare_credit(conn, ops)
        with pytest.raises(AuthorizationError), engine.scoped(customer) as conn:
            actions.confirm(conn, customer, view.action_id)

    def test_role_is_rechecked_at_confirm_time(self, ops, agent):
        """Not trusted from preparation.

        Permissions change; a stale approval must not carry an authority its
        approver no longer has. Asserted by confirming as a role that could
        legitimately have prepared it but cannot commit it.
        """
        with engine.scoped(agent) as conn:
            view = actions.prepare(
                conn,
                agent,
                action_type=ActionType.UPDATE_ORDER_STATUS,
                account_id="ACCT-002",
                payload={"order_id": "ORD-2002", "status": "CANCELLED"},
                summary="Cancel ORD-2002",
            )
        with pytest.raises(AuthorizationError), engine.scoped(agent) as conn:
            actions.confirm(conn, agent, view.action_id)

    def test_expired_approval_is_refused(self, ops):
        """Expiry is enforced in the same statement that executes, so there is
        no window between checking it and using it."""
        with engine.scoped(ops) as conn:
            view = _prepare_credit(conn, ops, ttl_seconds=1)

        with engine.admin() as conn:
            conn.execute(
                "UPDATE pending_actions SET expires_at = now() - interval '1 minute' "
                "WHERE action_id = %s",
                (view.action_id,),
            )
            conn.commit()

        with pytest.raises(ActionExpired), engine.scoped(ops) as conn:
            actions.confirm(conn, ops, view.action_id)

        with engine.admin() as conn:
            row = fetch_one(
                conn,
                "SELECT status FROM pending_actions WHERE action_id = %s",
                (view.action_id,),
            )
        assert row["status"] == "expired"

    def test_tampered_payload_is_refused(self, ops):
        """The client never sees the payload, so a digest mismatch means the
        stored row was altered. Refuse rather than execute."""
        with engine.scoped(ops) as conn:
            view = _prepare_credit(conn, ops)

        with engine.admin() as conn:
            conn.execute(
                "UPDATE pending_actions SET payload = %s WHERE action_id = %s",
                (json.dumps(_credit_payload("999999")), view.action_id),
            )
            conn.commit()

        with pytest.raises(ActionPayloadTampered), engine.scoped(ops) as conn:
            actions.confirm(conn, ops, view.action_id)

        with engine.admin() as conn:
            credits = fetch_one(
                conn,
                "SELECT count(*) AS n FROM service_credits WHERE action_id = %s",
                (view.action_id,),
            )
            row = fetch_one(
                conn, "SELECT status FROM pending_actions WHERE action_id = %s",
                (view.action_id,),
            )
        assert credits["n"] == 0
        assert row["status"] == "failed"

    def test_unknown_action_is_indistinguishable_from_forbidden(self, ops):
        with pytest.raises(ActionError), engine.scoped(ops) as conn:
            actions.confirm(conn, ops, uuid4())

    def test_monthly_cap_is_enforced_at_execution(self, ops):
        """The policy engine reports a contract's aggregate cap; enforcing it
        needs this month's issued total, which is a ledger question answered at
        execution time rather than at preparation, when it could go stale."""
        with engine.scoped(ops) as conn:
            view = actions.prepare(
                conn,
                ops,
                action_type=ActionType.ISSUE_SERVICE_CREDIT,
                account_id="ACCT-002",
                payload=_credit_payload("900", monthly_cap=500),
                summary="Credit above the agreed cap",
            )
        with pytest.raises(ActionError, match="cap"), engine.scoped(ops) as conn:
            actions.confirm(conn, ops, view.action_id)


class TestRejection:
    def test_rejecting_records_the_decision(self, ops):
        with engine.scoped(ops) as conn:
            view = _prepare_credit(conn, ops)
            rejected = actions.reject(conn, ops, view.action_id, "not our fault")

            assert rejected.status is ActionStatus.REJECTED
            audit = fetch_one(
                conn,
                """
                SELECT event, detail FROM audit_log
                WHERE subject_id = %s AND event = 'action.rejected'
                """,
                (str(view.action_id),),
            )
        assert audit is not None
        assert audit["detail"]["reason"] == "not our fault"

    def test_rejected_action_cannot_then_be_confirmed(self, ops):
        with engine.scoped(ops) as conn:
            view = _prepare_credit(conn, ops)
            actions.reject(conn, ops, view.action_id)
            with pytest.raises(ActionAlreadySettled):
                actions.confirm(conn, ops, view.action_id)


class TestAuditTrail:
    def test_execution_is_audited_with_its_result(self, ops):
        with engine.scoped(ops) as conn:
            view = _prepare_credit(conn, ops)
            actions.confirm(conn, ops, view.action_id)
            audit = fetch_one(
                conn,
                """
                SELECT actor, actor_role, event, detail FROM audit_log
                WHERE subject_id = %s
                """,
                (str(view.action_id),),
            )
        assert audit["event"] == "action.issue_service_credit"
        assert audit["actor_role"] == "operations_admin"
        assert audit["detail"]["result"]["currency"] == "INR"

    def test_issued_credits_cannot_be_edited_or_deleted(self, ops):
        """A credit that was issued happened.

        Reversing one is a new, separately-approved action -- which leaves both
        events visible. Enforced by revoked grants, not by convention.
        """
        import psycopg

        from agentcore.errors import RepositoryError

        with engine.scoped(ops) as conn:
            view = _prepare_credit(conn, ops)
            actions.confirm(conn, ops, view.action_id)

        for statement in (
            "UPDATE service_credits SET amount = 1",
            "DELETE FROM service_credits",
        ):
            with (
                pytest.raises((RepositoryError, psycopg.errors.InsufficientPrivilege)),
                engine.scoped(ops) as conn,
            ):
                conn.execute(statement)


class TestScoping:
    def test_a_customer_cannot_see_another_accounts_pending_action(self, ops):
        """RLS again: the queue is scoped, so an action for ACCT-002 is
        invisible to ACCT-001 rather than merely un-approvable."""
        with engine.scoped(ops) as conn:
            _prepare_credit(conn, ops)

        other = _principal(Role.CUSTOMER, "ACCT-001")
        with engine.scoped(other, read_only=True) as conn:
            visible = actions.list_pending(conn, other)
        assert all(view.account_id == "ACCT-001" for view in visible)

    def test_staff_see_the_whole_tenant_queue(self, ops):
        with engine.scoped(ops) as conn:
            view = _prepare_credit(conn, ops)
            visible = actions.list_pending(conn, ops)
        assert view.action_id in {v.action_id for v in visible}
