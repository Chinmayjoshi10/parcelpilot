"""Document classification: deciding what a source is allowed to do.

This is the most consequential step in ingestion. `eligibility` decided here is
what stops the deprecated v2 policy from ever grounding an answer, and
`owner_account_id` is what keeps one customer's contract out of another's
retrieval. A misclassification is a wrong answer or a data leak.

So classification does not guess. Three ordered strategies, most trustworthy
first:

1. **Declared metadata.** These documents state their own status in machine-
   readable lines: `Status: CURRENT`, `Status: DEPRECATED - DO NOT USE`,
   `Supersedes: Support Policy v2`, `Account: ACCT-001`. A document's own
   declaration beats any inference about it.
2. **Structural signals.** Title and section shape ("Agreement" + "Account:" =
   a contract; "SOP" = a procedure).
3. **Fail safe.** Anything still unresolved is `context_only` -- retrievable,
   never groundable. An unclassified document cannot silently acquire the
   authority to answer a policy question.

Contract ownership is then **cross-checked** against `accounts.contract_file`.
Two independent sources -- the PDF's own `Account:` line and the account
record's foreign key -- must agree, or ingestion fails. Getting this wrong in
one direction leaks a contract; in the other it silently drops the override
that makes the answer correct.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from agentcore.errors import IngestionError
from agentcore.ingestion.documents import ParsedDocument
from agentcore.logging import get_logger
from agentcore.settings import SourcesConfig
from agentcore.types import Eligibility, Freshness, SourceClass

log = get_logger(__name__)

_STATUS_RE = re.compile(r"^status:\s*(?P<value>.+)$", re.IGNORECASE | re.MULTILINE)
_EFFECTIVE_RE = re.compile(
    r"^(?:effective|updated):\s*(?P<value>.+)$", re.IGNORECASE | re.MULTILINE
)
_ACCOUNT_RE = re.compile(r"^account:\s*(?P<value>[A-Z]+-\d+)\s*$", re.IGNORECASE | re.MULTILINE)
_SUPERSEDES_RE = re.compile(r"^supersedes:\s*(?P<value>.+)$", re.IGNORECASE | re.MULTILINE)
_SUPERSEDED_BY_RE = re.compile(
    r"^superseded by:\s*(?P<value>.+)$", re.IGNORECASE | re.MULTILINE
)
_VERSION_RE = re.compile(r"\bv(?P<value>\d+)\b", re.IGNORECASE)
_TERM_RE = re.compile(
    r"^term:\s*(?P<start>.+?)\s+to\s+(?P<end>.+?)\s*$", re.IGNORECASE | re.MULTILINE
)


@dataclass(frozen=True)
class Classification:
    source_class: SourceClass
    authority: int
    eligibility: Eligibility
    freshness: Freshness
    owner_account_id: str | None
    policy_family: str | None
    version_label: str | None
    effective_from: datetime | None
    effective_to: datetime | None
    #: How each field was decided, persisted on the run for audit. When someone
    #: asks why v2 was ignored, this is the answer.
    rationale: dict[str, str]


def classify(
    doc: ParsedDocument,
    sources: SourcesConfig,
    *,
    contract_owners: dict[str, str] | None = None,
) -> Classification:
    """Classify one parsed document.

    `contract_owners` maps filename -> account_id, built from
    `accounts.contract_file`. Passing it enables the cross-check; omitting it
    (in unit tests) falls back to the document's own declaration alone.
    """
    text = doc.full_text
    head = text[:1500]  # Declarations live in the preamble.
    rationale: dict[str, str] = {}

    declared_status = _first(_STATUS_RE, head)
    declared_account = _first(_ACCOUNT_RE, head)
    supersedes = _first(_SUPERSEDES_RE, head)
    superseded_by = _first(_SUPERSEDED_BY_RE, head)

    # --- freshness -------------------------------------------------------
    # Evaluated most-disqualifying first: anything that says it is superseded
    # is deprecated, whatever else it also claims about itself.
    status_text = (declared_status or "").lower()
    if "deprecated" in status_text:
        freshness = Freshness.DEPRECATED
        rationale["freshness"] = f"declared: Status: {declared_status}"
    elif superseded_by:
        # Belt and braces: a document can be superseded without ever using the
        # word DEPRECATED, and being superseded is disqualifying on its own.
        freshness = Freshness.DEPRECATED
        rationale["freshness"] = f"declared superseded by: {superseded_by}"
    elif any(token in status_text for token in ("current", "active")):
        # Contracts say ACTIVE; policies say CURRENT. Both mean in force.
        freshness = Freshness.CURRENT
        rationale["freshness"] = f"declared: Status: {declared_status}"
    else:
        freshness = Freshness.UNKNOWN
        rationale["freshness"] = "no declared status found"

    # --- source class ----------------------------------------------------
    title = doc.title.lower()
    source_class, class_reason = _decide_class(
        title=title, text=text, declared_account=declared_account, freshness=freshness
    )
    rationale["source_class"] = class_reason

    # --- ownership, with cross-check -------------------------------------
    owner = None
    if source_class is SourceClass.CUSTOMER_AGREEMENT:
        registry_owner = (contract_owners or {}).get(doc.filename)

        if declared_account and registry_owner and declared_account != registry_owner:
            raise IngestionError(
                f"{doc.filename} declares Account: {declared_account} but "
                f"accounts.contract_file attributes it to {registry_owner}. "
                "Refusing to ingest a contract whose owner is ambiguous.",
                filename=doc.filename,
                declared=declared_account,
                registry=registry_owner,
            )

        owner = declared_account or registry_owner
        if not owner:
            # A contract with no identifiable owner would be visible to every
            # account in the tenant. Downgrade rather than over-share.
            log.warning(
                "contract_without_owner",
                filename=doc.filename,
                action="demoted to context_only",
            )
            source_class = SourceClass.TICKET_RESOLUTION
            rationale["ownership"] = "no owner could be established; demoted"
        else:
            rationale["ownership"] = (
                f"account {owner} (declared={declared_account}, registry={registry_owner})"
            )

    # --- policy family and version --------------------------------------
    family = _policy_family(title)
    version = _first(_VERSION_RE, doc.title) or _first(_VERSION_RE, head)
    if supersedes:
        rationale["supersedes"] = supersedes

    # --- effective window ------------------------------------------------
    effective_from = _parse_date(_first(_EFFECTIVE_RE, head))
    effective_to = None
    term = _TERM_RE.search(head)
    if term:
        effective_from = effective_from or _parse_date(term.group("start"))
        effective_to = _parse_date(term.group("end"))
        rationale["term"] = f"{term.group('start')} to {term.group('end')}"

    # --- authority and eligibility come from config, never from here -----
    # The trust model is reviewed and version-controlled in config.yaml. A
    # classifier that could also assign authority would be able to promote a
    # document past review.
    class_config = sources.get(source_class)

    result = Classification(
        source_class=source_class,
        authority=class_config.authority,
        eligibility=class_config.eligibility,
        freshness=freshness,
        owner_account_id=owner,
        policy_family=family,
        version_label=f"v{version}" if version else None,
        effective_from=effective_from,
        effective_to=effective_to,
        rationale=rationale,
    )

    log.info(
        "document_classified",
        filename=doc.filename,
        source_class=result.source_class.value,
        eligibility=result.eligibility.value,
        freshness=result.freshness.value,
        owner_account_id=result.owner_account_id,
        policy_family=result.policy_family,
        version=result.version_label,
    )
    return result


def _decide_class(
    *, title: str, text: str, declared_account: str | None, freshness: Freshness
) -> tuple[SourceClass, str]:
    lowered = text[:2000].lower()

    # A contract is identified by having a named account, not by the word
    # "agreement" -- which also appears inside general policies.
    if declared_account and ("agreement" in title or "agreement" in lowered[:400]):
        return SourceClass.CUSTOMER_AGREEMENT, f"declares Account: {declared_account}"

    if "sop" in title or "standard operating procedure" in title:
        return SourceClass.SOP_CURRENT, "title identifies a procedure"

    if "policy" in title:
        if freshness is Freshness.DEPRECATED:
            # The whole point: a deprecated policy gets a different class, and
            # therefore a different eligibility, from a current one.
            return SourceClass.POLICY_DEPRECATED, "policy with deprecated status"
        return SourceClass.POLICY_CURRENT, "policy with current status"

    if any(k in title for k in ("guide", "operations", "known issues", "documentation")):
        return SourceClass.PRODUCT_GUIDE, "title identifies product documentation"

    # Fail safe. Retrievable for context, never able to ground a claim.
    return SourceClass.TICKET_RESOLUTION, "unclassified; defaulted to context_only"


def _policy_family(title: str) -> str | None:
    """Group versions of the same document so conflicts are structural.

    Comparing "support_policy v2" against "support_policy v3" is a join on this
    column; without it, detecting the conflict means parsing filenames at query
    time and hoping the convention held.
    """
    for keyword, family in (
        ("support policy", "support_policy"),
        ("cancellation", "cancellation_and_credit"),
        ("service credit", "cancellation_and_credit"),
        ("operations guide", "product_operations"),
        ("known issues", "product_operations"),
    ):
        if keyword in title:
            return family
    return None


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    groups = match.groupdict()
    value = groups.get("value") if "value" in groups else match.group(1)
    return value.strip() if value else None


_DATE_FORMATS = ("%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%d/%m/%Y", "%B %d, %Y")


def _parse_date(raw: str | None) -> datetime | None:
    """Parse a human-written effective date.

    Unparseable dates return None rather than a guess. An `effective_from` that
    is wrong is worse than absent, because as-of-date resolution would then
    confidently apply the wrong version of a policy.
    """
    if not raw:
        return None
    cleaned = re.sub(r"\s+", " ", raw).strip().rstrip(".")
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return parsed
    log.warning("effective_date_unparsed", value=cleaned)
    return None


def as_of(when: date | None = None) -> datetime:
    """Reference instant for effective-window filtering."""
    return datetime.combine(when or date.today(), datetime.min.time())
