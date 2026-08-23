"""The deterministic planner is the shipped default, so it is tested directly.

These are the cheapest tests in the suite and they cover the decision that used
to cost a 2.6-second model call. If a future corpus needs the LLM planner, it is
these assertions that should fail first and say so.
"""

from __future__ import annotations

from agentcore.orchestrator import router


def _tools(question: str) -> list[str]:
    return [t["tool"] for t in router.plan(question)["tools"]]


class TestRecordIdentifiers:
    def test_named_order_is_looked_up_and_ruled_on(self):
        plan = router.plan("Can Northstar cancel ORD-1001 without a cancellation fee?")
        calls = plan["tools"]
        assert {"tool": "data_query", "record_id": "ORD-1001"} in calls
        assert {
            "tool": "policy_decide",
            "record_id": "ORD-1001",
            "rule": "cancellation_fee",
        } in calls

    def test_lowercase_identifiers_are_normalised(self):
        """Users type `ord-1001`. The database stores `ORD-1001`."""
        calls = router.plan("what about ord-1001?")["tools"]
        assert {"tool": "data_query", "record_id": "ORD-1001"} in calls

    def test_a_ticket_is_not_sent_to_the_order_rule_engine(self):
        """`policy_decide` takes order facts; a ticket id would be a lookup miss."""
        plan = router.plan("Please escalate TKT-501, it is urgent")
        assert "policy_decide" not in _tools("Please escalate TKT-501, it is urgent")
        assert {"tool": "data_query", "record_id": "TKT-501"} in plan["tools"]

    def test_repeated_identifiers_are_looked_up_once(self):
        calls = router.plan("ORD-1001 and again ORD-1001")["tools"]
        assert sum(1 for c in calls if c["tool"] == "data_query") == 1

    def test_identifier_fan_out_is_bounded(self):
        """A pasted list must not turn into an unbounded step count."""
        question = " ".join(f"ORD-{1000 + i}" for i in range(12))
        calls = router.plan(question)["tools"]
        assert sum(1 for c in calls if c["tool"] == "data_query") <= 3


class TestRuleSelection:
    def test_credit_wording_selects_the_credit_rule(self):
        plan = router.plan("ORD-2002 pickup was missed due to carrier fault -- credit?")
        rules = {c.get("rule") for c in plan["tools"] if c["tool"] == "policy_decide"}
        assert "failed_pickup_credit" in rules

    def test_ambiguous_wording_runs_both_rules_rather_than_guessing(self):
        """A superfluous verdict costs ~85ms; a missing one costs a wrong answer."""
        plan = router.plan("ORD-2002: do we charge a cancellation fee or owe a credit?")
        rules = {c.get("rule") for c in plan["tools"] if c["tool"] == "policy_decide"}
        assert rules == {"cancellation_fee", "failed_pickup_credit"}

    def test_a_general_question_reaches_only_the_corpus(self):
        assert _tools("What is the bulk upload row limit?") == ["doc_search"]


class TestActionIntent:
    def test_imperative_escalation_prepares_an_action(self):
        plan = router.plan("Please escalate TKT-501 to P1")
        prepared = [c for c in plan["tools"] if c["tool"] == "prepare_action"]
        assert len(prepared) == 1
        assert prepared[0]["action_type"] == "escalate_ticket"
        assert prepared[0]["record_id"] == "TKT-501"

    def test_a_question_about_escalation_is_not_an_instruction_to_escalate(self):
        """"What is the escalation policy" must not stage a state change."""
        assert "prepare_action" not in _tools("What is the escalation policy for P2?")

    def test_asking_whether_a_credit_is_owed_does_not_stage_one(self):
        assert "prepare_action" not in _tools(
            "Is ORD-2002 eligible for a service credit?"
        )


class TestInvariants:
    def test_every_plan_retrieves_a_citable_source(self):
        """No claim without a citation, and citations come from documents."""
        for question in (
            "cancellation fee for ORD-1001?",
            "escalate TKT-501",
            "what is the SLA?",
            "",
        ):
            assert "doc_search" in _tools(question)

    def test_the_planner_never_declares_a_question_out_of_scope(self):
        """Scope is decided by whether evidence exists, not by a keyword match.

        Refusal belongs to the retrieval gate and the citation validator, which
        can see the corpus. A pattern matcher cannot, so it must not pre-empt
        them -- that is how a supported question gets refused for free.
        """
        assert router.plan("how do I file taxes in Portugal?")["out_of_scope"] is False

    def test_the_question_travels_as_the_search_query_verbatim(self):
        question = "Can I cancel a booked shipment without a fee?"
        search = next(c for c in router.plan(question)["tools"] if c["tool"] == "doc_search")
        assert search["search_query"] == question


