"""Real PostgreSQL acceptance. Run only against a disposable local database."""

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.tenancy.config import RuntimeConfigurationError, TenantRuntime, schema_names
from app.tenancy.locks import advisory_lock_key
from app.tenancy.sql import render, validate_database_runtime


@pytest.fixture
def isolated_database():
    dsn = os.environ.get("RESTCONTROL_RUNTIME_TEST_DSN")
    if not dsn:
        pytest.skip("Set RESTCONTROL_RUNTIME_TEST_DSN to a disposable local PostgreSQL database")
    db = psycopg.connect(dsn, autocommit=True)
    tenants = []
    # This fixture is deliberately separate from application DSN configuration.
    for company in [uuid4(), uuid4()]:
        runtime = TenantRuntime(
            mode="tenant",
            company_id=company,
            **{f"{area}_schema": schema for area, schema in schema_names(company).items()},
            database_role=f"c_{company.hex}_runtime",
            timezone="UTC",
            frontend_origin="https://customer.example",
            api_origin="https://api.customer.example",
            runtime_directory=f"/tmp/rc-runtime/c_{company.hex}",
            configuration_version=1,
        )
        db.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(runtime.database_role)))
        for area in ["analytics", "documents", "payments"]:
            name = sql.Identifier(runtime.schema(area))
            db.execute(sql.SQL("CREATE SCHEMA {}").format(name))
            db.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    name, sql.Identifier(runtime.database_role)
                )
            )
            db.execute(
                sql.SQL("CREATE TABLE {}.records (id integer primary key, value text)").format(name)
            )
            db.execute(
                sql.SQL("GRANT SELECT, INSERT ON {}.records TO {}").format(
                    name, sql.Identifier(runtime.database_role)
                )
            )
        tenants.append(runtime)
    try:
        yield db, dsn, tenants
    finally:
        for runtime in tenants:
            for area in ["analytics", "documents", "payments"]:
                db.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(runtime.schema(area)))
                )
            db.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(runtime.database_role)))
            db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(runtime.database_role)))
        db.close()


def test_database_role_gate_and_real_two_tenant_isolation(isolated_database):
    operator, dsn, (a, b) = isolated_database
    with (
        psycopg.connect(dsn, user=a.database_role, autocommit=True) as first,
        psycopg.connect(dsn, user=b.database_role, autocommit=True) as second,
    ):
        validate_database_runtime(first, a)
        validate_database_runtime(second, b)
        for connection, runtime, value in [(first, a, "company A"), (second, b, "company B")]:
            connection.execute(
                render("INSERT INTO {analytics}.records VALUES (%s,%s)", runtime), (1, value)
            )
            assert (
                connection.execute(
                    render("SELECT value FROM {analytics}.records WHERE id=%s", runtime), (1,)
                ).fetchone()[0]
                == value
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            first.execute(render("SELECT * FROM {analytics}.records", b))
        with pytest.raises(RuntimeConfigurationError):
            validate_database_runtime(first, b)
        with pytest.raises(RuntimeConfigurationError):
            validate_database_runtime(operator, a)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            first.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(b.database_role)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            first.execute(render("CREATE TABLE {analytics}.forbidden(id integer)", a))
        assert first.execute(
            "SELECT pg_try_advisory_lock(%s)", (advisory_lock_key(a, "sync", "primary"),)
        ).fetchone()[0]
        assert second.execute(
            "SELECT pg_try_advisory_lock(%s)", (advisory_lock_key(b, "sync", "primary"),)
        ).fetchone()[0]
        assert not second.execute(
            "SELECT pg_try_advisory_lock(%s)", (advisory_lock_key(a, "sync", "primary"),)
        ).fetchone()[0]
        operator.execute(
            sql.SQL("GRANT SELECT ON {}.records TO {}").format(
                sql.Identifier(b.analytics_schema), sql.Identifier(a.database_role)
            )
        )
        with pytest.raises(RuntimeConfigurationError, match="foreign application data"):
            validate_database_runtime(first, a)


@pytest.mark.parametrize(
    "violation", ["own_create", "foreign_usage", "membership", "bypassrls", "database_create"]
)
def test_startup_rejects_privilege_drift(isolated_database, violation):
    operator, dsn, (a, b) = isolated_database
    statements = {
        "own_create": sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(
            sql.Identifier(a.analytics_schema), sql.Identifier(a.database_role)
        ),
        "foreign_usage": sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(b.analytics_schema), sql.Identifier(a.database_role)
        ),
        "membership": sql.SQL("GRANT {} TO {}").format(
            sql.Identifier(b.database_role), sql.Identifier(a.database_role)
        ),
        "bypassrls": sql.SQL("ALTER ROLE {} BYPASSRLS").format(sql.Identifier(a.database_role)),
    }
    if violation == "database_create":
        name = operator.execute("SELECT current_database()").fetchone()[0]
        statements[violation] = sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
            sql.Identifier(name), sql.Identifier(a.database_role)
        )
    operator.execute(statements[violation])
    with psycopg.connect(dsn, user=a.database_role, autocommit=True) as connection:
        with pytest.raises(RuntimeConfigurationError):
            validate_database_runtime(connection, a)


def test_existing_repository_pool_and_user_query_are_tenant_bound(isolated_database):
    """Identical global user UUIDs must resolve through each actual Repository pool."""
    from psycopg.conninfo import make_conninfo
    from test_tenant_runtime_wiring import run_startup, tenant_environment

    operator, dsn, tenants = isolated_database
    user_id = uuid4()
    for index, runtime in enumerate(tenants):
        operator.execute(
            render(
                """
            CREATE TABLE {analytics}.web_users (
                id uuid primary key, display_name text, role text, sections text[],
                is_portal_admin boolean, all_departments boolean,
                password_change_required boolean, warehouse_scope_mode text, active boolean,
                revision integer
            )
        """,
                runtime,
            )
        )
        operator.execute(
            render(
                """
            INSERT INTO {analytics}.web_users VALUES
                (%s, %s, 'deposits', ARRAY['deposits'], false, false, false, 'all', true, 1)
        """,
                runtime,
            ),
            (user_id, f"Company {index}"),
        )
        operator.execute(
            sql.SQL("GRANT SELECT ON {}.web_users TO {}").format(
                sql.Identifier(runtime.analytics_schema), sql.Identifier(runtime.database_role)
            )
        )
        env = tenant_environment(runtime.company_id)
        env["RESTCONTROL_TENANT_DATABASE_URL"] = make_conninfo(dsn, user=runtime.database_role)
        run_startup(
            f"""
from uuid import UUID
from app.core.config import Settings
from app.web.repository import Repository
repo = Repository(Settings())
try:
    repo.open()
    scope = repo.portal_scope(UUID({str(user_id)!r}))
    assert scope.user["display_name"] == {f"Company {index}"!r}
    assert scope.user["sections"] == ["deposits"]
finally:
    repo.close()
from app.portal import app
# Tenant HTTP must be built explicitly with the private verifier and payments;
# importing the legacy module cannot accidentally start an unbound app.
assert app is None
""",
            env,
        )
