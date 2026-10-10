"""Real empty-DB baseline; explicitly configured disposable local PostgreSQL only."""

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.tenancy.migrations import (
    MIGRATION_DIRECTORY,
    MigrationError,
    load_migrations,
    provision_tenant,
)
from app.tenancy.sql import render, validate_database_runtime
from tests.test_tenant_migrations import runtime_for


@pytest.fixture
def empty_database():
    dsn = os.environ.get("RESTCONTROL_MIGRATIONS_TEST_DSN")
    if not dsn:
        pytest.skip("Set RESTCONTROL_MIGRATIONS_TEST_DSN to a disposable local cluster")
    if conninfo_to_dict(dsn).get("host") not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail("Migration tests require an explicit loopback PostgreSQL host")
    name = "tenant_baseline_test_" + uuid4().hex
    admin = psycopg.connect(dsn, autocommit=True)
    admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    isolated_dsn = make_conninfo(dsn, dbname=name)
    operator = psycopg.connect(isolated_dsn, autocommit=True)
    tenants = []

    def tenant():
        runtime = runtime_for(uuid4())
        tenants.append(runtime)
        return runtime

    try:
        yield operator, isolated_dsn, tenant
    finally:
        operator.close()
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        for runtime in tenants:
            admin.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(runtime.database_role))
            )
            admin.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(
                    sql.Identifier(f"{runtime.key}_payments_runtime")
                )
            )
        for runtime in tenants:
            admin.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(
                    sql.Identifier(f"{runtime.key}_identity_runtime")
                )
            )
        admin.close()


def count_rows(db, runtime, area, table):
    return db.execute(
        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(runtime.schema(area), table))
    ).fetchone()[0]


