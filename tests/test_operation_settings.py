"""Settings contracts in an explicitly selected disposable loopback database."""

import os
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import psycopg
import pytest
from fastapi import HTTPException
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pydantic import ValidationError

from app.operation_settings import (
    DEFAULT_SETTINGS,
    allowed_store,
    filter_stores,
    load_settings,
    settings_schema,
)
from app.web.operation_settings import OperationSettingsWrite, save_operation_settings
from tests.test_management import restricted as restricted

ADMIN, DEPARTMENT, SOURCE, TARGET = (UUID(int=i) for i in range(1, 5))


def create_catalog(db, schema):
    namespace = sql.Identifier(schema)
    db.execute(sql.SQL("CREATE SCHEMA {}").format(namespace))
    db.execute(
        sql.SQL(
            "CREATE TABLE {}.corporate_nodes "
            "(source_id text,id uuid,parent_id uuid,name text,type text)"
        ).format(namespace)
    )
    db.execute(
        sql.SQL("CREATE TABLE {}.stores (source_id text,id uuid,parent_id uuid,name text)").format(
            namespace
        )
    )
    db.execute(
        sql.SQL(
            "INSERT INTO {}.corporate_nodes VALUES ('primary',%s,NULL,'Venue','DEPARTMENT')"
        ).format(namespace),
        (DEPARTMENT,),
    )
    db.execute(
        sql.SQL(
            "INSERT INTO {}.stores VALUES ('primary',%s,%s,'Source'),('primary',%s,%s,'Target')"
        ).format(namespace),
        (SOURCE, DEPARTMENT, TARGET, DEPARTMENT),
    )
    db.execute(
        sql.SQL("CREATE TABLE {}.store_balance_reports (id uuid PRIMARY KEY)").format(namespace)
    )
    db.execute(
        sql.SQL("CREATE TABLE {}.rms_bindings (source_id text PRIMARY KEY)").format(namespace)
    )
    for table in ("sales_reports", "raw_snapshots"):
        db.execute(
            sql.SQL("CREATE TABLE {}.{} (id uuid PRIMARY KEY)").format(
                namespace, sql.Identifier(table)
            )
        )
    for table in (
        "sales_report_rows",
        "store_balance_items",
        "counteragent_balance_items",
        "cash_shift_observations",
    ):
        db.execute(
            sql.SQL("CREATE TABLE {}.{} (id uuid PRIMARY KEY)").format(
                namespace, sql.Identifier(table)
            )
        )

    db.execute(
        sql.SQL("ALTER TABLE {}.raw_snapshots ADD COLUMN observed_at timestamptz").format(namespace)
    )
    db.execute(
        sql.SQL("ALTER TABLE {}.cash_shift_observations ADD COLUMN snapshot_id uuid").format(
            namespace
        )
    )
    db.execute(
        sql.SQL(
            "CREATE TABLE {}.cash_shift_days "
            "(source_id text,last_snapshot_id uuid,observed_at timestamptz)"
        ).format(namespace)
    )
    db.execute(
        sql.SQL(
            "CREATE VIEW {}.cash_shifts AS SELECT o.*,d.source_id,d.observed_at AS last_seen_at,"
            "d.last_snapshot_id FROM {}.cash_shift_observations o "
            "JOIN {}.cash_shift_days d ON d.last_snapshot_id=o.snapshot_id"
        ).format(namespace, namespace, namespace)
    )


@pytest.fixture(scope="module")
def connection():
    dsn = os.environ.get("CHAIKA_DOCUMENTS_TEST_DSN") or os.environ.get("CHAIKA_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable local PostgreSQL required")
    if conninfo_to_dict(dsn).get("host") not in {"127.0.0.1", "localhost"}:
        pytest.fail("Only explicitly configured loopback databases are allowed")
    with psycopg.connect(dsn, autocommit=True) as admin:
        for role in ("anon", "authenticated", "chaika_backend", "chaika_iiko_app"):
            if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
        if not admin.execute(
            "SELECT 1 FROM pg_database WHERE datname='operations_settings_tests'"
        ).fetchone():
            admin.execute("CREATE DATABASE operations_settings_tests")
    with psycopg.connect(
        make_conninfo(dsn, dbname="operations_settings_tests"),
        autocommit=True,
        row_factory=dict_row,
    ) as db:
        db.execute("DROP SCHEMA IF EXISTS chaika CASCADE")
        create_catalog(db, "chaika")
        db.execute(Path("supabase/migrations/20261016123000_operation_settings.sql").read_text())
        db.execute(Path("migrations/documents/0011_operation_settings.sql").read_text())
        yield db


