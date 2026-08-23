"""Test fixtures.

Seeding happens through the owner connection, which is exempt from RLS -- the
same privilege ingestion uses. Every assertion about visibility then runs
through `scoped()` as the app role, because a tenancy test that connects as the
owner proves nothing: Postgres would not be applying the policies at all.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

from agentcore.db import engine
from agentcore.types import Principal, Role

# The corpus contains characters outside cp1252, the Windows console default.
# Without this, pytest raises UnicodeEncodeError while *rendering* a failed
# assertion -- hiding the real failure behind an encoding error, which cost a
# diagnostic detour once already.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

TENANT = "test_tenant"
OTHER_TENANT = "other_tenant"

ACCT_A = "ACCT-A"
ACCT_B = "ACCT-B"

#: Company names for the seeded accounts. Distinct, multi-word and sharing no
#: token, so a name-matching test cannot pass by accident.
_ACCOUNT_NAMES = {
    ACCT_A: "Harbourline Freight",
    ACCT_B: "Quillmark Retail",
}


@pytest.fixture(scope="session", autouse=True)
def _schema_present() -> None:
    """Skip the whole suite rather than fail confusingly if migrations are behind."""
    with engine.admin() as conn:
        row = conn.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()
    if not row or not row["v"]:
        pytest.skip("run `parcelpilot db migrate` before the test suite")


@pytest.fixture(autouse=True)
def seed() -> Iterator[None]:
    """Two tenants, two accounts in one of them, and an order + ticket each.

    The second tenant exists solely so cross-tenant leakage has something to
    leak. A single-tenant fixture cannot fail the test that matters most.
    """
    with engine.admin() as conn:
        _purge(conn)
        conn.execute(
            "INSERT INTO tenants (tenant_id, name, timezone, currency) VALUES "
            "(%s, 'Test', 'Asia/Kolkata', 'INR'), (%s, 'Other', 'UTC', 'USD')",
            (TENANT, OTHER_TENANT),
        )
        for tenant, account in ((TENANT, ACCT_A), (TENANT, ACCT_B), (OTHER_TENANT, ACCT_A)):
            conn.execute(
                """
                INSERT INTO accounts (tenant_id, account_id, account_name, plan, status)
                VALUES (%s, %s, %s, 'Enterprise', 'active')
                """,
                # A real-looking company name, not "tenant:account". The old
                # form contained a colon, so it could not exercise the
                # word-boundary matching in `app_names_foreign_account` -- the
                # test for a sibling company name had nothing to match on.
                (tenant, account, _ACCOUNT_NAMES.get(account, f"{tenant} {account}")),
            )
            conn.execute(
                """
                INSERT INTO orders (tenant_id, order_id, account_id, carrier, status,
                                    booked_at, shipment_fee, currency)
                VALUES (%s, %s, %s, 'SwiftShip', 'BOOKED', now(), 1000, 'INR')
                """,
                (tenant, f"ORD-{tenant}-{account}", account),
            )
            conn.execute(
                """
                INSERT INTO tickets (tenant_id, ticket_id, account_id, created_at,
                                     status, subject, description)
                VALUES (%s, %s, %s, now(), 'open', 'subj', 'desc')
                """,
                (tenant, f"TKT-{tenant}-{account}", account),
            )
        conn.commit()

    yield

    with engine.admin() as conn:
        _purge(conn)
        conn.commit()


def _purge(conn) -> None:
    # Cascades from tenants clear accounts/orders/tickets/documents/runs.
    conn.execute("DELETE FROM tenants WHERE tenant_id IN (%s, %s)", (TENANT, OTHER_TENANT))
    conn.execute("DELETE FROM audit_log WHERE tenant_id IN (%s, %s)", (TENANT, OTHER_TENANT))


@pytest.fixture
def customer_a() -> Principal:
    return Principal(tenant_id=TENANT, user_id="u-a", role=Role.CUSTOMER, account_id=ACCT_A)


@pytest.fixture
def customer_b() -> Principal:
    return Principal(tenant_id=TENANT, user_id="u-b", role=Role.CUSTOMER, account_id=ACCT_B)


@pytest.fixture
def support() -> Principal:
    return Principal(tenant_id=TENANT, user_id="u-support", role=Role.SUPPORT_AGENT)


@pytest.fixture
def ops() -> Principal:
    return Principal(tenant_id=TENANT, user_id="u-ops", role=Role.OPERATIONS_ADMIN)


@pytest.fixture
def other_tenant_staff() -> Principal:
    return Principal(
        tenant_id=OTHER_TENANT, user_id="u-other", role=Role.OPERATIONS_ADMIN
    )


@pytest.fixture(scope="session", autouse=True)
def _close_pool() -> Iterator[None]:
    yield
    engine.close_pool()


# ---------------------------------------------------------------------------
# Protecting the real dataset from the tests that need it
# ---------------------------------------------------------------------------
#
# Most of this suite runs in `test_tenant`, isolated by construction. But
# `test_orchestrator.py` and `test_agent_actions.py` use `CONFIG.tenant.id` --
# the REAL tenant -- and they have to: they assert that an answer cites a real
# clause from the real corpus, which means they need the real documents, the
# real active index and the real orders.
#
# Reading that data is fine. Writing to it was not, and nothing stopped it.
# `test_escalation_is_prepared_but_not_executed` and its neighbours drive the
# action ledger end to end, which means a CONFIRMED action really does run --
# so one of them cancelled ORD-1001 for good:
#
#     executed  agent  by=ops@parcelpilot
#     payload: {"status": "CANCELLED", "order_id": "ORD-1001"}
#
# Nothing failed at the time. The damage surfaced later and elsewhere: the
# deterministic policy tests began returning INDETERMINATE ("status CANCELLED is
# not covered by the cancellation SOP"), and two eval cases went red -- in a
# rule engine that contains no model and had not been touched. Chasing a
# prompt change for a defect actually caused by leftover test state is exactly
# the kind of hour this fixture buys back.
#
# It is also the worse demo hazard: the flagship question is "Can I cancel
# ORD-1001 without a fee?" Run the suite before recording and it answers about
# an order that is already cancelled -- correctly, which is what makes it hard
# to notice.
#
# Rather than move these tests off the real corpus (which would cost them the
# thing they exist to prove), the mutable state is snapshotted before the
# session and restored after. Restore is by primary key and touches only the
# columns an action can change, so it cannot mask a schema problem the way a
# blanket re-ingest would.
_MUTABLE = {
    "orders": ("order_id", ("status", "cancellation_requested_at")),
    "tickets": ("ticket_id", ("status", "assigned_to")),
}

#: Rows an action CREATES. Deleted on the way out, keyed on the ids present
#: before the session, so a developer's own data is never touched.
_CREATED = ("service_credits", "follow_ups", "pending_actions")

_CREATED_KEYS = {
    "service_credits": "credit_id",
    "follow_ups": "follow_up_id",
    "pending_actions": "action_id",
}


@pytest.fixture(scope="session", autouse=True)
def _protect_real_dataset() -> Iterator[None]:
    """Snapshot the real tenant's mutable rows, and put them back afterwards."""
    from agentcore.settings import load_config

    real_tenant = load_config().tenant.id

    snapshot: dict[str, list[dict]] = {}
    pre_existing: dict[str, set] = {}

    with engine.admin() as conn:
        for table, (key, columns) in _MUTABLE.items():
            cols = ", ".join((key, *columns))
            snapshot[table] = [
                dict(r)
                for r in conn.execute(
                    f"SELECT {cols} FROM {table} WHERE tenant_id = %s",  # noqa: S608
                    (real_tenant,),
                ).fetchall()
            ]
        for table in _CREATED:
            key = _CREATED_KEYS[table]
            pre_existing[table] = {
                r[key]
                for r in conn.execute(
                    f"SELECT {key} FROM {table} WHERE tenant_id = %s",  # noqa: S608
                    (real_tenant,),
                ).fetchall()
            }

    yield

    with engine.admin() as conn:
        for table, (key, columns) in _MUTABLE.items():
            assignments = ", ".join(f"{c} = %s" for c in columns)
            for row in snapshot[table]:
                conn.execute(
                    f"UPDATE {table} SET {assignments} "  # noqa: S608
                    f"WHERE tenant_id = %s AND {key} = %s",
                    (*(row[c] for c in columns), real_tenant, row[key]),
                )
        for table in _CREATED:
            key = _CREATED_KEYS[table]
            keep = pre_existing[table]
            if keep:
                conn.execute(
                    f"DELETE FROM {table} "  # noqa: S608
                    f"WHERE tenant_id = %s AND {key} <> ALL(%s)",
                    (real_tenant, list(keep)),
                )
            else:
                conn.execute(
                    f"DELETE FROM {table} WHERE tenant_id = %s",  # noqa: S608
                    (real_tenant,),
                )
        conn.commit()