def test_empty_database_two_tenant_duplicate_ids_and_role_isolation(empty_database):
    operator, dsn, tenant = empty_database
    first, second = tenant(), tenant()
    for runtime in (first, second):
        assert len(provision_tenant(operator, runtime)) == len(load_migrations())
        assert provision_tenant(operator, runtime) == ()
        tables = operator.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=ANY(%s) "
            "AND tablename <> '_tenant_migrations'",
            ([runtime.analytics_schema, runtime.documents_schema],),
        ).fetchall()
        assert len(tables) >= 95
        for area in ("analytics", "documents"):
            for (name,) in operator.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=%s", (runtime.schema(area),)
            ).fetchall():
                if name != "_tenant_migrations":
                    assert count_rows(operator, runtime, area, name) == 0
        assert (
            operator.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname=%s", (runtime.payments_schema,)
            ).fetchone()[0]
            == 12
        )
    for runtime in (first, second):
        with psycopg.connect(
            dsn, user=f"{runtime.key}_payments_runtime", autocommit=True
        ) as payment:
            assert count_rows(payment, runtime, "payments", "terminal_versions") == 0
            for query, target in (
                ("SELECT * FROM {analytics}.web_users", runtime),
                ("SELECT * FROM {documents}.authentication_user", runtime),
                (
                    "SELECT * FROM {payments}.terminal_versions",
                    second if runtime == first else first,
                ),
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    payment.execute(render(query, target))
        with psycopg.connect(dsn, user=runtime.database_role, autocommit=True) as core:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                core.execute(render("SELECT * FROM {payments}.terminal_versions", runtime))
    user, operation, store = uuid4(), uuid4(), uuid4()
    for runtime, label in ((first, "first tenant"), (second, "second tenant")):
        with psycopg.connect(dsn, user=runtime.database_role, autocommit=True) as db:
            validate_database_runtime(db, runtime)
            db.execute(
                render(
                    "INSERT INTO {analytics}.sources(id,label,base_url,fingerprint) "
                    "VALUES(%s,%s,%s,%s)",
                    runtime,
                ),
                ("primary", label, "https://same-iiko.example", "a" * 64),
            )
            db.execute(
                render(
                    "INSERT INTO {analytics}.web_users(id,display_name,role,sections) "
                    "VALUES(%s,%s,'owner',ARRAY['transfers'])",
                    runtime,
                ),
                (user, label),
            )
            db.execute(
                render(
                    """INSERT INTO {documents}.authentication_user
                (id,password,is_superuser,username,first_name,last_name,email,is_staff,is_active,date_joined)
                VALUES(1,'!',false,'same-user','','','',false,true,now())""",
                    runtime,
                )
            )
            db.execute(
                render("INSERT INTO {documents}.stores(id,name) VALUES(%s,%s)", runtime),
                (store, "same store"),
            )
            db.execute(
                render(
                    """INSERT INTO {documents}.waybills
                (id,comment,status,created_at,counteragent_id,created_by_id,store_id,submission_state,version)
                VALUES(1,%s,'Created',now(),%s,1,%s,'draft',1)""",
                    runtime,
                ),
                (label, store, store),
            )
            db.execute(
                render(
                    """INSERT INTO {documents}.portal_documents_operation
                (id,kind,document_id,action,fingerprint,state,result,created_at,actor_id)
                VALUES(%s,'waybill',1,'create','same','done','{{}}',now(),1)""",
                    runtime,
                ),
                (operation,),
            )
            db.execute(
                render(
                    "INSERT INTO {documents}.native_dispatch "
                    "(operation_id,kind,document_id,version,payload) "
                    "VALUES(%s,'waybill',1,1,'{{}}')",
                    runtime,
                ),
                (operation,),
            )
            db.execute(
                render(
                    "INSERT INTO {documents}.commercial_invoices "
                    "(id,kind,number,store_id,created_by_id,snapshot) "
                    "VALUES(%s,'sale','SAME-1',%s,1,'{{}}')",
                    runtime,
                ),
                (operation, store),
            )
            assert (
                db.execute(render("SELECT label FROM {analytics}.sources", runtime)).fetchone()[0]
                == label
            )
            assert (
                db.execute(render("SELECT comment FROM {documents}.waybills", runtime)).fetchone()[
                    0
                ]
                == label
            )
            assert db.execute(
                render("SELECT sections FROM {documents}.portal_access", runtime)
            ).fetchone()[0] == ["transfers"]
            assert (
                db.execute(
                    render("SELECT * FROM {analytics}.portal_identities", runtime)
                ).fetchall()
                == []
            )
            foreign = second if runtime == first else first
            for query in (
                "SELECT * FROM {analytics}.sources",
                "SELECT * FROM {documents}.waybills",
                "INSERT INTO {analytics}.sources(id,label,base_url,fingerprint) "
                "VALUES('other','x','x','x')",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    db.execute(render(query, foreign))
            for query in (
                "DELETE FROM {analytics}.raw_snapshots",
                "DELETE FROM {documents}.portal_documents_event",
                "SELECT * FROM {analytics}._tenant_migrations",
                "CREATE TABLE {documents}.forbidden(id integer)",
                "ALTER TABLE {documents}.waybills DISABLE ROW LEVEL SECURITY",
                "INSERT INTO {analytics}.portal_identity_metadata(id) VALUES(NULL)",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    db.execute(render(query, runtime))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(foreign.database_role)))
        assert provision_tenant(operator, runtime) == ()
        assert count_rows(operator, runtime, "documents", "waybills") == 1


def test_checksum_and_sql_failure_rollback(empty_database, tmp_path):
    operator, _, tenant = empty_database
    existing, broken = tenant(), tenant()
    provision_tenant(operator, existing)
    directory = tmp_path / "migrations"
    shutil.copytree(MIGRATION_DIRECTORY, directory)
    path = directory / load_migrations(directory)[2].name
    path.write_text(path.read_text() + "\nSELECT 1/0;\n")
    with pytest.raises(MigrationError, match="checksum"):
        provision_tenant(operator, existing, directory=directory)
    assert provision_tenant(operator, existing) == ()
    with pytest.raises(psycopg.errors.DivisionByZero):
        provision_tenant(operator, broken, directory=directory)
    assert (
        operator.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (broken.database_role,)
        ).fetchone()
        is None
    )
    assert (
        operator.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname=ANY(%s)",
            ([broken.schema(a) for a in ("analytics", "documents", "payments")],),
        ).fetchone()
        is None
    )
    assert len(provision_tenant(operator, broken)) == len(load_migrations())