@pytest.fixture
def database(connection):
    with connection.transaction(force_rollback=True):
        yield connection


class TestAdministration:
    __test__ = False

    def __init__(self, db):
        self.db = db

    @contextmanager
    def write(self, actor):
        with self.db.transaction():
            yield self.db


def payload(**changes):
    settings = {
        k: deepcopy(DEFAULT_SETTINGS[k]) for k in ("revision", "sync", "writeoff", "waybill")
    }
    settings.update(changes)
    return OperationSettingsWrite.model_validate(settings)


def test_absent_row_defaults_all_and_results_are_not_shared(database):
    first = load_settings(database)
    first["writeoff"]["mode"] = "selected"
    assert load_settings(database)["writeoff"]["mode"] == "all"
    assert allowed_store(database, "writeoff", SOURCE)
    assert allowed_store(database, "waybill", TARGET)


def test_selected_empty_and_columns_are_independent_with_actual_actor_audit(database):
    actor = SimpleNamespace(auth_user_id=ADMIN)
    result = save_operation_settings(
        TestAdministration(database),
        actor,
        payload(
            sync={"mode": "selected", "department_ids": [str(DEPARTMENT)]},
            writeoff={"mode": "selected", "store_ids": []},
            waybill={"mode": "selected", "store_ids": [str(SOURCE)]},
        ),
    )
    assert result["revision"] == 1
    assert result["updated_by"] == str(ADMIN)
    assert not allowed_store(database, "writeoff", SOURCE)
    assert allowed_store(database, "waybill", SOURCE)
    assert not allowed_store(database, "waybill", TARGET)
    stores = [{"id": SOURCE}, {"id": TARGET}]
    assert filter_stores(database, "waybill", stores) == [stores[0]]
    assert filter_stores(database, "writeoff", stores) == []
    assert result["stores"][0]["department_id"] == str(DEPARTMENT)
    audit = database.execute("SELECT * FROM chaika.operation_settings_audit").fetchone()
    assert audit["actor_id"] == ADMIN
    assert audit["before_settings"]["writeoff"]["mode"] == "all"
    assert audit["after_settings"]["writeoff"]["store_ids"] == []


def test_stale_revision_does_not_overwrite_or_add_audit(database):
    administration = TestAdministration(database)
    save_operation_settings(administration, ADMIN, payload())
    with pytest.raises(HTTPException) as error:
        save_operation_settings(administration, ADMIN, payload())
    assert error.value.status_code == 409
    assert load_settings(database)["revision"] == 1
    assert (
        database.execute("SELECT count(*) AS n FROM chaika.operation_settings_audit").fetchone()[
            "n"
        ]
        == 1
    )


@pytest.mark.parametrize(
    "field,key", [("sync", "department_ids"), ("writeoff", "store_ids"), ("waybill", "store_ids")]
)
def test_unknown_ids_rejected_before_persistence(database, field, key):
    with pytest.raises(HTTPException) as error:
        save_operation_settings(
            TestAdministration(database),
            ADMIN,
            payload(
                **{
                    field: {"mode": "selected", key: [str(UUID(int=999))]},
                }
            ),
        )
    assert error.value.status_code == 422
    assert load_settings(database)["revision"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": True},
        {"writeoff": {"mode": "all", "store_ids": [str(SOURCE)]}},
        {"waybill": {"mode": "selected", "store_ids": [str(SOURCE), str(SOURCE)]}},
        {"sync": {"mode": "selected", "department_ids": [], "unexpected": True}},
    ],
)
def test_invalid_selection_contract(changes):
    with pytest.raises(ValidationError):
        payload(**changes)


def test_missing_migration_does_not_enable_defaults(database):
    database.execute("ALTER TABLE chaika.operation_settings RENAME TO operation_settings_missing")
    with pytest.raises(psycopg.errors.UndefinedTable), database.transaction():
        load_settings(database)


