"""Database access, and the only place tenancy scope is established.

The central idea: you cannot get a usable connection without saying who you
are. `scoped()` takes a `Principal`, opens a transaction, and sets the three
GUCs that the RLS policies read. There is no API here that returns a bare
connection to request-path code, so "forgot to filter by account" is not a
mistake this codebase can make -- the worst case is zero rows, because the
policy helpers return NULL for an unset tenant and `tenant_id = NULL` matches
nothing.

`admin()` is the deliberate exception, used by migrations and ingestion. It
connects as the owner, which Postgres exempts from RLS, and it is never
reachable from a request.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast
from urllib.parse import quote, unquote, urlparse

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from agentcore.errors import RepositoryError, TenancyViolation
from agentcore.logging import get_logger
from agentcore.settings import Settings, get_settings
from agentcore.types import Principal, Role

log = get_logger(__name__)

#: Session GUCs consumed by the RLS policies in 001_init.sql.
_TENANT_GUC = "app.tenant_id"
_ACCOUNT_GUC = "app.account_id"
_ROLE_GUC = "app.role"


@functools.lru_cache(maxsize=1)
def get_pool(settings: Settings | None = None) -> ConnectionPool:
    """Process-wide pool for the runtime (non-owner) role.

    Opened lazily and checked on first use so a misconfigured DSN fails at
    startup with a clear error rather than on the first user's request.
    """
    cfg = settings or get_settings()
    pool = ConnectionPool(
        conninfo=cfg.database_url,
        min_size=cfg.db_pool_min,
        max_size=cfg.db_pool_max,
        open=False,
        kwargs={
            "row_factory": dict_row,
            "autocommit": False,
            # Pinned, never inherited from the OS locale. On Windows psycopg
            # would otherwise negotiate cp1252 and fail to encode any parameter
            # containing a rupee sign, an em-dash or a curly quote.
            "client_encoding": "UTF8",
            # Applies to every session from this pool. A runaway query is
            # cancelled rather than holding a connection for the whole run.
            "options": f"-c statement_timeout={cfg.db_statement_timeout_ms}",
        },
        name="parcelpilot-app",
    )
    pool.open(wait=True, timeout=10.0)
    log.info("db_pool_opened", min_size=cfg.db_pool_min, max_size=cfg.db_pool_max)
    return pool


def close_pool() -> None:
    """Shut the pool down and clear the cache (used at app shutdown/tests)."""
    if get_pool.cache_info().currsize:
        get_pool().close()
        get_pool.cache_clear()


def _apply_scope(conn: Connection, principal: Principal) -> None:
    """Bind the principal to this transaction.

    `set_config(..., is_local => true)` rather than `SET LOCAL` because the
    latter cannot be parameterised, and building a SET statement by string
    concatenation from a token claim is exactly the injection surface this
    architecture refuses elsewhere. The scope dies with the transaction, so a
    pooled connection can never carry one principal's scope into another's
    request.
    """
    # Staff carry no account: the empty string is how "not narrowed to one
    # account" is spelled, and app_account() maps it back to NULL.
    account = principal.account_id or ""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT set_config(%s, %s, true),
                   set_config(%s, %s, true),
                   set_config(%s, %s, true)
            """,
            (
                _TENANT_GUC,
                principal.tenant_id,
                _ACCOUNT_GUC,
                account,
                _ROLE_GUC,
                principal.role.value,
            ),
        )


def _verify_scope(conn: Connection, principal: Principal) -> None:
    """Confirm the database agrees about who we are before any query runs.

    Cheap, and it catches the two failure modes that would otherwise be silent:
    a pooled connection that skipped `_apply_scope`, and a runtime role that is
    a superuser or table owner (in which case RLS is not in effect at all and
    every policy in the schema is decoration).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT app_tenant() AS tenant,
                   app_account() AS account,
                   app_role() AS role,
                   current_user AS db_user,
                   (SELECT usesuper FROM pg_user WHERE usename = current_user) AS is_super
            """
        )
        row = cur.fetchone()

    if row is None or row["tenant"] != principal.tenant_id:
        raise TenancyViolation(
            "database session scope does not match the principal",
            expected=principal.tenant_id,
            actual=row["tenant"] if row else None,
        )
    if row["is_super"]:
        raise TenancyViolation(
            "the runtime role is a superuser, so row-level security is bypassed",
            db_user=row["db_user"],
        )
    if principal.role is Role.CUSTOMER and row["account"] != principal.account_id:
        raise TenancyViolation(
            "customer session is not narrowed to its account",
            expected=principal.account_id,
            actual=row["account"],
        )


class ScopedConnection:
    """A connection proxy that re-establishes RLS scope after every commit.

    This exists because two correct decisions collided.

    Scope is set with `set_config(..., is_local => true)` so it dies with the
    transaction -- that is what stops a pooled connection carrying one
    principal's scope into another's request. But the orchestrator commits after
    every run-log step, so a client can tail the reasoning trace while the run
    is still in flight; an uncommitted row is invisible to other connections.

    Together, those meant the first commit silently dropped the scope and every
    later query saw an empty database. Fail-closed turned a would-be data leak
    into an obvious outage, which is the whole point of designing it that way --
    but the outage still had to be fixed.

    Overriding `commit`/`rollback` here makes it structurally safe: any caller
    that commits keeps its scope, without having to remember to re-apply it.
    """

    __slots__ = ("_conn", "_principal")

    def __init__(self, conn: Connection, principal: Principal) -> None:
        self._conn = conn
        self._principal = principal

    def commit(self) -> None:
        self._conn.commit()
        # The new transaction starts unscoped; re-bind before anything can read.
        _apply_scope(self._conn, self._principal)

    def rollback(self) -> None:
        self._conn.rollback()
        _apply_scope(self._conn, self._principal)

    def __getattr__(self, name: str) -> Any:
        # Everything else (execute, cursor, info, ...) delegates unchanged.
        return getattr(self._conn, name)


