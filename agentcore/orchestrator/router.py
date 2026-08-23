"""Deterministic tool planning.

Replaces a ~2.6-second model round trip with sub-millisecond pattern matching,
and makes tool selection **reproducible**: the same question always runs the same
tools. For a system whose product is trustworthiness that is a feature, not a
compromise — "why did it not check the contract this time" stops being a possible
question.

It works here because the domain has strong, explicit signals:

* record identifiers have a fixed shape (`ORD-1001`, `TKT-501`, `ACCT-002`)
* the operative verbs are a short, closed list (cancel, credit, escalate)
* every question ultimately needs policy text, so `doc_search` always runs

What it gives up is nuance on genuinely ambiguous phrasing. `router: llm` in
config.yaml restores the model-driven planner, and the eval suite is the arbiter
of whether a corpus needs it — on this one, the golden set passes identically
either way.

The planner is *permissive*: when a signal is ambiguous it adds a tool rather
than guessing which to drop. A superfluous `policy_decide` costs 85ms; a missing
one costs a wrong answer.
"""

from __future__ import annotations

import re
from typing import Any

from agentcore.logging import get_logger

log = get_logger(__name__)

#: Fixed-shape record ids. Case-insensitive because users type them either way.
RECORD_ID_RE = re.compile(r"\b((?:TKT|ORD|ACCT)-\d+)\b", re.IGNORECASE)

#: Verbs that mean "do something", as opposed to "tell me something".
#:
#: These are VERBS ONLY. An earlier version listed nouns too ("escalation",
#: "urgent"), and "What is the escalation policy for P2?" staged a real ticket
#: escalation -- an informational question turning into a pending state change.
#: A test caught it. Nouns name a topic; verbs request an act.
_ACTION_VERBS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("escalate_ticket", ("escalate",)),
    ("issue_service_credit", ("credit", "refund", "compensate")),
    ("update_order_status", ("cancel", "update")),
    ("create_follow_up", ("follow up", "follow-up", "remind", "chase")),
)

#: Forms in which a verb is an instruction rather than a subject of enquiry.
#: "Please escalate", "escalate TKT-501" (imperative opener), "can you escalate".
_POLITE_REQUEST = ("please ", "can you ", "could you ", "would you ", "kindly ",
                   "go ahead and ", "i need you to ", "we need to ", "let us ",
                   "lets ", "let's ")

#: Imperative verbs that TAKE one of the action verbs as their object.
#:
#: The gate originally fired only when a matched verb opened the sentence or a
#: polite frame preceded it. That missed the single most natural way to ask for
#: a credit: "Issue a service credit of INR 300 for ORD-2002" -- where "issue"
#: was in no list and "credit" sits four words in. The result was that the
#: credit path could not be reached by the exact phrasing in our own test
#: catalog, and no action was ever staged.
#:
#: These are transitive imperatives with no interrogative reading: a sentence
#: opening "Issue ...", "Grant ...", "Apply ..." is a request, full stop. The
#: enquiry-marker veto still runs first, so "What do I do to issue a credit?"
#: is still a question.
_IMPERATIVE_OPENERS = ("issue ", "grant ", "apply ", "raise ", "give ", "add ",
                       "put ", "process ")

#: Words that mark a sentence as asking ABOUT something. If one of these appears
#: before the verb, the verb is being discussed, not commanded.
_ENQUIRY_MARKERS = (
    "what", "which", "who", "when", "where", "why", "how", "policy", "eligible",
    "eligibility", "do we owe", "are we", "is there", "am i", "is it", "does the",
    "allowed", "entitled",
)


#: "Raise this to P1", "bump it to P2". An imperative opener plus an explicit
#: severity target is an escalation request even though no verb in
#: `_ACTION_VERBS` appears -- "raise" on its own is too ambiguous to list as a
#: verb ("raise an invoice"), but "raise ... to P1" is not ambiguous at all.
_SEVERITY_TARGET_RE = re.compile(r"\bto\s+p[1-4]\b")


