"""Global owners exercise native workflows without local identity records."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.documents.dispatch import claim, complete, reserve
from app.tenancy.actor import ActorContext
from tests.test_tenant_documents import PRODUCT, SOURCE, TARGET, companies  # noqa: F401
from tests.test_tenant_migrations_postgres import empty_database  # noqa: F401


def owner(service):
    return ActorContext(
        service.database.runtime.company_id, UUID(int=800), "platform_owner", "Platform Owner"
    )


def create(service, actor, **changes):
    body = {
        "request_id": str(uuid4()),
        "store_id": str(SOURCE),
        "counteragent_id": str(TARGET),
        "comment": "Owner created",
        "items": [{"product_id": str(PRODUCT), "amount": 2}],
    }
    body.update(changes)
    return service.dispatch(actor, "POST", "waybill", payload=body), body


def action(service, actor, doc, name, **extra):
    return service.dispatch(
        actor,
        "POST",
        f"waybill/{doc['id']}/{name}",
        payload={"request_id": str(uuid4()), "version": doc["version"], **extra},
    )


def test_owner_native_history_receipts_and_no_personal_account(companies):  # noqa: F811
    for service, _ in companies:
        actor = owner(service)
        doc, body = create(service, actor)
        assert service.dispatch(actor, "POST", "waybill", payload=body) == doc
        assert len(service.dispatch(actor, "GET", "waybill/options")["stores"]) == 2
        with pytest.raises(HTTPException) as conflict:
            service.dispatch(
                replace(actor, auth_user_id=UUID(int=801)), "POST", "waybill", payload=body
            )
        assert conflict.value.status_code == 409
        doc = action(
            service, actor, doc, "edit", **{k: v for k, v in body.items() if k != "request_id"}
        )
        doc = action(
            service, actor, doc, "receive", items=[{"product_id": str(PRODUCT), "amount": 1}]
        )
        assert doc["receipt_state"] == "pending_sender"
        doc = action(service, actor, doc, "reject_receipt")
        doc = action(
            service, actor, doc, "receive", items=[{"product_id": str(PRODUCT), "amount": 1}]
        )
        doc = action(service, actor, doc, "confirm_receipt")
        assert doc["submission_state"] == "queued"
        detail = service.dispatch(actor, "GET", f"waybill/{doc['id']}")
        assert detail["created_by"] == "Platform Owner"
        assert all(
            e["actor_context"]["auth_user_id"] == str(actor.auth_user_id) for e in detail["history"]
        )
        assert {e["action"] for e in detail["history"]} >= {
            "create",
            "edit",
            "receive",
            "reject_receipt",
            "confirm_receipt",
        }
        with service.database.connection(readonly=True) as db:
            assert db.execute("SELECT count(*) AS n FROM authentication_user").fetchone()["n"] == 2
            assert (
                db.execute("SELECT count(*) AS n FROM portal_documents_userlink").fetchone()["n"]
                == 2
            )
            saved = db.execute("SELECT * FROM waybills WHERE id=%s", (doc["id"],)).fetchone()
            assert saved["created_by_id"] is None and saved["processed_by_id"] is None
            assert saved["created_actor"]["auth_user_id"] == str(actor.auth_user_id)
            assert db.execute("SELECT count(*) AS n FROM native_actor_audit").fetchone()["n"] >= 7
            assert (
                "Platform Owner"
                in db.execute("SELECT payload FROM native_dispatch").fetchone()["payload"]["xml"]
            )
        job = claim(service.database)
        assert reserve(service.database, job)
        complete(service.database, job, "sent")
        completed = service.dispatch(actor, "GET", f"waybill/{doc['id']}")
        assert completed["status"] == "Sent"
        assert completed["history"][0]["actor_context"]["auth_user_id"] == str(actor.auth_user_id)
        assert completed["history"][0]["action"] == "iiko_sent"
        writeoff = service.dispatch(
            actor,
            "POST",
            "writeoff",
            payload={
                "request_id": str(uuid4()),
                "store_id": str(SOURCE),
                "reason": "Reason",
                "reason_id": 1,
                "items": [{"product_id": str(PRODUCT), "amount": 1}],
            },
        )
        approved_writeoff = service.dispatch(
            actor,
            "POST",
            f"writeoff/{writeoff['id']}/confirm",
            payload={"request_id": str(uuid4()), "version": writeoff["version"]},
        )
        assert approved_writeoff["submission_state"] == "queued"
        assert (
            service.dispatch(actor, "GET", f"writeoff/{writeoff['id']}")["created_by"]
            == actor.display_name
        )
        for terminal in ("deny", "cancel", "confirm"):
            fresh, _ = create(service, actor)
            assert action(service, actor, fresh, terminal)["version"] == 2


def test_owner_cannot_cross_tenant_or_forge_snapshot(companies):  # noqa: F811
    first, second = [pair[0] for pair in companies]
    actor = owner(first)
    for ref in (actor, actor.as_dict()):
        with pytest.raises(HTTPException) as denied:
            second.dispatch(ref, "GET", "waybill/options")
        assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as denied:
        first.dispatch(actor.auth_user_id, "GET", "waybill/options")
    assert denied.value.status_code == 403


def test_owner_staff_creation_audits_real_target_id(companies):  # noqa: F811
    from app.tenancy.sql import render

    service = companies[0][0]
    actor = owner(service)
    employee = uuid4()
    with service.database.connection() as db:
        db.execute(
            render(
                "INSERT INTO {analytics}.web_users(id,display_name,role,sections) "
                "VALUES(%s,'New employee','manager',ARRAY['transfers'])",
                service.database.runtime,
            ),
            (employee,),
        )
        db.execute(
            
                'SELECT '
                "nextval(pg_get_serial_sequence('authentication_user','id')) FROM "
                'generate_series(1,2)'
            
        ).fetchall()
    service.dispatch(
        actor,
        "POST",
        "admin/users/0",
        payload={
            "revision": 0,
            "supabase_id": str(employee),
            "active": True,
            "grants": [],
            "name": "New employee",
        },
    )
    with service.database.connection(readonly=True) as db:
        created = db.execute(
            "SELECT user_id FROM portal_documents_userlink WHERE supabase_id=%s", (employee,)
        ).fetchone()
        audit = db.execute(
            "SELECT object_id,actor_uuid FROM native_actor_audit WHERE action='access_update'"
        ).fetchone()
        assert audit["object_id"] == str(created["user_id"]) != "0"
        assert audit["actor_uuid"] == actor.auth_user_id
        assert not db.execute(
            "SELECT 1 FROM portal_documents_userlink WHERE supabase_id=%s", (actor.auth_user_id,)
        ).fetchone()
