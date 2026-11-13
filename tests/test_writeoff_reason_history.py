"""A mutable reason dictionary must never rewrite an existing document choice."""

# ruff: noqa: F811 -- shared PostgreSQL fixtures.
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.documents.workflow import validate
from tests.test_writeoff_costs import (  # noqa: F401
    RECEIVER,
    SENDER,
    database,
    service,
    writeoff_body,
)


def test_rename_and_account_change_preserve_pending_and_reserved_documents(service):
    original = service.dispatch(SENDER, "POST", "writeoff", payload=writeoff_body())
    with service.database.connection() as db:
        account = db.execute("SELECT account_id FROM writeoffs_reasons WHERE id=1").fetchone()[
            "account_id"
        ]
        db.execute(
            "UPDATE writeoffs_reasons SET name='Renamed',account_id=%s,version=version+1 "
            "WHERE id=1",
            (uuid4(),),
        )
        # Reusing an old name must not rebind an old document to this new reason.
        db.execute(
            "INSERT INTO writeoffs_reasons(id,name,account_id) VALUES(2,'Reason',%s)", (uuid4(),)
        )
    detail = service.dispatch(SENDER, "GET", f"writeoff/{original['id']}")
    assert detail["reason"] == "Reason" and detail["reason_id"] == 1
    service.dispatch(
        RECEIVER,
        "POST",
        f"writeoff/{original['id']}/confirm",
        payload={"request_id": str(uuid4()), "version": original["version"]},
    )
    with service.database.connection() as db:
        payload = db.execute(
            "SELECT payload FROM native_dispatch WHERE document_id=%s", (original["id"],)
        ).fetchone()["payload"]
        assert payload["json"]["accountId"] == str(account)
        assert "Reason" in payload["json"]["comment"]
        db.execute("UPDATE writeoffs_reasons SET account_id=%s WHERE id=1", (uuid4(),))
        assert (
            db.execute(
                "SELECT payload FROM native_dispatch WHERE document_id=%s", (original["id"],)
            ).fetchone()["payload"]
            == payload
        )
    assert service.provider.sends == []


def test_legacy_insert_snapshots_and_unrelated_update_keeps_snapshot(service):
    with service.database.connection() as db:
        old = db.execute(
            "INSERT INTO writeoffs "
            "(reason_id,store_id,created_by_id,status,version,submission_state,created_at) "
            "SELECT 'Reason',id,1,'Created',1,'idle',now() FROM stores LIMIT 1 RETURNING *"
        ).fetchone()
        assert old["reason_pk"] == 1 and old["reason_account_id"] is not None
        db.execute("UPDATE writeoffs_reasons SET account_id=%s WHERE id=1", (uuid4(),))
        row = db.execute(
            "UPDATE writeoffs SET reason_id=reason_id,comment='Updated' WHERE id=%s RETURNING *",
            (old["id"],),
        ).fetchone()
        assert row["reason_account_id"] == old["reason_account_id"]


@pytest.mark.parametrize("deferred", [False, True])
def test_migration_preserves_payload_and_does_not_invent_completed_history(database, deferred):
    schema = "snapshot_test_" + uuid4().hex
    with database.connection() as db:
        db.execute(f"CREATE SCHEMA {schema}")
        try:
            db.execute(f"""
                CREATE TABLE {schema}.writeoffs_reasons (
                    id bigserial PRIMARY KEY,name varchar(128) UNIQUE,account_id uuid);
                CREATE TABLE {schema}.writeoffs (
                    id bigint PRIMARY KEY,reason_id varchar(128),status text,submission_state text,
                    CONSTRAINT writeoffs_reason_id_ca8d5b1b_fk_writeoffs_reasons_name
                    FOREIGN KEY(reason_id) REFERENCES {schema}.writeoffs_reasons(name)
                    {"DEFERRABLE INITIALLY DEFERRED" if deferred else "NOT DEFERRABLE"});
                CREATE TABLE {schema}.native_dispatch (
                    kind text,document_id bigint,version integer,
                    created_at timestamptz,payload jsonb);
            """)
            current, reserved = uuid4(), uuid4()
            db.execute(
                f"INSERT INTO {schema}.writeoffs_reasons(name,account_id) VALUES('Old',%s)",
                (current,),
            )
            db.execute(f"""INSERT INTO {schema}.writeoffs VALUES
                (1,'Old','Created','idle'),(2,'Old','Sent','sent'),(3,'Old','Created','queued')""")
            payload = {"json": {"accountId": str(reserved), "comment": "Original"}}
            db.execute(
                f"INSERT INTO {schema}.native_dispatch VALUES('writeoff',3,2,now(),%s)",
                (Jsonb(payload),),
            )
            # Existing production rows have no pending INSERT events at migration entry.
            db.execute("SET CONSTRAINTS ALL IMMEDIATE")
            if deferred:
                db.execute("SET CONSTRAINTS ALL DEFERRED")
            migration = Path("migrations/documents/0012_writeoff_reasons.sql").read_text()
            migration = migration.split("-- The analytics dictionary")[0].removeprefix("BEGIN;\n")
            with db.transaction():
                db.execute(migration.replace("chaika_iiko_documents", schema))
            rows = db.execute(f"SELECT * FROM {schema}.writeoffs ORDER BY id").fetchall()
            assert [r["reason_pk"] for r in rows] == [1, 1, 1]
            assert [r["reason_account_id"] for r in rows] == [current, None, reserved]
            assert (
                db.execute(f"SELECT payload FROM {schema}.native_dispatch").fetchone()["payload"]
                == payload
            )
            db.execute(f"UPDATE {schema}.writeoffs_reasons SET name='New'")
            assert (
                db.execute(f"SELECT reason_id FROM {schema}.writeoffs WHERE id=1").fetchone()[
                    "reason_id"
                ]
                == "Old"
            )
        finally:
            db.execute(f"DROP SCHEMA {schema} CASCADE")


def test_selection_holds_reason_stable_until_document_transaction_commits(service):
    with service.database.connection() as first:
        fields, _rows = validate(first, "writeoff", writeoff_body())
        with pytest.raises(psycopg.errors.LockNotAvailable):
            with service.database.connection() as second:
                second.execute("SET LOCAL lock_timeout='100ms'")
                second.execute("UPDATE writeoffs_reasons SET account_id=%s WHERE id=1", (uuid4(),))
        assert fields["reason_pk"] == 1
        assert (
            fields["reason_account_id"]
            == first.execute("SELECT account_id FROM writeoffs_reasons WHERE id=1").fetchone()[
                "account_id"
            ]
        )
    with service.database.connection() as db:
        db.execute("UPDATE writeoffs_reasons SET account_id=%s WHERE id=1", (uuid4(),))


def test_missing_historical_account_never_falls_back_to_current_dictionary(service):
    original = service.dispatch(SENDER, "POST", "writeoff", payload=writeoff_body())
    with service.database.connection() as db:
        db.execute("UPDATE writeoffs SET reason_account_id=NULL WHERE id=%s", (original["id"],))
    with pytest.raises(HTTPException) as caught:
        service.dispatch(
            RECEIVER,
            "POST",
            f"writeoff/{original['id']}/confirm",
            payload={"request_id": str(uuid4()), "version": original["version"]},
        )
    assert caught.value.status_code == 409
    with service.database.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM native_dispatch").fetchone()["n"] == 0
        assert (
            db.execute(
                "SELECT submission_state FROM writeoffs WHERE id=%s", (original["id"],)
            ).fetchone()["submission_state"]
            == "idle"
        )
