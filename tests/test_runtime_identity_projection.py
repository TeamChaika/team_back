"""Real PostgreSQL acceptance for central primary-admin identity projection."""

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.saas_admin.company_accounts import IdentityTarget
from app.saas_admin.provisioning import PendingCheck
from app.tenancy.migrations import provision_tenant
from app.tenancy.sql import render
from tests.test_tenant_migrations_postgres import empty_database as empty_database


@pytest.fixture
def projection_database(empty_database):
    db, dsn, tenant = empty_database
    runtime = tenant()
    provision_tenant(db, runtime)
    user, marker = uuid4(), uuid4()
    email = "primary@example.invalid"
    db.execute("CREATE SCHEMA restcontrol")
    db.execute("""CREATE TABLE restcontrol.companies (
        id uuid PRIMARY KEY, version integer, body jsonb, archived_at text, status text);
        CREATE TABLE restcontrol.memberships (
        id uuid PRIMARY KEY, company_id uuid, auth_user_id uuid, username text,
        display_name text, role text, is_primary_admin boolean, active boolean,
        auth_exclusive boolean, must_change boolean);
        CREATE TABLE restcontrol.auth_identities (
        id uuid PRIMARY KEY, email text, provision_id text);
        CREATE TABLE restcontrol.auth_provisioning (
        company_id uuid PRIMARY KEY, request_id uuid, username text,
        state text, auth_user_id uuid);
        CREATE TABLE restcontrol.platform_memberships (
        id uuid PRIMARY KEY, auth_user_id uuid, active boolean);""")
    company = {"id": str(runtime.company_id), "version": 1, "status": "active"}
    db.execute(
        "INSERT INTO restcontrol.companies VALUES (%s,1,%s,NULL,'active')",
        (runtime.company_id, Jsonb(company)),
    )
    db.execute(
        """INSERT INTO restcontrol.memberships VALUES
        (%s,%s,%s,%s,'Primary Admin','company_admin',true,true,true,false)""",
        (uuid4(), runtime.company_id, user, email),
    )
    db.execute(
        "INSERT INTO restcontrol.auth_identities VALUES (%s,%s,%s)", (user, email, str(marker))
    )
    db.execute(
        "INSERT INTO restcontrol.auth_provisioning VALUES (%s,%s,%s,'complete',%s)",
        (runtime.company_id, marker, email, user),
    )

    @contextmanager
    def connect(write=False):
        with psycopg.connect(dsn, row_factory=dict_row) as central:
            central.execute("SET LOCAL search_path TO restcontrol,pg_catalog")
            yield central

    operator = SimpleNamespace(
        runtime=runtime,
        company=company,
        repo=SimpleNamespace(connect=connect),
        config={
            "runtime_dsn": make_conninfo(dsn, user=runtime.database_role),
            "identity_dsn": make_conninfo(dsn, user=f"{runtime.key}_identity_runtime"),
        },
    )
    yield SimpleNamespace(
        db=db,
        dsn=dsn,
        tenant=tenant,
        runtime=runtime,
        operator=operator,
        user=user,
        marker=marker,
        email=email,
    )


def project(env):
    from app.saas_admin.runtime_identity import ensure_company_identities

    return ensure_company_identities(env.operator)


def profile(env):
    return env.db.execute(
        render(
            """SELECT role,active,sections,is_portal_admin,
        all_departments,password_change_required,warehouse_scope_mode
        FROM {analytics}.web_users WHERE id=%s""",
            env.runtime,
        ),
        (env.user,),
    ).fetchone()


