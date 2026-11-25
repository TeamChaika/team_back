"""Read-only iiko worker and real PostgreSQL scheduling."""

import httpx
import pytest

from app.documents.config import DocumentSettings
from app.documents.dispatch import deliver_one
from app.documents.transport import DocumentTransport
from tests.test_native_documents import (
    act,
    create,
    database,  # noqa: F401
    service,  # noqa: F401
)
from tests.test_writeoff_reconciliation import actual


def test_rechecks_unknown_and_legacy_without_mutating_dispatch(service):  # noqa: F811
    from app.documents.writeoff_reconciliation import reconcile_batch

    doc = create(service, "writeoff", reason="Reason", reason_id=1)
    act(service, doc)
    service.provider.outcome = "unknown"
    assert deliver_one(service)
    legacy = create(service, "writeoff", reason="Reason", reason_id=1)
    with service.database.connection() as db:
        db.execute("UPDATE writeoffs SET status='Sent' WHERE id=%s", (legacy["id"],))
    records = {doc["number"]: actual(doc["number"]), legacy["number"]: actual(legacy["number"])}
    reads = []

    def handler(request):
        assert request.method == "GET"
        path = request.url.path
        if path.endswith("/auth"):
            return httpx.Response(200, text="0123456789abcdef")
        if path.endswith("/logout"):
            return httpx.Response(200)
        assert path.endswith("/v2/documents/writeoff/byNumber")
        number = request.url.params["documentNumber"]
        reads.append(number)
        return httpx.Response(200, json=[records[number]])

    service.provider = DocumentTransport(
        DocumentSettings(
            _env_file=None,
            iiko_url="https://example.invalid",
            iiko_login="u",
            iiko_password_hash="test",
        ),
        transport=httpx.MockTransport(handler),
    )
    assert reconcile_batch(service)
    assert set(reads) == {doc["number"], legacy["number"]}
    legacy_record = records.pop(legacy["number"])
    records[doc["number"]]["status"] = "DELETED"
    with service.database.connection() as db:
        db.execute("UPDATE writeoff_reconciliation SET next_check_at=now()")
    # A lookup failure for the other document preserves its previous verified result.
    with pytest.raises(KeyError):
        reconcile_batch(service)
    with service.database.connection() as db:
        results = db.execute(
            "SELECT document_id,result FROM writeoff_reconciliation ORDER BY document_id"
        ).fetchall()
        assert [r["result"]["iiko_status"] for r in results] == ["DELETED", "PROCESSED"]
        assert db.execute("SELECT state,attempts FROM native_dispatch").fetchall() == [
            {"state": "unknown", "attempts": 1}
        ]
        db.execute("UPDATE native_dispatch SET payload='{}'")
        db.execute("UPDATE writeoff_reconciliation SET next_check_at=now()")
    records[legacy["number"]] = {**legacy_record, "status": "NEW"}
    with pytest.raises((KeyError, TypeError, ValueError)):
        reconcile_batch(service)
    with service.database.connection(readonly=True) as db:
        result = db.execute(
            "SELECT result FROM writeoff_reconciliation WHERE document_id=%s",
            (legacy["id"],),
        ).fetchone()["result"]
        assert result["iiko_status"] == "NEW"
