"""Company identity provisioning through an execution-only PostgreSQL role."""

from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.tenancy.migrations import MigrationError, provision_tenant
from app.tenancy.sql import render
from tests.test_tenant_migrations_postgres import empty_database as empty_database


def provision(db, runtime, user, *, email="member@example.invalid", marker="test-provision"):
    return db.execute(
        render("SELECT {analytics}.provision_company_identity(%s,%s,%s,%s)", runtime),
        (user, email, marker, "Company member"),
    ).fetchone()[0]


@pytest.fixture
def identity_database(empty_database):
    operator, dsn, tenant = empty_database
    first, second = tenant(), tenant()
    for runtime in (first, second):
        provision_tenant(operator, runtime)
    with psycopg.connect(dsn, user=f"{first.key}_identity_runtime", autocommit=True) as identity:
        yield operator, dsn, first, second, identity


def test_identity_routine_creates_employee_once_without_grants(identity_database):
    operator, dsn, runtime, foreign, identity = identity_database
    user = uuid4()
    assert provision(identity, runtime, user) == user
    assert provision(identity, runtime, user) == user
    row = operator.execute(
        render(
            """SELECT display_name,role,active,sections,is_portal_admin,
        all_departments,password_change_required,warehouse_scope_mode FROM {analytics}.web_users
        WHERE id=%s""",
            runtime,
        ),
        (user,),
    ).fetchone()
    assert row == ("Company member", "manager", True, [], False, False, True, "selected")
    local = operator.execute(
        render(
            """SELECT u.id,u.password,u.is_superuser,u.is_staff,l.supabase_id
        FROM {documents}.authentication_user u JOIN {documents}.portal_documents_userlink l
        ON l.user_id=u.id""",
            runtime,
        )
    ).fetchall()
    assert len(local) == 1 and local[0][1:] == ("!", False, False, user)
    for area, table in (
        ("analytics", "web_warehouse_access"),
        ("documents", "portal_documents_grant"),
        ("documents", "commercial_invoice_grants"),
    ):
        assert (
            operator.execute(
                sql.SQL("SELECT count(*) FROM {}").format(
                    sql.Identifier(runtime.schema(area), table)
                )
            ).fetchone()[0]
            == 0
        )
    assert not identity.execute(
        render("SELECT {analytics}.company_identity_actor_is_admin(%s)", runtime), (user,)
    ).fetchone()[0]
    # A replay must preserve explicit administrator decisions and disabled state.
    operator.execute(
        render(
            """UPDATE {analytics}.web_users SET role='analyst',active=false,
        sections=ARRAY['sales'],is_portal_admin=true WHERE id=%s""",
            runtime,
        ),
        (user,),
    )
    assert provision(identity, runtime, user) == user
    assert operator.execute(
        render("SELECT role,active,sections FROM {analytics}.web_users", runtime)
    ).fetchone() == ("analyst", False, ["sales"])
    assert not identity.execute(
        render("SELECT {analytics}.company_identity_actor_is_admin(%s)", runtime), (user,)
    ).fetchone()[0]
    operator.execute(
        render("UPDATE {analytics}.web_users SET active=true WHERE id=%s", runtime), (user,)
    )
    assert identity.execute(
        render("SELECT {analytics}.company_identity_actor_is_admin(%s)", runtime), (user,)
    ).fetchone()[0]
    identity.execute(
        render("SELECT {analytics}.set_company_identity_password_required(%s,false)", runtime),
        (user,),
    )
    assert operator.execute(
        render("SELECT password_change_required FROM {analytics}.web_users", runtime)
    ).fetchone() == (False,)
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        identity.execute(
            render("SELECT {analytics}.set_company_identity_password_required(%s,true)", runtime),
            (user,),
        )
    for other_user, email, marker in (
        (user, "other@example.invalid", "test-provision"),
        (user, "member@example.invalid", "other-request"),
        (uuid4(), "member@example.invalid", "other-request"),
    ):
        with pytest.raises(psycopg.errors.UniqueViolation):
            provision(identity, runtime, other_user, email=email, marker=marker)
    assert operator.execute(
        render("SELECT count(*) FROM {analytics}.web_users", runtime)
    ).fetchone() == (1,)


