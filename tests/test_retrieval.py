"""Hybrid retrieval.

The tests worth having here are about *recall on realistic questions* and about
the eligibility gate holding. A retrieval layer that scores beautifully on
keyword queries and returns nothing for "can I cancel without a fee" is worse
than useless, because the failure is silent -- the engine simply refuses, and
looks appropriately cautious while being broken.
"""

from __future__ import annotations

import pytest

from agentcore.db import engine
from agentcore.retrieval.hybrid import (
    authority_order,
    detect_superseded,
    reciprocal_rank_fusion,
    retrieve,
)
from agentcore.settings import load_config
from agentcore.types import Eligibility, Freshness, Principal, Role, SourceClass

CONFIG = load_config()
TENANT = CONFIG.tenant.id


def _principal(account_id: str | None = "ACCT-001", role: Role = Role.CUSTOMER) -> Principal:
    return Principal(tenant_id=TENANT, user_id="u", role=role, account_id=account_id)


async def _search(query: str, principal: Principal | None = None):
    who = principal or _principal()
    with engine.scoped(who, read_only=True) as conn:
        return await retrieve(conn, who, query, CONFIG.retrieval, embedder=None)


@pytest.fixture(scope="module", autouse=True)
def _index_present():
    with engine.admin() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM index_versions WHERE tenant_id = %s AND status = 'active'",
            (TENANT,),
        ).fetchone()
    if not row or not row["n"]:
        pytest.skip("run `parcelpilot ingest run` before this suite")


class TestNaturalLanguageRecall:
    """Regression tests for the AND-semantics bug.

    `websearch_to_tsquery` and `plainto_tsquery` require EVERY term to appear in
    a chunk. Each question below contains at least one word the operative clause
    does not use, so each returned zero results before the fix -- while looking
    like a working system.
    """

    @pytest.mark.parametrize(
        ("query", "expect_file", "expect_section"),
        [
            # "shipment" never appears in the SOP's cancellation section.
            (
                "Can I cancel a booked shipment without a cancellation fee?",
                "03_Cancellation_and_Service_Credit_SOP_v4.pdf",
                "cancellation",
            ),
            # "outage" and "first" are not both in the targets table.
            (
                "What is the first response time for a P1 outage on Enterprise?",
                "01_Support_Policy_v3_CURRENT.pdf",
                "response",
            ),
            # Conversational phrasing with several incidental words.
            (
                "my pickup was missed by the carrier, do I get a service credit?",
                "03_Cancellation_and_Service_Credit_SOP_v4.pdf",
                "credit",
            ),
        ],
    )
    async def test_question_finds_its_clause_at_rank_one(
        self, query, expect_file, expect_section
    ):
        result = await _search(query)

        assert result.groundable, f"no groundable results for {query!r}"
        top = result.groundable[0]
        assert top.document.filename == expect_file
        assert expect_section in (top.chunk.section_path or "").lower()

    async def test_keyword_query_still_works(self):
        """OR semantics must not have cost precision on terse queries."""
        result = await _search("bulk upload CSV row limit")
        assert result.groundable
        assert "04_Product" in result.groundable[0].document.filename

    async def test_stopword_only_query_does_not_crash(self):
        """to_tsvector yields no lexemes, so the tsquery is empty.

        Guarded with numnode() > 0 -- an empty tsquery would otherwise behave
        unpredictably rather than simply matching nothing.
        """
        result = await _search("the and of")
        assert result.groundable == []
        assert result.lexical_hits == 0

    async def test_empty_query_returns_nothing(self):
        result = await _search("   ")
        assert result.candidates == []


