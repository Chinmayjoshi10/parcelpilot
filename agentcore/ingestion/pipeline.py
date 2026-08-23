"""The ingestion job.

Runs as the owner role, offline, and never from a request. Its contract with
the serving side is a single row: an `index_versions` record that flips from
`building` to `active` in one transaction. Until that flip, readers keep
serving the previous version; after it, they serve the new one. There is no
moment where a query sees a half-built index.

Ordering is load-bearing:

1. **Structured data first.** `accounts.contract_file` is what tells us which
   contract governs which account, so documents cannot be classified until the
   accounts exist.
2. **Documents second**, each classified and cross-checked against those
   accounts.
3. **Embeddings last**, and optional. A failure here leaves a lexical-only but
   completely usable index rather than no index at all.
4. **Activate**, atomically.

Per-document failures are contained: one unparseable PDF is reported and
skipped, never fatal. A failure of the *whole* run marks the version `failed`
and leaves the previous one active.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg import Connection

from agentcore.db.engine import admin
from agentcore.errors import IngestionError
from agentcore.ingestion.classify import classify
from agentcore.ingestion.documents import parse_pdf
from agentcore.ingestion.structured import (
    COLUMN_MAPPINGS,
    build_rows,
    load_workbook_rows,
    upsert_sql,
)
from agentcore.llm.base import Embedder
from agentcore.logging import get_logger
from agentcore.settings import EngineConfig

log = get_logger(__name__)


@dataclass
class IngestReport:
    index_version_id: int | None = None
    tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    documents_ingested: list[dict[str, Any]] = field(default_factory=list)
    documents_failed: list[dict[str, Any]] = field(default_factory=list)
    chunks: int = 0
    embedded: int = 0
    embedding_model: str | None = None
    activated: bool = False
    duration_ms: int = 0
    warnings: list[str] = field(default_factory=list)
    #: Sheets present in a workbook that no column mapping claims. Reported
    #: rather than silently skipped -- see _load_structured.
    unmapped_sheets: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index_version_id": self.index_version_id,
            "tables": self.tables,
            "documents_ingested": self.documents_ingested,
            "documents_failed": self.documents_failed,
            "chunks": self.chunks,
            "embedded": self.embedded,
            "embedding_model": self.embedding_model,
            "activated": self.activated,
            "duration_ms": self.duration_ms,
            "warnings": self.warnings,
            "unmapped_sheets": self.unmapped_sheets,
        }


async def ingest(
    config: EngineConfig,
    *,
    embedder: Embedder | None = None,
    activate: bool = True,
) -> IngestReport:
    started = time.monotonic()
    report = IngestReport()
    tenant_id = config.tenant.id

    with admin() as conn:
        _ensure_tenant(conn, config)

        version_id = _create_version(conn, tenant_id, embedder)
        report.index_version_id = version_id
        conn.commit()

        try:
            # 1. Structured data. Not versioned: it is operational state, and
            #    an order's status is a fact about now, not about an index.
            report.tables = _load_structured(conn, config, report)
            conn.commit()

            # 2. Documents, classified against the accounts just loaded.
            contract_owners = _contract_owners(conn, tenant_id)
            if not contract_owners:
                report.warnings.append(
                    "no accounts declare a contract_file; contract override "
                    "cannot be resolved by lookup"
                )

            _ingest_documents(conn, config, version_id, contract_owners, report)
            conn.commit()

            # 3. Embeddings. Optional by design.
            if embedder is not None:
                report.embedding_model = embedder.model
                report.embedded = await _embed_chunks(conn, version_id, embedder)
                conn.commit()
            else:
                report.warnings.append(
                    "no embedder configured; index is lexical-only "
                    "(Postgres full-text search)"
                )

            _finalise_counts(conn, version_id)

            # 4. Flip. One transaction: the old version is superseded and the
            #    new one activated together, so there is never a window with
            #    two active versions or none.
            if activate:
                if not report.documents_ingested:
                    raise IngestionError(
                        "refusing to activate an index version with no documents"
                    )
                _activate(conn, tenant_id, version_id)
                report.activated = True
            conn.commit()

        except Exception:
            conn.rollback()
            # Mark the attempt failed in its own transaction so the trail
            # survives, then re-raise. The previous version stays active.
            with admin() as fail_conn:
                fail_conn.execute(
                    "UPDATE index_versions SET status = 'failed' WHERE index_version_id = %s",
                    (version_id,),
                )
                fail_conn.commit()
            raise

    report.duration_ms = int((time.monotonic() - started) * 1000)
    log.info("ingest_complete", **{k: v for k, v in report.as_dict().items() if k != "tables"})
    return report


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _ensure_tenant(conn: Connection, config: EngineConfig) -> None:
    conn.execute(
        """
        INSERT INTO tenants (tenant_id, name, industry, timezone, currency)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id) DO UPDATE SET
            name = EXCLUDED.name, industry = EXCLUDED.industry,
            timezone = EXCLUDED.timezone, currency = EXCLUDED.currency
        """,
        (
            config.tenant.id,
            config.tenant.name,
            config.tenant.industry,
            config.tenant.timezone,
            config.tenant.currency,
        ),
    )


def _create_version(conn: Connection, tenant_id: str, embedder: Embedder | None) -> int:
    row = conn.execute(
        """
        INSERT INTO index_versions (tenant_id, status, embedding_model, embedding_dim)
        VALUES (%s, 'building', %s, %s)
        RETURNING index_version_id
        """,
        (
            tenant_id,
            embedder.model if embedder else None,
            embedder.dimension if embedder else None,
        ),
    ).fetchone()
    assert row is not None
    log.info("index_version_created", index_version_id=row["index_version_id"])
    return int(row["index_version_id"])


#: Sheets that are documentation rather than data. Skipping these is expected,
#: so they are not reported as unmapped.
_IGNORED_SHEETS = frozenset({"readme", "notes", "about", "changelog", "cover"})


def _load_structured(
    conn: Connection, config: EngineConfig, report: IngestReport | None = None
) -> dict[str, dict[str, Any]]:
    """Load every mapped sheet from every workbook in the structured directory.

    Unmapped sheets are REPORTED, not silently skipped.

    The earlier version simply `continue`d past any sheet no mapping claimed.
    That is the worst possible behaviour when onboarding a new customer's
    workbook: you get zero rows, no error, and an ingest that looks like it
    worked. The mapping is deliberately declared rather than inferred (the
    policy engine reads `orders.booked_at` by name, so a shape-shifting schema
    cannot support a deterministic rule) -- but the boundary has to be visible.
    """
    results: dict[str, dict[str, Any]] = {}
    workbooks = sorted(config.data.structured_dir.glob("*.xlsx"))
    if not workbooks:
        raise IngestionError(f"no .xlsx files found in {config.data.structured_dir}")

    declared = {m.sheet for m in COLUMN_MAPPINGS}
    unmapped: list[dict[str, Any]] = []
    missing: list[str] = []

    for workbook in workbooks:
        sheets = load_workbook_rows(workbook)

        for name, records in sheets.items():
            if name in declared or name.strip().lower() in _IGNORED_SHEETS:
                continue
            if not records:
                continue
            unmapped.append(
                {
                    "workbook": workbook.name,
                    "sheet": name,
                    "rows": len(records),
                    "columns": sorted(records[0].keys())[:20],
                }
            )

        for mapping in COLUMN_MAPPINGS:
            if mapping.sheet not in sheets:
                missing.append(f"{workbook.name}:{mapping.sheet}")

        # Mapping order matters: accounts before orders and tickets, which
        # carry foreign keys to it.
        for mapping in COLUMN_MAPPINGS:
            records = sheets.get(mapping.sheet)
            if records is None:
                continue

            rows, load_report = build_rows(
                mapping,
                records,
                tenant_id=config.tenant.id,
                timezone=config.tenant.timezone,
            )
            if rows:
                columns = sorted({c for row in rows for c in row})
                statement = upsert_sql(mapping, columns)
                with conn.cursor() as cur:
                    cur.executemany(
                        statement,
                        [{c: row.get(c) for c in columns} for row in rows],
                    )

            results[mapping.table] = {
                "rows_read": load_report.rows_read,
                "rows_written": load_report.rows_written,
                "skipped": load_report.skipped,
            }
            log.info(
                "table_loaded",
                table=mapping.table,
                rows=load_report.rows_written,
                skipped=len(load_report.skipped),
            )

    # Surfaced at WARNING, and on the report the CLI prints. An operator
    # onboarding a new workbook needs to see "these 4 sheets carried 12,000 rows
    # that nothing consumed" rather than discovering it when an answer is wrong.
    for entry in unmapped:
        log.warning(
            "sheet_not_mapped",
            workbook=entry["workbook"],
            sheet=entry["sheet"],
            rows=entry["rows"],
            columns=entry["columns"],
            hint="add a TableMapping in agentcore/ingestion/structured.py",
        )
    if missing:
        log.warning("mapped_sheet_absent", sheets=missing)

    if report is not None:
        report.unmapped_sheets = unmapped
        if unmapped:
            total = sum(e["rows"] for e in unmapped)
            report.warnings.append(
                f"{len(unmapped)} sheet(s) carrying {total} rows were not "
                f"ingested because no column mapping claims them: "
                + ", ".join(f"{e['workbook']}:{e['sheet']}" for e in unmapped)
            )
        if missing:
            report.warnings.append(
                "mapped sheets absent from the workbook: " + ", ".join(missing)
            )

    return results


def _contract_owners(conn: Connection, tenant_id: str) -> dict[str, str]:
    """filename -> account_id, from the accounts table.

    This mapping is why a contract override is a deterministic lookup: we know
    which agreement binds an account without asking retrieval to find it.
    """
    rows = conn.execute(
        """
        SELECT account_id, contract_file FROM accounts
        WHERE tenant_id = %s AND contract_file IS NOT NULL AND contract_file <> ''
        """,
        (tenant_id,),
    ).fetchall()
    return {r["contract_file"]: r["account_id"] for r in rows}


def _ingest_documents(
    conn: Connection,
    config: EngineConfig,
    version_id: int,
    contract_owners: dict[str, str],
    report: IngestReport,
) -> None:
    pdfs = sorted(config.data.documents_dir.glob("*.pdf"))
    if not pdfs:
        raise IngestionError(f"no PDFs found in {config.data.documents_dir}")

    for path in pdfs:
        try:
            _ingest_one(conn, config, version_id, contract_owners, path, report)
        except IngestionError as exc:
            # Contained: one bad document must not abort the corpus.
            conn.rollback()
            report.documents_failed.append({"filename": path.name, "error": exc.message})
            log.error("document_failed", filename=path.name, error=exc.message)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            report.documents_failed.append({"filename": path.name, "error": str(exc)})
            log.error("document_failed", filename=path.name, error=str(exc))


def _ingest_one(
    conn: Connection,
    config: EngineConfig,
    version_id: int,
    contract_owners: dict[str, str],
    path: Path,
    report: IngestReport,
) -> None:
    parsed = parse_pdf(path)
    classification = classify(parsed, config.sources, contract_owners=contract_owners)

    row = conn.execute(
        """
        INSERT INTO documents (
            tenant_id, index_version_id, filename, title, source_class, authority,
            eligibility, freshness, owner_account_id, policy_family, version_label,
            effective_from, effective_to, content_sha256, page_count
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING document_id
        """,
        (
            config.tenant.id,
            version_id,
            parsed.filename,
            parsed.title,
            classification.source_class.value,
            classification.authority,
            classification.eligibility.value,
            classification.freshness.value,
            classification.owner_account_id,
            classification.policy_family,
            classification.version_label,
            classification.effective_from,
            classification.effective_to,
            parsed.content_sha256,
            parsed.page_count,
        ),
    ).fetchone()
    assert row is not None
    document_id = row["document_id"]

    # owner_account_id and eligibility are denormalised onto chunks so the RLS
    # policy is a column comparison rather than a subquery on every retrieval.
    # Copied from the parent row in the same statement, so they cannot drift.
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO chunks (
                tenant_id, document_id, index_version_id, ordinal, page_from, page_to,
                section_path, text, owner_account_id, eligibility, token_estimate
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    config.tenant.id,
                    document_id,
                    version_id,
                    chunk.ordinal,
                    chunk.page_from,
                    chunk.page_to,
                    chunk.section_path,
                    chunk.text,
                    classification.owner_account_id,
                    classification.eligibility.value,
                    chunk.token_estimate,
                )
                for chunk in parsed.chunks
            ],
        )

    report.chunks += len(parsed.chunks)
    report.documents_ingested.append(
        {
            "filename": parsed.filename,
            "title": parsed.title,
            "source_class": classification.source_class.value,
            "eligibility": classification.eligibility.value,
            "freshness": classification.freshness.value,
            "owner_account_id": classification.owner_account_id,
            "policy_family": classification.policy_family,
            "version": classification.version_label,
            "chunks": len(parsed.chunks),
            "rationale": classification.rationale,
        }
    )


async def _embed_chunks(conn: Connection, version_id: int, embedder: Embedder) -> int:
    """Embed every chunk in this version, in batches.

    Chunks are fetched with their section heading prepended for embedding only:
    "2. Failed-pickup service credits" is real signal about what the passage is
    for, and it is not stored back into `text` (which must stay byte-identical
    to what citations quote).
    """
    rows = conn.execute(
        """
        SELECT chunk_id, section_path, text FROM chunks
        WHERE index_version_id = %s AND embedding IS NULL
        ORDER BY document_id, ordinal
        """,
        (version_id,),
    ).fetchall()
    if not rows:
        return 0

    payloads = [
        f"{r['section_path']}\n{r['text']}" if r["section_path"] else r["text"] for r in rows
    ]
    vectors = await embedder.embed(payloads, is_query=False)

    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE chunks SET embedding = %s WHERE chunk_id = %s",
            [(vector, row["chunk_id"]) for vector, row in zip(vectors, rows, strict=True)],
        )
    return len(vectors)


def _finalise_counts(conn: Connection, version_id: int) -> None:
    conn.execute(
        """
        UPDATE index_versions SET
            document_count = (SELECT count(*) FROM documents WHERE index_version_id = %(v)s),
            chunk_count    = (SELECT count(*) FROM chunks WHERE index_version_id = %(v)s),
            embedded_count = (SELECT count(*) FROM chunks
                              WHERE index_version_id = %(v)s AND embedding IS NOT NULL)
        WHERE index_version_id = %(v)s
        """,
        {"v": version_id},
    )


def _activate(conn: Connection, tenant_id: str, version_id: int) -> None:
    """Supersede the old version and activate the new one, together.

    A unique partial index enforces at most one active version per tenant, so
    if this ordering were ever wrong the database would reject it rather than
    leave two live indexes.
    """
    conn.execute(
        """
        UPDATE index_versions SET status = 'superseded'
        WHERE tenant_id = %s AND status = 'active' AND index_version_id <> %s
        """,
        (tenant_id, version_id),
    )
    conn.execute(
        """
        UPDATE index_versions SET status = 'active', activated_at = now()
        WHERE index_version_id = %s
        """,
        (version_id,),
    )
    log.info("index_version_activated", index_version_id=version_id)


def rollback_to(tenant_id: str, index_version_id: int) -> dict[str, Any]:
    """Point the tenant back at an earlier version.

    A bad ingest is recovered by moving a pointer, not by re-parsing anything:
    the previous version's rows were never deleted.
    """
    with admin() as conn:
        target = conn.execute(
            """
            SELECT status, document_count, chunk_count FROM index_versions
            WHERE tenant_id = %s AND index_version_id = %s
            """,
            (tenant_id, index_version_id),
        ).fetchone()
        if target is None:
            raise IngestionError(
                f"index version {index_version_id} does not exist for tenant {tenant_id}"
            )
        if target["status"] == "failed":
            raise IngestionError(
                f"index version {index_version_id} is marked failed and is not serviceable"
            )
        _activate(conn, tenant_id, index_version_id)
        conn.commit()
    return {"tenant_id": tenant_id, "active_index_version": index_version_id, **target}
