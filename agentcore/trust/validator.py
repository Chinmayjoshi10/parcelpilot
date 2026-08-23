"""Citation validation: the mechanism behind "every claim is cited".

Every RAG system claims this. Almost none check it. The difference between the
claim and the guarantee is this file, and it is about eighty lines of real work.

A generated answer arrives as structured data -- a list of claims, each with
`chunk_id` and a verbatim `quote`. Before it may be shown to anyone, every
citation is checked against the chunks that were *actually retrieved for this
run*:

1. **Existence.** The cited chunk must be one of the retrieved candidates. A
   plausible-looking UUID the model produced from nothing fails here.
2. **Eligibility.** The chunk must be groundable. A quote from the deprecated v2
   policy is rejected even though the text really is in the corpus -- which is
   the whole point of the gate surviving all the way to the end.
3. **Verbatim span.** The quote must appear in that chunk, word for word.
   Whitespace-insensitive, because PDF line wrapping is layout rather than
   content; nothing else is forgiven. A quote that changes "30 minutes" to "60
   minutes" fails.
4. **Support.** Every claim must retain at least one surviving citation. A claim
   whose citations were all rejected is dropped, not silently kept.

The outcome is deliberately blunt: an answer that fails validation is never
returned. The orchestrator retries once and then refuses, because a refusal that
offers a human is cheaper than a fluent answer that is wrong.

What this does NOT check is whether the quote actually *entails* the claim. That
is a judgement, not a computation. This layer removes the mechanical failures --
fabricated sources, misattributed quotes, ineligible sources, invented numbers --
which is where the wrong answers empirically come from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from agentcore.logging import get_logger
from agentcore.trust.spans import find_span
from agentcore.types import (
    Citation,
    Claim,
    Eligibility,
    RetrievedChunk,
)

log = get_logger(__name__)


class RejectionReason:
    """Why one citation was thrown away. Strings, because they are logged and
    surfaced to whoever is debugging a refusal."""

    UNKNOWN_CHUNK = "cited a chunk that was not retrieved for this run"
    NOT_GROUNDABLE = "cited a source that may not ground a claim"
    QUOTE_NOT_FOUND = "quote does not appear in the cited chunk"
    EMPTY_QUOTE = "citation carries no quote"
    QUOTE_TOO_SHORT = "quote is too short to identify a clause"

    #: Below this many characters a quote matches almost anything ("no fee",
    #: "30 minutes") and stops being evidence for a specific rule.
    MIN_QUOTE_CHARS = 12


@dataclass
class RejectedCitation:
    claim_text: str
    citation: Citation
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim_text[:200],
            "chunk_id": str(self.citation.chunk_id),
            "reason": self.reason,
            "quote": self.citation.quote[:160],
        }


@dataclass
class ValidationOutcome:
    """Result of validating one generated answer."""

    #: Claims that survived, with citations rewritten to carry resolved offsets.
    claims: list[Claim] = field(default_factory=list)
    rejected: list[RejectedCitation] = field(default_factory=list)
    #: Claims dropped entirely because every citation was rejected.
    unsupported_claims: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing was rejected and at least one claim survived.

        Strict on purpose. A partially-valid answer is not a smaller problem
        than an invalid one: the surviving claims sit beside rejected ones that
        the model believed equally, so the whole generation is suspect.
        """
        return bool(self.claims) and not self.rejected and not self.unsupported_claims

    @property
    def has_content(self) -> bool:
        return bool(self.claims)

    def as_log(self) -> dict[str, Any]:
        return {
            "claims_valid": len(self.claims),
            "citations_rejected": len(self.rejected),
            "claims_unsupported": len(self.unsupported_claims),
            "reasons": sorted({r.reason for r in self.rejected}),
        }


def validate_claims(
    claims: list[Claim],
    retrieved: list[RetrievedChunk],
    *,
    require_verbatim: bool = True,
) -> ValidationOutcome:
    """Check every citation against what was actually retrieved.

    `retrieved` is the run's own candidate set, not the whole corpus. That
    matters: a model that cites a real clause it was never shown has still
    fabricated its reasoning, and a system that accepts it cannot claim its
    answers are grounded in what it read.
    """
    by_id: dict[UUID, RetrievedChunk] = {c.chunk.chunk_id: c for c in retrieved}
    outcome = ValidationOutcome()

    for claim in claims:
        surviving: list[Citation] = []

        for citation in claim.citations:
            rejection = _check(citation, by_id, require_verbatim=require_verbatim)
            if rejection is not None:
                outcome.rejected.append(
                    RejectedCitation(
                        claim_text=claim.text, citation=citation, reason=rejection
                    )
                )
                continue

            # Offsets are resolved here rather than asked of the model: an LLM
            # cannot reliably count characters, and a citation that highlights
            # the wrong span is worse than one that highlights none.
            chunk = by_id[citation.chunk_id]
            span = find_span(chunk.chunk.text, citation.quote)
            assert span is not None  # _check already established this
            surviving.append(
                Citation(
                    chunk_id=citation.chunk_id,
                    document_id=chunk.chunk.document_id,
                    quote=span.text,
                    start=span.start,
                    end=span.end,
                )
            )

        if surviving:
            outcome.claims.append(Claim(text=claim.text, citations=surviving))
        else:
            # Never kept as an uncited assertion. The only way to say something
            # unsupported is a refusal.
            outcome.unsupported_claims.append(claim.text)

    log.info("citations_validated", **outcome.as_log())
    return outcome