class TestEligibilityGate:
    """The gate must be a filter, not a weight.

    v1 multiplied authority into the relevance score, which only *penalises* a
    deprecated document -- a strong enough match still wins. These assert the
    stronger property: it cannot win, at any score.
    """

    async def test_deprecated_policy_is_never_groundable(self):
        result = await _search("Enterprise P1 P2 P3 response targets hours")

        groundable_files = {c.document.filename for c in result.groundable}
        assert "02_Support_Policy_v2_DEPRECATED.pdf" not in groundable_files

        for chunk in result.groundable:
            assert chunk.eligibility is Eligibility.GROUNDABLE
            assert chunk.document.freshness is not Freshness.DEPRECATED
            assert chunk.document.authority > 0

    async def test_deprecated_policy_is_still_retrieved_for_conflict(self):
        """Gated, not hidden.

        The engine has to be able to say "a superseded version says otherwise".
        Dropping v2 silently would leave a user who read v2 with no explanation.
        """
        result = await _search("Enterprise P1 P2 P3 response targets hours")

        conflict_files = {c.document.filename for c in result.conflict}
        assert "02_Support_Policy_v2_DEPRECATED.pdf" in conflict_files
        assert all(c.eligibility is Eligibility.CONFLICT_ONLY for c in result.conflict)

    async def test_superseded_pairs_are_detected_structurally(self):
        """policy_family makes this a join, not filename parsing."""
        result = await _search("Enterprise P1 P2 P3 response targets hours")
        pairs = detect_superseded(result)

        assert pairs, "expected v2 to be paired with v3"
        stale, current = pairs[0]
        assert stale.document.version_label == "v2"
        assert current.document.version_label == "v3"
        assert stale.document.policy_family == current.document.policy_family

    async def test_channels_are_disjoint(self):
        result = await _search("cancellation fee service credit policy")
        ids = [
            {c.chunk.chunk_id for c in channel}
            for channel in (result.groundable, result.conflict, result.context)
        ]
        assert ids[0].isdisjoint(ids[1])
        assert ids[0].isdisjoint(ids[2])
        assert ids[1].isdisjoint(ids[2])

    async def test_gate_is_applied_before_truncation(self):
        """Split by eligibility, then take final_k.

        Truncating first would let a run of gated chunks crowd the groundable
        ones out of the window -- the same failure a weight-based scheme has.
        """
        result = await _search("Enterprise P1 P2 P3 response targets hours")
        assert len(result.groundable) == min(
            CONFIG.retrieval.final_k,
            len([c for c in result.candidates if c.eligibility is Eligibility.GROUNDABLE]),
        )


class TestAccountIsolation:
    """Retrieval contains no account predicate; RLS does this.

    So these tests are really asserting that relying on the database was
    sufficient -- that nothing leaks through a path the query author forgot.
    """

    async def test_customer_never_retrieves_another_customers_contract(self):
        # Phrased to match the LumenWorks credit clause as closely as possible.
        query = "fixed INR 300 service credit 4 hours past pickup window LumenWorks"
        result = await _search(query, _principal("ACCT-001"))

        files = {c.document.filename for c in result.candidates}
        assert "06_LumenWorks_Service_Agreement.pdf" not in files

    async def test_each_customer_retrieves_only_its_own_agreement(self):
        query = "cancellation fee waiver agreement"

        northstar = await _search(query, _principal("ACCT-001"))
        lumenworks = await _search(query, _principal("ACCT-002"))

        def contracts(result):
            return {
                c.document.filename
                for c in result.candidates
                if c.document.source_class is SourceClass.CUSTOMER_AGREEMENT
            }

        assert contracts(northstar) <= {"05_Northstar_Logistics_Enterprise_Agreement.pdf"}
        assert contracts(lumenworks) <= {"06_LumenWorks_Service_Agreement.pdf"}

    async def test_account_without_an_agreement_retrieves_no_contract(self):
        result = await _search("cancellation fee waiver agreement", _principal("ACCT-003"))
        assert not [
            c
            for c in result.candidates
            if c.document.source_class is SourceClass.CUSTOMER_AGREEMENT
        ]

    async def test_staff_retrieve_across_accounts(self):
        result = await _search(
            "cancellation fee waiver agreement credit",
            _principal(None, Role.OPERATIONS_ADMIN),
        )
        contracts = {
            c.document.filename
            for c in result.candidates
            if c.document.source_class is SourceClass.CUSTOMER_AGREEMENT
        }
        assert len(contracts) == 2