def test_identity_and_tenant_roles_cannot_cross_boundary(identity_database):
    _, dsn, runtime, foreign, identity = identity_database
    for query in (
        "SELECT * FROM {analytics}.web_users",
        "SELECT * FROM {analytics}.portal_identity_metadata",
        "SELECT * FROM {documents}.authentication_user",
        "SELECT * FROM {analytics}.sources",
        "UPDATE {analytics}.web_users SET is_portal_admin=true",
        "TRUNCATE {analytics}.web_users",
        "CREATE TABLE {analytics}.forbidden(id integer)",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            identity.execute(render(query, runtime))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        provision(identity, foreign, uuid4())
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        identity.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(runtime.database_role)))
    with psycopg.connect(dsn, user=runtime.database_role, autocommit=True) as web:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            provision(web, runtime, uuid4())
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            web.execute(
                render("INSERT INTO {analytics}.portal_identity_metadata(id) VALUES(%s)", runtime),
                (uuid4(),),
            )


def test_global_actor_fks_removed_but_employee_subject_fks_remain(identity_database):
    operator, dsn, runtime, _, _ = identity_database
    owner = uuid4()
    with psycopg.connect(dsn, user=runtime.database_role, autocommit=True) as web:
        web.execute(
            "SELECT set_config(%s,%s,false)",
            (runtime.analytics_schema + ".assistant_user", str(owner)),
        )
        web.execute(
            render(
                """INSERT INTO {analytics}.employee_changes
            (id,user_id,employee_id,is_create,request_hash,fields,status)
            VALUES(%s,%s,%s,true,'test','{{}}','confirmed')""",
                runtime,
            ),
            (uuid4(), owner, uuid4()),
        )
        web.execute(
            render(
                """INSERT INTO {analytics}.assistant_conversations
            (id,user_id,access_hash,context,title) VALUES(%s,%s,'test','{{}}','Owner')""",
                runtime,
            ),
            (uuid4(), owner),
        )
        web.execute(
            render(
                "INSERT INTO {analytics}.sources(id,label,base_url,fingerprint) VALUES('primary','test','https://iiko.invalid',repeat('a',64))",
                runtime,
            )
        )
        web.execute(
            render(
                """INSERT INTO {analytics}.sales_report_sets
            (id,source_id,business_date,observed_at,checks,reviewed_by)
            VALUES(%s,'primary',current_date,now(),'{{}}',%s)""",
                runtime,
            ),
            (uuid4(), owner),
        )
        assert web.execute(
            render("SELECT count(*) FROM {analytics}.web_users", runtime)
        ).fetchone() == (0,)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            web.execute(
                render(
                    """INSERT INTO {analytics}.assistant_turns
                (id,conversation_id,user_id,question) VALUES(%s,%s,%s,'bad parent')""",
                    runtime,
                ),
                (uuid4(), uuid4(), owner),
            )
    preserved = operator.execute(
        """SELECT conname FROM pg_constraint c
        JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname=%s
        AND contype='f' AND confrelid=to_regclass(%s)""",
        (runtime.analytics_schema, runtime.analytics_schema + ".web_users"),
    ).fetchall()
    assert {r[0] for r in preserved} == {
        "portal_identity_metadata_id_fkey",
        "portal_account_initializations_user_id_fkey",
        "web_department_access_user_id_fkey",
        "web_warehouse_access_user_id_fkey",
    }


def test_identity_role_privilege_drift_rejected(identity_database):
    operator, _, runtime, _, _ = identity_database
    operator.execute(
        render(
            "GRANT SELECT ON {analytics}.web_users TO {identity_role}",
            runtime,
            identity_role=f"{runtime.key}_identity_runtime",
        )
    )
    with pytest.raises(MigrationError, match="Identity role"):
        provision_tenant(operator, runtime)


def test_non_superuser_routine_owner_and_force_rls(empty_database):
    operator, dsn, tenant = empty_database
    runtime = tenant()
    role = "identity_migrator_" + uuid4().hex
    operator.execute(
        sql.SQL("CREATE ROLE {} LOGIN CREATEROLE NOBYPASSRLS").format(sql.Identifier(role))
    )
    operator.execute(
        sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
            sql.Identifier(operator.info.dbname), sql.Identifier(role)
        )
    )
    try:
        with psycopg.connect(dsn, user=role, autocommit=True) as migrator:
            provision_tenant(migrator, runtime)
            assert provision_tenant(migrator, runtime) == ()
        with psycopg.connect(
            dsn, user=f"{runtime.key}_identity_runtime", autocommit=True
        ) as identity:
            assert provision(identity, runtime, uuid4())
        assert operator.execute(
            """SELECT bool_and(relrowsecurity AND relforcerowsecurity)
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND relname IN ('web_users','portal_identity_metadata')""",
            (runtime.analytics_schema,),
        ).fetchone() == (True,)
    finally:
        for area in ("documents", "analytics", "payments"):
            operator.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(runtime.schema(area))
                )
            )
        operator.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        operator.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
