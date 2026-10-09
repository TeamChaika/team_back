"""Operator-only, atomic provisioning of empty tenant schemas.

This module is never called by a customer request or runtime startup. Pass an
explicit privileged connection; it does not read a DSN, connect to production,
copy application rows, or activate modules.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from psycopg import sql
from psycopg.pq import TransactionStatus

from app.tenancy.config import RuntimeConfigurationError, TenantRuntime
from app.tenancy.locks import advisory_lock_key
from app.tenancy.sql import render

MIGRATION_DIRECTORY = Path(__file__).resolve().parents[2] / "migrations" / "tenant"
_AREAS = ("analytics", "documents", "payments")


class MigrationError(RuntimeError):
    """Provisioning refuses an untrusted or inconsistent database state."""


@dataclass(frozen=True)
class Migration:
    name: str
    area: str
    template: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.template.encode("utf-8")).hexdigest()


def load_migrations(directory: Path = MIGRATION_DIRECTORY) -> tuple[Migration, ...]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise MigrationError("Unsupported tenant migration manifest")
    result = []
    names = set()
    for entry in manifest["migrations"]:
        name, area = entry["file"], entry["area"]
        if Path(name).name != name or not name.endswith(".sql") or area not in _AREAS:
            raise MigrationError("Unsafe tenant migration manifest entry")
        if name in names:
            raise MigrationError("Duplicate tenant migration name")
        names.add(name)
        result.append(Migration(name, area, (directory / name).read_text(encoding="utf-8")))
    if not result or result[0].area != "analytics":
        raise MigrationError("Analytics baseline must precede document migrations")
    return tuple(result)


def migration_fingerprint() -> str:
    """Detect added/changed migration files before trusting an earlier stage cache."""
    return hashlib.sha256(
        json.dumps([(item.name, item.area, item.checksum) for item in load_migrations()]).encode()
    ).hexdigest()


def _check_role(connection, runtime: TenantRuntime) -> bool:
    row = connection.execute(
        """
        SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls,
               EXISTS(SELECT 1 FROM pg_auth_members WHERE member=r.oid),
               r.oid = (SELECT datdba FROM pg_database WHERE datname=current_database())
        FROM pg_roles r WHERE rolname=%s
    """,
        (runtime.database_role,),
    ).fetchone()
    if row is None:
        return False
    if any(row):
        raise MigrationError("Runtime role is privileged, a database owner or a role member")
    own = [runtime.schema(area) for area in _AREAS]
    unsafe = connection.execute(
        """
        SELECT EXISTS(
            SELECT 1 FROM pg_namespace n
            WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
              AND (has_schema_privilege(%s,n.oid,'CREATE') OR
                   (n.nspname <> ALL(%s) AND n.nspname NOT IN ('public','extensions')
                    AND has_schema_privilege(%s,n.oid,'USAGE')))
        ) OR EXISTS(
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname <> ALL(%s) AND n.nspname <> 'information_schema'
              AND n.nspname !~ '^pg_'
              AND CASE WHEN c.relkind='S' THEN
                  has_sequence_privilege(%s,c.oid,'USAGE,SELECT,UPDATE')
                  WHEN c.relkind IN ('r','v','m','f','p') THEN
                  has_table_privilege(%s,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                  OR has_any_column_privilege(%s,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')
                  ELSE false END
        )
    """,
        (
            runtime.database_role,
            own,
            runtime.database_role,
            own,
            runtime.database_role,
            runtime.database_role,
            runtime.database_role,
        ),
    ).fetchone()[0]
    if unsafe:
        raise MigrationError("Runtime role has foreign data access or schema CREATE privilege")
    if connection.execute(
        "SELECT EXISTS(SELECT 1 FROM pg_class WHERE relowner = "
        "(SELECT oid FROM pg_roles WHERE rolname=%s))",
        (runtime.database_role,),
    ).fetchone()[0]:
        raise MigrationError("Runtime role must not own database objects")
    return True


def _check_identity_role(connection, runtime: TenantRuntime) -> None:
    """The central identity channel may execute only this company's fixed API."""
    role = f"{runtime.key}_identity_runtime"
    row = connection.execute(
        "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls "
        "OR rolinherit OR EXISTS(SELECT 1 FROM pg_database WHERE datdba=r.oid) "
        "OR EXISTS(SELECT 1 FROM pg_class WHERE relowner=r.oid) "
        "OR EXISTS(SELECT 1 FROM pg_proc WHERE proowner=r.oid), "
        "rolcanlogin, EXISTS(SELECT 1 FROM pg_auth_members WHERE member=r.oid), "
        "has_database_privilege(r.oid,current_database(),'CREATE') "
        "FROM pg_roles r WHERE rolname=%s",
        (role,),
    ).fetchone()
    if row is None:
        return
    if row[0] or not row[1] or row[2] or row[3]:
        raise MigrationError("Identity role is privileged or a role member")
    unsafe = connection.execute(
        """
        SELECT EXISTS(
            SELECT 1 FROM pg_namespace n WHERE n.nspname <> 'information_schema'
              AND n.nspname !~ '^pg_' AND (
                has_schema_privilege(%s,n.oid,'CREATE') OR
                (n.nspname NOT IN (%s,'public','extensions')
                 AND has_schema_privilege(%s,n.oid,'USAGE')))
        ) OR EXISTS(
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
              AND CASE WHEN c.relkind='S' THEN
                  has_sequence_privilege(%s,c.oid,'USAGE,SELECT,UPDATE')
                WHEN c.relkind IN ('r','v','m','f','p') THEN
                  has_table_privilege(%s,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                  OR has_any_column_privilege(%s,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')
                ELSE false END
        ) OR EXISTS(
            SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname=%s AND NOT EXISTS(
                SELECT 1 FROM unnest(%s::text[]) AS allowed(signature)
                WHERE p.oid=to_regprocedure(allowed.signature)
            ) AND has_function_privilege(%s,p.oid,'EXECUTE')
        )
        """,
        (
            role,
            runtime.analytics_schema,
            role,
            role,
            role,
            role,
            runtime.analytics_schema,
            [
                f"{runtime.analytics_schema}.provision_company_identity(uuid,text,text,text)",
                f"{runtime.analytics_schema}.provision_company_primary_admin(uuid,text,text,text,boolean)",
                f"{runtime.analytics_schema}.set_company_identity_password_required(uuid,boolean)",
                f"{runtime.analytics_schema}.company_identity_actor_is_admin(uuid)",
                f"{runtime.analytics_schema}.consume_company_password_recovery(text)",
                f"{runtime.analytics_schema}.lock_company_password_recovery(text)",
            ],
            role,
        ),
    ).fetchone()[0]
    if unsafe:
        raise MigrationError("Identity role has access outside its provisioning routines")


def _schema(connection, runtime: TenantRuntime, area: str) -> None:
    namespace = runtime.schema(area)
    row = connection.execute(
        """
        SELECT n.nspowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
        FROM pg_namespace n WHERE n.nspname=%s
    """,
        (namespace,),
    ).fetchone()
    if row is not None:
        if not row[0]:
            raise MigrationError("Existing tenant schema has a different owner")
        marker = connection.execute(
            "SELECT to_regclass(%s)", (f"{namespace}._tenant_migrations",)
        ).fetchone()[0]
        if marker is None:
            raise MigrationError("Refusing to adopt an existing schema without a tenant journal")
    else:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
        connection.execute(
            sql.SQL("""
            CREATE TABLE {}._tenant_migrations (
                name text PRIMARY KEY,
                company_id uuid NOT NULL,
                checksum text NOT NULL CHECK(checksum ~ '^[0-9a-f]{{64}}$'),
                applied_at timestamptz NOT NULL DEFAULT now()
            )
        """).format(sql.Identifier(namespace))
        )
    connection.execute(
        sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(namespace))
    )
    connection.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(namespace), sql.Identifier(runtime.database_role)
        )
    )
    # Prevent inherited Supabase operator default privileges on future objects.
    for kind in ("TABLES", "SEQUENCES", "FUNCTIONS"):
        for role in ["PUBLIC", "anon", "authenticated", "service_role"]:
            if (
                role != "PUBLIC"
                and not connection.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
                ).fetchone()
            ):
                continue
            grantee = sql.SQL("PUBLIC") if role == "PUBLIC" else sql.Identifier(role)
            connection.execute(
                sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} REVOKE ALL ON {} FROM {}").format(
                    sql.Identifier(namespace), sql.SQL(kind), grantee
                )
            )


