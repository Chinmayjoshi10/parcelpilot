"""Deterministic policy rules.

No LLM appears anywhere in this file, and none may. Every function is a pure
mapping from (order record, resolved parameters, reference time) to a
`PolicyDecision` carrying the operative clause. Same inputs, same verdict, for
ever.

That constraint is not architectural purity, it is the specific defence against
the specific failure this dataset is built to punish. TKT-450 records a human
agent telling Northstar that "a INR 250 cancellation fee applied after 30
minutes" -- a fluent, plausible answer, correct under the default SOP, and wrong
for that account because their agreement waives the fee outright. A language
model asked to reason over retrieved text will reproduce that error whenever
retrieval fails to surface the contract. A lookup on `accounts.contract_file`
cannot.

Two further rules of construction:

* **Unknown is a verdict.** Missing timestamps or unknown fault attribution
  return INDETERMINATE with an escalation, never a guess. The SOP says so
  explicitly: "Do not promise a credit when carrier fault, pickup timing, or
  customer fault is unknown."
* **Every decision shows its arithmetic.** `inputs` records the parameter values
  and record fields actually read, so a human can re-check the computation line
  by line instead of trusting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from agentcore.errors import PolicyParameterMissing
from agentcore.logging import get_logger
from agentcore.policy.pack import ResolvedPolicy
from agentcore.types import PolicyDecision, Verdict

log = get_logger(__name__)

RULE_CANCELLATION_FEE = "cancellation_fee"
RULE_PICKUP_CREDIT = "failed_pickup_service_credit"

#: Statuses whose cancellability the SOP defines explicitly.
STATUS_DRAFT = "DRAFT"
STATUS_BOOKED = "BOOKED"
STATUS_PICKED_UP = "PICKED_UP"
STATUS_DELIVERED = "DELIVERED"


@dataclass(frozen=True)
class OrderFacts:
    """The typed subset of an order a rule is allowed to read.

    A narrow struct rather than a raw row, so a rule cannot quietly start
    depending on a free-text `notes` field -- which is customer-authored and
    therefore untrusted.
    """

    order_id: str
    account_id: str
    status: str | None
    booked_at: datetime | None
    pickup_window_end: datetime | None
    pickup_actual_at: datetime | None
    cancellation_requested_at: datetime | None
    shipment_fee: Decimal | None
    currency: str | None
    carrier_fault: bool
    customer_fault: bool

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> OrderFacts:
        return cls(
            order_id=row["order_id"],
            account_id=row["account_id"],
            status=(row.get("status") or None),
            booked_at=row.get("booked_at"),
            pickup_window_end=row.get("pickup_window_end"),
            pickup_actual_at=row.get("pickup_actual_at"),
            cancellation_requested_at=row.get("cancellation_requested_at"),
            shipment_fee=row.get("shipment_fee"),
            currency=row.get("currency"),
            carrier_fault=bool(row.get("carrier_fault")),
            customer_fault=bool(row.get("customer_fault")),
        )


def _indeterminate(rule_id: str, reason: str, inputs: dict[str, Any]) -> PolicyDecision:
    """The honest outcome when the data cannot support a decision.

    INDETERMINATE is the only verdict permitted to omit a citation, because
    there is no operative clause when the question cannot be answered from the
    record.
    """
    return PolicyDecision(
        rule_id=rule_id,
        verdict=Verdict.INDETERMINATE,
        inputs=inputs,
        citation=None,
        explanation=reason,
    )


# ---------------------------------------------------------------------------
# Cancellation fee
# ---------------------------------------------------------------------------


def cancellation_fee(
    order: OrderFacts, policy: ResolvedPolicy, *, now: datetime | None = None
) -> PolicyDecision:
    """Decide whether a cancellation attracts a fee.

    Order of evaluation mirrors the SOP: status first, then the contract waiver,
    then the timing window. Status has to come first because a picked-up
    shipment is not cancellable *at all* -- the Northstar waiver removes a fee,
    it does not create a right to cancel after pickup (ORD-1002).
    """
    status = (order.status or "").upper()
    requested_at = order.cancellation_requested_at or now
    inputs: dict[str, Any] = {
        "order_id": order.order_id,
        "account_id": order.account_id,
        "status": status or None,
        "booked_at": order.booked_at,
        "cancellation_requested_at": requested_at,
    }

    if not status:
        return _indeterminate(
            RULE_CANCELLATION_FEE, "the order has no recorded status", inputs
        )

    if status == STATUS_DELIVERED:
        # No parameter needed: the SOP states it flatly, so the clause itself is
        # the authority and the fee question does not arise.
        try:
            blocked = policy.get("cancellation.picked_up_blocked")
        except PolicyParameterMissing as exc:
            return _indeterminate(RULE_CANCELLATION_FEE, str(exc.message), inputs)
        return PolicyDecision(
            rule_id=RULE_CANCELLATION_FEE,
            verdict=Verdict.DENIED,
            inputs=inputs,
            citation=blocked.citation(),
            explanation=(
                f"{order.order_id} is DELIVERED. A delivered shipment cannot be "
                "cancelled, so no cancellation fee applies."
            ),
        )

    if status == STATUS_PICKED_UP:
        try:
            blocked = policy.get("cancellation.picked_up_blocked")
        except PolicyParameterMissing as exc:
            return _indeterminate(RULE_CANCELLATION_FEE, str(exc.message), inputs)
        return PolicyDecision(
            rule_id=RULE_CANCELLATION_FEE,
            verdict=Verdict.DENIED,
            inputs=inputs,
            citation=blocked.citation(),
            explanation=(
                f"{order.order_id} has already been PICKED_UP, so it must not be "
                "cancelled. The return-to-origin workflow applies instead. Note that "
                "an account-level cancellation-fee waiver does not change this: it "
                "waives a fee on a BOOKED shipment before pickup, it does not permit "
                "cancellation after pickup."
            ),
        )

    if status == STATUS_DRAFT:
        try:
            window = policy.get("cancellation.free_window_minutes")
        except PolicyParameterMissing as exc:
            return _indeterminate(RULE_CANCELLATION_FEE, str(exc.message), inputs)
        return PolicyDecision(
            rule_id=RULE_CANCELLATION_FEE,
            verdict=Verdict.ALLOWED,
            inputs=inputs,
            citation=window.citation(),
            explanation=f"{order.order_id} is a DRAFT and may be cancelled with no fee.",
        )

    if status != STATUS_BOOKED:
        return _indeterminate(
            RULE_CANCELLATION_FEE,
            f"status {status} is not covered by the cancellation SOP",
            inputs,
        )

    # --- BOOKED, not yet picked up ---
    try:
        waived = policy.get("cancellation.fee_waived")
        window = policy.get("cancellation.free_window_minutes")
        fee = policy.get("cancellation.fee_amount")
    except PolicyParameterMissing as exc:
        return _indeterminate(RULE_CANCELLATION_FEE, str(exc.message), inputs)

    inputs["fee_waived"] = waived.value
    inputs["fee_waived_source"] = waived.scope
    inputs["free_window_minutes"] = window.value

    # Elapsed time is recorded whenever it can be computed, even on the waiver
    # path where it does not affect the outcome. It costs nothing and makes the
    # audit trail answer the question a reviewer actually asks: did the engine
    # know this was 120 minutes and waive anyway, or did it simply never look?
    elapsed_minutes: float | None = None
    if order.booked_at is not None and requested_at is not None:
        elapsed_minutes = (requested_at - order.booked_at).total_seconds() / 60.0
        inputs["elapsed_minutes"] = round(elapsed_minutes, 2)

    # The waiver is checked before the clock *decides* anything. Northstar's
    # agreement waives the fee "regardless of how long ago the shipment was
    # booked", so letting the window gate the waiver would reproduce TKT-450.
    if bool(waived.value):
        return PolicyDecision(
            rule_id=RULE_CANCELLATION_FEE,
            verdict=Verdict.ALLOWED,
            inputs=inputs,
            citation=waived.citation(),
            explanation=(
                f"{order.order_id} is BOOKED and not yet picked up"
                + (
                    f", cancelled {elapsed_minutes:.0f} minutes after booking"
                    if elapsed_minutes is not None
                    else ""
                )
                + f". The agreement for {order.account_id} waives the cancellation fee "
                "regardless of elapsed time, so no fee applies. (Under the default "
                "policy alone this would have attracted a fee.)"
            ),
            conflicts=policy.overrides_for("cancellation"),
        )

    if elapsed_minutes is None:
        return _indeterminate(
            RULE_CANCELLATION_FEE,
            "cannot determine elapsed time: booking or cancellation-request "
            "timestamp is missing",
            inputs,
        )

    if elapsed_minutes < 0:
        # Cancellation before booking is incoherent data, not a free cancellation.
        return _indeterminate(
            RULE_CANCELLATION_FEE,
            "the cancellation request predates the booking; the record is inconsistent",
            inputs,
        )

    if elapsed_minutes <= float(window.value):
        return PolicyDecision(
            rule_id=RULE_CANCELLATION_FEE,
            verdict=Verdict.ALLOWED,
            inputs=inputs,
            citation=window.citation(),
            explanation=(
                f"{order.order_id} was cancelled {elapsed_minutes:.0f} minutes after "
                f"booking, within the {window.value}-minute free window. No fee applies."
            ),
            conflicts=policy.overrides_for("cancellation"),
        )

    inputs["fee_amount"] = fee.value
    inputs["fee_currency"] = fee.unit
    return PolicyDecision(
        rule_id=RULE_CANCELLATION_FEE,
        verdict=Verdict.DENIED,
        inputs=inputs,
        citation=fee.citation(),
        explanation=(
            f"{order.order_id} was cancelled {elapsed_minutes:.0f} minutes after "
            f"booking, outside the {window.value}-minute free window, and no agreement "
            f"waives the fee for {order.account_id}. A {fee.unit} {fee.value} "
            "cancellation fee applies."
        ),
        conflicts=policy.overrides_for("cancellation"),
    )


# ---------------------------------------------------------------------------
# Failed-pickup service credit
# ---------------------------------------------------------------------------


def failed_pickup_credit(
    order: OrderFacts, policy: ResolvedPolicy, *, now: datetime
) -> PolicyDecision:
    """Decide service-credit eligibility and amount.

    `now` is required, not defaulted. Eligibility for an unpicked shipment
    depends on how long it has been unpicked, so the reference instant is part of
    the decision and must be recorded with it -- the same question answered an
    hour later can legitimately have a different answer.
    """
    inputs: dict[str, Any] = {
        "order_id": order.order_id,
        "account_id": order.account_id,
        "status": order.status,
        "pickup_window_end": order.pickup_window_end,
        "pickup_actual_at": order.pickup_actual_at,
        "carrier_fault": order.carrier_fault,
        "customer_fault": order.customer_fault,
        "as_of": now,
    }

    try:
        threshold = policy.get("credit.delay_threshold_hours")
        mode = policy.get("credit.mode")
    except PolicyParameterMissing as exc:
        return _indeterminate(RULE_PICKUP_CREDIT, str(exc.message), inputs)

    inputs["delay_threshold_hours"] = threshold.value
    inputs["delay_threshold_source"] = threshold.scope
    inputs["credit_mode"] = mode.value

    if order.pickup_window_end is None:
        return _indeterminate(
            RULE_PICKUP_CREDIT,
            "no scheduled pickup window is recorded, so lateness cannot be measured",
            inputs,
        )

    # The SOP is explicit that unknown fault must not produce a promise. Our
    # schema stores fault as booleans, so "not carrier fault" is a definite
    # negative rather than an unknown -- but customer fault being true is a
    # disqualifier that has to be checked separately.
    if order.customer_fault:
        return PolicyDecision(
            rule_id=RULE_PICKUP_CREDIT,
            verdict=Verdict.NOT_ELIGIBLE,
            inputs=inputs,
            citation=threshold.citation(),
            explanation=(
                f"{order.order_id} has a customer-caused issue recorded, and the "
                "policy requires that there be no customer-caused issue."
            ),
            conflicts=policy.overrides_for("credit"),
        )

    if not order.carrier_fault:
        return PolicyDecision(
            rule_id=RULE_PICKUP_CREDIT,
            verdict=Verdict.NOT_ELIGIBLE,
            inputs=inputs,
            citation=threshold.citation(),
            explanation=(
                f"{order.order_id} does not have carrier fault recorded. A service "
                "credit requires the carrier to be at fault."
            ),
            conflicts=policy.overrides_for("credit"),
        )

    # Lateness is measured from the END of the window, not from booking, and for
    # a shipment still unpicked it runs to `now`.
    reference = order.pickup_actual_at or now
    delay_hours = (reference - order.pickup_window_end).total_seconds() / 3600.0
    inputs["measured_from"] = "pickup_actual_at" if order.pickup_actual_at else "as_of"
    inputs["delay_hours"] = round(delay_hours, 2)

    if delay_hours <= float(threshold.value):
        return PolicyDecision(
            rule_id=RULE_PICKUP_CREDIT,
            verdict=Verdict.NOT_ELIGIBLE,
            inputs=inputs,
            citation=threshold.citation(),
            explanation=(
                f"{order.order_id} was {delay_hours:.1f} hours past the end of the "
                f"pickup window, which does not exceed the {threshold.value}-hour "
                "threshold that applies to this account."
            ),
            conflicts=policy.overrides_for("credit"),
        )

    # --- eligible: compute the amount ---
    amount, amount_param, workings = _credit_amount(order, policy, mode.value)
    if amount is None:
        return _indeterminate(
            RULE_PICKUP_CREDIT,
            workings.get("error", "the credit amount cannot be determined"),
            inputs | workings,
        )

    inputs.update(workings)
    inputs["credit_amount"] = str(amount)

    explanation = (
        f"{order.order_id} was {delay_hours:.1f} hours past the end of the pickup "
        f"window (threshold {threshold.value} hours), the carrier is at fault and there "
        f"is no customer-caused issue. A service credit of "
        f"{amount_param.unit if mode.value == 'fixed' else 'INR'} {amount} is due."
    )

    # Approval thresholds change *how* the action is confirmed, not whether the
    # credit is owed, so this is recorded on the decision rather than altering
    # the verdict.
    try:
        approval = policy.get("credit.manager_approval_above")
        if amount > Decimal(str(approval.value)):
            inputs["requires_manager_approval"] = True
            explanation += (
                f" This exceeds {approval.unit} {approval.value}, so it requires "
                "manager approval."
            )
        else:
            inputs["requires_manager_approval"] = False
    except PolicyParameterMissing:
        inputs["requires_manager_approval"] = None

    monthly_cap = policy.parameters.get("credit.monthly_cap")
    if monthly_cap is not None:
        # Reported, not applied: enforcing an aggregate needs this month's
        # already-issued credits, which is a ledger query the caller performs.
        inputs["monthly_cap"] = monthly_cap.value
        explanation += (
            f" Note that {order.account_id} has a monthly aggregate credit cap of "
            f"{monthly_cap.unit} {monthly_cap.value}, which must be checked against "
            "credits already issued this month."
        )

    return PolicyDecision(
        rule_id=RULE_PICKUP_CREDIT,
        verdict=Verdict.ELIGIBLE,
        inputs=inputs,
        citation=amount_param.citation(),
        explanation=explanation,
        conflicts=policy.overrides_for("credit"),
    )


def _credit_amount(
    order: OrderFacts, policy: ResolvedPolicy, mode: str
) -> tuple[Decimal | None, Any, dict[str, Any]]:
    """Compute the credit. Decimal throughout: this is money."""
    if mode == "fixed":
        try:
            fixed = policy.get("credit.fixed_amount")
        except PolicyParameterMissing as exc:
            return None, None, {"error": str(exc.message)}
        return Decimal(str(fixed.value)), fixed, {"amount_rule": "fixed"}

    if mode == "lower_of":
        try:
            cap = policy.get("credit.flat_cap")
            percent = policy.get("credit.percent_of_fee")
        except PolicyParameterMissing as exc:
            return None, None, {"error": str(exc.message)}

        if order.shipment_fee is None:
            return (
                None,
                None,
                {"error": "the shipment fee is unknown, so a percentage cannot be computed"},
            )

        pct_amount = (
            Decimal(str(order.shipment_fee)) * Decimal(str(percent.value)) / Decimal(100)
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cap_amount = Decimal(str(cap.value))
        chosen = min(cap_amount, pct_amount)

        # The winning parameter is cited, so the citation points at the clause
        # that actually determined the number.
        return (
            chosen,
            cap if chosen == cap_amount else percent,
            {
                "amount_rule": "lower_of",
                "flat_cap": str(cap_amount),
                "percent_of_fee": percent.value,
                "shipment_fee": str(order.shipment_fee),
                "percent_amount": str(pct_amount),
            },
        )

    return None, None, {"error": f"unknown credit mode {mode!r}"}
