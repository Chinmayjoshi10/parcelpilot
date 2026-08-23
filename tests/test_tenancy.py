"""Tenancy isolation, verified against the live database.

This is the test the architecture exists for. The claim being checked is not
"our queries include a WHERE clause" -- it is the stronger one: **a query with
no filter at all cannot return another account's rows**, because the database
refuses, not because the code remembered.

So every query below is deliberately written the wrong way, with no account
predicate whatsoever. If RLS is misconfigured, these tests fail loudly rather
than passing for the wrong reason.
"""

from __future__ import annotations

import psycopg
import pytest

from agentcore.db import engine
from agentcore.db.engine import fetch_all, fetch_one
from agentcore.errors import RepositoryError
from tests.conftest import ACCT_A, ACCT_B, OTHER_TENANT, TENANT

# Intentionally unfiltered. In a system where tenancy lives in application
# code, each of these is a data breach.
UNFILTERED = {
    "accounts": "SELECT tenant_id, account_id FROM accounts",
    "orders": "SELECT tenant_id, account_id, order_id FROM orders",
    "tickets": "SELECT tenant_id, account_id, ticket_id FROM tickets",
}


class TestCustomerIsolation:
    @pytest.mark.parametrize("table", sorted(UNFILTERED))
    def test_customer_sees_only_own_account(self, customer_a, table):
        with engine.scoped(customer_a, read_only=True) as conn:
            rows = fetch_all(conn, UNFILTERED[table])

        assert rows, f"{table}: customer should see their own rows"
        assert {r["account_id"] for r in rows} == {ACCT_A}
        assert {r["tenant_id"] for r in rows} == {TENANT}

    @pytest.mark.parametrize("table", sorted(UNFILTERED))
    def test_customer_cannot_reach_sibling_account(self, customer_a, table):
        """The classic assessment probe: logged in as A, ask for B by name."""
        sql = UNFILTERED[table] + " WHERE account_id = %s"
        with engine.scoped(customer_a, read_only=True) as conn:
            rows = fetch_all(conn, sql, (ACCT_B,))
        # Not an error -- an empty result. The row is invisible, so the
        # existence of ACCT-B is not even confirmed.
        assert rows == []

    def test_two_customers_see_disjoint_data(self, customer_a, customer_b):
        with engine.scoped(customer_a, read_only=True) as conn:
            a_orders = {r["order_id"] for r in fetch_all(conn, UNFILTERED["orders"])}
        with engine.scoped(customer_b, read_only=True) as conn:
            b_orders = {r["order_id"] for r in fetch_all(conn, UNFILTERED["orders"])}

        assert a_orders and b_orders
        assert a_orders.isdisjoint(b_orders)


class TestStaffScope:
    def test_staff_see_every_account_in_their_tenant(self, support):
        with engine.scoped(support, read_only=True) as conn:
            rows = fetch_all(conn, UNFILTERED["orders"])
        assert {r["account_id"] for r in rows} == {ACCT_A, ACCT_B}

    def test_staff_are_still_confined_to_their_tenant(self, support):
        """Staff privilege is tenant-wide, never global.

        The other tenant also has an ACCT-A, so a policy that checked only the
        account and not the tenant would leak here.
        """
        with engine.scoped(support, read_only=True) as conn:
            rows = fetch_all(conn, UNFILTERED["orders"])
        assert {r["tenant_id"] for r in rows} == {TENANT}
        assert OTHER_TENANT not in {r["tenant_id"] for r in rows}

    def test_other_tenant_admin_sees_only_their_own(self, other_tenant_staff):
        with engine.scoped(other_tenant_staff, read_only=True) as conn:
            rows = fetch_all(conn, UNFILTERED["accounts"])
        assert {r["tenant_id"] for r in rows} == {OTHER_TENANT}


