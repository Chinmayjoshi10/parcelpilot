"""Evaluation harness.

The point: turn "our answers are accurate" from an assertion into a number that
CI can fail on.

The split between offline and online cases is the design decision worth
defending. Offline cases -- policy verdicts, retrieval ranking, tenancy
isolation -- are deterministic, free and fast, so they run on every push. Online
cases need a live model, cost money and vary slightly between runs, so they run
before a release.

The consequence is that the checks which must NEVER regress (a tenancy leak, a
wrong fee) are the ones gating every commit, while the checks that are
inherently fuzzy (does the prose read well) are gated by a human deciding to run
them. A harness that required an API key to test tenancy would end up not being
run.

Scoring is deliberately strict. A tenancy failure fails the whole suite
regardless of the rest, because "97% of accounts stayed isolated" is not a
passing grade.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from agentcore.db import engine
from agentcore.db.engine import fetch_one
from agentcore.logging import get_logger
from agentcore.policy.pack import resolve_for_account
from agentcore.policy.rules import OrderFacts, cancellation_fee, failed_pickup_credit
from agentcore.retrieval.hybrid import retrieve
from agentcore.settings import EngineConfig, load_config
from agentcore.types import Principal, Query, Role, SourceClass

log = get_logger(__name__)

DEFAULT_CASES = Path(__file__).parent / "golden" / "cases.yaml"

#: Kinds whose failure is a security incident rather than a quality regression.
CRITICAL_KINDS = frozenset({"tenancy"})


@dataclass
class CaseResult:
    case_id: str
    kind: str
    mode: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    observed: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    skipped: bool = False
    skip_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "case_id": self.case_id,
            "kind": self.kind,
            "mode": self.mode,
            "status": "skip" if self.skipped else ("pass" if self.passed else "FAIL"),
            "duration_ms": self.duration_ms,
        }
        if self.failures:
            out["failures"] = self.failures
        if self.skip_reason:
            out["skip_reason"] = self.skip_reason
        if self.observed:
            out["observed"] = self.observed
        return out


@dataclass
class Report:
    results: list[CaseResult] = field(default_factory=list)
    started_at: str = ""
    duration_ms: int = 0

    @property
    def ran(self) -> list[CaseResult]:
        return [r for r in self.results if not r.skipped]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.ran if r.passed)

    @property
    def failed(self) -> list[CaseResult]:
        return [r for r in self.ran if not r.passed]

    @property
    def critical_failures(self) -> list[CaseResult]:
        return [r for r in self.failed if r.kind in CRITICAL_KINDS]

    @property
    def ok(self) -> bool:
        return not self.failed

    def by_kind(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for result in self.ran:
            bucket = out.setdefault(result.kind, {"pass": 0, "fail": 0})
            bucket["pass" if result.passed else "fail"] += 1
        return out

    def as_dict(self) -> dict[str, Any]:
        ran = self.ran
        return {
            "ok": self.ok,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "total": len(self.results),
            "ran": len(ran),
            "skipped": len(self.results) - len(ran),
            "passed": self.passed,
            "failed": len(self.failed),
            "pass_rate": round(self.passed / len(ran), 4) if ran else None,
            # Called out separately: a tenancy failure is not a percentage point.
            "critical_failures": [r.case_id for r in self.critical_failures],
            "by_kind": self.by_kind(),
            "results": [r.as_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_suite(
    *,
    cases_path: Path | None = None,
    offline_only: bool = False,
    only: str | None = None,
) -> Report:
    """Execute the golden set.

    `offline_only` is what CI uses: no model, no cost, no network variance.
    """
    path = cases_path or DEFAULT_CASES
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = load_config()

    as_of = datetime.fromisoformat(spec["as_of"]) if spec.get("as_of") else None
    started = time.monotonic()
    report = Report(started_at=datetime.now().astimezone().isoformat())

    for case in spec.get("cases", []):
        case_id = case["id"]
        if only and only not in case_id:
            continue

        mode = case.get("mode", "offline")
        kind = case.get("kind", "answer")

        if offline_only and mode == "online":
            report.results.append(
                CaseResult(
                    case_id=case_id,
                    kind=kind,
                    mode=mode,
                    passed=True,
                    skipped=True,
                    skip_reason="online case skipped (offline run)",
                )
            )
            continue

        case_started = time.monotonic()
        try:
            if kind == "policy":
                result = _run_policy_case(case, config, as_of)
            elif kind == "retrieval":
                result = await _run_retrieval_case(case, config)
            elif kind == "tenancy":
                result = await _run_tenancy_case(case, config, as_of)
            elif kind == "answer":
                result = await _run_answer_case(case, config, as_of)
            else:
                result = CaseResult(
                    case_id=case_id,
                    kind=kind,
                    mode=mode,
                    passed=False,
                    failures=[f"unknown case kind {kind!r}"],
                )
        except Exception as exc:  # noqa: BLE001 - a crash is a failed case
            result = CaseResult(
                case_id=case_id,
                kind=kind,
                mode=mode,
                passed=False,
                failures=[f"{type(exc).__name__}: {exc}"],
            )

        result.duration_ms = int((time.monotonic() - case_started) * 1000)
        report.results.append(result)
        log.info(
            "eval_case",
            case_id=case_id,
            kind=kind,
            status="pass" if result.passed else "FAIL",
            duration_ms=result.duration_ms,
        )

    report.duration_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "eval_complete",
        passed=report.passed,
        failed=len(report.failed),
        critical=len(report.critical_failures),
    )
    return report


def _principal(config: EngineConfig, account_id: str | None) -> Principal:
    if account_id:
        return Principal(
            tenant_id=config.tenant.id,
            user_id="eval",
            role=Role.CUSTOMER,
            account_id=account_id,
        )
    return Principal(
        tenant_id=config.tenant.id, user_id="eval", role=Role.OPERATIONS_ADMIN
    )


# ---------------------------------------------------------------------------
# Policy cases
# ---------------------------------------------------------------------------


def _run_policy_case(
    case: dict[str, Any], config: EngineConfig, as_of: datetime | None
) -> CaseResult:
    """Assert a deterministic verdict, with its amount and its citation.

    Reads the order through `admin()` rather than a scoped connection: these
    cases test the RULE, and tenancy is covered separately. Mixing the two would
    make a policy failure and an access failure indistinguishable.
    """
    expect = case["expect"]
    result = CaseResult(
        case_id=case["id"], kind="policy", mode=case.get("mode", "offline"), passed=True
    )

    with engine.admin() as conn:
        row = fetch_one(
            conn,
            "SELECT * FROM orders WHERE tenant_id = %s AND order_id = %s",
            (config.tenant.id, case["order_id"]),
        )
    if row is None:
        result.passed = False
        result.failures.append(f"order {case['order_id']} not found")
        return result

    order = OrderFacts.from_row(row)
    policy = resolve_for_account(config.tenant.id, case.get("account_id"))
    reference = as_of or datetime.now().astimezone()

    decision = (
        failed_pickup_credit(order, policy, now=reference)
        if case["rule"] == "failed_pickup_credit"
        else cancellation_fee(order, policy, now=reference)
    )

    result.observed = {
        "verdict": decision.verdict.value,
        "explanation": decision.explanation[:200],
        "inputs": {k: str(v) for k, v in decision.inputs.items()},
    }

    if decision.verdict.value != expect["verdict"]:
        result.passed = False
        result.failures.append(
            f"verdict: expected {expect['verdict']}, got {decision.verdict.value}"
        )

    if "cites_document" in expect:
        if decision.citation is None:
            result.passed = False
            result.failures.append("expected a citation, got none")
        else:
            with engine.admin() as conn:
                doc = fetch_one(
                    conn,
                    "SELECT filename FROM documents WHERE document_id = %s",
                    (decision.citation.document_id,),
                )
            actual = (doc or {}).get("filename")
            result.observed["cited_document"] = actual
            if actual != expect["cites_document"]:
                result.passed = False
                result.failures.append(
                    f"citation: expected {expect['cites_document']}, got {actual}"
                )

    for key, wanted in (expect.get("inputs") or {}).items():
        actual = decision.inputs.get(key)
        if actual is None:
            result.passed = False
            result.failures.append(f"inputs.{key}: missing")
            continue
        # Compared as strings: the pack holds ints, the rules produce Decimals
        # and floats, and this is about the value rather than the type.
        if str(actual) != str(wanted):
            result.passed = False
            result.failures.append(f"inputs.{key}: expected {wanted}, got {actual}")

    if "explanation_contains" in expect:
        needle = expect["explanation_contains"].lower()
        if needle not in decision.explanation.lower():
            result.passed = False
            result.failures.append(f"explanation missing {needle!r}")

    return result


# ---------------------------------------------------------------------------
# Retrieval cases
# ---------------------------------------------------------------------------


async def _run_retrieval_case(case: dict[str, Any], config: EngineConfig) -> CaseResult:
    """Assert what ranks first, and what is gated.

    Runs without an embedder so the case measures the LEXICAL half only. That
    keeps it deterministic and free -- and it is the half that regressed to zero
    recall once already.
    """
    expect = case["expect"]
    result = CaseResult(
        case_id=case["id"], kind="retrieval", mode=case.get("mode", "offline"), passed=True
    )
    who = _principal(config, case.get("account_id"))

    with engine.scoped(who, read_only=True) as conn:
        retrieved = await retrieve(
            conn, who, case["query"], config.retrieval, embedder=None
        )

    result.observed = {
        "groundable": [c.document.filename for c in retrieved.groundable],
        "conflict": [c.document.filename for c in retrieved.conflict],
        "top_section": (
            retrieved.groundable[0].chunk.section_path if retrieved.groundable else None
        ),
    }

    if not retrieved.groundable:
        result.passed = False
        result.failures.append("no groundable results")
        return result

    top = retrieved.groundable[0]
    if "top_document" in expect and top.document.filename != expect["top_document"]:
        result.passed = False
        result.failures.append(
            f"top document: expected {expect['top_document']}, got {top.document.filename}"
        )

    if "section_contains" in expect:
        section = (top.chunk.section_path or "").lower()
        if expect["section_contains"].lower() not in section:
            result.passed = False
            result.failures.append(
                f"top section {section!r} missing {expect['section_contains']!r}"
            )

    if "gated_document" in expect:
        gated = {c.document.filename for c in retrieved.conflict}
        groundable = {c.document.filename for c in retrieved.groundable}
        wanted = expect["gated_document"]
        if wanted in groundable:
            # The important half: it must not be citable.
            result.passed = False
            result.failures.append(f"{wanted} appeared as GROUNDABLE; it must be gated")
        elif wanted not in gated:
            result.passed = False
            result.failures.append(f"{wanted} was not retrieved into the conflict channel")

    if "min_groundable" in expect and len(retrieved.groundable) < expect["min_groundable"]:
        result.passed = False
        result.failures.append(
            f"groundable count {len(retrieved.groundable)} < {expect['min_groundable']}"
        )

    return result


# ---------------------------------------------------------------------------
# Tenancy cases
# ---------------------------------------------------------------------------


async def _run_tenancy_case(
    case: dict[str, Any], config: EngineConfig, as_of: datetime | None
) -> CaseResult:
    """Assert that a boundary held. Any failure fails the suite."""
    expect = case["expect"]
    result = CaseResult(
        case_id=case["id"], kind="tenancy", mode=case.get("mode", "offline"), passed=True
    )
    who = _principal(config, case.get("account_id"))

    if expect.get("record_invisible"):
        with engine.scoped(who, read_only=True) as conn:
            row = fetch_one(
                conn, "SELECT order_id FROM orders WHERE order_id = %s", (case["order_id"],)
            )
        result.observed = {"row_visible": row is not None}
        if row is not None:
            result.passed = False
            result.failures.append(
                f"{case['order_id']} was VISIBLE to {case.get('account_id')} -- data leak"
            )
        return result

    with engine.scoped(who, read_only=True) as conn:
        retrieved = await retrieve(
            conn, who, case["query"], config.retrieval, embedder=None
        )

    seen = {c.document.filename for c in retrieved.candidates}
    classes = {c.document.source_class for c in retrieved.candidates}
    result.observed = {"retrieved": sorted(seen)}

    if "must_not_retrieve" in expect and expect["must_not_retrieve"] in seen:
        result.passed = False
        result.failures.append(
            f"{expect['must_not_retrieve']} was retrieved for "
            f"{case.get('account_id')} -- contract leak"
        )

    if "must_not_retrieve_class" in expect:
        forbidden = SourceClass(expect["must_not_retrieve_class"])
        if forbidden in classes:
            leaked = sorted(
                c.document.filename
                for c in retrieved.candidates
                if c.document.source_class is forbidden
            )
            result.passed = False
            result.failures.append(f"retrieved {forbidden.value} documents: {leaked}")

    return result


# ---------------------------------------------------------------------------
# Answer cases (online)
# ---------------------------------------------------------------------------


async def _run_answer_case(
    case: dict[str, Any], config: EngineConfig, as_of: datetime | None
) -> CaseResult:
    """Run the full agent loop and score the answer.

    Scores refusals as first-class outcomes: for an out-of-corpus question a
    refusal is the CORRECT answer, and a harness that treated every refusal as a
    failure would pressure the system toward guessing.
    """
    from agentcore.llm.registry import get_embedder, get_llm
    from agentcore.orchestrator.engine import Orchestrator, summarise

    expect = case["expect"]
    result = CaseResult(
        case_id=case["id"], kind="answer", mode=case.get("mode", "online"), passed=True
    )
    who = _principal(config, case.get("account_id"))
    agent = Orchestrator(config, get_llm(), embedder=get_embedder())

    with engine.scoped(who) as conn:
        response = await agent.run(
            conn, who, Query(text=case["question"]), now=as_of
        )
    summary = summarise(response)

    # Map cited chunks back to filenames, so a case can assert the document
    # rather than an opaque chunk id.
    cited_documents: set[str] = set()
    with engine.admin() as conn:
        for citation in summary["citations"]:
            doc = fetch_one(
                conn,
                """
                SELECT d.filename FROM chunks c
                JOIN documents d ON d.document_id = c.document_id
                WHERE c.chunk_id = %s
                """,
                (citation["chunk_id"],),
            )
            if doc:
                cited_documents.add(doc["filename"])

    prose = summary["prose"]
    result.observed = {
        "refused": summary["refused"],
        "refusal_reason": summary["refusal_reason"],
        "claims": len(summary["claims"]),
        "cited_documents": sorted(cited_documents),
        "prose": prose[:300],
        "tokens": summary["tokens"],
    }

    if "refused" in expect and summary["refused"] != expect["refused"]:
        result.passed = False
        result.failures.append(
            f"refused: expected {expect['refused']}, got {summary['refused']} "
            f"({summary['refusal_reason']})"
        )
        # A refusal cannot satisfy content assertions, so stop here rather than
        # emitting a cascade of misleading failures.
        if summary["refused"]:
            return result

    for wanted in expect.get("cites_documents", []):
        if wanted not in cited_documents:
            result.passed = False
            result.failures.append(f"did not cite {wanted}")

    if "prose_contains_any" in expect:
        options = [str(o).lower() for o in expect["prose_contains_any"]]
        if not any(option in prose.lower() for option in options):
            result.passed = False
            result.failures.append(f"prose contains none of {options}")

    for forbidden in expect.get("prose_excludes", []):
        if str(forbidden).lower() in prose.lower():
            result.passed = False
            result.failures.append(f"prose contains forbidden {forbidden!r}")

    if "min_claims" in expect and len(summary["claims"]) < expect["min_claims"]:
        result.passed = False
        result.failures.append(
            f"claims {len(summary['claims'])} < {expect['min_claims']}"
        )

    # The security property, checked against the ledger rather than the prose.
    #
    # The injection case originally asserted `prose_excludes: "5000"` as a proxy
    # for "did not grant the injected credit". It is a bad proxy in both
    # directions. It failed a REBUTTAL -- "you are not automatically entitled to
    # a INR 5000 credit, that amount is a monthly aggregate cap" -- which is the
    # best possible answer and names the number in order to correct it. And it
    # would have passed an answer that quietly prepared a real credit while
    # phrasing the amount differently.
    #
    # What actually matters is that no state change was staged. That is a fact
    # about the action ledger, so ask the ledger.
    if expect.get("prepares_no_action") and summary.get("pending_action_id"):
        result.passed = False
        result.failures.append(
            f"prepared action {summary['pending_action_id']} -- "
            "an injection must never stage a state change"
        )

    # Substring exclusion is still useful, but only for text that is wrong in
    # every context: adopting an injected persona, for instance.
    for forbidden in expect.get("prose_excludes_always", []):
        if str(forbidden).lower() in prose.lower():
            result.passed = False
            result.failures.append(f"prose contains forbidden {forbidden!r}")

    return result


def run(offline_only: bool = False, only: str | None = None) -> Report:
    """Synchronous entry point for the CLI."""
    return asyncio.run(run_suite(offline_only=offline_only, only=only))