def _requested_action(lowered: str) -> str | None:
    """Decide whether the user asked for an act, and which.

    Conservative by design: the cost of a false positive is a pending action a
    human then has to reject, which erodes exactly the trust the confirmation
    gate exists to build. The cost of a false negative is the user saying
    "escalate it" once more. So an ambiguous sentence stages nothing.
    """
    opens_imperatively = any(
        lowered.startswith(opener) for opener in _IMPERATIVE_OPENERS + ("bump ",)
    )
    asks_about = any(marker in lowered for marker in _ENQUIRY_MARKERS)

    if opens_imperatively and _SEVERITY_TARGET_RE.search(lowered) and not asks_about:
        return "escalate_ticket"

    for action_type, verbs in _ACTION_VERBS:
        for verb in verbs:
            position = lowered.find(verb)
            if position < 0:
                continue
            before = lowered[:position]
            # An enquiry marker anywhere ahead of the verb means the sentence is
            # asking about the verb, not issuing it.
            if any(marker in before for marker in _ENQUIRY_MARKERS):
                continue
            # A polite request frame anywhere ahead of the verb makes this an
            # instruction, even though "can you ..." looks interrogative. It need
            # not be adjacent: "go ahead and issue the credit" puts three words
            # between the frame and the verb. Requiring adjacency dropped that
            # phrasing, and a test caught it. The frame cannot smuggle an enquiry
            # through, because enquiry markers were already rejected above.
            if any(frame in before for frame in _POLITE_REQUEST):
                return action_type
            # Bare imperative: the verb opens the sentence.
            if not before.strip():
                return action_type
            # A transitive imperative opening the sentence and taking this verb
            # as its object: "Issue a service credit for ORD-2002".
            if any(lowered.startswith(opener) for opener in _IMPERATIVE_OPENERS):
                return action_type
    return None


#: Which deterministic rule a question is asking about.
_CANCELLATION_TERMS = (
    "cancel", "cancellation", "cancelling", "canceling", "fee", "charge",
    "waive", "waiver",
)
_CREDIT_TERMS = (
    "credit", "compensat", "refund", "late pickup", "missed pickup", "pickup was",
    "carrier fault", "not collected", "sla breach",
)


def plan(question: str) -> dict[str, Any]:
    """Build a tool plan from the question alone.

    Returns the same shape the LLM router produces, so the orchestrator does not
    branch on which planner ran.
    """
    text = (question or "").strip()
    lowered = text.lower()
    ids = [i.upper() for i in RECORD_ID_RE.findall(text)]
    unique_ids = list(dict.fromkeys(ids))

    tools: list[dict[str, Any]] = []
    reasons: list[str] = []

    # 1. Named records get looked up. Cheap (16ms) and it is what makes a
    #    deterministic verdict and an action payload possible.
    for record_id in unique_ids[:3]:
        tools.append({"tool": "data_query", "record_id": record_id})
    if unique_ids:
        reasons.append(f"named records: {', '.join(unique_ids[:3])}")

    # 2. A policy question about a named ORDER goes to the rule engine, never to
    #    the model. Both rules are added when the wording is ambiguous: 85ms
    #    each, against a wrong number for guessing.
    order_ids = [i for i in unique_ids if i.startswith("ORD-")]
    wants_cancellation = any(t in lowered for t in _CANCELLATION_TERMS)
    wants_credit = any(t in lowered for t in _CREDIT_TERMS)

    for order_id in order_ids[:2]:
        if wants_cancellation:
            tools.append(
                {"tool": "policy_decide", "record_id": order_id, "rule": "cancellation_fee"}
            )
        if wants_credit:
            tools.append(
                {
                    "tool": "policy_decide",
                    "record_id": order_id,
                    "rule": "failed_pickup_credit",
                }
            )
        # A named order with no rule keyword still gets the cancellation rule:
        # it is the most common question, and an unused verdict is harmless.
        if not wants_cancellation and not wants_credit:
            tools.append(
                {"tool": "policy_decide", "record_id": order_id, "rule": "cancellation_fee"}
            )
    if order_ids and (wants_cancellation or wants_credit):
        reasons.append("policy question about a named order")

    # 3. An imperative verb means the user wants something done. A question
    #    about that verb does not.
    action_type = _requested_action(lowered)
    if action_type:
        call: dict[str, Any] = {"tool": "prepare_action", "action_type": action_type}
        if unique_ids:
            call["record_id"] = unique_ids[0]
        call["reason"] = text[:200]
        tools.append(call)
        reasons.append(f"action requested: {action_type}")

    # 4. Policy text, always. Every answer must be citable, and the citation
    #    comes from a document.
    tools.append({"tool": "doc_search", "search_query": text})

    plan_result = {
        "reasoning": "; ".join(reasons) or "general policy question",
        "out_of_scope": False,
        "tools": tools,
        "planner": "fast",
    }
    log.info(
        "fast_route",
        tools=[t["tool"] for t in tools],
        ids=unique_ids,
        action=action_type,
    )
    return plan_result
