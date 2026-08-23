"""Citation validation.

Every test here is an attack. Each one is a way a language model actually
produces a wrong-but-plausible answer, and the assertion is that the mechanism
catches it rather than that the happy path works.

Real chunks from the ingested corpus are used as the source of truth, so a quote
that "looks right" is tested against the bytes that are genuinely stored.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from agentcore.db import engine
from agentcore.retrieval.hybrid import retrieve
from agentcore.settings import load_config
from agentcore.trust.validator import (
    ANSWER_SCHEMA,
    RejectionReason,
    parse_answer,
    render_prose,
    validate_claims,
)
from agentcore.types import Citation, Claim, Eligibility, Principal, Role

CONFIG = load_config()
TENANT = CONFIG.tenant.id


@pytest.fixture(scope="module")
async def retrieved():
    """A real retrieval result: groundable SOP/contract chunks plus a gated v2."""
    principal = Principal(
        tenant_id=TENANT, user_id="u", role=Role.CUSTOMER, account_id="ACCT-001"
    )
    with engine.scoped(principal, read_only=True) as conn:
        result = await retrieve(
            conn,
            principal,
            "cancellation fee Enterprise P1 response targets service credit",
            CONFIG.retrieval,
            embedder=None,
        )
    if not result.groundable:
        pytest.skip("run `parcelpilot ingest run` before this suite")
    # Both channels, as the orchestrator would pass them: the model sees the
    # gated chunk as context, so it is able to try citing it.
    return result.groundable + result.conflict


def _chunk_containing(retrieved, needle: str):
    for candidate in retrieved:
        if needle.lower() in candidate.chunk.text.lower():
            return candidate
    pytest.skip(f"no retrieved chunk contains {needle!r}")


class TestFabricatedSources:
    def test_citing_a_chunk_that_was_never_retrieved_is_rejected(self, retrieved):
        """The model invents a well-formed UUID.

        It cannot be allowed even if such a chunk exists somewhere in the
        corpus: an answer grounded in something the model never read is not
        grounded.
        """
        claim = Claim(
            text="No cancellation fee applies.",
            citations=[
                Citation(
                    chunk_id=uuid4(),
                    document_id=uuid4(),
                    quote="No fee within 30 minutes of booking.",
                )
            ],
        )
        outcome = validate_claims([claim], retrieved)

        assert not outcome.ok
        assert outcome.claims == []
        assert outcome.rejected[0].reason == RejectionReason.UNKNOWN_CHUNK
        assert outcome.unsupported_claims == ["No cancellation fee applies."]

    def test_unparseable_chunk_id_is_dropped_at_parse_time(self):
        payload = {
            "claims": [
                {
                    "text": "A fee applies.",
                    "citations": [{"chunk_id": "doc-3-para-2", "quote": "some text here"}],
                }
            ],
            "insufficient_evidence": False,
        }
        claims, insufficient = parse_answer(payload)
        # The claim loses its only citation, so it cannot be constructed at all.
        assert claims == []
        assert insufficient is False


class TestMisquotation:
    def test_a_quote_that_is_not_in_the_chunk_is_rejected(self, retrieved):
        target = _chunk_containing(retrieved, "cancel")
        claim = Claim(
            text="Cancellation is always free.",
            citations=[
                Citation(
                    chunk_id=target.chunk.chunk_id,
                    document_id=target.chunk.document_id,
                    quote="Cancellation is always free of charge for all customers.",
                )
            ],
        )
        outcome = validate_claims([claim], retrieved)

        assert not outcome.ok
        assert outcome.rejected[0].reason == RejectionReason.QUOTE_NOT_FOUND

    def test_a_single_altered_number_is_rejected(self, retrieved):
        """The highest-value case in the whole suite.

        A quote that is correct except for the number is exactly what a
        confidently-wrong answer looks like, and it is invisible to a reviewer
        skimming for a plausible citation.
        """
        target = _chunk_containing(retrieved, "No fee within 30 minutes")

        good = Citation(
            chunk_id=target.chunk.chunk_id,
            document_id=target.chunk.document_id,
            quote="No fee within 30 minutes of booking.",
        )
        tampered = Citation(
            chunk_id=target.chunk.chunk_id,
            document_id=target.chunk.document_id,
            quote="No fee within 60 minutes of booking.",
        )

        assert validate_claims([Claim(text="ok", citations=[good])], retrieved).ok
        outcome = validate_claims([Claim(text="bad", citations=[tampered])], retrieved)
        assert not outcome.ok
        assert outcome.rejected[0].reason == RejectionReason.QUOTE_NOT_FOUND

    def test_line_wrapping_does_not_break_a_correct_quote(self, retrieved):
        """PDF wrapping is layout, not content.

        The SOP wraps mid-sentence ("After 30 minutes,\\ncharge INR 250"). A
        human or model quoting that clause writes it as one line, and rejecting
        that would make the validator fail on CORRECT citations -- after which
        the team would turn it off.
        """
        target = _chunk_containing(retrieved, "charge INR 250")
        claim = Claim(
            text="A fee of INR 250 applies after the free window.",
            citations=[
                Citation(
                    chunk_id=target.chunk.chunk_id,
                    document_id=target.chunk.document_id,
                    quote=(
                        "After 30 minutes, charge INR 250 unless a customer agreement "
                        "explicitly waives the cancellation fee."
                    ),
                )
            ],
        )
        outcome = validate_claims([claim], retrieved)

        assert outcome.ok
        # The stored text, line break included, is what gets recorded -- so the
        # citation reproduces the document rather than a tidied-up version.
        assert "\n" in outcome.claims[0].citations[0].quote

    def test_a_too_short_quote_is_rejected(self, retrieved):
        """"no fee" matches several unrelated rules, so it is not evidence for
        any particular one."""
        target = _chunk_containing(retrieved, "no fee")
        claim = Claim(
            text="It is free.",
            citations=[
                Citation(
                    chunk_id=target.chunk.chunk_id,
                    document_id=target.chunk.document_id,
                    quote="no fee",
                )
            ],
        )
        outcome = validate_claims([claim], retrieved)
        assert outcome.rejected[0].reason == RejectionReason.QUOTE_TOO_SHORT


class TestEligibilityAtTheLastMoment:
    def test_quoting_the_deprecated_policy_is_rejected(self, retrieved):
        """The gate has to survive to the end of the pipeline.

        The deprecated v2 chunk is handed to the model as conflict context, so
        the model *can* try to cite it -- and its text really is in the corpus,
        so a verbatim check alone would pass. Only eligibility stops it.
        """
        gated = next(
            (c for c in retrieved if c.eligibility is Eligibility.CONFLICT_ONLY), None
        )
        if gated is None:
            pytest.skip("no gated chunk in this retrieval")

        # A genuine, verbatim quote from the real deprecated document.
        quote = gated.chunk.text.strip().split("\n")[0][:80]
        claim = Claim(
            text="Enterprise P1 response time is one hour.",
            citations=[
                Citation(
                    chunk_id=gated.chunk.chunk_id,
                    document_id=gated.chunk.document_id,
                    quote=quote,
                )
            ],
        )
        outcome = validate_claims([claim], retrieved)

        assert not outcome.ok
        assert outcome.rejected[0].reason == RejectionReason.NOT_GROUNDABLE

    def test_every_surviving_citation_points_at_a_groundable_source(self, retrieved):
        groundable = [c for c in retrieved if c.eligibility is Eligibility.GROUNDABLE]
        by_id = {c.chunk.chunk_id: c for c in retrieved}

        claims = [
            Claim(
                text=f"Claim {i}",
                citations=[
                    Citation(
                        chunk_id=c.chunk.chunk_id,
                        document_id=c.chunk.document_id,
                        quote=c.chunk.text.strip().split("\n")[0][:90],
                    )
                ],
            )
            for i, c in enumerate(groundable[:3])
        ]
        outcome = validate_claims(claims, retrieved)

        for claim in outcome.claims:
            for citation in claim.citations:
                assert by_id[citation.chunk_id].eligibility is Eligibility.GROUNDABLE


class TestOffsetResolution:
    def test_offsets_are_resolved_by_the_validator(self, retrieved):
        """The model is never asked to count characters.

        It cannot do it reliably, and a citation that highlights the wrong span
        is worse than one that highlights none.
        """
        target = _chunk_containing(retrieved, "No fee within 30 minutes")
        claim = Claim(
            text="There is a free window.",
            citations=[
                Citation(
                    chunk_id=target.chunk.chunk_id,
                    document_id=target.chunk.document_id,
                    quote="No fee within 30 minutes of booking.",
                )
            ],
        )
        outcome = validate_claims([claim], retrieved)
        citation = outcome.claims[0].citations[0]

        assert citation.is_resolved
        # The span must actually locate the quote in the stored text.
        assert target.chunk.text[citation.start : citation.end] == citation.quote

    def test_document_id_is_taken_from_the_chunk_not_the_model(self, retrieved):
        """Prevents misattributing a real quote to the wrong document -- which
        would show the right words under the wrong contract."""
        target = _chunk_containing(retrieved, "No fee within 30 minutes")
        wrong_document = uuid4()
        claim = Claim(
            text="There is a free window.",
            citations=[
                Citation(
                    chunk_id=target.chunk.chunk_id,
                    document_id=wrong_document,
                    quote="No fee within 30 minutes of booking.",
                )
            ],
        )
        outcome = validate_claims([claim], retrieved)

        resolved = outcome.claims[0].citations[0]
        assert resolved.document_id == target.chunk.document_id
        assert resolved.document_id != wrong_document


class TestPartialFailure:
    def test_a_claim_keeps_its_surviving_citations(self, retrieved):
        target = _chunk_containing(retrieved, "No fee within 30 minutes")
        claim = Claim(
            text="A free window applies.",
            citations=[
                Citation(
                    chunk_id=target.chunk.chunk_id,
                    document_id=target.chunk.document_id,
                    quote="No fee within 30 minutes of booking.",
                ),
                Citation(chunk_id=uuid4(), document_id=uuid4(), quote="invented clause text"),
            ],
        )
        outcome = validate_claims([claim], retrieved)

        assert len(outcome.claims) == 1
        assert len(outcome.claims[0].citations) == 1
        assert len(outcome.rejected) == 1
        # Still not `ok`: the model believed the fabricated citation as much as
        # the real one, so the whole generation is suspect.
        assert not outcome.ok

    def test_ok_requires_content_as_well_as_no_rejections(self):
        outcome = validate_claims([], [])
        assert not outcome.ok
        assert not outcome.has_content


class TestProseRendering:
    def test_markers_index_the_citation_list(self, retrieved):
        target = _chunk_containing(retrieved, "No fee within 30 minutes")
        citation = Citation(
            chunk_id=target.chunk.chunk_id,
            document_id=target.chunk.document_id,
            quote="No fee within 30 minutes of booking.",
        )
        outcome = validate_claims(
            [
                Claim(text="There is a free window.", citations=[citation]),
                Claim(text="It lasts thirty minutes.", citations=[citation]),
            ],
            retrieved,
        )

        text, citations = render_prose(outcome.claims)

        assert "[1]" in text
        # The same span cited twice is one entry, numbered once.
        assert len(citations) == 1
        assert "[2]" not in text
        # Marker sits inside the full stop, where a footnote belongs.
        assert text.startswith("There is a free window [1].")

    def test_prose_is_built_only_from_validated_claims(self, retrieved):
        """The model's own paragraph is never displayed.

        If it were, it could assert things no claim covers, and the citation
        markers would decorate text that was never checked.
        """
        claim = Claim(
            text="Unsupported assertion.",
            citations=[Citation(chunk_id=uuid4(), document_id=uuid4(), quote="fabricated text")],
        )
        outcome = validate_claims([claim], retrieved)
        text, citations = render_prose(outcome.claims)

        assert text == ""
        assert citations == []


class TestSchema:
    def test_schema_has_no_free_prose_field(self):
        """The model returns claims and quotes only.

        A `prose` or `answer` field would be text nobody validated, displayed
        beside citations that imply it was.
        """
        properties = ANSWER_SCHEMA["properties"]
        assert set(properties) == {"claims", "insufficient_evidence"}

    def test_schema_requires_a_quote_per_citation(self):
        citation_schema = ANSWER_SCHEMA["properties"]["claims"]["items"]["properties"][
            "citations"
        ]["items"]
        assert set(citation_schema["required"]) == {"chunk_id", "quote"}

    def test_insufficient_evidence_is_a_first_class_outcome(self):
        """Refusing must be as easy for the model as answering."""
        claims, insufficient = parse_answer(
            {"claims": [], "insufficient_evidence": True}
        )
        assert claims == []
        assert insufficient is True

    def test_schema_survives_translation_to_the_gemini_dialect(self):
        """Guards the provider-specific conversion against schema changes."""
        from agentcore.llm.gemini import to_gemini_schema

        converted = to_gemini_schema(ANSWER_SCHEMA)
        assert converted["type"] == "OBJECT"
        claims = converted["properties"]["claims"]
        assert claims["type"] == "ARRAY"
        assert claims["items"]["properties"]["citations"]["type"] == "ARRAY"
        # propertyOrdering makes generations diffable when debugging.
        assert "propertyOrdering" in converted
