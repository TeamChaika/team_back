"""Receipt negotiation uses a real disposable database and fake external services."""

# ruff: noqa: F811
from uuid import uuid4

import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.documents.dispatch import deliver_one
from app.documents.telegram import deliver_notification, handle_update
from tests.test_native_documents import (
    PRODUCT,
    SENDER,
    act,
    create,
    database,  # noqa: F401
    detail,
    service,  # noqa: F401
)


def receive(service, doc, amount=1):
    return act(
        service,
        doc,
        "receive",
        payload={
            "request_id": str(uuid4()),
            "version": doc["version"],
            "items": [{"product_id": str(PRODUCT), "amount": amount}],
        },
    )


def test_proposal_does_not_export_and_sender_accepts_once(service):
    doc = receive(service, create(service))
    assert doc["receipt_state"] == "pending_sender"
    assert doc["submission_state"] == "idle"
    assert not deliver_one(service)
    original = detail(service, doc)
    assert original["items"][0]["amount"] == 1.25
    assert original["items"][0]["received_amount"] == 1
    assert "confirm" not in original["actions"]
    for action in ("confirm", "deny", "receive"):
        with pytest.raises(HTTPException) as error:
            act(service, doc, action)
        assert error.value.status_code == 409
    for action in ("edit", "cancel"):
        with pytest.raises(HTTPException) as error:
            act(service, doc, action, SENDER)
        assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        act(service, doc, "confirm_receipt")
    assert error.value.status_code == 403
    payload = {"request_id": str(uuid4()), "version": doc["version"]}
    accepted = act(service, doc, "confirm_receipt", SENDER, payload)
    assert accepted["submission_state"] == "queued"
    assert accepted["receipt_state"] == "accepted"
    assert act(service, doc, "confirm_receipt", SENDER, payload) == accepted
    with service.database.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM native_dispatch").fetchone()["n"] == 1
        saved = db.execute("SELECT payload FROM native_dispatch").fetchone()["payload"]
        assert "<amount>1</amount>" in saved["xml"] or "<amount>1.0</amount>" in saved["xml"]
    assert deliver_one(service)
    assert len(service.provider.sends) == 1


def test_rejection_retains_audit_and_returns_to_receiver(service):
    doc = receive(service, create(service))
    rejected = act(service, doc, "reject_receipt", SENDER)
    assert rejected["receipt_state"] == "rejected"
    assert not deliver_one(service)
    current = detail(service, rejected)
    assert "receive" in current["actions"]
    assert current["items"][0]["received_amount"] == 1
    assert current["history"][0]["data"]["items"][0]["amount"] == 1.25
    assert receive(service, rejected)["receipt_state"] == "pending_sender"
    with pytest.raises(HTTPException) as error:
        act(service, doc, "confirm_receipt", SENDER)
    assert error.value.status_code == 409


@pytest.mark.parametrize("amount", [0, -1, True, 1e10, 10**400])
def test_invalid_quantity_does_not_mutate(service, amount):
    doc = create(service)
    with pytest.raises(HTTPException) as error:
        receive(service, doc, amount)
    assert error.value.status_code == 422
    assert detail(service, doc)["version"] == 1


def test_unchanged_receipt_and_plain_confirmation_after_rejection(service):
    doc = receive(service, create(service), 1.25)
    assert doc["submission_state"] == "queued"
    second = act(service, receive(service, create(service)), "reject_receipt", SENDER)
    assert act(service, second)["receipt_state"] == "none"
    assert detail(service, second)["items"][0]["received_amount"] is None


def test_no_active_sender_editor_is_clear_error(service):
    doc = create(service)
    with service.database.connection() as db:
        db.execute(
            "UPDATE portal_documents_grant SET actions=%s WHERE user_id=1 AND kind='waybill'",
            (Jsonb(["view", "create"]),),
        )
    with pytest.raises(HTTPException) as error:
        receive(service, doc)
    assert error.value.status_code == 409
    assert "согласующего" in error.value.detail


def test_sender_telegram_notification_and_stale_receiver_button(service):
    class Bot:
        def __init__(self):
            self.calls = []

        def call(self, method, **kwargs):
            self.calls.append((method, kwargs))
            return {"message_id": 9}

    bot = Bot()
    initial = create(service)
    doc = receive(service, initial)
    while deliver_notification(service, bot):
        pass
    sends = [p for method, p in bot.calls if method == "sendMessage"]
    assert len(sends) == 1 and sends[0]["chat_id"] == 101
    assert "confirmReceipt" in str(sends[0]["reply_markup"])
    assert "факт" in sends[0]["text"]
    assert not service.pending(102, "waybill")
    assert service.pending(101, "waybill")[0]["receipt_state"] == "pending_sender"
    handle_update(
        service,
        bot,
        {
            "callback_query": {
                "id": "receipt-test",
                "from": {"id": 102},
                "data": f"confirmWaybill:{doc['id']}:{doc['version']}",
            }
        },
    )
    assert not deliver_one(service)