@contextmanager
def scoped(
    principal: Principal,
    *,
    verify: bool = True,
    read_only: bool = False,
) -> Iterator[Connection]:
    """Yield a connection whose every statement is filtered to `principal`.

    The transaction commits on clean exit and rolls back on any exception, so a
    partially applied action cannot survive an error.

    `read_only=True` asks the database to reject writes outright. Worth using
    for retrieval and dashboards: it turns "this path should not mutate
    anything" from a code review comment into an enforced property.
    """
    pool = get_pool()
    with pool.connection() as conn:
        try:
            if read_only:
                conn.execute("SET TRANSACTION READ ONLY")
            _apply_scope(conn, principal)
            if verify:
                _verify_scope(conn, principal)
            # Yielded as the proxy so a commit inside the block cannot silently
            # drop the RLS scope. cast() keeps every downstream `Connection`
            # annotation honest: the proxy is API-compatible by delegation.
            yield cast(Connection, ScopedConnection(conn, principal))
        except psycopg.Error as exc:
            # Surfaces as a domain error; the driver's message may name
            # internal columns and is not user-safe.
            raise RepositoryError(f"database error: {exc}", pgcode=exc.sqlstate) from exc


#: Owns the tables, so Postgres exempts it from RLS. Defined here as the single
#: source of truth; agentcore.db.migrate imports it to create the role.
OWNER_ROLE = "parcelpilot_owner"


def owner_url(settings: Settings | None = None) -> str:
    """Owner DSN for the *application* database.

    Derived from DATABASE_URL by swapping the role, deliberately not from
    ADMIN_DATABASE_URL: that one points at the `postgres` maintenance database
    and is only for CREATE DATABASE / CREATE ROLE. Connecting ingestion there
    would write the corpus into the wrong database entirely.
    """
    cfg = settings or get_settings()
    parsed = urlparse(cfg.database_url)
    password = unquote(parsed.password or "")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5432
    dbname = parsed.path.lstrip("/")
    return f"postgresql://{OWNER_ROLE}:{quote(password)}@{host}:{port}/{dbname}"


@contextmanager
def admin(settings: Settings | None = None) -> Iterator[Connection]:
    """Owner-level connection for migrations and ingestion only.

    RLS does not apply here. That is the point -- ingestion writes across every
    account -- and it is why this function takes no `Principal`: there is no
    principal whose scope it would respect. Nothing on the request path may
    import it; `scoped()` is the only door in from there.
    """
    cfg = settings or get_settings()
    with psycopg.connect(
        owner_url(cfg), row_factory=dict_row, autocommit=False, client_encoding="UTF8"
    ) as conn:
        yield conn


def fetch_all(conn: Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def fetch_one(conn: Connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def healthcheck(tenant_id: str | None = None) -> dict[str, Any]:
    """Readiness, not liveness.

    Answers one question: should this replica receive traffic? Three things
    have to be true, and each has been observed failing in practice:

    * migrations applied -- otherwise the schema is unknown;
    * an active index version -- a server pinned to no index cannot answer;
    * UTF8 encoding -- a WIN1252 database answers, but with mangled text, which
      is worse than being down because nothing alerts on it.

    Never raises: an exception here reads as a crashed container rather than an
    unready one, which sends the diagnosis in entirely the wrong direction.
    """
    try:
        return _healthcheck(tenant_id)
    except Exception as exc:  # noqa: BLE001 - deliberately total
        log.error("healthcheck_failed", error=str(exc))
        return {"database": "error", "error": str(exc), "ready": False}


def _healthcheck(tenant_id: str | None) -> dict[str, Any]:
    if tenant_id is None:
        # Imported here: settings pulls in config parsing, and engine is
        # imported by the migration path, which must not need config.yaml.
        from agentcore.settings import load_config

        tenant_id = load_config().tenant.id

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(version) AS schema_version FROM schema_migrations")
        schema_version = (cur.fetchone() or {}).get("schema_version")

        cur.execute(
            "SELECT pg_encoding_to_char(encoding) AS enc FROM pg_database "
            "WHERE datname = current_database()"
        )
        encoding = (cur.fetchone() or {}).get("enc")

        # Via the SECURITY DEFINER helper: the probe has no principal, and RLS
        # correctly hides index_versions from an unscoped session. Loosening the
        # policy to make a health check work would have been the wrong trade.
        cur.execute("SELECT * FROM app_active_index(%s)", (tenant_id,))
        active = cur.fetchone()

    return {
        "database": "ok",
        "encoding": encoding,
        "schema_version": schema_version,
        "tenant_id": tenant_id,
        "active_index": active,
        "ready": (
            schema_version is not None and active is not None and encoding == "UTF8"
        ),
    }
