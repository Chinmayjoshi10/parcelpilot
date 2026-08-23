"""Hybrid retrieval: lexical + dense, fused, with the eligibility gate applied.

Three decisions here are the substance of the module.

**Lexical is not the fallback.** On this corpus it is the stronger half. The
documents are numbered clauses full of exact tokens -- ORD-1001, "cancellation
fee", "INR 250", KI-208 -- and a question about a cancellation fee shares those
tokens literally. Dense retrieval adds recall for paraphrase ("can I back out of
a shipment"), so both run and their ranks are fused. With no embedder configured
the lexical half alone still answers well, which is why `EMBEDDING_BACKEND=none`
is a supported mode rather than a broken one.

**Fusion is rank-based (RRF), not score-based.** `ts_rank_cd` and cosine
similarity have unrelated scales; normalising them against each other means
inventing a conversion nobody can justify. Reciprocal rank fusion needs only the
ordering, so there is no fudge factor to tune or to explain to a reviewer.

**Eligibility is a gate, not a weight.** Retrieval returns three separate
channels -- groundable, conflict, context -- and they never mix. This is where the
v1 design failed: it multiplied authority into the relevance score, so a
strongly-matching DEPRECATED clause could still outrank a weaker CURRENT one and
end up cited. A filter cannot be outvoted by a good match.

Account isolation needs no code here at all. Every query runs on a `scoped()`
connection, so RLS restricts chunks to the caller's own contract plus global
sources. There is no `WHERE account_id` in this file, and there must not be.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from psycopg import Connection

from agentcore.db.engine import fetch_all
from agentcore.llm.base import Embedder
from agentcore.logging import get_logger
from agentcore.settings import RetrievalConfig
from agentcore.types import (
    Chunk,
    DocumentRef,
    Eligibility,
    Freshness,
    Principal,
    RetrievedChunk,
    SourceClass,
)

log = get_logger(__name__)


@dataclass
class RetrievalResult:
    """Three channels, deliberately kept apart.

    `groundable` is the only one an answer may cite. `conflict` exists so the
    engine can say "a superseded version says otherwise" -- naming the drift
    instead of silently ignoring it. `context` is narrative colour, including
    historical ticket resolutions that may be wrong.
    """

    groundable: list[RetrievedChunk] = field(default_factory=list)
    conflict: list[RetrievedChunk] = field(default_factory=list)
    context: list[RetrievedChunk] = field(default_factory=list)
    #: Everything considered, for the run log. A retrieval miss is invisible
    #: after the fact without this, and "why didn't it find the clause" is the
    #: most common real question about a wrong answer.
    candidates: list[RetrievedChunk] = field(default_factory=list)
    lexical_hits: int = 0
    dense_hits: int = 0
    dense_available: bool = False
    #: True when the dense half was deliberately not run because lexical was
    #: already strong. Reported so a thin answer is never mistaken for a
    #: full-fidelity search.
    dense_skipped: bool = False
    index_version_id: int | None = None

    @property
    def top_score(self) -> float:
        return max((c.fused_score for c in self.groundable), default=0.0)

    def as_log(self) -> dict[str, Any]:
        return {
            "groundable": len(self.groundable),
            "conflict": len(self.conflict),
            "context": len(self.context),
            "candidates": len(self.candidates),
            "lexical_hits": self.lexical_hits,
            "dense_hits": self.dense_hits,
            "dense_available": self.dense_available,
            "dense_skipped": self.dense_skipped,
            "top_score": round(self.top_score, 4),
        }


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

#: Columns every candidate carries, so both halves return the same shape and
#: fusion never has to special-case one of them.
_SELECT = """
    c.chunk_id, c.document_id, c.tenant_id, c.ordinal, c.page_from, c.page_to,
    c.section_path, c.text, c.eligibility AS chunk_eligibility,
    d.filename, d.title, d.source_class, d.authority, d.eligibility,
    d.freshness, d.owner_account_id, d.policy_family, d.version_label,
    d.effective_from, d.effective_to, d.content_sha256, d.page_count,
    c.index_version_id
"""

#: OR semantics, built from the query's own lexemes.
#:
#: This is the correction to the obvious implementation. `websearch_to_tsquery`
#: and `plainto_tsquery` both AND every term, which means a chunk must contain
#: *all* of them. Asked "Can I cancel a booked shipment without a cancellation
#: fee?", that returned NOTHING: the operative clause says "cancelled", "BOOKED"
#: and "cancellation fee" but never says "shipment", so the AND failed on one
#: incidental noun. Recall was near zero for exactly the questions users ask.
#:
#: So the query is tokenised with to_tsvector (which applies the same stemming
#: and stop-word removal as the indexed column, so the two always agree), its
#: lexemes extracted, and joined with `|`. Discrimination then comes from
#: ranking rather than from filtering -- which is what ranking is for.
#:
#: ts_rank_cd (cover density) rather than ts_rank: it rewards matched terms
#: appearing close together, so "cancellation" beside "fee" outranks a document
#: that merely mentions both somewhere. That proximity signal is what replaces
#: the discarded AND.
#:
#: numnode(...) > 0 guards a query of nothing but stop words, which yields an
#: empty tsquery that matches everything or nothing depending on operator.
_LEXICAL_SQL = f"""
    WITH q AS (
        SELECT to_tsquery(
                   'english',
                   array_to_string(
                       tsvector_to_array(to_tsvector('english', %(query)s)), ' | '
                   )
               ) AS tsq
    )
    SELECT {_SELECT},
           ts_rank_cd(c.tsv, q.tsq, 32) AS score
    FROM chunks c
    JOIN documents d ON d.document_id = c.document_id
    CROSS JOIN q
    WHERE c.index_version_id = %(index_version)s
      AND numnode(q.tsq) > 0
      AND c.tsv @@ q.tsq
    ORDER BY score DESC, c.chunk_id
    LIMIT %(limit)s