def test_zero_lines_excluded_and_originals_retained(service):
    from tests.test_native_documents import body

    other = uuid4()
    with service.database.connection() as db:
        db.execute(
            "UPDATE native_catalog SET data=%s WHERE name='products'",
            (Jsonb({str(PRODUCT): "First", str(other): "Second"}),),
        )
    doc = service.dispatch(
        SENDER,
        "POST",
        "waybill",
        payload=body(
            items=[
                {"product_id": str(PRODUCT), "amount": 2},
                {"product_id": str(other), "amount": 3},
            ]
        ),
    )
    doc = act(
        service,
        doc,
        "receive",
        payload={
            "request_id": str(uuid4()),
            "version": doc["version"],
            "items": [
                {"product_id": str(PRODUCT), "amount": 0},
                {"product_id": str(other), "amount": 3},
            ],
        },
    )
    act(service, doc, "confirm_receipt", SENDER)
    with service.database.connection() as db:
        xml = db.execute("SELECT payload FROM native_dispatch").fetchone()["payload"]["xml"]
    assert str(PRODUCT) not in xml and str(other) in xml
    assert "user2" in xml  # Actual recipient, not the sender who accepted the discrepancy.
    assert len(detail(service, doc)["items"]) == 2


@pytest.mark.parametrize("outcome", ["sent", "absent", "rejected"])
@pytest.mark.parametrize("discrepancy", [False, True])
def test_reconciliation_and_retry_keep_accepted_actual(service, outcome, discrepancy):
    from app.documents.reconcile import reconcile

    amount = 0.75 if discrepancy else 1.25
    doc = receive(service, create(service), amount)
    if discrepancy:
        doc = act(service, doc, "confirm_receipt", SENDER)
    service.provider.outcome = "rejected" if outcome == "rejected" else "unknown"
    deliver_one(service)
    current = detail(service, doc)
    if outcome != "rejected":
        current = reconcile(
            service.database,
            "waybill",
            doc["id"],
            version=current["version"],
            result=outcome,
            operator="Test",
            evidence="Synthetic provider lookup evidence",
            writers_stopped=True,
            apply=True,
        )
    if outcome == "sent":
        assert current["status"] == "Sent"
        return
    assert current["receipt_state"] == "accepted"
    retried = act(service, current)
    assert retried["submission_state"] == "queued"
    with service.database.connection() as db:
        rows = db.execute("SELECT payload FROM native_dispatch ORDER BY created_at").fetchall()
    from xml.etree.ElementTree import fromstring

    for row in rows:
        xml = fromstring(row["payload"]["xml"])
        assert float(xml.findtext("items/item/amount")) == amount
        assert "user2" in xml.findtext("comment")


def test_racing_sender_confirmations_create_one_dispatch(service):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    doc = receive(service, create(service))
    barrier = Barrier(2)

    def confirm():
        barrier.wait()
        try:
            return act(service, doc, "confirm_receipt", SENDER)["submission_state"]
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: confirm(), range(2)))
    assert set(results) == {"queued", 409}
    with service.database.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM native_dispatch").fetchone()["n"] == 1


def test_telegram_sender_confirmation_and_permission_revocation(service):
    doc = receive(service, create(service))
    payload = {"request_id": str(uuid4()), "version": doc["version"]}
    with pytest.raises(HTTPException) as error:
        service.bot_action(102, "waybill", "confirm_receipt", doc["id"], payload)
    assert error.value.status_code == 403
    with service.database.connection() as db:
        db.execute(
            "UPDATE portal_documents_grant SET actions=%s WHERE user_id=1 AND kind='waybill'",
            (Jsonb(["view", "create"]),),
        )
    with pytest.raises(HTTPException) as error:
        service.bot_action(101, "waybill", "confirm_receipt", doc["id"], payload)
    assert error.value.status_code == 403
    with service.database.connection() as db:
        db.execute(
            "UPDATE portal_documents_grant SET actions=%s WHERE user_id=1 AND kind='waybill'",
            (Jsonb(["view", "edit"]),),
        )
    assert (
        service.bot_action(101, "waybill", "confirm_receipt", doc["id"], payload)[
            "submission_state"
        ]
        == "queued"
    )


def test_revoked_global_sender_warehouse_prevents_orphaned_discrepancy(service):
    from tests.test_native_documents import TARGET

    doc = create(service)
    with service.database.connection() as db:
        db.execute(
            "INSERT INTO portal_warehouse_scope_test VALUES(%s,'selected',%s)",
            (SENDER, TARGET),
        )
    with pytest.raises(HTTPException) as error:
        receive(service, doc)
    assert error.value.status_code == 409
    assert "согласующего" in error.value.detail
    current = detail(service, doc)
    assert current["version"] == 1 and current["receipt_state"] == "none"
    with service.database.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM native_dispatch").fetchone()["n"] == 0
