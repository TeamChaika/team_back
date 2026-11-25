"""Unanswered waybills use the same durable queue as an explicit approval."""

# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from xml.etree.ElementTree import fromstring

import pytest
from fastapi import HTTPException

from app.documents import auto_receipt_deadlines
from app.documents.auto_receipt import queue_due_one
from app.documents.dispatch import deliver_one
from app.documents.reads import ZONE
from app.documents.reconcile import reconcile
from tests.test_native_documents import (
    FOREIGN,
    PRODUCT,
    RECEIVER,
    SENDER,
    SOURCE,
    TARGET,
    act,
    body,
    create,
    database,  # noqa: F401
    detail,
    service,  # noqa: F401
)
from tests.test_receipt_discrepancies import receive


def shown_deadline(service, doc):
    card = detail(service, doc)
    rows = service.dispatch(RECEIVER, "GET", "waybill", params={"query": doc["number"]})["rows"]
    assert len(rows) == 1
    assert rows[0]["auto_receipt_due_at"] == card["auto_receipt_due_at"]
    return card["auto_receipt_due_at"]


def test_deadline_matches_the_item_edit_clock_and_disappears_on_dispute(service, enabled):
    doc = create(service)
    created = age_waybill(service, doc, timedelta(days=4))
    assert shown_deadline(service, doc) == (created + timedelta(hours=72)).isoformat()
    assert service.dispatch(FOREIGN, "GET", "waybill")["rows"] == []
    with pytest.raises(HTTPException) as forbidden:
        service.dispatch(FOREIGN, "GET", f"waybill/{doc['id']}")
    assert forbidden.value.status_code == 404
    edited = act(service, doc, "edit", SENDER, body(version=1, comment="Details"))
    assert shown_deadline(service, edited) == (created + timedelta(hours=72)).isoformat()
    edited = act(
        service, edited, "edit", SENDER,
        body(version=edited["version"], items=[{"product_id": str(PRODUCT), "amount": 5}]),
    )
    with service.database.connection(readonly=True) as db:
        changed = db.execute(
            "SELECT max(created_at) AS at FROM portal_documents_event WHERE kind='waybill' "
            "AND document_id=%s AND action='edit' AND data->>'items_changed'='true'", (doc["id"],),
        ).fetchone()["at"]
    assert shown_deadline(service, edited) == (changed + timedelta(hours=72)).isoformat()
    assert not queue_due_one(service)
    received = receive(service, edited)
    assert shown_deadline(service, received) is None


def test_deadline_is_absent_for_legacy_disabled_and_finished_waybills(service):
    doc = create(service)
    assert shown_deadline(service, doc) is None
    with service.database.connection() as db:
        db.execute("UPDATE waybill_auto_receipt_policy SET enabled_at=now()")
    try:
        assert shown_deadline(service, doc) is None
        eligible = create(service)
        assert shown_deadline(service, eligible) is not None
        act(service, eligible, "deny")
        assert shown_deadline(service, eligible) is None
    finally:
        with service.database.connection() as db:
            db.execute("UPDATE waybill_auto_receipt_policy SET enabled_at=NULL")


@pytest.fixture
def enabled(service):
    with service.database.connection() as db:
        db.execute("UPDATE waybill_auto_receipt_policy SET enabled_at=now()-interval '7 days'")
    yield
    with service.database.connection() as db:
        db.execute("UPDATE waybill_auto_receipt_policy SET enabled_at=NULL")


def age_waybill(service, doc, age):
    with service.database.connection() as db:
        created = db.execute(
            "UPDATE waybills SET created_at=now()-%s WHERE id=%s RETURNING created_at",
            (age, doc["id"]),
        ).fetchone()["created_at"]
        db.execute(
            "UPDATE portal_documents_event SET created_at=%s WHERE kind='waybill' "
            "AND document_id=%s AND action IN ('create','copy')",
            (created, doc["id"]),
        )
    return created


def test_feature_is_opt_in_and_never_takes_legacy_or_early_documents(service):
    due, legacy, recent = (create(service) for _ in range(3))
    age_waybill(service, due, timedelta(hours=72))
    age_waybill(service, legacy, timedelta(days=8))
    age_waybill(service, recent, timedelta(hours=71, minutes=59))
    assert not queue_due_one(service)
    with service.database.connection() as db:
        db.execute("UPDATE waybill_auto_receipt_policy SET enabled_at=now()-interval '7 days'")
    try:
        assert queue_due_one(service)
        assert detail(service, due)["submission_state"] == "queued"
        assert not queue_due_one(service)
        assert all(detail(service, d)["submission_state"] == "idle" for d in (legacy, recent))
    finally:
        with service.database.connection() as db:
            db.execute("UPDATE waybill_auto_receipt_policy SET enabled_at=NULL")