class TestFailClosed:
    def test_unscoped_session_sees_nothing(self):
        """The property that makes this safe by construction.

        A connection with no scope set returns zero rows, not everything,
        because app_tenant() is NULL and `tenant_id = NULL` matches nothing. A
        forgotten scope is therefore a visible outage, never a silent leak.
        """
        pool = engine.get_pool()
        with pool.connection() as conn:
            # No _apply_scope call: this is the "someone bypassed scoped()" case.
            for sql in UNFILTERED.values():
                assert fetch_all(conn, sql) == []

    def test_runtime_role_is_not_superuser_or_owner(self, support):
        """If this fails, every policy in the schema is decoration.

        Postgres exempts superusers and table owners from RLS, so the single
        most important property of the runtime role is that it is neither.
        """
        with engine.scoped(support, read_only=True) as conn:
            row = fetch_one(
                conn,
                """
                SELECT current_user AS role,
                       (SELECT usesuper FROM pg_user WHERE usename = current_user) AS is_super,
                       (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)
                           AS bypasses_rls,
                       (SELECT count(*) FROM pg_class c
                         JOIN pg_roles r ON r.oid = c.relowner
                        WHERE r.rolname = current_user
                          AND c.relname IN ('orders', 'tickets', 'accounts')) AS owned_tables
                """,
            )

        assert row["role"] == "parcelpilot_app"
        assert row["is_super"] is False
        assert row["bypasses_rls"] is False
        assert row["owned_tables"] == 0, "the runtime role must not own the tables it queries"

    def test_rls_is_enabled_on_every_scoped_table(self, support):
        """Guards against the most likely future regression: a new table that
        carries tenant_id but nobody remembered to enable RLS on."""
        with engine.scoped(support, read_only=True) as conn:
            rows = fetch_all(
                conn,
                """
                SELECT c.relname AS table_name, c.relrowsecurity AS rls_enabled
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                  AND EXISTS (
                      SELECT 1 FROM pg_attribute a
                      WHERE a.attrelid = c.oid AND a.attname = 'tenant_id'
                            AND NOT a.attisdropped
                  )
                """,
            )

        assert rows, "expected to find tenant-scoped tables"
        unprotected = [r["table_name"] for r in rows if not r["rls_enabled"]]
        assert unprotected == [], f"tables with tenant_id but no RLS: {unprotected}"


class TestWriteRestrictions:
    def test_app_role_cannot_write_reference_data(self, ops):
        """Even an operations admin cannot rewrite a policy document at request
        time. Corpus changes go through ingestion, which runs as the owner."""
        with (
            pytest.raises((RepositoryError, psycopg.errors.InsufficientPrivilege)),
            engine.scoped(ops) as conn,
        ):
            conn.execute("UPDATE documents SET title = 'tampered'")

    def test_audit_log_is_append_only(self, ops):
        """Append-only by permission, not convention.

        UPDATE and DELETE are revoked from the runtime role, so no code path --
        including a buggy or hostile one -- can rewrite history.
        """
        with engine.scoped(ops) as conn:
            conn.execute(
                """
                INSERT INTO audit_log (tenant_id, account_id, actor, actor_role, event)
                VALUES (%s, %s, 'u-ops', 'operations_admin', 'test_event')
                """,
                (TENANT, ACCT_A),
            )

        for statement in (
            "UPDATE audit_log SET event = 'rewritten'",
            "DELETE FROM audit_log",
        ):
            with (
                pytest.raises((RepositoryError, psycopg.errors.InsufficientPrivilege)),
                engine.scoped(ops) as conn,
            ):
                conn.execute(statement)

    def test_read_only_transaction_rejects_writes(self, ops):
        """`read_only=True` turns "this path should not mutate anything" from a
        review comment into an enforced property."""
        with (
            pytest.raises((RepositoryError, psycopg.errors.ReadOnlySqlTransaction)),
            engine.scoped(ops, read_only=True) as conn,
        ):
            conn.execute(
                    "INSERT INTO audit_log (tenant_id, actor, actor_role, event) "
                    "VALUES (%s, 'x', 'operations_admin', 'e')",
                    (TENANT,),
                )


class TestDatabaseEncoding:
    """A WIN1252 database cannot store the corpus.

    The cluster's template1 is WIN1252 (a Windows initdb default), so a
    database created without an explicit ENCODING inherits it and silently
    cannot hold a rupee sign, an em-dash or a curly quote -- all of which occur
    in real support text and contracts. Bootstrap now creates UTF8 from
    template0 and refuses to proceed against a non-UTF8 database.
    """

    def test_database_is_utf8(self, support):
        with engine.scoped(support, read_only=True) as conn:
            row = fetch_one(
                conn,
                "SELECT pg_encoding_to_char(encoding) AS enc FROM pg_database "
                "WHERE datname = current_database()",
            )
        assert row["enc"] == "UTF8"

    def test_non_cp1252_text_round_trips(self, support):
        """Fails on a WIN1252 database at the driver, before the query runs."""
        probe = "₹5,000 — “curly” ünïcödé"
        with engine.scoped(support, read_only=True) as conn:
            row = fetch_one(conn, "SELECT %s::text AS v", (probe,))
        assert row["v"] == probe

    def test_health_treats_encoding_as_a_readiness_condition(self):
        """Serving from a non-UTF8 database is worse than being down: it
        answers, with mangled text, and nothing alerts."""
        state = engine.healthcheck()
        assert state["encoding"] == "UTF8"
        assert state["ready"] is True