def _harden(connection, runtime: TenantRuntime) -> None:
    """ACL preserves append-only tables; RLS is additional tenant-role protection."""
    for area in _AREAS:
        namespace = runtime.schema(area)
        for role in ["PUBLIC", "anon", "authenticated", "service_role"]:
            if (
                role != "PUBLIC"
                and not connection.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
                ).fetchone()
            ):
                continue
            grantee = sql.SQL("PUBLIC") if role == "PUBLIC" else sql.Identifier(role)
            connection.execute(
                sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(
                    sql.Identifier(namespace), grantee
                )
            )
            for kind in ("TABLES", "SEQUENCES", "FUNCTIONS"):
                connection.execute(
                    sql.SQL("REVOKE ALL ON ALL {} IN SCHEMA {} FROM {}").format(
                        sql.SQL(kind), sql.Identifier(namespace), grantee
                    )
                )
        tables = connection.execute(
            """
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND c.relkind IN ('r','p')
        """,
            (namespace,),
        ).fetchall()
        for (table,) in tables:
            qualified = sql.Identifier(namespace, table)
            connection.execute(
                sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(qualified)
            )
            if table == "_tenant_migrations":
                # An unprivileged runtime cannot alter migration evidence.
                connection.execute(
                    sql.SQL("REVOKE ALL ON {} FROM {}").format(
                        qualified, sql.Identifier(runtime.database_role)
                    )
                )
                continue
            connection.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(qualified))
            # Existing analytics policies carry operation-specific behavior. The
            # document tables historically used ACL only: add a role-scoped policy.
            if area == "documents":
                exists = connection.execute(
                    """
                    SELECT 1 FROM pg_policies WHERE schemaname=%s AND tablename=%s
                      AND policyname='tenant_runtime'
                """,
                    (namespace, table),
                ).fetchone()
                if not exists:
                    connection.execute(
                        sql.SQL(
                            "CREATE POLICY tenant_runtime ON {} TO {} USING(true) WITH CHECK(true)"
                        ).format(qualified, sql.Identifier(runtime.database_role))
                    )