"""

#: Exact cosine over float4[]. Correct and fast at this corpus size; the
#: expression is the one place that changes if pgvector is ever installed
#: (`1 - (c.embedding <=> %(vector)s::vector)` plus an HNSW index).
#:
#: Vectors are unit-normalised at ingest, so the dot product IS the cosine --
#: which keeps this to a single pass with no per-row magnitude computation.
_DENSE_SQL = f"""
    WITH q AS (
        SELECT %(vector)s::real[] AS v
    )
    SELECT {_SELECT},
           (
               SELECT sum(a * b)
               FROM unnest(c.embedding, (SELECT v FROM q)) AS t(a, b)
           ) AS score
    FROM chunks c
    JOIN documents d ON d.document_id = c.document_id
    WHERE c.index_version_id = %(index_version)s
      AND c.embedding IS NOT NULL
    ORDER BY score DESC NULLS LAST, c.chunk_id
    LIMIT %(limit)s
"""


def _to_retrieved(row: dict[str, Any]) -> tuple[Chunk, DocumentRef]:
    chunk = Chunk(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        tenant_id=row["tenant_id"],
        ordinal=row["ordinal"],
        page_from=row["page_from"],
        page_to=row["page_to"],
        section_path=row["section_path"],
        text=row["text"],
    )
    document = DocumentRef(
        document_id=row["document_id"],
        tenant_id=row["tenant_id"],
        filename=row["filename"],
        title=row["title"],
        source_class=SourceClass(row["source_class"]),
        authority=row["authority"],
        eligibility=Eligibility(row["eligibility"]),
        freshness=Freshness(row["freshness"]),
        owner_account_id=row["owner_account_id"],
        policy_family=row["policy_family"],
        version_label=row["version_label"],
        effective_from=row["effective_from"],
        effective_to=row["effective_to"],
        content_sha256=row["content_sha256"],
        page_count=row["page_count"],
    )
    return chunk, document


def active_index_version(conn: Connection, tenant_id: str) -> int | None:
    """The version this query will be served from.

    Pinned once per retrieval and recorded on the run, so an answer stays
    reproducible after the next ingest replaces the active version.
    """
    rows = fetch_all(
        conn,
        """
        SELECT index_version_id FROM index_versions
        WHERE tenant_id = %s AND status = 'active'
        """,
        (tenant_id,),
    )
    return int(rows[0]["index_version_id"]) if rows else None


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------


def reciprocal_rank_fusion(
    lexical: list[dict[str, Any]],
    dense: list[dict[str, Any]],
    *,
    k: int = 60,
) -> dict[Any, dict[str, Any]]:
    """Combine two rankings by rank alone.

    score(d) = sum over rankers of 1 / (k + rank(d)).

    `k` damps the top of each list so one ranker's confident first place cannot
    dominate; 60 is the value from the original RRF paper and needs no tuning
    against this corpus. A chunk found by both halves is scored above one found
    by either, which is the whole point of running both.
    """
    fused: dict[Any, dict[str, Any]] = {}

    for rank, row in enumerate(lexical, start=1):
        entry = fused.setdefault(
            row["chunk_id"], {"row": row, "fused": 0.0, "lex": None, "dense": None}
        )
        entry["lex"] = (rank, float(row["score"] or 0.0))
        entry["fused"] += 1.0 / (k + rank)

    for rank, row in enumerate(dense, start=1):
        entry = fused.setdefault(
            row["chunk_id"], {"row": row, "fused": 0.0, "lex": None, "dense": None}
        )
        entry["dense"] = (rank, float(row["score"] or 0.0))
        entry["fused"] += 1.0 / (k + rank)

    return fused


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def retrieve(
    conn: Connection,
    principal: Principal,
    query: str,
    config: RetrievalConfig,
    *,
    embedder: Embedder | None = None,
    index_version_id: int | None = None,
) -> RetrievalResult:
    """Retrieve for one query on an already-scoped connection.

    The connection must come from `scoped()`. Account isolation is entirely the
    database's job here: this function contains no account predicate, and adding
    one would create a second, weaker place for tenancy to be enforced.
    """
    version = index_version_id or active_index_version(conn, principal.tenant_id)
    if version is None:
        log.warning("retrieval_without_active_index", tenant_id=principal.tenant_id)
        return RetrievalResult()

    result = RetrievalResult(index_version_id=version)

    # --- lexical ---
    lexical_rows: list[dict[str, Any]] = []
    if config.lexical.enabled and query.strip():
        lexical_rows = fetch_all(
            conn,
            _LEXICAL_SQL,
            {
                "query": query,
                "index_version": version,
                "limit": config.lexical.candidates,
            },
        )
        result.lexical_hits = len(lexical_rows)

    # --- dense ---
    #
    # Skipped when lexical already found plenty. Embedding the query is a ~1.9s
    # API round trip, against 26ms for the lexical half over this corpus -- so
    # paying it on every question to improve recall on the minority of
    # paraphrased ones is the wrong default. It still runs when lexical returns
    # little, which is precisely when paraphrase recall is what is missing.
    dense_rows: list[dict[str, Any]] = []
    lexical_is_weak = len(lexical_rows) <= config.lexical_weak_threshold
    skip_dense = (
        config.dense_only_when_lexical_weak
        and not lexical_is_weak
        and bool(lexical_rows)
    )
    # Capability, not activity. Conflating the two made a deliberate skip read
    # in the logs as a broken embedder -- `dense_available=False` on a healthy
    # system is exactly the signal that sends someone debugging credentials.
    result.dense_available = bool(config.dense.enabled and embedder is not None)

    if skip_dense:
        result.dense_skipped = True
        log.info(
            "dense_skipped",
            lexical_hits=len(lexical_rows),
            threshold=config.lexical_weak_threshold,
        )

    if config.dense.enabled and embedder is not None and query.strip() and not skip_dense:
        # is_query=True: retrieval is asymmetric, and a question embedded as
        # though it were a document measurably retrieves worse.
        vectors = await embedder.embed([query], is_query=True)
        if vectors:
            dense_rows = fetch_all(
                conn,
                _DENSE_SQL,
                {
                    "vector": vectors[0],
                    "index_version": version,
                    "limit": config.dense.candidates,
                },
            )
            result.dense_hits = len(dense_rows)

    if not lexical_rows and not dense_rows:
        log.info("retrieval_empty", query=query, index_version=version)
        return result

    fused = reciprocal_rank_fusion(lexical_rows, dense_rows, k=config.fusion.k)

    candidates: list[RetrievedChunk] = []
    for entry in fused.values():
        chunk, document = _to_retrieved(entry["row"])
        candidates.append(
            RetrievedChunk(
                chunk=chunk,
                document=document,
                lexical_rank=entry["lex"][0] if entry["lex"] else None,
                lexical_score=entry["lex"][1] if entry["lex"] else None,
                dense_rank=entry["dense"][0] if entry["dense"] else None,
                dense_score=entry["dense"][1] if entry["dense"] else None,
                fused_score=entry["fused"],
            )
        )

    candidates.sort(key=lambda c: (-c.fused_score, str(c.chunk.chunk_id)))
    result.candidates = candidates

    # --- the gate ---
    # Split by eligibility BEFORE truncating to final_k. Truncating first would
    # let a run of conflict_only chunks crowd the groundable ones out of the
    # window entirely, which is the failure a weight-based scheme produces.
    groundable = [c for c in candidates if c.eligibility is Eligibility.GROUNDABLE]
    conflict = [c for c in candidates if c.eligibility is Eligibility.CONFLICT_ONLY]
    context = [c for c in candidates if c.eligibility is Eligibility.CONTEXT_ONLY]

    selected = groundable[: config.final_k]
    result.groundable = [c.model_copy(update={"selected": True}) for c in selected]
    # Two of each is enough to name a conflict or add colour without displacing
    # the evidence that actually answers the question.
    result.conflict = conflict[:2]
    result.context = context[:2]

    log.info("retrieval_complete", query=query, **result.as_log())
    return result


def authority_order(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Re-order by authority, then relevance.

    Applied only at conflict-resolution time, over chunks that are already
    eligible -- never folded into the retrieval score. Authority decides between
    two sources that could both legitimately answer; it must not decide whether
    a source may answer at all.
    """
    return sorted(
        chunks,
        key=lambda c: (-c.document.authority, -c.fused_score, str(c.chunk.chunk_id)),
    )


def detect_superseded(result: RetrievalResult) -> list[tuple[RetrievedChunk, RetrievedChunk]]:
    """Pair a retrieved deprecated clause with the current version of its family.

    `policy_family` makes this a join rather than filename parsing: if v2 and v3
    both surface, the engine can state that v3 governs and cite both, instead of
    quietly dropping v2 and leaving the user wondering which they read.
    """
    pairs: list[tuple[RetrievedChunk, RetrievedChunk]] = []
    current_by_family = {
        c.document.policy_family: c
        for c in result.groundable
        if c.document.policy_family and c.document.freshness is Freshness.CURRENT
    }
    for stale in result.conflict:
        family = stale.document.policy_family
        if family and family in current_by_family:
            pairs.append((stale, current_by_family[family]))
    return pairs