class TestScopeVerification:
    def test_scope_does_not_leak_between_pooled_transactions(self, customer_a, customer_b):
        """The pooling hazard: connections are reused across principals.

        Scope is set with `set_config(..., is_local => true)`, so it dies with
        the transaction. If it were session-level instead, B could inherit A's
        scope from a recycled connection.
        """
        for _ in range(3):
            with engine.scoped(customer_a, read_only=True) as conn:
                a = {r["account_id"] for r in fetch_all(conn, UNFILTERED["orders"])}
            with engine.scoped(customer_b, read_only=True) as conn:
                b = {r["account_id"] for r in fetch_all(conn, UNFILTERED["orders"])}
            assert a == {ACCT_A}
            assert b == {ACCT_B}


class TestInternalRecordsAreStaffOnly:
    """A NULL account means INTERNAL, not "everyone".

    Staff act tenant-wide, so their runs, steps, candidates and audit rows carry
    account_id = NULL. The original policies said
    `account_id IS NULL OR app_can_see_account(...)`, which made every internal
    record readable by every customer in the tenant -- their questions, answers,
    reasoning traces and retrieval candidates across all accounts.

    Found by auditing what a customer could actually reach through the API, not
    by reading the policy: the SQL looked right in isolation. Fixed in
    006_internal_records_are_staff_only.sql.
    """

    @staticmethod
    def _seed_internal_run(conn) -> str:
        """An internal run: tenant-scoped, no account."""
        row = conn.execute(
            """
            INSERT INTO runs (tenant_id, account_id, user_id, role, query, status)
            VALUES (%s, NULL, 'ops@internal', 'operations_admin',
                    'internal cross-account review', 'completed')
            RETURNING run_id
            """,
            (TENANT,),
        ).fetchone()
        run_id = row["run_id"]
        conn.execute(
            """
            INSERT INTO run_steps (run_id, seq, tenant_id, account_id, kind, label)
            VALUES (%s, 1, %s, NULL, 'reason', 'internal step')
            """,
            (run_id, TENANT),
        )
        conn.execute(
            """
            INSERT INTO audit_log (tenant_id, account_id, actor, actor_role, event)
            VALUES (%s, NULL, 'ops@internal', 'operations_admin', 'internal.review')
            """,
            (TENANT,),
        )
        conn.commit()
        return run_id

    def test_customer_cannot_read_an_internal_run(self, customer_a, support):
        with engine.admin() as conn:
            run_id = self._seed_internal_run(conn)

        try:
            with engine.scoped(customer_a, read_only=True) as conn:
                runs = fetch_all(conn, "SELECT run_id, account_id FROM runs")
                steps = fetch_all(conn, "SELECT run_id FROM run_steps")
                audit = fetch_all(conn, "SELECT event FROM audit_log")

            # Nothing with a NULL account is visible to a customer.
            assert all(r["account_id"] is not None for r in runs)
            assert run_id not in {r["run_id"] for r in runs}
            assert run_id not in {s["run_id"] for s in steps}
            assert "internal.review" not in {a["event"] for a in audit}

            # And staff CAN see it -- otherwise this test would pass on a policy
            # that simply hid everything.
            with engine.scoped(support, read_only=True) as conn:
                staff_runs = {r["run_id"] for r in fetch_all(conn, "SELECT run_id FROM runs")}
            assert run_id in staff_runs
        finally:
            with engine.admin() as conn:
                conn.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))
                conn.execute("DELETE FROM audit_log WHERE event = 'internal.review'")
                conn.commit()

    def test_global_documents_stay_visible_to_customers(self):
        """The same SQL shape means something different here, deliberately.

        For documents, `owner_account_id IS NULL` means "a general policy", which
        every customer must read. That asymmetry is why the bug survived review:
        one pattern, two meanings.

        Uses the real ingested tenant rather than the synthetic fixture one,
        because the assertion is about the actual corpus.
        """
        from agentcore.settings import load_config
        from agentcore.types import Principal, Role

        real_tenant = load_config().tenant.id
        who = Principal(
            tenant_id=real_tenant,
            user_id="u",
            role=Role.CUSTOMER,
            account_id="ACCT-001",
        )
        with engine.scoped(who, read_only=True) as conn:
            rows = fetch_all(
                conn,
                "SELECT count(*) AS n FROM documents WHERE owner_account_id IS NULL",
            )
        assert rows[0]["n"] > 0, "global policy documents must remain readable"


