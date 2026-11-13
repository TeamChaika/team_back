"""Reason management uses persisted own-source accounts, without contacting iiko."""

from uuid import UUID

import pytest
from fastapi import HTTPException

from tests.test_native_documents import ADMIN, SENDER
from tests.test_native_documents import database as database
from tests.test_native_documents import service as service

ACCOUNT = UUID(int=901)


@pytest.fixture
def reasons_service(service):
    with service.database.connection() as db:
        db.execute(
            "SELECT setval('writeoffs_reasons_id_seq', (SELECT max(id) FROM writeoffs_reasons))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS chaika.accounts ("
            "source_id text,id uuid,name text,code text,deleted boolean,"
            "present_in_latest boolean,PRIMARY KEY(source_id,id))"
        )
        db.execute("TRUNCATE chaika.accounts")
        for source, key, deleted, present in [
            ("primary", ACCOUNT, False, True),
            ("other", UUID(int=902), False, True),
            ("primary", UUID(int=903), True, True),
            ("primary", UUID(int=904), False, False),
        ]:
            db.execute(
                "INSERT INTO chaika.accounts(source_id,id,name,code,deleted,present_in_latest) "
                "VALUES (%s,%s,'Account','1',%s,%s)",
                (source, key, deleted, present),
            )
    return service


def call(service, method="GET", payload=None, reason_id=None, actor=ADMIN):
    path = "admin/writeoff-reasons" + (f"/{reason_id}" if reason_id else "")
    return service.dispatch(actor, method, path, payload=payload)


def test_list_and_write_deny_nonadmin(reasons_service):
    for method in ("GET", "POST"):
        with pytest.raises(HTTPException) as error:
            call(
                reasons_service,
                method,
                {"name": "Denied", "account_id": str(ACCOUNT)},
                actor=SENDER,
            )
        assert error.value.status_code == 403


def test_create_edit_versions_and_transactional_audit(reasons_service):
    options = call(reasons_service)
    assert [row["id"] for row in options["accounts"]] == [ACCOUNT]
    created = call(reasons_service, "POST", {"name": " New reason ", "account_id": str(ACCOUNT)})[
        "reason"
    ]
    assert created["name"] == "New reason" and created["version"] == 1
    assert created["account_name"] == "Account"
    edited = call(
        reasons_service,
        "POST",
        {"name": "Renamed", "account_id": str(ACCOUNT), "version": 1},
        created["id"],
    )["reason"]
    assert edited["name"] == "Renamed" and edited["version"] == 2
    with reasons_service.database.connection(readonly=True) as db:
        events = db.execute(
            "SELECT version,data FROM writeoffs_reason_events WHERE reason_id=%s ORDER BY id",
            (created["id"],),
        ).fetchall()
    assert [event["version"] for event in events] == [1, 2]
    assert events[1]["data"]["before"]["name"] == "New reason"
    with pytest.raises(HTTPException) as stale:
        call(
            reasons_service,
            "POST",
            {"name": "Stale", "account_id": str(ACCOUNT), "version": 1},
            created["id"],
        )
    assert stale.value.status_code == 409
    assert call(reasons_service)["reasons"][-1]["name"] == "Renamed"


@pytest.mark.parametrize(
    "account",
    ["bad", str(UUID(int=902)), str(UUID(int=903)), str(UUID(int=904)), str(UUID(int=999))],
)
def test_invalid_foreign_deleted_or_absent_account_rejected(reasons_service, account):
    with pytest.raises(HTTPException) as error:
        call(reasons_service, "POST", {"name": "Invalid", "account_id": account})
    assert error.value.status_code == 422


def test_duplicate_name_and_invalid_payload(reasons_service):
    body = {"name": "Unique", "account_id": str(ACCOUNT)}
    call(reasons_service, "POST", body)
    with pytest.raises(HTTPException) as error:
        call(reasons_service, "POST", body)
    assert error.value.status_code == 409
    for invalid in (
        {**body, "name": " "},
        {**body, "name": "n" * 129},
        {**body, "company_id": "foreign"},
    ):
        with pytest.raises(HTTPException) as error:
            call(reasons_service, "POST", invalid)
        assert error.value.status_code == 422
