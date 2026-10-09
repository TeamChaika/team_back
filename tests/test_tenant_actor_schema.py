"""Platform document attribution on disposable PostgreSQL, without local users."""

from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.tenancy.migrations import provision_tenant
from app.tenancy.sql import render
from tests.test_tenant_migrations_postgres import empty_database as empty_database


@pytest.fixture
def actor_database(empty_database):
    operator, dsn, tenant = empty_database
    runtime = tenant()
    provision_tenant(operator, runtime)
    with psycopg.connect(dsn, user=runtime.database_role, autocommit=True) as db:
        store = uuid4()
        db.execute(
            render("INSERT INTO {documents}.stores(id,name) VALUES(%s,'test')", runtime), (store,)
        )
        actor = dict(
            company_id=str(runtime.company_id),
            auth_user_id=str(uuid4()),
            kind="platform_owner",
            display_name="Platform operator",
            membership_id=None,
        )
        yield db, runtime, store, actor


def insert_document(db, runtime, store, actor, *, local_id=None, table="waybills"):
    extra = ",counteragent_id" if table == "waybills" else ""
    value = ",%s" if extra else ""
    query = f"""INSERT INTO {{documents}}.{table}
        (status,created_at,store_id,submission_state,version,created_by_id,created_actor{extra})
        VALUES('Created',now(),%s,'draft',1,%s,%s{value}) RETURNING id"""
    params = (store, local_id, Jsonb(actor) if actor is not None else None)
    if extra:
        params += (store,)
    return db.execute(render(query, runtime), params).fetchone()[0]


def test_owner_document_and_audit_need_no_local_identity(actor_database):
    db, runtime, store, actor = actor_database
    waybill = insert_document(db, runtime, store, actor)
    insert_document(db, runtime, store, actor, table="writeoffs")
    db.execute(
        render(
            """UPDATE {documents}.waybills
        SET processed_actor=%s WHERE id=%s""",
            runtime,
        ),
        (Jsonb(actor), waybill),
    )
    db.execute(
        render(
            """INSERT INTO {documents}.portal_documents_event
        (kind,document_id,version,action,data,created_at,actor)
        VALUES('waybill',%s,1,'create','{{}}',now(),%s)""",
            runtime,
        ),
        (waybill, Jsonb(actor)),
    )
    db.execute(
        render(
            """INSERT INTO {documents}.portal_documents_operation
        (id,kind,document_id,action,fingerprint,state,result,created_at,actor)
        VALUES(%s,'waybill',%s,'create','test','done','{{}}',now(),%s)""",
            runtime,
        ),
        (uuid4(), waybill, Jsonb(actor)),
    )
    invoice = uuid4()
    db.execute(
        render(
            """INSERT INTO {documents}.commercial_invoices
        (id,kind,number,store_id,snapshot,created_actor)
        VALUES(%s,'sale','TEST-1',%s,'{{}}',%s)""",
            runtime,
        ),
        (invoice, store, Jsonb(actor)),
    )
    db.execute(
        render(
            """INSERT INTO {documents}.commercial_invoice_operations
        (request_id,fingerprint,document_id,result,actor)
        VALUES(%s,'test',%s,'{{}}',%s)""",
            runtime,
        ),
        (uuid4(), invoice, Jsonb(actor)),
    )
    db.execute(
        render(
            """INSERT INTO {documents}.commercial_invoice_events
        (document_id,version,action,actor) VALUES(%s,1,'create',%s)""",
            runtime,
        ),
        (invoice, Jsonb(actor)),
    )
    # Worker/system events already allowed an absent actor; retain that contract.
    db.execute(
        render(
            """INSERT INTO {documents}.commercial_invoice_events
        (document_id,version,action) VALUES(%s,1,'processed')""",
            runtime,
        ),
        (invoice,),
    )
    db.execute(
        render(
            """INSERT INTO {documents}.commercial_counterparty_operations
        (id,portal_id,kind,fingerprint,source_key,iiko_id,code,payload,identity_key,actor)
        VALUES(%s,%s,'sale','test','primary',%s,'TEST','{{}}','test',%s)""",
            runtime,
        ),
        (uuid4(), actor["auth_user_id"], uuid4(), Jsonb(actor)),
    )
    db.execute(
        render(
            """INSERT INTO {documents}.native_actor_audit
        (company_id,actor_uuid,actor_kind,actor_display_name,action,object_kind,object_id)
        VALUES(%s,%s,'platform_owner','Platform operator','create','waybill',%s)""",
            runtime,
        ),
        (runtime.company_id, actor["auth_user_id"], str(waybill)),
    )
    assert (
        db.execute(
            render("SELECT count(*) FROM {documents}.authentication_user", runtime)
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            render("SELECT count(*) FROM {documents}.native_actor_audit", runtime)
        ).fetchone()[0]
        == 1
    )
    for statement in (
        "UPDATE {documents}.native_actor_audit SET action='forged'",
        "DELETE FROM {documents}.native_actor_audit",
        "TRUNCATE {documents}.native_actor_audit",
        "ALTER TABLE {documents}.native_actor_audit DISABLE ROW LEVEL SECURITY",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute(render(statement, runtime))


@pytest.mark.parametrize(
    "change",
    [
        None,
        {},
        {"company_id": str(uuid4())},
        {"auth_user_id": str(uuid4()).replace("-", "")},
        {"auth_user_id": "00000000-0000-0000-0000-000000000000"},
        {"display_name": " "},
        {"display_name": "\t\n"},
        {"display_name": 12},
        {"kind": "owner"},
        {"kind": "company_member"},
        {"membership_id": str(uuid4())},
        {"auth_user_id": None},
        {"company_id": None},
        {"membership_id": "invalid"},
    ],
)
def test_missing_or_invalid_global_actor_rejected(actor_database, change):
    db, runtime, store, actor = actor_database
    snapshot = None if change is None else ({} if not change else {**actor, **change})
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_document(db, runtime, store, snapshot)


def test_local_identity_compatibility_and_foreign_key_preserved(actor_database):
    db, runtime, store, actor = actor_database
    db.execute(
        render(
            """INSERT INTO {documents}.authentication_user
        (id,password,is_superuser,username,first_name,last_name,email,is_staff,is_active,date_joined)
        VALUES(1,'!',false,'local','','','',false,true,now())""",
            runtime,
        )
    )
    insert_document(db, runtime, store, None, local_id=1)
    member = {**actor, "kind": "company_member", "membership_id": str(uuid4())}
    insert_document(db, runtime, store, member, local_id=1)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert_document(db, runtime, store, None, local_id=999)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_document(db, runtime, store, {**member, "company_id": str(uuid4())}, local_id=1)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            render(
                """INSERT INTO {documents}.native_actor_audit
            (company_id,actor_uuid,actor_kind,actor_display_name,action,object_kind)
            VALUES(%s,%s,'platform_owner','Operator','create','waybill')""",
                runtime,
            ),
            (uuid4(), actor["auth_user_id"]),
        )