class TestConversationThreading:
    """A supplied conversation id must be verified, not merely referenced.

    The foreign key on `runs.conversation_id` is NOT the check that matters
    here. PostgreSQL evaluates referential integrity as the referenced table's
    owner, and an owner is exempt from row-level security -- so the constraint
    accepts an id the caller cannot see. Without the explicit visibility read in
    `app.routers.chat._resolve_conversation`, one customer could file a run
    under another customer's thread.
    """

    def test_a_conversation_is_created_for_its_owner(self, customer_a):
        from app.routers.chat import _resolve_conversation

        with engine.scoped(customer_a) as conn:
            conversation_id = _resolve_conversation(conn, customer_a, None)
            row = fetch_one(
                conn,
                "SELECT account_id, user_id FROM conversations WHERE conversation_id = %s",
                (conversation_id,),
            )

        assert row["account_id"] == ACCT_A
        assert row["user_id"] == customer_a.user_id

    def test_an_owned_conversation_is_reused(self, customer_a):
        from app.routers.chat import _resolve_conversation

        with engine.scoped(customer_a) as conn:
            first = _resolve_conversation(conn, customer_a, None)
            again = _resolve_conversation(conn, customer_a, first)

        assert again == first

    def test_another_customers_conversation_is_rejected(self, customer_a, customer_b):
        """The FK would accept this. The visibility check must not."""
        from fastapi import HTTPException

        from app.routers.chat import _resolve_conversation

        with engine.scoped(customer_b) as conn:
            theirs = _resolve_conversation(conn, customer_b, None)

        with engine.scoped(customer_a) as conn, pytest.raises(HTTPException) as raised:
            _resolve_conversation(conn, customer_a, theirs)

        assert raised.value.status_code == 404

    def test_staff_conversations_are_not_visible_to_customers(self, ops, customer_a):
        """Staff threads carry a NULL account, which migration 006 made staff-only."""
        from fastapi import HTTPException

        from app.routers.chat import _resolve_conversation

        with engine.scoped(ops) as conn:
            internal = _resolve_conversation(conn, ops, None)

        with engine.scoped(customer_a) as conn, pytest.raises(HTTPException):
            _resolve_conversation(conn, customer_a, internal)


class TestForeignAccountMentions:
    """A question ABOUT another customer must not be answered with your own data.

    The record-level guard cannot cover this: "What cancellation terms does
    LumenWorks have?" names no id, so nothing resolves to zero rows, and the run
    happily answered with the ASKER's contract terms. Nothing leaked -- RLS meant
    LumenWorks' agreement was never read -- but describing one company's contract
    in reply to a question about another's is wrong for the question asked, and an
    earlier build merged the identities into one sentence.

    `app_names_foreign_account` is SECURITY DEFINER because the only way to
    detect this is to know the tenant's account names, and granting the request
    path a tenant-wide read on `accounts` to fix a tenancy bug would be
    self-defeating. It returns one boolean and never a row.
    """

    def test_a_sibling_company_name_is_detected(self, customer_a):
        with engine.scoped(customer_a, read_only=True) as conn:
            row = fetch_one(
                conn,
                "SELECT app_names_foreign_account(%s) AS x",
                ("What cancellation terms does Quillmark Retail have?",),
            )
        assert row["x"] is True

    def test_your_own_company_name_is_not(self, customer_a):
        with engine.scoped(customer_a, read_only=True) as conn:
            own = fetch_one(
                conn, "SELECT account_name FROM accounts WHERE account_id = %s", (ACCT_A,)
            )
            row = fetch_one(
                conn,
                "SELECT app_names_foreign_account(%s) AS x",
                (f"tell me about {own['account_name']}",),
            )
        assert row["x"] is False

    def test_staff_never_trip_it(self, ops):
        """Staff act tenant-wide, so no account is foreign to them."""
        with engine.scoped(ops, read_only=True) as conn:
            for name in ("Harbourline Freight", "Quillmark Retail"):
                row = fetch_one(
                    conn,
                    "SELECT app_names_foreign_account(%s) AS x",
                    (f"what about {name}?",),
                )
                assert row["x"] is False, name

    def test_it_matches_on_word_boundaries(self, customer_a):
        """"quill of a feather" must not fire on an account called Quillmark."""
        with engine.scoped(customer_a, read_only=True) as conn:
            row = fetch_one(
                conn,
                "SELECT app_names_foreign_account(%s) AS x",
                ("does the quill of a feather matter for palletised freight?",),
            )
        assert row["x"] is False

    def test_it_returns_a_bit_and_never_a_row(self, customer_a):
        """The point of the function: no enumeration surface.

        If the runtime role could read other accounts directly, the function
        would be pointless -- so assert the underlying grant is still absent.
        """
        with engine.scoped(customer_a, read_only=True) as conn:
            rows = fetch_all(conn, "SELECT account_id FROM accounts")
        assert {r["account_id"] for r in rows} == {ACCT_A}
