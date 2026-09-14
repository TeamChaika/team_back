import hashlib
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Event
from uuid import uuid4

import httpx
import psycopg
import pytest
from test_iiko_writeoffs import body, document
from test_invoice_history import source as source
from test_reference_sync import bundle as bundle
from test_reference_sync import db as db

from app.schemas.iiko_writeoffs import WriteoffsQuery
from app.services.iiko_writeoffs import read_writeoffs
from app.sync_invoices import commit_day, fetch_day, initial_progress, prepare_run
from app.sync_references import SyncError
from app.sync_writeoffs import prerequisite_state

DAY = date(2026, 9, 9)


def captured(tmp_path, source, day, documents=None):
    raw = body(documents)
    path = tmp_path / f"{uuid4()}.json"
    path.write_bytes(raw)
    export = read_writeoffs(path, WriteoffsQuery(date_from=day, date_to=day))
    return {
        "id": str(uuid4()),
        "source_id": source.id,
        "resource": "writeoffs",
        "raw": raw,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "observed_at": datetime.now(UTC),
        "payload": {
            "items": [d.model_dump(mode="json") for d in export.response],
            "sync_business_date": day.isoformat(),
            "source_fingerprint": source.fingerprint,
            "revision": export.revision,
        },
    }


def test_writeoff_history_precision_empty_days_and_resume(db, source, tmp_path):
    end = DAY + timedelta(days=1)
    run, state = prepare_run(db, source, DAY, end, None, "writeoffs")
    data = captured(tmp_path, source, DAY)
    state = commit_day(db, run, state, data)
    assert db.execute("SELECT amount,cost FROM chaika.writeoff_items").fetchone() == (
        Decimal("-1.23456789123456789"),
        Decimal("101.123456789123456789"),
    )
    _, resumed = prepare_run(db, source, DAY, end, run, "writeoffs")
    assert resumed["next_date"] == end.isoformat()
    state = commit_day(db, run, resumed, captured(tmp_path, source, end, []))
    assert state["completed_days"] == 2 and state["documents_read"] == 1
    assert db.execute("SELECT present_in_latest FROM chaika.writeoff_items").fetchone()[0]
    assert db.execute("SELECT count(*) FROM chaika.incoming_invoices").fetchone()[0] == 0
    with pytest.raises(SyncError, match="scope_mismatch"):
        prepare_run(db, source, DAY, end, run)


def test_writeoff_new_observation_removes_lines_without_duplicates(db, source, tmp_path):
    first = captured(tmp_path, source, DAY)
    for data in (
        first,
        captured(tmp_path, source, DAY),
        captured(tmp_path, source, DAY, [document(items=[], status="DELETED")]),
    ):
        run, state = prepare_run(db, source, DAY, DAY, None, "writeoffs")
        commit_day(db, run, state, data)
    assert db.execute("SELECT count(*) FROM chaika.writeoffs").fetchone()[0] == 1
    assert db.execute("SELECT status,first_seen_at FROM chaika.writeoffs").fetchone() == (
        "DELETED",
        first["observed_at"],
    )
    assert db.execute("SELECT present_in_latest FROM chaika.writeoff_items").fetchone()[0] is False
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 3


def test_writeoff_bad_line_rolls_back_checkpoint(db, source, tmp_path):
    run, state = prepare_run(db, source, DAY, DAY, None, "writeoffs")
    data = captured(tmp_path, source, DAY)
    data["payload"]["items"][0]["items"][0]["amount"] = None
    with pytest.raises(psycopg.errors.NotNullViolation):
        commit_day(db, run, state, data)
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM chaika.writeoffs").fetchone()[0] == 0
    assert (
        db.execute(
            "SELECT counts->>'next_date' FROM chaika.sync_runs WHERE id=%s", (run,)
        ).fetchone()[0]
        == DAY.isoformat()
    )


def test_prerequisite_allows_only_completed_invoice_history(source):
    state = initial_progress(source, DAY, DAY)
    assert not prerequisite_state(("running", state), DAY, DAY, source.fingerprint)
    with pytest.raises(SyncError, match="failed"):
        prerequisite_state(("failed", state), DAY, DAY, source.fingerprint)
    with pytest.raises(SyncError, match="incomplete"):
        prerequisite_state(("succeeded", state), DAY, DAY, source.fingerprint)
    state.update(completed_days=1, next_date=(DAY + timedelta(days=1)).isoformat())
    assert prerequisite_state(("succeeded", state), DAY, DAY, source.fingerprint)
    for changed in (
        {"resource": "writeoffs"},
        {"source_fingerprint": "wrong"},
        {"date_to": "2026-09-08"},
    ):
        bad = {**deepcopy(state), **changed}
        with pytest.raises(SyncError, match="scope_mismatch"):
            prerequisite_state(("succeeded", bad), DAY, DAY, source.fingerprint)


def test_fetch_writeoffs_uses_correct_route_and_keeps_unfiltered_status():
    def handle(request):
        assert request.url.path == "/api/v1/iiko/writeoffs"
        assert dict(request.url.params) == {
            "date_from": DAY.isoformat(),
            "date_to": DAY.isoformat(),
            "revision_from": "-1",
        }
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(
        base_url="http://127.0.0.1:8010", transport=httpx.MockTransport(handle)
    ) as client:
        assert fetch_day(client, DAY, Event(), "writeoffs") == {"ok": True}


def test_invalid_format_is_not_retried():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(502, json={"error": {"code": "iiko_invoices_invalid_response"}})

    with httpx.Client(
        base_url="http://127.0.0.1:8010", transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(SyncError, match="iiko_invoices_invalid_response"):
            fetch_day(client, DAY, Event())
    assert len(requests) == 1
