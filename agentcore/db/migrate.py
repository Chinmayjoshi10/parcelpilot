"""Bootstrap and migrations.

Plain, numbered SQL files applied in order and recorded with a checksum. No
migration framework, for two reasons: the RLS policies and grants are raw SQL
anyway, so an ORM's abstraction would only be something to fight; and a
reviewer can read the whole schema history as SQL, which matters when the
schema *is* the security model.

The checksum is the useful part. If an already-applied file changes on disk,
migration refuses to run rather than pretending the database matches the
repository -- the failure mode where a policy was tightened in git but never in
production is precisely the one that has to be loud.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from agentcore.db.engine import OWNER_ROLE, owner_url
from agentcore.errors import MigrationError
from agentcore.logging import get_logger
from agentcore.settings import Settings, get_settings

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILENAME_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")

#: Serves requests; non-owner, non-superuser, so RLS applies to every query.
APP_ROLE = "parcelpilot_app"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql_text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        # Newlines normalised so a Windows checkout and a Linux CI run agree.
        normalised = self.sql_text.replace("\r\n", "\n").encode("utf-8")
        return hashlib.sha256(normalised).hexdigest()


def discover() -> list[Migration]:
    found: list[Migration] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"migration filename must look like 001_name.sql: {path.name}"
            )
        found.append(Migration(int(match.group(1)), match.group(2), path))

    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise MigrationError("duplicate migration version numbers")
    return found


# ---------------------------------------------------------------------------
# Bootstrap: database and roles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Dsn:
    user: str
    password: str
    host: str
    port: int
    dbname: str


def _parse_dsn(url: str) -> _Dsn:
    parsed = urlparse(url)
    if not parsed.path or parsed.path == "/":
        raise MigrationError(f"connection URL has no database name: {url}")
    return _Dsn(
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
    )


def bootstrap(settings: Settings | None = None) -> dict[str, str]:
    """Create the database and the two roles, idempotently.

    Requires ADMIN_DATABASE_URL (a superuser). This is the only step that needs
    one; afterwards migrations run as the owner and the server runs as the app
    role. Running it twice is safe and changes nothing except the passwords,
    which are re-synced from .env.
    """
    cfg = settings or get_settings()
    if not cfg.admin_database_url:
        raise MigrationError(
            "ADMIN_DATABASE_URL is required to create the database and roles"
        )

    admin_dsn = _parse_dsn(cfg.admin_database_url)
    app_dsn = _parse_dsn(cfg.database_url)

    if app_dsn.user != APP_ROLE:
        log.warning(
            "app_role_name_mismatch",
            hint="001_init.sql grants privileges to the literal role name",
            expected=APP_ROLE,
            configured=app_dsn.user,
        )

    # The owner shares the app password: it is a local admin identity used by
    # ingestion, not a separately distributed credential.
    owner_password = app_dsn.password

    actions: dict[str, str] = {}

    # Fail early with a message that names the fix. This is the first command
    # anyone runs against a new box, so it is the one that most needs to say
    # what is wrong rather than raise a driver traceback.
    try:
        psycopg.connect(cfg.admin_database_url, connect_timeout=5).close()
    except psycopg.OperationalError as exc:
        raise MigrationError(
            "cannot connect with ADMIN_DATABASE_URL. It needs a PostgreSQL "
            "superuser (usually 'postgres') and the correct password for the "
            f"instance on {admin_dsn.host}:{admin_dsn.port}.",
            detail=str(exc).strip(),
        ) from exc

    # CREATE DATABASE cannot run inside a transaction block.
    # `dict_row` because the encoding check below reads a column BY NAME.
    # Without it the rows are tuples, and the branch that reads them only
    # runs when the target database ALREADY EXISTS -- which never happens
    # locally, because bootstrap creates it. The first managed Postgres this
    # ran against (Railway ships a `railway` database) crashed with
    # `TypeError: tuple indices must be integers`, on the deploy path, at the
    # first command of the deploy.
    with psycopg.connect(
        cfg.admin_database_url, autocommit=True, row_factory=dict_row
    ) as conn:
        for role, password in ((OWNER_ROLE, owner_password), (APP_ROLE, app_dsn.password)):
            exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone()
            # Role names come from module constants, never user input, but they
            # still go through sql.Identifier so this file contains no example
            # of building DDL by concatenation.
            if exists:
                conn.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
                actions[role] = "password_synced"
            else:
                conn.execute(
                    sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
                actions[role] = "created"
            # Neither role is a superuser and neither may create more roles.
            # An app role that can CREATEROLE can escalate out of RLS.
            conn.execute(
                sql.SQL(
                    "ALTER ROLE {} WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
                ).format(sql.Identifier(role))
            )

        existing = conn.execute(
            """
            SELECT pg_encoding_to_char(encoding) AS encoding
            FROM pg_database WHERE datname = %s
            """,
            (app_dsn.dbname,),
        ).fetchone()

        if existing:
            actions[app_dsn.dbname] = f"exists (encoding={existing['encoding']})"
            if existing["encoding"] != "UTF8":
                # Refuse to proceed rather than silently accept a database that
                # cannot store the corpus. This cluster's template1 is WIN1252
                # (a Windows initdb default), and a database inheriting it
                # cannot hold a rupee sign, an em-dash or a curly quote --
                # characters that appear throughout real support text and
                # contracts. Detected here because the alternative is discovering
                # it as a mangled citation months later.
                raise MigrationError(
                    f"database {app_dsn.dbname} has encoding {existing['encoding']}, "
                    "but UTF8 is required. Drop it and re-run bootstrap: "
                    f'DROP DATABASE "{app_dsn.dbname}";',
                    encoding=existing["encoding"],
                )
        else:
            # TEMPLATE template0 is essential: template1 on this cluster is
            # WIN1252, and CREATE DATABASE cannot change encoding while copying
            # a template that has a different one.
            #
            # The builtin C.UTF-8 locale gives UTF-8 semantics with a
            # deterministic, platform-independent collation -- so text sorts the
            # same on this Windows box as it will in Linux CI or production.
            # OS-provided locales differ subtly between platforms, which turns
            # into ordering differences nobody expects.
            try:
                conn.execute(
                    sql.SQL(
                        "CREATE DATABASE {} OWNER {} ENCODING 'UTF8' "
                        "LOCALE_PROVIDER builtin BUILTIN_LOCALE 'C.UTF-8' "
                        "TEMPLATE template0"
                    ).format(
                        sql.Identifier(app_dsn.dbname), sql.Identifier(OWNER_ROLE)
                    )
                )
                actions[app_dsn.dbname] = "created (UTF8, builtin C.UTF-8)"
            except psycopg.errors.SyntaxError:
                # Pre-17 servers have no builtin locale provider.
                conn.execute(
                    sql.SQL(
                        "CREATE DATABASE {} OWNER {} ENCODING 'UTF8' "
                        "LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0"
                    ).format(
                        sql.Identifier(app_dsn.dbname), sql.Identifier(OWNER_ROLE)
                    )
                )
                actions[app_dsn.dbname] = "created (UTF8, C collation)"

    # Revoke the default public CREATE on the new database's public schema.
    # Without this, any role that can connect can create objects, and an
    # attacker-created function earlier in search_path can shadow ours.
    owner_admin_url = (
        f"postgresql://{admin_dsn.user}:{admin_dsn.password}"
        f"@{admin_dsn.host}:{admin_dsn.port}/{app_dsn.dbname}"
    )
    with psycopg.connect(owner_admin_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(OWNER_ROLE))
        )
        conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        conn.execute(
            sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(sql.Identifier(OWNER_ROLE))
        )
        actions["public_schema"] = "locked_down"

    log.info("bootstrap_complete", **actions)
    return actions


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@contextmanager
def _owner_connection(cfg: Settings) -> Iterator[psycopg.Connection]:
    """Connect as the owner, translating connection failures into guidance.

    The two ways this fails in practice are "bootstrap has not been run" and
    "the password in .env is wrong", and a psycopg traceback distinguishes them
    poorly. Telling the operator which one it is costs three lines here and
    saves a diagnostic detour every time.
    """
    try:
        with psycopg.connect(owner_url(cfg), row_factory=dict_row) as conn:
            yield conn
    except psycopg.OperationalError as exc:
        detail = str(exc).strip()
        if "does not exist" in detail or "authentication failed" in detail:
            raise MigrationError(
                f"cannot connect as {OWNER_ROLE}. Run `parcelpilot db bootstrap` first "
                "(it needs ADMIN_DATABASE_URL with a superuser), and check that the "
                "password in DATABASE_URL matches.",
                detail=detail,
            ) from exc
        raise MigrationError(
            f"cannot reach PostgreSQL: {detail}",
            hint="is the postgresql-x64-18 service running on this host and port?",
        ) from exc


def _ensure_bookkeeping(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    integer PRIMARY KEY,
            name       text NOT NULL,
            checksum   text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def applied(conn: psycopg.Connection) -> dict[int, dict[str, str]]:
    _ensure_bookkeeping(conn)
    rows = conn.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {r["version"]: r for r in rows}


def migrate(settings: Settings | None = None, *, dry_run: bool = False) -> list[int]:
    """Apply every pending migration. Returns the versions applied.

    Each file runs in its own transaction, so a failure leaves the schema at
    the last complete revision rather than half-way through one.
    """
    cfg = settings or get_settings()
    migrations = discover()
    done: list[int] = []

    with _owner_connection(cfg) as conn:
        already = applied(conn)
        conn.commit()

        # Drift check before applying anything: if the repository and the
        # database disagree about history, nothing downstream can be trusted.
        for m in migrations:
            prior = already.get(m.version)
            if prior and prior["checksum"] != m.checksum:
                raise MigrationError(
                    f"migration {m.version:03d}_{m.name} changed after being applied; "
                    "the database no longer matches the repository",
                    applied_checksum=prior["checksum"],
                    file_checksum=m.checksum,
                )

        pending = [m for m in migrations if m.version not in already]
        if not pending:
            log.info("migrations_up_to_date", version=max(already, default=0))
            return []

        if dry_run:
            log.info("migrations_pending", versions=[m.version for m in pending])
            return [m.version for m in pending]

        for m in pending:
            log.info("migration_applying", version=m.version, name=m.name)
            try:
                conn.execute(m.sql_text)
                conn.execute(
                    """
                    INSERT INTO schema_migrations (version, name, checksum)
                    VALUES (%s, %s, %s)
                    """,
                    (m.version, m.name, m.checksum),
                )
                conn.commit()
            except psycopg.Error as exc:
                conn.rollback()
                raise MigrationError(
                    f"migration {m.version:03d}_{m.name} failed: {exc}",
                    pgcode=exc.sqlstate,
                ) from exc
            done.append(m.version)
            log.info("migration_applied", version=m.version, name=m.name)

    return done


def status(settings: Settings | None = None) -> dict[str, object]:
    cfg = settings or get_settings()
    migrations = discover()
    with _owner_connection(cfg) as conn:
        already = applied(conn)
    return {
        "current_version": max(already, default=0),
        "applied": sorted(already),
        "pending": [m.version for m in migrations if m.version not in already],
        "drifted": [
            m.version
            for m in migrations
            if m.version in already and already[m.version]["checksum"] != m.checksum
        ],
    }
