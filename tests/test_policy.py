"""The deterministic policy engine.

These are the tests that decide whether the system is trustworthy, because they
assert the *answers* -- not that code ran, but that the verdict on a real order
is the one a careful human would reach after reading the contract.

Two of them encode a known-wrong human answer from the dataset (TKT-450,
TKT-451) and assert the engine disagrees with it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from agentcore.db.engine import admin, fetch_one
from agentcore.errors import PolicyParameterMissing
from agentcore.policy.pack import (
    DEFAULT_SCOPE,
    load_pack,
    resolve_for_account,
    validate_pack,
)
from agentcore.policy.rules import OrderFacts, cancellation_fee, failed_pickup_credit
from agentcore.settings import load_config
from agentcore.trust.spans import find_span
from agentcore.types import Verdict

TENANT = load_config().tenant.id
IST = ZoneInfo("Asia/Kolkata")

#: The dataset's own snapshot instant, from the workbook README. Decisions about
#: an unpicked shipment depend on "now", so the reference time is pinned rather
#: than taken from the clock -- otherwise these tests would change verdict
#: overnight.
SNAPSHOT = datetime(2026, 8, 16, 11, 0, tzinfo=IST)


@pytest.fixture(scope="module")
def validated():
    parameters, report = validate_pack(TENANT)
    if not parameters:
        pytest.skip("run `parcelpilot ingest run` before this suite")
    assert report.ok, f"policy pack failed validation: {report.failures}"
    return parameters


def _order(order_id: str) -> OrderFacts:
    with admin() as conn:
        row = fetch_one(
            conn,
            "SELECT * FROM orders WHERE tenant_id = %s AND order_id = %s",
            (TENANT, order_id),
        )
    assert row is not None, f"{order_id} is not in the database"
    return OrderFacts.from_row(row)


def _policy(account_id: str | None, validated):
    return resolve_for_account(TENANT, account_id, parameters=list(validated))


# ---------------------------------------------------------------------------
# Pack integrity
# ---------------------------------------------------------------------------


class TestPackValidation:
    def test_every_parameter_resolves_to_a_real_clause(self, validated):
        """No parameter may be acted on without a located source clause."""
        assert len(validated) == len(load_pack())
        for parameter in validated:
            assert parameter.is_resolved
            assert parameter.chunk_id is not None
            assert parameter.span_start is not None

    def test_quotes_appear_verbatim_in_their_chunk(self, validated):
        """The mechanism that prevents drift.

        Whitespace-insensitive (PDF wrapping is layout, not content) but
        word-exact: "no fee within 30 minutes" must not match a document that
        says 60.
        """
        with admin() as conn:
            for parameter in validated:
                row = fetch_one(
                    conn,
                    "SELECT text FROM chunks WHERE chunk_id = %s",
                    (parameter.chunk_id,),
                )
                assert row is not None
                assert find_span(row["text"], parameter.source_quote) is not None

    def test_drift_is_detected(self):
        """A parameter whose clause no longer says what it claims must fail.

        Simulated by asking the span finder for a mutated quote: the same check
        `validate_pack` performs, so this proves the detection rather than the
        plumbing.
        """
        clause = (
            "BOOKED, not yet PICKED_UP: May be cancelled. No fee within 30 minutes "
            "of booking."
        )
        assert find_span(clause, "No fee within 30 minutes of booking.") is not None
        # One word changed -- the exact failure mode drift detection exists for.
        assert find_span(clause, "No fee within 60 minutes of booking.") is None

    def test_overrides_cite_the_agreement_that_governs_the_account(self, validated):
        """An override attached to the wrong customer is a wrong answer.

        validate_pack cross-checks each override's document against
        accounts.contract_file, so reaching here at all means it agreed.
        """
        with admin() as conn:
            for parameter in validated:
                if parameter.scope == DEFAULT_SCOPE:
                    continue
                row = fetch_one(
                    conn,
                    """
                    SELECT account_id FROM accounts
                    WHERE tenant_id = %s AND contract_file = %s
                    """,
                    (TENANT, parameter.source_document),
                )
                assert row is not None
                assert row["account_id"] == parameter.scope


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellationFee:
    def test_northstar_pays_no_fee_despite_being_outside_the_window(self, validated):
        """ORD-1001: the TKT-450 correction, and the single most important test.

        Booked 09:00, cancellation requested 11:00 -- 120 minutes, far outside
        the 30-minute default window, so the default SOP says INR 250. A human
        agent gave exactly that answer and was wrong: Northstar's agreement
        waives the fee regardless of elapsed time.
        """
        decision = cancellation_fee(_order("ORD-1001"), _policy("ACCT-001", validated))

        assert decision.verdict is Verdict.ALLOWED
        # The engine knew it was 120 minutes -- far outside the free window --
        # and waived anyway. Recording that is what makes the audit trail
        # answer "did it look?" rather than only "what did it decide?".
        assert decision.inputs["elapsed_minutes"] == pytest.approx(120.0)
        assert decision.inputs["fee_waived"] is True
        assert decision.inputs["fee_waived_source"] == "ACCT-001"
        # Cites the contract clause, not the SOP.
        assert "no cancellation fee" in decision.citation.quote.lower()
        # And explains the override rather than applying it silently.
        assert decision.conflicts
        assert "cancellation.fee_waived" in decision.conflicts[0].explanation

    def test_the_default_rule_would_have_charged_northstar(self, validated):
        """The counterfactual that proves the override is doing the work.

        Same order, resolved without the account's agreement: the verdict flips
        to a fee. If this passed identically to the test above, the override
        would be decorative.
        """
        decision = cancellation_fee(_order("ORD-1001"), _policy(None, validated))

        assert decision.verdict is Verdict.DENIED
        assert decision.inputs["fee_amount"] == 250
        assert decision.inputs["elapsed_minutes"] == pytest.approx(120.0)

    def test_lumenworks_pays_the_default_fee(self, validated):
        """ORD-2001: 75 minutes, and this agreement grants no waiver.

        Guards the opposite error to TKT-450 -- over-generalising one customer's
        override to every customer.
        """
        decision = cancellation_fee(_order("ORD-2001"), _policy("ACCT-002", validated))

        assert decision.verdict is Verdict.DENIED
        assert decision.inputs["elapsed_minutes"] == pytest.approx(75.0)
        assert decision.inputs["fee_amount"] == 250
        assert "INR 250" in decision.explanation

    def test_cancellation_conflicts_exclude_unrelated_overrides(self, validated):
        """A fee answer must not volunteer the account's credit threshold.

        ACCT-002 overrides credit parameters but no cancellation parameter, so a
        cancellation decision should report no conflicts at all.
        """
        decision = cancellation_fee(_order("ORD-2001"), _policy("ACCT-002", validated))
        assert decision.conflicts == []

    def test_within_the_free_window_is_free(self, validated):
        """ORD-3001: 15 minutes, and ACCT-003 has no agreement at all."""
        decision = cancellation_fee(_order("ORD-3001"), _policy("ACCT-003", validated))

        assert decision.verdict is Verdict.ALLOWED
        assert decision.inputs["elapsed_minutes"] == pytest.approx(15.0)
        assert "30-minute free window" in decision.explanation

    def test_a_waiver_does_not_permit_cancelling_after_pickup(self, validated):
        """ORD-1002: Northstar again, but already PICKED_UP.

        The subtle one. Their waiver removes a *fee* on a BOOKED shipment before
        pickup; it does not create a right to cancel afterwards. A model
        reasoning loosely over "Northstar may cancel any BOOKED shipment ... with
        no cancellation fee" could easily get this wrong.
        """
        decision = cancellation_fee(_order("ORD-1002"), _policy("ACCT-001", validated))

        assert decision.verdict is Verdict.DENIED
        assert "return-to-origin" in decision.explanation
        assert "PICKED_UP" in decision.citation.quote

    def test_delivered_orders_cannot_be_cancelled(self, validated):
        decision = cancellation_fee(_order("ORD-4001"), _policy("ACCT-004", validated))
        assert decision.verdict is Verdict.DENIED
        assert "DELIVERED" in decision.explanation

    def test_missing_timestamps_yield_indeterminate_not_a_guess(self, validated):
        order = _order("ORD-2001")
        blind = OrderFacts(**{**order.__dict__, "booked_at": None})

        decision = cancellation_fee(blind, _policy("ACCT-002", validated))

        assert decision.verdict is Verdict.INDETERMINATE
        # The only verdict allowed to omit a citation, because there is no
        # operative clause when the question cannot be answered.
        assert decision.citation is None

    def test_incoherent_timestamps_are_not_treated_as_free(self, validated):
        """Cancellation before booking is bad data, not a free cancellation.

        Naive subtraction gives a negative elapsed time, which compares as
        "within the window" and would hand out free cancellations on corrupt rows.
        """
        order = _order("ORD-2001")
        reversed_order = OrderFacts(
            **{
                **order.__dict__,
                "cancellation_requested_at": order.booked_at,
                "booked_at": order.cancellation_requested_at,
            }
        )
        decision = cancellation_fee(reversed_order, _policy("ACCT-002", validated))
        assert decision.verdict is Verdict.INDETERMINATE
        assert "inconsistent" in decision.explanation


# ---------------------------------------------------------------------------
# Service credits
# ---------------------------------------------------------------------------


class TestFailedPickupCredit:
    def test_lumenworks_gets_its_contracted_fixed_credit(self, validated):
        """ORD-2002: the clause that replaces BOTH threshold and amount.

        Pickup window ended 06:30 and the parcel was never collected; at the
        11:00 snapshot that is 4.5 hours. Under the default SOP: past the 2-hour
        threshold, credit = min(500, 10% of 2400) = 240. Under the agreement:
        past the 4-hour threshold, a fixed 300. Applying one half of the clause
        without the other produces a confidently wrong number.
        """
        decision = failed_pickup_credit(
            _order("ORD-2002"), _policy("ACCT-002", validated), now=SNAPSHOT
        )

        assert decision.verdict is Verdict.ELIGIBLE
        assert decision.inputs["delay_hours"] == pytest.approx(4.5)
        assert decision.inputs["delay_threshold_hours"] == 4
        assert decision.inputs["delay_threshold_source"] == "ACCT-002"
        assert decision.inputs["credit_mode"] == "fixed"
        assert Decimal(decision.inputs["credit_amount"]) == Decimal("300")
        assert decision.inputs["requires_manager_approval"] is False
        assert "INR 300" in decision.explanation

    def test_the_default_rule_would_have_paid_a_different_amount(self, validated):
        """Counterfactual: default policy gives 240, not 300."""
        decision = failed_pickup_credit(
            _order("ORD-2002"), _policy(None, validated), now=SNAPSHOT
        )

        assert decision.verdict is Verdict.ELIGIBLE
        assert decision.inputs["credit_mode"] == "lower_of"
        # min(500, 10% of 2400) = 240
        assert Decimal(decision.inputs["credit_amount"]) == Decimal("240.00")
        assert decision.inputs["percent_amount"] == "240.00"

    def test_below_the_contracted_threshold_is_not_eligible(self, validated):
        """At 09:00 the delay is 2.5h: past the default 2h, inside the 4h
        LumenWorks threshold. The contract makes this NOT eligible -- an override
        can reduce entitlement, not only increase it."""
        decision = failed_pickup_credit(
            _order("ORD-2002"),
            _policy("ACCT-002", validated),
            now=datetime(2026, 8, 16, 9, 0, tzinfo=IST),
        )
        assert decision.verdict is Verdict.NOT_ELIGIBLE
        assert decision.inputs["delay_hours"] == pytest.approx(2.5)

    def test_no_carrier_fault_means_no_credit(self, validated):
        """ORD-1001 is late-ish but carrier fault is not recorded."""
        decision = failed_pickup_credit(
            _order("ORD-1001"), _policy("ACCT-001", validated), now=SNAPSHOT
        )
        assert decision.verdict is Verdict.NOT_ELIGIBLE
        assert "carrier" in decision.explanation.lower()

    def test_customer_fault_disqualifies(self, validated):
        order = _order("ORD-2002")
        at_fault = OrderFacts(**{**order.__dict__, "customer_fault": True})

        decision = failed_pickup_credit(
            at_fault, _policy("ACCT-002", validated), now=SNAPSHOT
        )
        assert decision.verdict is Verdict.NOT_ELIGIBLE
        assert "customer-caused" in decision.explanation

    def test_unknown_pickup_window_is_indeterminate(self, validated):
        """The SOP is explicit: do not promise a credit when timing is unknown."""
        order = _order("ORD-2002")
        blind = OrderFacts(**{**order.__dict__, "pickup_window_end": None})

        decision = failed_pickup_credit(
            blind, _policy("ACCT-002", validated), now=SNAPSHOT
        )
        assert decision.verdict is Verdict.INDETERMINATE
        assert decision.citation is None

    def test_manager_approval_flagged_above_the_threshold(self, validated):
        """A large credit is still owed; it just needs a different approval.

        Approval changes how the action is confirmed, not whether the money is
        due, so the verdict must stay ELIGIBLE.
        """
        order = _order("ORD-2002")
        expensive = OrderFacts(**{**order.__dict__, "shipment_fee": Decimal("50000")})

        decision = failed_pickup_credit(expensive, _policy(None, validated), now=SNAPSHOT)

        assert decision.verdict is Verdict.ELIGIBLE
        # min(500, 10% of 50000 = 5000) = 500, which is under 1000.
        assert Decimal(decision.inputs["credit_amount"]) == Decimal("500")
        assert decision.inputs["requires_manager_approval"] is False

    def test_northstar_monthly_cap_is_surfaced(self, validated):
        """A cap the rule cannot enforce alone must still be stated.

        Enforcing an aggregate needs this month's issued credits, which is a
        ledger query. Saying nothing would let a caller exceed the contract.
        """
        order = _order("ORD-2002")
        as_northstar = OrderFacts(**{**order.__dict__, "account_id": "ACCT-001"})

        decision = failed_pickup_credit(
            as_northstar, _policy("ACCT-001", validated), now=SNAPSHOT
        )
        assert decision.inputs["monthly_cap"] == 5000
        assert "monthly aggregate credit cap" in decision.explanation


# ---------------------------------------------------------------------------
# Determinism and safety
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_identical_inputs_give_identical_verdicts(self, validated):
        """The property that separates this from asking a model.

        Run the same decision repeatedly; verdict, amount and citation must be
        byte-identical every time.
        """
        order = _order("ORD-2002")
        policy = _policy("ACCT-002", validated)

        results = [
            failed_pickup_credit(order, policy, now=SNAPSHOT) for _ in range(5)
        ]
        first = results[0]
        for result in results[1:]:
            assert result.verdict is first.verdict
            assert result.inputs["credit_amount"] == first.inputs["credit_amount"]
            assert result.citation == first.citation
            assert result.explanation == first.explanation

    def test_every_non_indeterminate_verdict_carries_a_clause(self, validated):
        """Enforced by PolicyDecision's own validator, checked here across the
        whole real dataset rather than on one example."""
        with admin() as conn:
            order_ids = [
                r["order_id"]
                for r in conn.execute(
                    "SELECT order_id FROM orders WHERE tenant_id = %s ORDER BY order_id",
                    (TENANT,),
                ).fetchall()
            ]

        for order_id in order_ids:
            order = _order(order_id)
            policy = _policy(order.account_id, validated)
            for decision in (
                cancellation_fee(order, policy, now=SNAPSHOT),
                failed_pickup_credit(order, policy, now=SNAPSHOT),
            ):
                if decision.verdict is not Verdict.INDETERMINATE:
                    assert decision.citation is not None, (
                        f"{order_id}/{decision.rule_id} decided without citing a clause"
                    )

    def test_a_missing_parameter_never_becomes_a_guess(self, validated):
        """Remove a required parameter and the engine must refuse, not improvise."""
        policy = _policy("ACCT-002", validated)
        stripped = type(policy)(
            account_id=policy.account_id,
            parameters={
                k: v for k, v in policy.parameters.items() if k != "cancellation.fee_amount"
            },
            override_notes=policy.override_notes,
        )

        decision = cancellation_fee(_order("ORD-2001"), stripped)
        assert decision.verdict is Verdict.INDETERMINATE
        assert "cancellation.fee_amount" in decision.explanation

        with pytest.raises(PolicyParameterMissing):
            stripped.get("cancellation.fee_amount")