def provision_tenant(
    connection, runtime: TenantRuntime, *, directory: Path = MIGRATION_DIRECTORY
) -> tuple[str, ...]:
    """Create/update one tenant atomically, returning newly applied migration names.

    Connection must be idle and operator-owned. The dedicated role has LOGIN but
    no password on creation; install a secret through the operator credential
    workflow before remote use. No credentials are read or returned here.
    Payments uses a separate restricted role; its credential is provisioned separately.
    """
    if runtime.mode != "tenant":
        raise RuntimeConfigurationError("Tenant provisioning cannot target legacy Chaika")
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise MigrationError("Provisioning requires an idle dedicated operator connection")
    migrations = load_migrations(directory)
    applied = []
    with connection.transaction():
        connection.execute("SET LOCAL lock_timeout='5s'")
        connection.execute("SET LOCAL search_path=pg_catalog")
        connection.execute(
            "SELECT pg_advisory_xact_lock(%s)", (advisory_lock_key(runtime, "migrations"),)
        )
        _check_identity_role(connection, runtime)
        if not _check_role(connection, runtime):
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(runtime.database_role))
            )
            _check_role(connection, runtime)
        for area in _AREAS:
            _schema(connection, runtime, area)
        known = {m.name: m for m in migrations}
        seen = set()
        for area in _AREAS:
            rows = connection.execute(
                sql.SQL("SELECT name,company_id,checksum FROM {}._tenant_migrations").format(
                    sql.Identifier(runtime.schema(area))
                )
            ).fetchall()
            for name, company_id, checksum in rows:
                expected = known.get(name)
                if (
                    expected is None
                    or expected.area != area
                    or company_id != runtime.company_id
                    or checksum != expected.checksum
                ):
                    raise MigrationError("Tenant migration journal/checksum mismatch")
                seen.add(name)
        missing_seen = False
        for migration in migrations:
            if migration.name in seen:
                if missing_seen:
                    raise MigrationError("Tenant migration journal is not an ordered prefix")
                continue
            missing_seen = True
            connection.execute(
                render(
                    migration.template,
                    runtime,
                    runtime_role=runtime.database_role,
                    payments_role=f"{runtime.key}_payments_runtime",
                    identity_role=f"{runtime.key}_identity_runtime",
                    migration_owner_role=connection.execute("SELECT current_user").fetchone()[0],
                )
            )
            connection.execute(
                sql.SQL(
                    "INSERT INTO {}._tenant_migrations(name,company_id,checksum) VALUES(%s,%s,%s)"
                ).format(sql.Identifier(runtime.schema(migration.area))),
                (migration.name, runtime.company_id, migration.checksum),
            )
            applied.append(migration.name)
        _harden(connection, runtime)
        _check_identity_role(connection, runtime)
    return tuple(applied)