def test_system_receives_exact_quantity_once_with_original_document_date(service, enabled):
    doc = create(service)
    created = age_waybill(service, doc, timedelta(days=4))
    assert queue_due_one(service)
    assert not queue_due_one(service)
    assert not service.provider.sends
    queued = detail(service, doc)
    assert shown_deadline(service, doc) is None
    assert queued["receipt_state"] == "accepted"
    assert queued["items"][0]["received_amount"] == queued["items"][0]["amount"] == 1.25
    assert queued["history"][0]["action"] == "auto_receive"
    assert queued["history"][0]["actor"] == "Система"
    assert queued["history"][0]["data"]["items"] == [
        {"product_id": str(PRODUCT), "amount": 1.25, "received_amount": 1.25}
    ]
    with service.database.connection() as db:
        doc_row = db.execute("SELECT processed_by_id FROM waybills").fetchone()
        assert doc_row["processed_by_id"] is None
        op = db.execute(
            "SELECT actor_id FROM portal_documents_operation WHERE action='auto_receive'"
        ).fetchone()
        assert op["actor_id"] is None
    assert deliver_one(service)
    assert not deliver_one(service)
    [(kind, payload)] = service.provider.sends
    assert kind == "waybill"
    xml = fromstring(payload)
    assert xml.findtext("dateIncoming") == created.astimezone(ZONE).isoformat(timespec="seconds")
    assert xml.findtext("items/item/productId") == str(PRODUCT)
    assert xml.findtext("items/item/amount") == "1.25"
    assert "Автоприёмка" in xml.findtext("comment")
    assert detail(service, doc)["status"] == "Sent"


def test_both_warehouses_must_be_enabled_at_acceptance_time(service, enabled, monkeypatch):
    doc = create(service)
    age_waybill(service, doc, timedelta(days=4))
    selection = {"mode": "selected", "store_ids": []}
    monkeypatch.setattr(auto_receipt_deadlines, "load_settings", lambda db: {"waybill": selection})
    for single_store in (SOURCE, TARGET):
        selection["store_ids"] = [str(single_store)]
        assert shown_deadline(service, doc) is None
        assert not queue_due_one(service)
    selection["store_ids"] = [str(SOURCE), str(TARGET)]
    assert shown_deadline(service, doc) is not None
    assert queue_due_one(service)


@pytest.mark.parametrize("amount,expect_immediate", [(1.25, True), (2.5, False)])
def test_only_changed_contents_restart_the_clock(service, enabled, amount, expect_immediate):
    doc = create(service)
    age_waybill(service, doc, timedelta(days=4))
    act(
        service,
        doc,
        "edit",
        SENDER,
        body(version=1, comment="Changed", items=[{"product_id": str(PRODUCT), "amount": amount}]),
    )
    assert queue_due_one(service) is expect_immediate
    if not expect_immediate:
        with service.database.connection() as db:
            db.execute(
                "UPDATE portal_documents_event SET created_at=now()-interval '72 hours' "
                "WHERE kind='waybill' AND document_id=%s AND action='edit'",
                (doc["id"],),
            )
        assert queue_due_one(service)
    assert detail(service, doc)["items"][0]["received_amount"] == amount


def test_receipt_disputes_stay_manual_even_after_sender_edits(service, enabled):
    doc = create(service)
    age_waybill(service, doc, timedelta(days=4))
    proposed = receive(service, doc)
    assert not queue_due_one(service)
    rejected = act(service, proposed, "reject_receipt", SENDER)
    assert not queue_due_one(service)
    act(service, rejected, "edit", SENDER, body(version=rejected["version"], comment="Edited"))
    assert detail(service, doc)["receipt_state"] == "none"
    assert not queue_due_one(service)
    assert not deliver_one(service)


def test_manual_and_concurrent_automatic_receipts_can_only_queue_once(service, enabled):
    doc = create(service)
    age_waybill(service, doc, timedelta(days=4))
    start = Barrier(3)

    def try_receipt(automatic):
        start.wait(timeout=5)
        if automatic:
            return queue_due_one(service)
        try:
            return act(service, doc)["submission_state"] == "queued"
        except HTTPException as error:
            assert error.status_code == 409
            return False

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert sum(pool.map(try_receipt, [True, True, False])) == 1
    assert deliver_one(service)
    assert not deliver_one(service)
    assert len(service.provider.sends) == 1


def test_uncertain_delivery_requires_reconciliation_and_manual_reapproval(service, enabled):
    doc = create(service)
    age_waybill(service, doc, timedelta(days=4))
    assert queue_due_one(service)
    service.provider.outcome = "unknown"
    assert deliver_one(service)
    unknown = detail(service, doc)
    assert unknown["submission_state"] == "unknown"
    assert not queue_due_one(service)
    current = reconcile(
        service.database,
        "waybill",
        doc["id"],
        version=unknown["version"],
        result="absent",
        operator="Test",
        evidence="Synthetic transport verified absence",
        writers_stopped=True,
        apply=True,
    )
    assert not queue_due_one(service)
    assert not deliver_one(service)
    act(service, current)
    service.provider.outcome = "sent"
    assert deliver_one(service)
    assert service.provider.sends[0] == service.provider.sends[1]