class TestEnquiryVersusInstruction:
    """The distinction the noun-based first draft got wrong."""

    def test_polite_request_frames_are_instructions(self):
        for phrasing in (
            "Please escalate TKT-501",
            "Can you escalate TKT-501 to P1?",
            "Could you cancel ORD-1002 for me?",
            "Go ahead and issue the credit for ORD-2002",
        ):
            assert "prepare_action" in _tools(phrasing), phrasing

    def test_enquiries_are_not_instructions(self):
        for phrasing in (
            "What is the escalation policy for P2?",
            "How do credits work?",
            "Is ORD-2002 eligible for a credit?",
            "Am I allowed to cancel after pickup?",
            "Do we owe a credit on ORD-2002?",
            "Can I cancel a booked shipment without a fee?",
        ):
            assert "prepare_action" not in _tools(phrasing), phrasing


class TestImperativeOpeners:
    """The phrasings our own test catalog uses, which the gate used to miss.

    The gate fired only when a matched verb opened the sentence or a polite frame
    preceded it. "Issue a service credit of INR 300 for ORD-2002" satisfied
    neither -- "issue" was in no list and "credit" sits four words in -- so the
    credit path was unreachable by the most natural imperative there is, and no
    action was ever staged.
    """

    def test_issue_grant_and_apply_are_instructions(self):
        for phrasing in (
            "Issue a service credit of INR 300 for ORD-2002",
            "Issue a service credit of 300 for ORD-2002",
            "Grant a credit for ORD-2002",
            "Apply a credit to ORD-2002",
            "Process the refund for ORD-2002",
        ):
            plan = router.plan(phrasing)
            prepared = [c for c in plan["tools"] if c["tool"] == "prepare_action"]
            assert prepared, phrasing
            assert prepared[0]["action_type"] == "issue_service_credit", phrasing

    def test_a_severity_target_reads_as_an_escalation(self):
        """"Raise this to P1" names no verb in the action list, but is not vague."""
        for phrasing in ("Raise this to P1", "Bump TKT-501 to P2"):
            plan = router.plan(phrasing)
            prepared = [c for c in plan["tools"] if c["tool"] == "prepare_action"]
            assert prepared, phrasing
            assert prepared[0]["action_type"] == "escalate_ticket", phrasing

    def test_the_enquiry_veto_still_wins(self):
        """Widening the openers must not reopen the noun bug it sits next to."""
        for phrasing in (
            "How do I issue a credit?",
            "What is the process to apply a credit?",
            "What happens when you raise to P1?",
            "Is ORD-2002 eligible for a credit?",
            "What is the escalation policy for P2?",
        ):
            assert "prepare_action" not in _tools(phrasing), phrasing


class TestToolsAreDeclared:
    """Every tool the engine dispatches must be declared in config.yaml.

    CLAUDE.md has a working agreement: a new tool declares whether it is
    tenant-scoped and whether it requires confirmation, in config.yaml. I broke
    it — `cohort_query` and `issue_scan` shipped dispatching in the engine and
    absent from the config, so the reviewed description of the system's tool
    surface silently disagreed with the system.

    That matters beyond tidiness: `config.yaml` is the version-controlled answer
    to "what could this agent do in August", and a tool missing from it is a
    capability with no reviewed declaration. An agreement enforced only by
    remembering is the same shape as the tenancy filter this whole architecture
    exists to replace — so it gets a test.
    """

    def test_every_dispatched_tool_is_declared(self):
        import re
        from pathlib import Path

        from agentcore.settings import load_config

        source = Path("agentcore/orchestrator/engine.py").read_text(encoding="utf-8")
        # Both dispatch shapes: `tool == "x"` and `tool in ("x", "y")`.
        dispatched = set(re.findall(r'tool == "([a-z_]+)"', source))
        for group in re.findall(r'tool in \(((?:"[a-z_]+",?\s*)+)\)', source):
            dispatched.update(re.findall(r'"([a-z_]+)"', group))

        declared = {t.name for t in load_config().tools}
        undeclared = dispatched - declared

        assert not undeclared, (
            f"dispatched but not declared in config.yaml: {sorted(undeclared)}"
        )

    def test_the_router_only_plans_declared_tools(self):
        """A plan naming a tool the engine cannot dispatch is a silent no-op."""
        from agentcore.settings import load_config

        declared = {t.name for t in load_config().tools}
        questions = [
            "Can I cancel ORD-1001 without a cancellation fee?",
            "Show me all open P1 tickets across accounts.",
            "Is TKT-501 an SLA breach?",
            "Please escalate TKT-501",
            "Issue a service credit for ORD-2002",
            "What is the bulk upload row limit?",
            "Which accounts have credit exposure?",
        ]
        for question in questions:
            planned = {t["tool"] for t in router.plan(question)["tools"]}
            assert planned <= declared, (
                f"{question!r} planned undeclared {sorted(planned - declared)}"
            )
