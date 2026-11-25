"""Warehouse-scoped document API regressions; iiko is a local test double."""

# ruff: noqa: F811
from datetime import UTC, datetime
from uuid import uuid4
from xml.etree.ElementTree import fromstring

import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.documents.dispatch import deliver_one
from tests.test_native_documents import (
    FOREIGN,
    PRODUCT,
    RECEIVER,
    act,
    create,
    database,  # noqa: F401
    detail,
    service,  # noqa: F401
)


def test_unchecked_writeoff_has_no_external_status(service):
    document = create(service, "writeoff", reason="Reason", reason_id=1)
    assert detail(service, document)["iiko_reconciliation"] is None
    assert service.dispatch(RECEIVER, "GET", "writeoff")["rows"][0]["iiko_reconciliation"] is None


def test_writeoff_list_and_detail_only_expose_current_warehouse_evidence(service):
    document = create(service, "writeoff", reason="Reason", reason_id=1)
    act(service, document)
    service.provider.outcome = "unknown"
    assert deliver_one(service)
    unknown_product = str(uuid4())
    checked_at = datetime(2026, 10, 3, 10, 11, tzinfo=UTC)
    result = {
        "state": "matched",
        "iiko_status": "PROCESSED",
        "expected_date": "2026-10-02",
        "actual_date": "2026-10-01",
        "date_differs": True,
        "quantity_differences": [
            {"product_id": str(PRODUCT), "expected_amount": "1.25", "actual_amount": "2"},
            {"product_id": unknown_product, "expected_amount": None, "actual_amount": "1"},
        ],
    }
    with service.database.connection() as db:
        db.execute(
            "UPDATE writeoff_reconciliation SET checked_at=%s,result=%s WHERE document_id=%s",
            (checked_at, Jsonb(result), document["id"]),
        )
    listing = service.dispatch(RECEIVER, "GET", "writeoff")["rows"][0]
    evidence = listing["iiko_reconciliation"]
    assert evidence["quantity_difference_count"] == 2
    assert "quantity_differences" not in evidence
    assert datetime.fromisoformat(evidence["checked_at"]) == checked_at
    assert evidence["iiko_status"] == "PROCESSED"
    assert listing["submission_state"] == "unknown"
    card = detail(service, document)
    assert card["actions"] == []
    assert card["iiko_reconciliation"]["expected_date"] == "2026-10-02"
    assert card["iiko_reconciliation"]["actual_date"] == "2026-10-01"
    assert card["iiko_reconciliation"]["quantity_differences"] == [
        {**result["quantity_differences"][0], "name": "Product"},
        {**result["quantity_differences"][1], "name": unknown_product},
    ]
    assert service.dispatch(FOREIGN, "GET", "writeoff")["rows"] == []
    with pytest.raises(HTTPException) as denied:
        service.dispatch(FOREIGN, "GET", f"writeoff/{document['id']}")
    assert denied.value.status_code == 404
    with service.database.connection() as db:
        db.execute("UPDATE writeoffs SET version=version+1 WHERE id=%s", (document["id"],))
    assert detail(service, document)["iiko_reconciliation"] is None
    assert len(service.provider.sends) == 1


@pytest.mark.parametrize("kind", ["writeoff", "waybill"])
def test_later_approval_freezes_local_creation_date_on_original_submission(service, kind):
    doc = create(service, kind, reason="Reason", reason_id=1)
    table = "writeoffs" if kind == "writeoff" else "waybills"
    with service.database.connection() as db:
        db.execute(
            f"UPDATE {table} SET created_at=%s WHERE id=%s",
            (datetime(2025, 12, 31, 22, 15, 39, tzinfo=UTC), doc["id"]),
        )
    act(service, doc)
    assert deliver_one(service)
    assert not deliver_one(service)
    assert len(service.provider.sends) == 1
    _, payload = service.provider.sends[0]
    if kind == "writeoff":
        assert payload["dateIncoming"] == "2026-01-01T01:15:39"
    else:
        assert fromstring(payload).findtext("dateIncoming") == "2026-01-01T01:15:39+03:00"
