"""Loading, validating and resolving policy parameters.

Validation is the point of this module. A parameter is only usable once its
`source_quote` has been located verbatim in the document it claims to come from,
which converts three silent failure modes into loud ones:

* **Drift.** Someone edits the SOP so the free window becomes 60 minutes. The
  quote no longer matches, validation fails in CI, and nobody ships a rule that
  disagrees with the policy it cites.
* **Misattribution.** An override declared under the wrong account, or citing an
  agreement that belongs to a different customer. Cross-checked against
  `accounts.contract_file`.
* **Uncitable decisions.** Validation resolves each parameter to a real
  `chunk_id` and character span, so every verdict carries a pointer to the
  operative clause rather than to a document in general.

Resolution is a two-layer merge -- defaults, then the account's agreement -- and
records which layer won for each parameter, so an override is explainable rather
than merely applied.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from agentcore.db.engine import admin
from agentcore.errors import ConfigError, PolicyParameterMissing
from agentcore.logging import get_logger
from agentcore.settings import REPO_ROOT
from agentcore.trust.spans import find_all_spans, find_span
from agentcore.types import Citation, ConflictNote

log = get_logger(__name__)

DEFAULT_PACK_PATH = REPO_ROOT / "policy_pack.yaml"

#: Sentinel for the default layer, so `scope` is never None and log lines and
#: explanations read the same for defaults and overrides.
DEFAULT_SCOPE = "default"


@dataclass(frozen=True)
class Parameter:
    id: str
    value: Any
    unit: str
    scope: str  # "default" or an account_id
    source_document: str
    source_quote: str
    description: str | None = None
    #: Resolved by validate_pack(): where this clause actually lives.
    chunk_id: UUID | None = None
    document_id: UUID | None = None
    span_start: int | None = None
    span_end: int | None = None
    #: The matched text exactly as stored, line breaks and all.
    matched_text: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.chunk_id is not None

    def citation(self) -> Citation:
        """The clause this parameter came from, as a citation.

        Raises if unresolved: a decision must never cite a parameter whose
        provenance was never checked.
        """
        if not self.is_resolved:
            raise PolicyParameterMissing(
                f"parameter {self.id} has not been validated against the corpus; "
                "run `parcelpilot policy validate`",
                parameter=self.id,
            )
        assert self.chunk_id is not None and self.document_id is not None
        return Citation(
            chunk_id=self.chunk_id,
            document_id=self.document_id,
            quote=self.matched_text or self.source_quote,
            start=self.span_start,
            end=self.span_end,
        )


@dataclass(frozen=True)
class ResolvedPolicy:
    """The parameter set in force for one account, with provenance."""

    account_id: str | None
    parameters: dict[str, Parameter]
    #: Keyed by parameter id, not a flat list, so a decision can report only the
    #: overrides it actually relied on. A cancellation answer that volunteers
    #: "your credit threshold is also different" reads as a non-sequitur and
    #: erodes exactly the trust the citation is meant to build.
    override_notes: dict[str, ConflictNote] = field(default_factory=dict)

    @property
    def overrides(self) -> list[ConflictNote]:
        """Every override in force for this account."""
        return list(self.override_notes.values())

    def overrides_for(self, *prefixes: str) -> list[ConflictNote]:
        """Overrides on parameters whose id starts with any given prefix.

        Rules pass the families they read ("cancellation", "credit"), which
        keeps relevance a property of the decision rather than of the caller
        remembering to filter.
        """
        return [
            note
            for parameter_id, note in self.override_notes.items()
            if parameter_id.startswith(prefixes)
        ]

    def get(self, parameter_id: str) -> Parameter:
        try:
            return self.parameters[parameter_id]
        except KeyError as exc:
            # An INDETERMINATE verdict and an escalation, never a guessed number.
            raise PolicyParameterMissing(
                f"no policy parameter {parameter_id} is defined for "
                f"{self.account_id or 'the default policy'}",
                parameter=parameter_id,
                account_id=self.account_id,
            ) from exc

    def value(self, parameter_id: str) -> Any:
        return self.get(parameter_id).value

    def was_overridden(self, parameter_id: str) -> bool:
        return self.parameters[parameter_id].scope != DEFAULT_SCOPE


@dataclass
class ValidationReport:
    resolved: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "parameters_resolved": self.resolved,
            "failures": self.failures,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_pack(path: Path | None = None) -> list[Parameter]:
    """Parse policy_pack.yaml into a flat parameter list. No I/O beyond the file."""
    pack_path = path or DEFAULT_PACK_PATH
    if not pack_path.exists():
        raise ConfigError(f"policy pack not found: {pack_path}")

    try:
        raw = yaml.safe_load(pack_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"policy pack is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"policy pack must contain a mapping: {pack_path}")

    parameters: list[Parameter] = []

    def collect(section: dict[str, Any], scope: str) -> None:
        section_document = section.get("source_document")
        if not section_document:
            raise ConfigError(f"policy pack scope {scope} has no source_document")
        for entry in section.get("parameters", []):
            missing = {"id", "value", "source_quote"} - set(entry)
            if missing:
                raise ConfigError(
                    f"policy pack scope {scope}: parameter is missing {sorted(missing)}"
                )
            parameters.append(
                Parameter(
                    id=entry["id"],
                    value=entry["value"],
                    unit=entry.get("unit", "unspecified"),
                    scope=scope,
                    # A parameter may name its own document, falling back to the
                    # section's. Needed because one scope legitimately draws on
                    # several sources: the default cancellation rules come from
                    # the SOP while the default SLA targets come from the support
                    # policy. Section-level only would have forced either a
                    # duplicated scope or a wrong citation -- and validation
                    # caught exactly that when the SLA params were first added.
                    source_document=entry.get("source_document", section_document),
                    source_quote=" ".join(str(entry["source_quote"]).split()),
                    description=entry.get("description"),
                )
            )

    default_section = raw.get("default")
    if not default_section:
        raise ConfigError("policy pack must define a `default` section")
    collect(default_section, DEFAULT_SCOPE)

    for account_id, section in (raw.get("accounts") or {}).items():
        collect(section, account_id)

    # A scope declaring the same parameter twice is ambiguous, and which one
    # wins would depend on dict ordering.
    for scope in {p.scope for p in parameters}:
        ids = [p.id for p in parameters if p.scope == scope]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ConfigError(
                f"policy pack scope {scope} declares {sorted(duplicates)} more than once"
            )

    return parameters


# ---------------------------------------------------------------------------
# Validation against the live corpus
# ---------------------------------------------------------------------------


def validate_pack(
    tenant_id: str, path: Path | None = None
) -> tuple[list[Parameter], ValidationReport]:
    """Locate every parameter's clause in the active index.

    Runs as the owner: it must see every account's contract at once, which no
    request-path principal is allowed to do.
    """
    declared = load_pack(path)
    report = ValidationReport()
    resolved: list[Parameter] = []

    with admin() as conn:
        chunks = conn.execute(
            """
            SELECT c.chunk_id, c.document_id, c.text, c.section_path,
                   d.filename, d.owner_account_id, d.eligibility
            FROM chunks c
            JOIN documents d ON d.document_id = c.document_id
            JOIN index_versions iv ON iv.index_version_id = c.index_version_id
            WHERE c.tenant_id = %s AND iv.status = 'active'
            ORDER BY d.filename, c.ordinal
            """,
            (tenant_id,),
        ).fetchall()

        contract_owners = {
            row["contract_file"]: row["account_id"]
            for row in conn.execute(
                """
                SELECT account_id, contract_file FROM accounts
                WHERE tenant_id = %s AND contract_file IS NOT NULL
                """,
                (tenant_id,),
            ).fetchall()
        }

    if not chunks:
        raise ConfigError(
            "no active index found; run `parcelpilot ingest run` before validating"
        )

    by_document: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        by_document.setdefault(chunk["filename"], []).append(chunk)

    for parameter in declared:
        candidates = by_document.get(parameter.source_document)
        if not candidates:
            report.failures.append(
                {
                    "parameter": parameter.id,
                    "scope": parameter.scope,
                    "error": f"document {parameter.source_document} is not in the active index",
                }
            )
            continue

        # An override must cite the agreement that actually governs the account
        # it is declared under. Otherwise a clause from customer A could set a
        # parameter for customer B.
        if parameter.scope != DEFAULT_SCOPE:
            attributed = contract_owners.get(parameter.source_document)
            if attributed != parameter.scope:
                report.failures.append(
                    {
                        "parameter": parameter.id,
                        "scope": parameter.scope,
                        "error": (
                            f"{parameter.source_document} is attributed to "
                            f"{attributed or 'no account'}, not to {parameter.scope}"
                        ),
                    }
                )
                continue

        match = None
        for chunk in candidates:
            span = find_span(chunk["text"], parameter.source_quote)
            if span is not None:
                match = (chunk, span)
                break

        if match is None:
            # The high-value failure: the pack and the corpus disagree.
            report.failures.append(
                {
                    "parameter": parameter.id,
                    "scope": parameter.scope,
                    "document": parameter.source_document,
                    "error": "source_quote does not appear in the document",
                    "quote": parameter.source_quote[:160],
                }
            )
            continue

        chunk, span = match

        if chunk["eligibility"] != "groundable":
            report.failures.append(
                {
                    "parameter": parameter.id,
                    "scope": parameter.scope,
                    "error": (
                        f"clause lives in a {chunk['eligibility']} source; a parameter "
                        "cannot be grounded in a document that may not ground claims"
                    ),
                }
            )
            continue

        occurrences = find_all_spans(chunk["text"], parameter.source_quote)
        if len(occurrences) > 1:
            report.warnings.append(
                {
                    "parameter": parameter.id,
                    "warning": (
                        f"quote appears {len(occurrences)} times in the chunk; the "
                        "citation cannot identify which occurrence was relied on"
                    ),
                }
            )

        resolved.append(
            Parameter(
                id=parameter.id,
                value=parameter.value,
                unit=parameter.unit,
                scope=parameter.scope,
                source_document=parameter.source_document,
                source_quote=parameter.source_quote,
                description=parameter.description,
                chunk_id=chunk["chunk_id"],
                document_id=chunk["document_id"],
                span_start=span.start,
                span_end=span.end,
                matched_text=span.text,
            )
        )
        report.resolved += 1

    log.info(
        "policy_pack_validated",
        resolved=report.resolved,
        failures=len(report.failures),
        warnings=len(report.warnings),
    )
    return resolved, report


@functools.lru_cache(maxsize=8)
def _validated_cache(tenant_id: str) -> tuple[Parameter, ...]:
    parameters, report = validate_pack(tenant_id)
    if not report.ok:
        # Refuse to serve decisions from an unvalidated pack. A rule that cannot
        # cite its clause must not be able to deny a customer a refund.
        raise ConfigError(
            "policy pack failed validation; refusing to make policy decisions",
            failures=report.failures,
        )
    return tuple(parameters)


def reset_cache() -> None:
    _validated_cache.cache_clear()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_for_account(
    tenant_id: str, account_id: str | None, *, parameters: list[Parameter] | None = None
) -> ResolvedPolicy:
    """Merge defaults with the account's agreement.

    The merge is recorded, not just performed: every displaced default becomes a
    `ConflictNote` carrying both documents, so the answer can say *why* the
    number is what it is.
    """
    pool = list(parameters) if parameters is not None else list(_validated_cache(tenant_id))

    merged: dict[str, Parameter] = {
        p.id: p for p in pool if p.scope == DEFAULT_SCOPE
    }
    override_notes: dict[str, ConflictNote] = {}

    if account_id:
        for parameter in pool:
            if parameter.scope != account_id:
                continue
            displaced = merged.get(parameter.id)
            merged[parameter.id] = parameter

            if displaced is not None and displaced.value != parameter.value:
                assert parameter.document_id is not None
                override_notes[parameter.id] = ConflictNote(
                    rule_id="contract_beats_policy",
                    winning_document_id=parameter.document_id,
                    losing_document_ids=(
                        [displaced.document_id] if displaced.document_id else []
                    ),
                    explanation=(
                        f"{parameter.id} is {parameter.value} under "
                        f"{parameter.source_document} for account {account_id}, "
                        f"which replaces the default of {displaced.value} in "
                        f"{displaced.source_document}."
                    ),
                )

    return ResolvedPolicy(
        account_id=account_id, parameters=merged, override_notes=override_notes
    )