@pytest.mark.parametrize("must_change", [False, True])
def test_proven_primary_projects_own_profile_and_document_identity(
    projection_database, must_change
):
    env = projection_database
    env.db.execute("UPDATE restcontrol.memberships SET must_change=%s", (must_change,))
    assert project(env) == {"registered_members": 1, "projected_primary_admins": 1}
    row = profile(env)
    assert row[:2] == ("manager", True)
    assert len(row[2]) == 16 and "finance" not in row[2]
    assert row[3:] == (True, True, must_change, "all")
    local = env.db.execute(
        render(
            """SELECT u.password,u.is_superuser,u.is_staff,l.supabase_id
        FROM {documents}.authentication_user u JOIN {documents}.portal_documents_userlink l
        ON l.user_id=u.id""",
            env.runtime,
        )
    ).fetchall()
    assert local == [("!", False, False, env.user)]
    assert project(env) == {"registered_members": 1, "projected_primary_admins": 0}


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE restcontrol.auth_identities SET email='other@example.invalid'",
        "UPDATE restcontrol.auth_identities SET provision_id='not-a-uuid'",
        "UPDATE restcontrol.auth_identities SET "
        "provision_id='11111111-1111-4111-8111-111111111111'",
        "UPDATE restcontrol.auth_provisioning SET "
        "auth_user_id='11111111-1111-4111-8111-111111111111'",
        "UPDATE restcontrol.auth_provisioning SET username='other@example.invalid'",
        "UPDATE restcontrol.auth_provisioning SET state='pending'",
        "DELETE FROM restcontrol.auth_provisioning",
        "DELETE FROM restcontrol.auth_identities",
        "UPDATE restcontrol.memberships SET auth_exclusive=false",
        "UPDATE restcontrol.memberships SET role='employee'",
        "UPDATE restcontrol.memberships SET is_primary_admin=false",
        "UPDATE restcontrol.companies SET version=2",
        "UPDATE restcontrol.companies SET status='suspended'",
        "UPDATE restcontrol.companies SET archived_at='2026-10-09'",
    ],
)
def test_unproven_or_nonprimary_missing_profile_is_rejected(projection_database, mutation):
    env = projection_database
    env.db.execute(mutation)
    with pytest.raises(PendingCheck):
        project(env)
    assert profile(env) is None
    assert env.db.execute(
        render("SELECT count(*) FROM {documents}.authentication_user", env.runtime)
    ).fetchone() == (0,)


@pytest.mark.parametrize("active", [False, True])
def test_platform_member_never_becomes_company_primary(projection_database, active):
    env = projection_database
    env.db.execute(
        "INSERT INTO restcontrol.platform_memberships VALUES (%s,%s,%s)",
        (uuid4(), env.user, active),
    )
    with pytest.raises(PendingCheck):
        project(env)
    assert profile(env) is None


def test_existing_profile_replay_preserves_acl_disabled_and_password_state(projection_database):
    env = projection_database
    target = IdentityTarget(env.runtime, env.operator.config["identity_dsn"])
    target.provision(env.user, env.email, str(env.marker), "Existing")
    env.db.execute(
        render(
            """UPDATE {analytics}.web_users SET role='analyst',active=false,
        sections=ARRAY['sales'],is_portal_admin=false,all_departments=false,
        password_change_required=false,warehouse_scope_mode='selected' WHERE id=%s""",
            env.runtime,
        ),
        (env.user,),
    )
    env.db.execute("UPDATE restcontrol.memberships SET must_change=true")
    before = profile(env)
    assert project(env) == {"registered_members": 1, "projected_primary_admins": 0}
    assert profile(env) == before


def test_inactive_membership_does_not_project(projection_database):
    env = projection_database
    env.db.execute("UPDATE restcontrol.memberships SET active=false")
    assert project(env) == {"registered_members": 0, "projected_primary_admins": 0}
    assert profile(env) is None


@pytest.mark.parametrize("marker_change", ["local_mismatch", "central_missing", "central_empty"])
def test_existing_primary_requires_matching_marker_without_profile_mutation(
    projection_database, marker_change
):
    env = projection_database
    assert project(env) == {"registered_members": 1, "projected_primary_admins": 1}
    before = profile(env)
    if marker_change == "local_mismatch":
        env.db.execute(
            render(
                "UPDATE {analytics}.portal_identity_metadata SET provision_id=%s WHERE id=%s",
                env.runtime,
            ),
            (str(uuid4()), env.user),
        )
    else:
        env.db.execute(
            "UPDATE restcontrol.auth_identities SET provision_id=%s WHERE id=%s",
            (None if marker_change == "central_missing" else "", env.user),
        )
    with pytest.raises(PendingCheck):
        project(env)
    assert profile(env) == before


def test_identity_role_cannot_project_foreign_primary(projection_database):
    env = projection_database
    foreign = env.tenant()
    provision_tenant(env.db, foreign)
    with psycopg.connect(env.operator.config["identity_dsn"], autocommit=True) as identity:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            identity.execute(
                render(
                    "SELECT {analytics}.provision_company_primary_admin(%s,%s,%s,%s,%s)", foreign
                ),
                (env.user, env.email, str(env.marker), "Primary", False),
            )