class TestFusion:
    """RRF is rank-based on purpose: ts_rank_cd and cosine have unrelated
    scales, and normalising them means inventing a conversion."""

    def test_found_by_both_rankers_outranks_found_by_one(self):
        lexical = [{"chunk_id": "a", "score": 0.1}, {"chunk_id": "b", "score": 0.09}]
        dense = [{"chunk_id": "b", "score": 0.8}, {"chunk_id": "c", "score": 0.7}]

        fused = reciprocal_rank_fusion(lexical, dense, k=60)

        # b is rank 2 lexically and rank 1 densely; a is rank 1 lexically only.
        assert fused["b"]["fused"] > fused["a"]["fused"]
        assert fused["b"]["fused"] > fused["c"]["fused"]

    def test_score_magnitude_cannot_dominate_rank(self):
        """A ranker reporting huge raw scores must not win by scale alone."""
        lexical = [{"chunk_id": "a", "score": 999999.0}]
        dense = [{"chunk_id": "b", "score": 0.0001}]

        fused = reciprocal_rank_fusion(lexical, dense, k=60)
        assert fused["a"]["fused"] == pytest.approx(fused["b"]["fused"])

    def test_provenance_of_each_half_is_kept(self):
        """The run log needs to show which ranker found what."""
        fused = reciprocal_rank_fusion(
            [{"chunk_id": "a", "score": 0.5}], [{"chunk_id": "a", "score": 0.9}], k=60
        )
        assert fused["a"]["lex"] == (1, 0.5)
        assert fused["a"]["dense"] == (1, 0.9)

    def test_missing_scores_are_tolerated(self):
        fused = reciprocal_rank_fusion([{"chunk_id": "a", "score": None}], [], k=60)
        assert fused["a"]["fused"] > 0


class TestAuthorityOrdering:
    async def test_authority_orders_only_eligible_chunks(self):
        """Authority decides between two sources that could both answer.

        Applied after the gate, never folded into the retrieval score -- so a
        contract outranks the SOP, but a deprecated policy is not in the running
        at all.
        """
        result = await _search("cancellation fee no fee before pickup")
        ordered = authority_order(result.groundable)

        authorities = [c.document.authority for c in ordered]
        assert authorities == sorted(authorities, reverse=True)
        assert all(a > 0 for a in authorities)

    async def test_contract_outranks_sop_on_authority(self):
        result = await _search("cancellation fee no fee before pickup")
        ordered = authority_order(result.groundable)
        classes = [c.document.source_class for c in ordered]
        if SourceClass.CUSTOMER_AGREEMENT in classes and SourceClass.SOP_CURRENT in classes:
            assert classes.index(SourceClass.CUSTOMER_AGREEMENT) < classes.index(
                SourceClass.SOP_CURRENT
            )


class TestReproducibility:
    async def test_results_are_pinned_to_an_index_version(self):
        """Recorded so an answer can be reproduced after the next ingest."""
        result = await _search("cancellation fee")
        assert result.index_version_id is not None

    async def test_repeated_queries_are_stable(self):
        """Ties break on chunk_id, so ordering does not wobble between runs."""
        first = await _search("cancellation fee service credit")
        second = await _search("cancellation fee service credit")
        assert [c.chunk.chunk_id for c in first.groundable] == [
            c.chunk.chunk_id for c in second.groundable
        ]

    async def test_dense_absence_is_reported_not_hidden(self):
        """Lexical-only is a supported mode, but it must be visible in the log
        rather than looking like full-fidelity retrieval."""
        result = await _search("cancellation fee")
        assert result.dense_available is False
        assert result.as_log()["dense_available"] is False