def test_actor_delta_preserves_existing_local_document(empty_database, tmp_path):
    import json
    import shutil

    from app.tenancy.migrations import MIGRATION_DIRECTORY, load_migrations

    operator, dsn, tenant = empty_database
    runtime = tenant()
    baseline = tmp_path / "baseline"
    shutil.copytree(MIGRATION_DIRECTORY, baseline)
    manifest_file = baseline / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    manifest["migrations"] = manifest["migrations"][:4]
    manifest_file.write_text(json.dumps(manifest))
    provision_tenant(operator, runtime, directory=baseline)
    store = uuid4()
    with psycopg.connect(dsn, user=runtime.database_role, autocommit=True) as db:
        db.execute(
            render("INSERT INTO {documents}.stores(id,name) VALUES(%s,'existing')", runtime),
            (store,),
        )
        db.execute(
            render(
                """INSERT INTO {documents}.authentication_user
            (id,password,is_superuser,username,first_name,last_name,email,is_staff,is_active,date_joined)
            VALUES(1,'!',false,'existing','','','',false,true,now())""",
                runtime,
            )
        )
        db.execute(
            render(
                """INSERT INTO {documents}.waybills
            (id,status,created_at,store_id,counteragent_id,submission_state,version,created_by_id)
            VALUES(1,'Created',now(),%s,%s,'draft',1,1)""",
                runtime,
            ),
            (store, store),
        )
    assert provision_tenant(operator, runtime) == tuple(m.name for m in load_migrations()[4:])
    assert provision_tenant(operator, runtime) == ()
    with psycopg.connect(dsn, user=runtime.database_role, autocommit=True) as db:
        assert db.execute(
            render(
                """SELECT created_by_id,created_actor,processed_actor
            FROM {documents}.waybills WHERE id=1""",
                runtime,
            )
        ).fetchone() == (1, None, None)
