"""The deterministic planner is the shipped default, so it is tested directly.

These are the cheapest tests in the suite and they cover the decision that used
to cost a 2.6-second model call. If a future corpus needs the LLM planner, it is
these assertions that should fail first and say so.
"""

from __future__ import annotations

import pytest

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


class TestResultTables:
    """The table's column keys must match what the detectors actually emit.

    My first pass invented `hours_past_window` and `carrier_fault`. The detector
    emits `overdue_hours` and `fault_attributed`, so every cell in that table
    rendered as an em-dash — which reads as missing data when the data was right
    there. Guessing at a neighbouring module's field names is exactly the kind of
    mistake a test catches for free.
    """

    def test_every_spec_renders_real_values_for_its_kind(self):
        from agentcore.analytics.issues import Issue
        from agentcore.orchestrator import tables

        # One synthetic finding per spec'd kind, carrying the metric keys the
        # detectors emit. Sourced from `agentcore/analytics/issues.py`.
        samples = {
            "sla_breach": {
                "elapsed_minutes": 30.0, "target_minutes": 15.0,
                "over_by_minutes": 15.0, "target_from": "ACCT-001",
                "assigned_to": "Rohit",
            },
            "credit_eligible": {
                "amount": "300", "currency": "INR", "delay_hours": 4.5,
                "threshold_hours": 4, "threshold_from": "ACCT-002",
                "carrier": "RoadRunner",
            },
            "pickup_overdue": {
                "overdue_hours": 4.5, "carrier": "RoadRunner",
                "fault_attributed": True,
            },
            "stale_answer": {},
        }

        for kind, metrics in samples.items():
            issue = Issue(
                kind=kind, severity="P1", title="t", detail="a detail",
                account_id="ACCT-001", subject_id="TKT-501", metrics=metrics,
            )
            built = tables.from_findings([issue], [])
            assert built, f"{kind} produced no table"
            table = built[0]
            assert len(table.rows) == 1
            row = table.rows[0]
            assert len(row) == len(table.columns), (
                f"{kind}: {len(row)} cells for {len(table.columns)} columns"
            )
            # The failure mode being guarded: a row of em-dashes.
            assert row.count("—") <= 1, f"{kind} rendered mostly em-dashes: {row}"

    def test_account_name_is_joined_in_when_available(self):
        from agentcore.analytics.issues import Issue
        from agentcore.orchestrator import tables

        issue = Issue(
            kind="sla_breach", severity="P1", title="t", detail="d",
            account_id="ACCT-004", subject_id="TKT-505",
            metrics={"target_minutes": 30, "elapsed_minutes": 150,
                     "over_by_minutes": 120, "target_from": "default"},
        )
        built = tables.from_findings(
            [issue], [{"account_id": "ACCT-004", "account_name": "Axis Labs"}]
        )
        assert "Axis Labs (ACCT-004)" in built[0].rows[0]

    def test_a_contract_sourced_threshold_is_marked(self):
        """"15 min (contract)" is the interesting cell: it says this customer's
        agreement set the target, not the plan default."""
        from agentcore.analytics.issues import Issue
        from agentcore.orchestrator import tables

        issue = Issue(
            kind="sla_breach", severity="P1", title="t", detail="d",
            account_id="ACCT-001", subject_id="TKT-501",
            metrics={"target_minutes": 15, "elapsed_minutes": 30,
                     "over_by_minutes": 15, "target_from": "ACCT-001"},
        )
        row = tables.from_findings([issue], [])[0].rows[0]
        assert any("(contract)" in cell for cell in row)