def test_document_role_can_read_but_cannot_update_settings(database):
    save_operation_settings(
        TestAdministration(database),
        ADMIN,
        payload(
            writeoff={"mode": "selected", "store_ids": []},
        ),
    )
    database.execute("SET LOCAL ROLE chaika_iiko_app")
    assert not allowed_store(database, "writeoff", SOURCE)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.transaction():
        database.execute("UPDATE chaika.operation_settings SET revision=99")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.transaction():
        database.execute("SELECT * FROM chaika.operation_settings_audit")


def test_settings_schema_uses_connection_context():
    runtime = SimpleNamespace(schema=lambda area: "company_own" if area == "analytics" else None)
    assert settings_schema(SimpleNamespace(tenant_runtime=runtime)) == "company_own"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_operation_settings_http_requires_admin(restricted, method):
    client, repo = restricted
    repo.sections = ["sales", "transfers", "writeoffs"]
    result = client.request(
        method,
        "/api/management/operation-settings",
        json=payload().model_dump(mode="json") if method == "POST" else None,
    )
    assert result.status_code == 403


def test_operation_settings_http_checks_origin_before_storage(restricted):
    client, repo = restricted
    repo.admin = True
    result = client.post(
        "/api/management/operation-settings",
        json=payload().model_dump(mode="json"),
        headers={"Origin": "https://attacker.invalid"},
    )
    assert result.status_code == 403


def test_sync_policy_change_reports_busy_without_a_partial_save(database):
    from app.sync_references import LOCK_ID

    with psycopg.connect(database.info.dsn, autocommit=True) as worker:
        worker.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            with pytest.raises(HTTPException) as error:
                save_operation_settings(
                    TestAdministration(database),
                    ADMIN,
                    payload(
                        sync={"mode": "selected", "department_ids": []},
                        writeoff={"mode": "selected", "store_ids": []},
                    ),
                )
            assert error.value.status_code == 423
            assert load_settings(database)["revision"] == 0
        finally:
            worker.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


def test_database_rejects_incomplete_settings_json(database):
    from psycopg.types.json import Jsonb

    invalid = {"sync": {}, "writeoff": {}, "waybill": {}}
    with pytest.raises(psycopg.errors.CheckViolation), database.transaction():
        database.execute(
            "INSERT INTO chaika.operation_settings(singleton,revision,settings,updated_by) "
            "VALUES(true,1,%s,%s)",
            (Jsonb(invalid), ADMIN),
        )
    assert load_settings(database)["revision"] == 0


def test_authenticated_admin_http_roundtrip_uses_real_persistence_and_rechecks_admin(
    database, restricted
):
    client, repo = restricted
    repo.admin = True
    repo.runtime = SimpleNamespace(mode="legacy")
    database.execute(
        "CREATE TABLE chaika.web_users (id uuid PRIMARY KEY,is_portal_admin boolean,active boolean)"
    )
    database.execute("INSERT INTO chaika.web_users VALUES(%s,true,true)", (ADMIN,))

    @contextmanager
    def connection(**kwargs):
        yield database

    repo.connection = connection
    repo._pool = SimpleNamespace(open=lambda: None, connection=connection)
    initial = client.get("/api/management/operation-settings")
    assert initial.status_code == 200 and initial.json()["revision"] == 0
    command = payload(writeoff={"mode": "selected", "store_ids": []}).model_dump(mode="json")
    response = client.post(
        "/api/management/operation-settings",
        json=command,
        headers={"Origin": "http://127.0.0.1:8013"},
    )
    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert response.json()["updated_by"] == str(ADMIN)
    assert client.get("/api/management/operation-settings").json()["writeoff"] == {
        "mode": "selected",
        "store_ids": [],
    }
    assert (
        client.post(
            "/api/management/operation-settings",
            json=command,
            headers={"Origin": "http://127.0.0.1:8013"},
        ).status_code
        == 409
    )
    # Even a previously authenticated admin loses writes when DB authority is revoked.
    database.execute("UPDATE chaika.web_users SET is_portal_admin=false WHERE id=%s", (ADMIN,))
    command["revision"] = 1
    assert (
        client.post(
            "/api/management/operation-settings",
            json=command,
            headers={"Origin": "http://127.0.0.1:8013"},
        ).status_code
        == 403
    )
    assert load_settings(database)["revision"] == 1