def _check(
    citation: Citation,
    by_id: dict[UUID, RetrievedChunk],
    *,
    require_verbatim: bool,
) -> str | None:
    """Return a rejection reason, or None if the citation is sound."""
    quote = (citation.quote or "").strip()
    if not quote:
        return RejectionReason.EMPTY_QUOTE
    if len(quote) < RejectionReason.MIN_QUOTE_CHARS:
        return RejectionReason.QUOTE_TOO_SHORT

    chunk = by_id.get(citation.chunk_id)
    if chunk is None:
        return RejectionReason.UNKNOWN_CHUNK

    # The gate again, at the last possible moment. Retrieval already separated
    # the channels, but a model handed conflict context in its prompt can still
    # try to cite it -- and by here it would otherwise be too late.
    if chunk.eligibility is not Eligibility.GROUNDABLE:
        return RejectionReason.NOT_GROUNDABLE

    if require_verbatim and find_span(chunk.chunk.text, quote) is None:
        return RejectionReason.QUOTE_NOT_FOUND

    return None


def render_prose(claims: list[Claim], *, marker: str = "[{n}]") -> tuple[str, list[Citation]]:
    """Assemble validated claims into display text with numbered markers.

    Rendered from the validated claims rather than from anything the model
    wrote as prose. If the model's own paragraph were displayed, it could assert
    things no claim covers -- and the citation markers would then decorate text
    that was never checked.

    Returns the text and the citation list the markers index into, so the UI can
    open the exact span behind `[2]`.
    """
    ordered: list[Citation] = []
    seen: dict[tuple[UUID, int | None], int] = {}
    parts: list[str] = []

    for claim in claims:
        markers: list[str] = []
        for citation in claim.citations:
            key = (citation.chunk_id, citation.start)
            if key not in seen:
                ordered.append(citation)
                seen[key] = len(ordered)
            markers.append(marker.format(n=seen[key]))
        text = claim.text.rstrip()
        # Markers go inside the sentence's full stop, where a reader expects a
        # footnote, rather than trailing after it.
        if text.endswith("."):
            text = f"{text[:-1]} {''.join(markers)}."
        else:
            text = f"{text} {''.join(markers)}"
        parts.append(text)

    return " ".join(parts), ordered


#: JSON Schema for the synthesis call. Lives here, next to the validator that
#: enforces it, so the shape asked for and the shape checked cannot drift.
#:
#: Note what is absent: any field for free-form prose. The model produces claims
#: and citations only; the displayed text is assembled from what survived
#: validation.
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "description": (
                "One entry per distinct assertion. Every claim must be supported "
                "by at least one quote from the provided sources."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "A single factual assertion, one sentence.",
                    },
                    "citations": {
                        "type": "array",
                        "description": "Sources supporting this exact claim.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "chunk_id": {
                                    "type": "string",
                                    "description": (
                                        "The chunk_id of a provided source, copied exactly."
                                    ),
                                },
                                "quote": {
                                    "type": "string",
                                    "description": (
                                        "Text copied VERBATIM from that source, long "
                                        "enough to identify the clause. Do not "
                                        "paraphrase, correct or shorten it."
                                    ),
                                },
                            },
                            "required": ["chunk_id", "quote"],
                        },
                    },
                },
                "required": ["text", "citations"],
            },
        },
        "insufficient_evidence": {
            "type": "boolean",
            "description": (
                "True if the provided sources do not answer the question. Prefer "
                "this over a claim you cannot quote."
            ),
        },
    },
    "required": ["claims", "insufficient_evidence"],
}


def parse_answer(payload: dict[str, Any]) -> tuple[list[Claim], bool]:
    """Turn raw model output into `Claim` objects.

    Malformed entries are dropped rather than raising: a model that returns nine
    good claims and one with no citations should yield nine claims and a
    rejection record, not a failed request. `Claim` itself refuses to be
    constructed without a citation, so nothing uncited can slip past here.
    """
    insufficient = bool(payload.get("insufficient_evidence"))
    claims: list[Claim] = []

    for raw in payload.get("claims") or []:
        text = (raw.get("text") or "").strip()
        if not text:
            continue

        citations: list[Citation] = []
        for raw_citation in raw.get("citations") or []:
            chunk_id = raw_citation.get("chunk_id")
            quote = raw_citation.get("quote")
            if not chunk_id or not quote:
                continue
            try:
                parsed_id = UUID(str(chunk_id))
            except (ValueError, AttributeError, TypeError):
                # A hallucinated identifier that is not even a UUID. Logged and
                # dropped; the claim then fails for lack of support.
                log.warning("citation_chunk_id_unparseable", value=str(chunk_id)[:80])
                continue
            citations.append(
                Citation(
                    chunk_id=parsed_id,
                    # Placeholder: the validator overwrites this from the chunk
                    # it resolves, so the model cannot misattribute a quote to
                    # the wrong document.
                    document_id=parsed_id,
                    quote=str(quote),
                )
            )

        if citations:
            claims.append(Claim(text=text, citations=citations))
        else:
            log.warning("claim_without_citations_dropped", claim=text[:120])

    return claims, insufficient