class TestListingPhrasings:
    """A person typing into a box does not consult our keyword list.

    The cohort detector began as exact phrases — "show me all", "across
    accounts", "open tickets". "show all tickets", the most obvious way anyone
    would ask, matched none of them, and nor did "list all tickets", "give me the
    tickets" or "what are the current tickets". Seven of twelve plain phrasings
    fell through to `doc_search`, which cannot answer a question about rows, so
    the run refused.

    Matching the shape — a plural record noun plus a listing signal — catches
    phrasings nobody enumerated, which is the point. These cases are the record
    of what real wording looks like.
    """

    LISTING = [
        "show all tickets",
        "show me all tickets",
        "list all tickets",
        "all tickets",
        "show tickets",
        "give me the tickets",
        "what tickets are open",
        "what are the current tickets",
        "show me the ticket list",
        "show me every account",
        "which orders are overdue",
        "how many tickets are open",
        "open tickets please",
        "show all open P1 tickets across accounts",
    ]

    #: Questions about ONE thing, or about a rule. A cohort query here is a
    #: wasted round trip at best and a confusing table at worst.
    NOT_LISTING = [
        "Can I cancel ORD-1001 without a cancellation fee?",
        "What is the cancellation fee on ORD-2001?",
        "What is the supported bulk upload row limit?",
        "Please escalate TKT-501",
        "What is the escalation policy for P2?",
        "Is ORD-2002 eligible for a credit?",
        "What is my first-response target for a P1 outage?",
        "Why did my bulk upload CSV with 3,500 rows fail?",
        "A pickup for ORD-2002 is late because of carrier fault. Do we owe a credit?",
        "What customs paperwork do I need for Germany?",
    ]

    def test_plain_listing_phrasings_reach_the_data(self):
        for question in self.LISTING:
            planned = _tools(question)
            assert "cohort_query" in planned or "issue_scan" in planned, question

    def test_single_record_and_rule_questions_do_not(self):
        for question in self.NOT_LISTING:
            assert "cohort_query" not in _tools(question), question

    def test_naming_a_record_beats_the_listing_shape(self):
        """"Show me ORD-1001" wants that order, not every order."""
        planned = _tools("show me ORD-1001")
        assert "cohort_query" not in planned
        assert "data_query" in planned


class TestRecordIdsAsPeopleTypeThem:
    """A guard keyed on punctuation is not a guard.

    Asked "what is cancelation price of ord 2001" -- no hyphen, lower case --
    the recogniser matched nothing. So no scoped lookup ran, the not-visible
    halt never fired, and the run answered from general policy as though
    ORD-2001 were the caller's order. It belongs to another customer.

    Nothing leaked, because no row was ever read. But the whole point of the
    halt is that a confident answer about the wrong record is worse than no
    answer, and the halt was reachable only by callers who typed the id the way
    the database writes it.
    """

    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("what is cancelation price of ord 2001", ["ORD-2001"]),
            ("Can I cancel ORD-1001 without a cancellation fee?", ["ORD-1001"]),
            ("tell me about order 2001", ["ORD-2001"]),
            ("status of ord2001", ["ORD-2001"]),
            ("What is the cancellation fee on ORD - 2001?", ["ORD-2001"]),
            ("escalate ticket 501 to P1", ["TKT-501"]),
            ("look up acct 001 and ORD-1001", ["ACCT-001", "ORD-1001"]),
            # Order preserved, duplicates collapsed.
            ("ORD-1001 and ord 1001 again", ["ORD-1001"]),
        ],
    )
    def test_spoken_and_written_forms_both_resolve(self, question, expected):
        assert router.find_record_ids(question) == expected

    @pytest.mark.parametrize(
        "question",
        [
            # Digits that are quantities, not identifiers. Each of these appears
            # verbatim in the corpus or the question catalog.
            "After 30 minutes, charge INR 250 unless a customer agreement waives",
            "the supported limit is 5000 rows",
            "What is the standard first-response target for a P1 on Enterprise?",
            "Show me all open P1 tickets across accounts",
            "issue a credit for 300 rupees",
            "resolve within 4 hours",
        ],
    )
    def test_quantities_are_not_read_as_records(self, question):
        assert router.find_record_ids(question) == []

    def test_a_named_record_still_routes_to_a_scoped_lookup(self):
        """The recogniser only matters because of what it triggers."""
        planned = _tools("what is cancelation price of ord 2001")
        assert "data_query" in planned

    def test_engine_and_router_share_one_recogniser(self):
        """Two copies of this pattern drifted once and could stage an action
        against a record the halt condition had not checked."""
        from agentcore.orchestrator import engine

        assert not hasattr(engine, "_RECORD_ID_RE")
