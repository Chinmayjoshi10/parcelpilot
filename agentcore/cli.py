"""Operator entry point: `parcelpilot <group> <command>`.

Ingestion is a command here rather than a startup hook in the web app. That
separation is deliberate and is what makes the server horizontally scalable: a
process that serves requests never mutates the index, so N replicas cannot
race each other, and readiness means "an index is pinned and loadable" rather
than "wait while I parse PDFs".
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from agentcore.errors import ParcelPilotError
from agentcore.logging import configure_logging, get_logger
from agentcore.settings import get_settings, load_config

log = get_logger(__name__)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------


def _cmd_db_bootstrap(_args: argparse.Namespace) -> int:
    from agentcore.db.migrate import bootstrap

    _emit(bootstrap())
    return 0


def _cmd_db_migrate(args: argparse.Namespace) -> int:
    from agentcore.db.migrate import migrate

    applied = migrate(dry_run=args.dry_run)
    _emit({"dry_run": args.dry_run, "versions": applied})
    return 0


def _cmd_db_status(_args: argparse.Namespace) -> int:
    from agentcore.db.migrate import status

    state = status()
    _emit(state)
    # Drift is an operational emergency, not information: the running schema
    # does not match the reviewed one, so exit non-zero for CI.
    return 1 if state["drifted"] else 0


def _cmd_db_health(_args: argparse.Namespace) -> int:
    from agentcore.db.engine import healthcheck

    state = healthcheck()
    _emit(state)
    return 0 if state["ready"] else 1


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


def _cmd_ingest_run(args: argparse.Namespace) -> int:
    import asyncio

    from agentcore.ingestion.pipeline import ingest
    from agentcore.llm.registry import get_embedder

    config = load_config()
    embedder = None if args.no_embeddings else get_embedder()
    report = asyncio.run(ingest(config, embedder=embedder, activate=not args.no_activate))
    _emit(report.as_dict())
    # Any document that failed to ingest is a hole in the corpus, so the corpus
    # is not silently declared good.
    return 1 if report.documents_failed else 0


def _cmd_ingest_versions(_args: argparse.Namespace) -> int:
    from agentcore.db.engine import admin

    config = load_config()
    with admin() as conn:
        rows = conn.execute(
            """
            SELECT index_version_id, status, document_count, chunk_count,
                   embedded_count, embedding_model, created_at, activated_at
            FROM index_versions WHERE tenant_id = %s
            ORDER BY index_version_id DESC LIMIT 20
            """,
            (config.tenant.id,),
        ).fetchall()
    _emit(rows)
    return 0


def _cmd_ingest_rollback(args: argparse.Namespace) -> int:
    from agentcore.ingestion.pipeline import rollback_to

    config = load_config()
    _emit(rollback_to(config.tenant.id, args.version))
    return 0


# ---------------------------------------------------------------------------
# eval
# ---------------------------------------------------------------------------


def _cmd_eval_run(args: argparse.Namespace) -> int:
    """Run the golden set.

    Exit codes are chosen for CI: 2 for a tenancy failure (a security incident),
    1 for any other failure, 0 for a clean run. A pipeline can therefore treat
    isolation regressions differently from quality regressions.
    """
    from eval.harness import run

    report = run(offline_only=args.offline, only=args.only)
    if args.json:
        _emit(report.as_dict())
    else:
        _print_eval(report)
    if report.critical_failures:
        return 2
    return 0 if report.ok else 1


def _print_eval(report) -> None:
    print()
    for result in report.results:
        if result.skipped:
            continue
        mark = "PASS" if result.passed else "FAIL"
        print(f"  {mark}  {result.kind:<10} {result.case_id:<42} {result.duration_ms:>6}ms")
        for failure in result.failures:
            print(f"          -> {failure}")
    skipped = [r for r in report.results if r.skipped]
    if skipped:
        print()
        print(f"  {len(skipped)} online case(s) skipped")
    print()
    for kind, counts in sorted(report.by_kind().items()):
        print(f"  {kind:<12} {counts['pass']} passed, {counts['fail']} failed")
    print()
    rate = report.as_dict()["pass_rate"]
    print(f"  {report.passed}/{len(report.ran)} passed"
          + (f" ({rate:.0%})" if rate is not None else ""))
    if report.critical_failures:
        print(f"  CRITICAL: tenancy failures in {report.critical_failures}")
    print()


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------


def _cmd_ask(args: argparse.Namespace) -> int:
    """Run one question through the full agent loop.

    Exits non-zero on a refusal, so a demo script or CI check can tell the
    difference between "answered" and "declined to answer".
    """
    import asyncio
    from datetime import datetime

    from agentcore.db import engine
    from agentcore.llm.registry import get_embedder, get_llm
    from agentcore.orchestrator.engine import Orchestrator, summarise
    from agentcore.types import Principal, Query, Role

    config = load_config()
    principal = Principal(
        tenant_id=config.tenant.id,
        user_id=args.user or "cli",
        role=Role(args.role),
        account_id=args.account,
    )
    orchestrator = Orchestrator(config, get_llm(), embedder=get_embedder())
    # The dataset is a snapshot; --as-of reproduces decisions at that instant.
    now = datetime.fromisoformat(args.as_of) if args.as_of else None

    async def run():
        with engine.scoped(principal) as conn:
            return await orchestrator.run(
                conn, principal, Query(text=args.question), now=now
            )

    response = asyncio.run(run())
    summary = summarise(response)
    if args.json:
        _emit(summary)
    else:
        _print_answer(summary)
    return 1 if summary["refused"] else 0


def _print_answer(summary: dict[str, Any]) -> None:
    """Human-readable transcript: reasoning trace, answer, citations."""
    print()
    for step in summary["steps"]:
        print(f"  {step['kind']:<12} {step['label']:<34} {step['ms']:>5}ms")
    print()
    if summary["refused"]:
        print(f"  REFUSED ({summary['refusal_reason']})")
        print()
        return

    print("  ANSWER")
    print(f"  {summary['prose']}")
    print()
    for citation in summary["citations"]:
        quote = " ".join(citation["quote"].split())
        print(f"  [{citation['n']}] \"{quote[:160]}\"")
    if summary["conflicts"]:
        print()
        print("  CONFLICTS RESOLVED")
        for conflict in summary["conflicts"]:
            print(f"  - {conflict}")
    print()
    print(f"  tokens={summary['tokens']} index_version={summary['index_version']}")
    print()


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def _cmd_policy_validate(_args: argparse.Namespace) -> int:
    """Check every policy parameter against the clause it cites.

    Non-zero on any failure, so CI blocks a pack that has drifted from the
    corpus. A rule that cannot cite its clause must not be able to deny a
    customer a refund.
    """
    from agentcore.policy.pack import validate_pack

    config = load_config()
    _parameters, report = validate_pack(config.tenant.id)
    _emit(report.as_dict())
    return 0 if report.ok else 1


def _cmd_policy_show(args: argparse.Namespace) -> int:
    """Print the parameters in force for an account, with provenance."""
    from agentcore.policy.pack import resolve_for_account

    config = load_config()
    resolved = resolve_for_account(config.tenant.id, args.account)
    _emit(
        {
            "account_id": resolved.account_id,
            "parameters": {
                pid: {
                    "value": p.value,
                    "unit": p.unit,
                    "from": p.scope,
                    "document": p.source_document,
                    "quote": p.source_quote,
                }
                for pid, p in sorted(resolved.parameters.items())
            },
            "overrides": [
                {"rule": o.rule_id, "explanation": o.explanation}
                for o in resolved.overrides
            ],
        }
    )
    return 0


def _cmd_policy_decide(args: argparse.Namespace) -> int:
    """Run a rule against a real order and print the decision with its clause."""
    from datetime import datetime

    from agentcore.db.engine import admin
    from agentcore.policy.pack import resolve_for_account
    from agentcore.policy.rules import (
        OrderFacts,
        cancellation_fee,
        failed_pickup_credit,
    )

    config = load_config()
    with admin() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE tenant_id = %s AND order_id = %s",
            (config.tenant.id, args.order),
        ).fetchone()
    if row is None:
        _emit({"error": f"order {args.order} not found"})
        return 1

    order = OrderFacts.from_row(row)
    resolved = resolve_for_account(config.tenant.id, order.account_id)

    # The dataset is a snapshot, so `--as-of` lets the demo reproduce decisions
    # at the snapshot instant rather than at whatever time it is run.
    now = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now().astimezone()

    if args.rule == "cancellation_fee":
        decision = cancellation_fee(order, resolved, now=now)
    else:
        decision = failed_pickup_credit(order, resolved, now=now)

    _emit(
        {
            "rule": decision.rule_id,
            "verdict": decision.verdict.value,
            "explanation": decision.explanation,
            "inputs": decision.inputs,
            "citation": (
                {
                    "document_id": str(decision.citation.document_id),
                    "chunk_id": str(decision.citation.chunk_id),
                    "span": [decision.citation.start, decision.citation.end],
                    "quote": decision.citation.quote,
                }
                if decision.citation
                else None
            ),
            "conflicts": [
                {"rule": c.rule_id, "explanation": c.explanation}
                for c in decision.conflicts
            ],
        }
    )
    return 0


# ---------------------------------------------------------------------------
# llm
# ---------------------------------------------------------------------------


def _cmd_llm_probe(_args: argparse.Namespace) -> int:
    """Verify the provider against the live API.

    Exits non-zero if the key is missing, a configured model is unreachable, or
    the embedding dimension does not match EMBEDDING_DIM -- each of which would
    otherwise surface as a confusing failure much later.
    """
    import asyncio

    from agentcore.llm.registry import probe

    result = asyncio.run(probe())
    _emit(result)

    # `credentials_present`, not `key_present`: Vertex with a service account has
    # no API key by design, so gating on the key made a healthy setup exit 1 --
    # which would fail a deploy gate on a working system.
    if not result.get("credentials_present") or result.get("error"):
        return 1
    configured = result.get("configured", {})
    if not all(
        configured.get(k, True)
        for k in ("routing_available", "synthesis_available", "embedding_available")
    ):
        return 1
    embeddings = result.get("embeddings", {})
    if embeddings and not embeddings.get("matches", True):
        return 1
    return 0 if result.get("structured_output", {}).get("ok", True) else 1


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def _cmd_config_show(_args: argparse.Namespace) -> int:
    """Print the effective configuration.

    'Effective' matters: env ceilings clamp the values in config.yaml, so what
    is running is not always what the file says.
    """
    cfg = load_config()
    settings = get_settings()
    _emit(
        {
            "tenant": cfg.tenant.model_dump(),
            "retrieval": cfg.retrieval.model_dump(),
            "agent": cfg.agent.model_dump(),
            "groundable_sources": sorted(c.value for c in cfg.sources.groundable),
            "tools_enabled": [t.name for t in cfg.tools if t.enabled],
            "runtime": {
                "llm_provider": settings.llm_provider,
                "llm_key_present": bool(settings.llm_api_key),
                "embedding_backend": settings.embedding_backend,
                "embedding_model": settings.embedding_model,
                "log_format": settings.log_format,
            },
        }
    )
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parcelpilot", description=__doc__)
    parser.add_argument("--log-level", default=None)
    subparsers = parser.add_subparsers(dest="group", required=True)

    db = subparsers.add_parser("db", help="database bootstrap and migrations")
    db_sub = db.add_subparsers(dest="command", required=True)
    bootstrap_parser = db_sub.add_parser(
        "bootstrap", help="create the database and roles (needs a superuser)"
    )
    bootstrap_parser.set_defaults(func=_cmd_db_bootstrap)
    migrate_parser = db_sub.add_parser("migrate", help="apply pending migrations")
    migrate_parser.add_argument("--dry-run", action="store_true")
    migrate_parser.set_defaults(func=_cmd_db_migrate)
    db_sub.add_parser("status", help="show applied, pending and drifted migrations").set_defaults(
        func=_cmd_db_status
    )
    db_sub.add_parser("health", help="readiness: schema revision and active index").set_defaults(
        func=_cmd_db_health
    )

    ingest_parser = subparsers.add_parser("ingest", help="build the search index")
    ingest_sub = ingest_parser.add_subparsers(dest="command", required=True)
    run_parser = ingest_sub.add_parser("run", help="parse, load, embed and activate")
    run_parser.add_argument(
        "--no-embeddings", action="store_true", help="lexical-only index"
    )
    run_parser.add_argument(
        "--no-activate", action="store_true", help="build but leave the current version live"
    )
    run_parser.set_defaults(func=_cmd_ingest_run)
    ingest_sub.add_parser("versions", help="list index versions").set_defaults(
        func=_cmd_ingest_versions
    )
    rollback_parser = ingest_sub.add_parser("rollback", help="activate an earlier version")
    rollback_parser.add_argument("version", type=int)
    rollback_parser.set_defaults(func=_cmd_ingest_rollback)

    eval_parser = subparsers.add_parser("eval", help="run the golden evaluation set")
    eval_sub = eval_parser.add_subparsers(dest="command", required=True)
    eval_run = eval_sub.add_parser("run", help="execute the golden set")
    eval_run.add_argument(
        "--offline",
        action="store_true",
        help="deterministic cases only: no model, no cost. Use this in CI.",
    )
    eval_run.add_argument("--only", default=None, help="substring filter on case id")
    eval_run.add_argument("--json", action="store_true")
    eval_run.set_defaults(func=_cmd_eval_run)

    ask_parser = subparsers.add_parser("ask", help="run a question through the agent")
    ask_parser.add_argument("question")
    ask_parser.add_argument("--account", default=None, help="e.g. ACCT-001")
    ask_parser.add_argument(
        "--role",
        default="customer",
        choices=["customer", "support_agent", "operations_admin"],
    )
    ask_parser.add_argument("--user", default=None)
    ask_parser.add_argument("--as-of", default=None, help="ISO timestamp")
    ask_parser.add_argument("--json", action="store_true")
    ask_parser.set_defaults(func=_cmd_ask)

    policy_parser = subparsers.add_parser("policy", help="deterministic policy engine")
    policy_sub = policy_parser.add_subparsers(dest="command", required=True)
    policy_sub.add_parser(
        "validate", help="verify every parameter against the clause it cites"
    ).set_defaults(func=_cmd_policy_validate)
    show_parser = policy_sub.add_parser("show", help="parameters in force for an account")
    show_parser.add_argument("--account", default=None)
    show_parser.set_defaults(func=_cmd_policy_show)
    decide_parser = policy_sub.add_parser("decide", help="run a rule against an order")
    decide_parser.add_argument("--order", required=True)
    decide_parser.add_argument(
        "--rule",
        default="cancellation_fee",
        choices=["cancellation_fee", "failed_pickup_credit"],
    )
    decide_parser.add_argument(
        "--as-of", default=None, help="ISO timestamp; the dataset is a snapshot"
    )
    decide_parser.set_defaults(func=_cmd_policy_decide)

    llm_parser = subparsers.add_parser("llm", help="inspect the model provider")
    llm_sub = llm_parser.add_subparsers(dest="command", required=True)
    llm_sub.add_parser("probe", help="verify key, models and embedding dimension").set_defaults(
        func=_cmd_llm_probe
    )

    config_parser = subparsers.add_parser("config", help="inspect configuration")
    config_sub = config_parser.add_subparsers(dest="command", required=True)
    config_sub.add_parser("show", help="print the effective configuration").set_defaults(
        func=_cmd_config_show
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(args.log_level or settings.log_level, settings.log_format)

    try:
        return int(args.func(args))
    except ParcelPilotError as exc:
        # Operator-facing, so the real message is printed rather than the
        # sanitised one a user would see.
        log.error("command_failed", error=exc.message, **exc.context)
        return 2


if __name__ == "__main__":
    sys.exit(main())