def test_concurrent_provisioning_single_committed_history(empty_database):
    operator, dsn, tenant = empty_database
    runtime = tenant()
    barrier = Barrier(2)

    def run(_):
        with psycopg.connect(dsn, autocommit=True) as db:
            barrier.wait(timeout=10)
            return provision_tenant(db, runtime)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, range(2)))
    assert sorted(map(len, results)) == [0, len(load_migrations())]
    for area in ("analytics", "documents", "payments"):
        assert count_rows(operator, runtime, area, "_tenant_migrations") == sum(
            migration.area == area for migration in load_migrations()
        )


def test_refuses_schema_adoption_and_privileged_role(empty_database):
    operator, _, tenant = empty_database
    adopted, privileged = tenant(), tenant()
    operator.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(adopted.analytics_schema)))
    with pytest.raises(MigrationError, match="without a tenant journal"):
        provision_tenant(operator, adopted)
    operator.execute(
        sql.SQL("CREATE ROLE {} BYPASSRLS").format(sql.Identifier(privileged.database_role))
    )
    with pytest.raises(MigrationError, match="privileged"):
        provision_tenant(operator, privileged)
    assert (
        operator.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname=%s", (privileged.analytics_schema,)
        ).fetchone()
        is None
    )


def test_non_superuser_migration_owner_and_invoker_views(empty_database):
    operator, dsn, tenant = empty_database
    runtime = tenant()
    owner = "tenant_migrator_test_" + uuid4().hex
    owner_identifier = sql.Identifier(owner)
    operator.execute(
        sql.SQL("CREATE ROLE {} LOGIN CREATEROLE NOBYPASSRLS").format(owner_identifier)
    )
    operator.execute(
        sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
            sql.Identifier(operator.info.dbname), owner_identifier
        )
    )
    try:
        with psycopg.connect(dsn, user=owner, autocommit=True) as migrator:
            assert len(provision_tenant(migrator, runtime)) == len(load_migrations())
            assert provision_tenant(migrator, runtime) == ()
        with psycopg.connect(dsn, user=runtime.database_role, autocommit=True) as client:
            validate_database_runtime(client, runtime)
            client.execute(
                render(
                    "INSERT INTO {analytics}.web_users(id,display_name,role) "
                    "VALUES(%s,'visible via invoker','owner')",
                    runtime,
                ),
                (uuid4(),),
            )
            assert (
                client.execute(
                    render("SELECT count(*) FROM {analytics}.portal_warehouse_access", runtime)
                ).fetchone()[0]
                == 1
            )
            assert (
                client.execute(
                    render("SELECT count(*) FROM {documents}.portal_access", runtime)
                ).fetchone()[0]
                == 1
            )
    finally:
        for area in ("documents", "analytics", "payments"):
            operator.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(runtime.schema(area))
                )
            )
        operator.execute(sql.SQL("DROP OWNED BY {}").format(owner_identifier))
        operator.execute(sql.SQL("DROP ROLE {}").format(owner_identifier))


def test_primary_admin_migration_upgrades_existing_journal_once(empty_database, tmp_path):
    import json

    operator, _dsn, tenant = empty_database
    runtime = tenant()
    manifest = json.loads((MIGRATION_DIRECTORY / "manifest.json").read_text())
    migration_file = "20261010100000_tenant_primary_admin_projection.sql"
    primary_index = next(
        index
        for index, entry in enumerate(manifest["migrations"])
        if entry["file"] == migration_file
    )
    old = dict(manifest, migrations=manifest["migrations"][:primary_index])
    (tmp_path / "manifest.json").write_text(json.dumps(old))
    for entry in old["migrations"]:
        shutil.copyfile(MIGRATION_DIRECTORY / entry["file"], tmp_path / entry["file"])
    assert len(provision_tenant(operator, runtime, directory=tmp_path)) == len(old["migrations"])
    applied = provision_tenant(operator, runtime)
    assert applied == tuple(entry["file"] for entry in manifest["migrations"][primary_index:])
    assert provision_tenant(operator, runtime) == ()
