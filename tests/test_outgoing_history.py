import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Event
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_iiko_outgoing import ITEM, LINK, PARAMS, config, document, wrap
from test_reference_sync import db as db

from app import sync_inventory
from app import sync_invoices as history
from app.api.routes import sync_jobs as routes
from app.main import create_app
from app.outgoing_storage import outgoing_link_counts
from app.schemas.iiko_outgoing import OutgoingInvoicesQuery, OutgoingInvoicesResponse
from app.services.iiko_outgoing import read_outgoing_invoices
from app.sync_references import Source, SyncError, register_sources

RESOURCE = "outgoing_invoices"
START = date(2023, 3, 1)
END = date(2023, 3, 3)


@pytest.fixture
def source(db):
    source = Source("primary", "Chain", "https://iiko.example/resto/api", "a" * 64)
    register_sources(db, [source])
    db.execute("UPDATE chaika.sources SET server_type='CHAIN' WHERE id='primary'")
    return source


def snapshot(tmp_path, source, day, items=ITEM):
    raw = wrap(document(day=day.isoformat(), items=items))
    path = tmp_path / f"{uuid4()}.xml"
    path.write_bytes(raw)
    rows = [r.model_dump(mode="json") for r in read_outgoing_invoices(path)]
    return {
        "id": str(uuid4()),
        "source_id": source.id,
        "resource": RESOURCE,
        "observed_at": datetime.now(UTC),
        "raw": raw,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "payload": {
            "items": rows,
            "total": 1,
            "sync_business_date": day.isoformat(),
            "source_fingerprint": source.fingerprint,
        },
    }


def test_reloads_repeated_lines_empty_document_and_return_preserve_raw(db, source, tmp_path):
    run, state = history.prepare_run(db, source, START, END, None, RESOURCE)
    first = snapshot(tmp_path, source, START, ITEM + ITEM)
    state = history.commit_day(db, run, state, first)
    assert db.execute(
        "SELECT line_num,sum FROM chaika.outgoing_invoice_items ORDER BY line_num"
    ).fetchall() == [(1, Decimal("101.123456789123456789")), (2, Decimal("101.123456789123456789"))]
    state = history.commit_day(
        db, run, state, snapshot(tmp_path, source, START + timedelta(days=1), "")
    )
    assert (
        db.execute(
            "SELECT count(*) FROM chaika.outgoing_invoice_items WHERE present_in_latest"
        ).fetchone()[0]
        == 0
    )
    state = history.commit_day(db, run, state, snapshot(tmp_path, source, END, ITEM + ITEM))
    assert (
        db.execute(
            "SELECT count(*) FROM chaika.outgoing_invoice_items WHERE present_in_latest"
        ).fetchone()[0]
        == 2
    )
    assert db.execute("SELECT count(*) FROM chaika.outgoing_invoices").fetchone()[0] == 1
    assert (
        db.execute("SELECT first_seen_at FROM chaika.outgoing_invoices").fetchone()[0]
        == first["observed_at"]
    )
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 3
    assert state["completed_days"] == 3 and state["last_day_checks"]["incoming_found"] == 0


def test_empty_day_does_not_delete_previous_documents(db, source, tmp_path):
    run, state = history.prepare_run(db, source, START, END, None, RESOURCE)
    state = history.commit_day(db, run, state, snapshot(tmp_path, source, START))
    empty = snapshot(tmp_path, source, START + timedelta(days=1))
    empty["payload"].update(items=[], total=0)
    state = history.commit_day(db, run, state, empty)
    assert state["documents_read"] == 1
    assert db.execute("SELECT count(*) FROM chaika.outgoing_invoices").fetchone()[0] == 1
    assert db.execute("SELECT present_in_latest FROM chaika.outgoing_invoice_items").fetchone()[0]


def test_uuid_link_direction_and_access_controls(db, source, tmp_path):
    run, state = history.prepare_run(db, source, START, END, None, RESOURCE)
    snap = snapshot(tmp_path, source, START)
    state = history.commit_day(db, run, state, snap)
    assert state["last_day_checks"]["with_incoming_link"] == 1
    assert state["last_day_checks"]["incoming_found"] == 0
    db.execute(
        "INSERT INTO chaika.incoming_invoices(source_id,id,status,last_export_date,details,"
        "first_seen_at,last_seen_at,last_snapshot_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            source.id,
            LINK,
            "PROCESSED",
            START,
            Jsonb({"linked_outgoing_invoice_id": str(UUID(int=1))}),
            snap["observed_at"],
            snap["observed_at"],
            snap["id"],
        ),
    )
    counts = outgoing_link_counts(db, source.id, [UUID(int=1)])
    assert (
        counts["incoming_found"]
        == counts["reverse_link_matches"]
        == counts["incoming_processed"]
        == 1
    )
    db.execute("UPDATE chaika.incoming_invoices SET details='{}'")
    assert outgoing_link_counts(db, source.id, [UUID(int=1)])["reverse_link_matches"] == 0
    for table in ["outgoing_invoices", "outgoing_invoice_items"]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute(f"DELETE FROM chaika.{table}")
        for role in ["anon", "authenticated"]:
            assert not db.execute(
                "SELECT has_table_privilege(%s,%s,%s)", (role, "chaika." + table, "SELECT")
            ).fetchone()[0]


@pytest.mark.parametrize("failure", ["line", "checkpoint"])
def test_daily_commit_is_atomic(db, source, tmp_path, failure):
    run, state = history.prepare_run(db, source, START, END, None, RESOURCE)
    snap = snapshot(tmp_path, source, START)
    if failure == "line":
        snap["payload"]["items"][0]["items"][0]["sum"] = None
    else:
        db.execute(
            "UPDATE chaika.sync_runs SET counts=jsonb_set(counts,'{next_date}','\"2023-03-02\"') "
            "WHERE id=%s",
            (run,),
        )
    with pytest.raises((SyncError, psycopg.errors.NotNullViolation)):
        history.commit_day(db, run, state, snap)
    for table in ["raw_snapshots", "outgoing_invoices", "outgoing_invoice_items"]:
        assert db.execute(f"SELECT count(*) FROM chaika.{table}").fetchone()[0] == 0


@pytest.mark.parametrize("bad", ["fingerprint", "endpoint", "date", "hash", "rows"])
def test_capture_verifies_scope_and_raw(db, source, tmp_path, monkeypatch, bad):
    snap = snapshot(tmp_path, source, START)
    folder = tmp_path / ".local/outgoing-invoices"
    folder.mkdir(parents=True)
    path = folder / f"{snap['id']}.xml"
    path.write_bytes(snap["raw"])
    response = OutgoingInvoicesResponse(
        snapshot_id=snap["id"],
        received_at=snap["observed_at"],
        request=OutgoingInvoicesQuery(date_from=START, date_to=START),
        total=1,
        items_count=1,
        documents=read_outgoing_invoices(path),
        source_bytes=len(snap["raw"]),
        sha256=snap["sha256"],
    ).model_dump(mode="json")
    meta = {k: v for k, v in response.items() if k != "documents"}
    meta.update(
        source_fingerprint=source.fingerprint, source_endpoint="documents/export/outgoingInvoice"
    )
    meta_path = folder / f"{snap['id']}.meta.json"
    meta_path.write_text(json.dumps(meta))
    monkeypatch.setattr(sync_inventory, "BACKEND_DIR", tmp_path)
    assert (
        sync_inventory.capture_inventory(config(), source, RESOURCE, response, START)["raw"]
        == snap["raw"]
    )
    if bad == "fingerprint":
        meta["source_fingerprint"] = "wrong"
    elif bad == "endpoint":
        meta["source_endpoint"] = "wrong"
    elif bad == "date":
        response["request"]["date_to"] = "2023-03-02"
    elif bad == "hash":
        path.write_bytes(b"broken")
    elif bad == "rows":
        response["documents"][0]["items"][0]["sum"] = "0"
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(SyncError):
        sync_inventory.capture_inventory(config(), source, RESOURCE, response, START)


def test_resume_after_failure_skips_only_committed_days(db, source, tmp_path, monkeypatch):
    requests = []
    fail = True
    factory = httpx.Client

    def handle(r):
        requests.append(r)
        if r.url.path.endswith("/connections"):
            return httpx.Response(
                200, json={"items": [{"connection_id": "primary", "base_url": source.base_url}]}
            )
        if r.url.path.endswith("/logout"):
            return httpx.Response(200, json={"state": "logged_out"})
        assert r.url.path.endswith("/outgoing-invoices")
        day = r.url.params["date_from"]
        assert dict(r.url.params) == {"date_from": day, "date_to": day}
        return httpx.Response(409 if fail and day == "2023-03-02" else 200, json={})

    monkeypatch.setattr(history.psycopg, "connect", lambda *a, **k: nullcontext(db))
    monkeypatch.setattr(history, "configured_sources", lambda settings: [source])
    monkeypatch.setattr(
        history.httpx, "Client", lambda **k: factory(**k, transport=httpx.MockTransport(handle))
    )
    monkeypatch.setattr(
        history,
        "capture_inventory",
        lambda settings, source, resource, response, day: snapshot(tmp_path, source, day),
    )
    output = tmp_path / "history.json"
    with pytest.raises(SyncError):
        history.synchronize_invoices(
            config(database_url="unused"),
            "http://127.0.0.1:8010",
            START,
            END,
            output,
            resource=RESOURCE,
        )
    first = json.loads(output.read_text())
    assert first["counts"]["completed_days"] == 1 and first["logout_ok"]
    assert requests[-1].url.path.endswith("/logout")
    fail = False
    requests.clear()
    result = history.synchronize_invoices(
        config(database_url="unused"),
        "http://127.0.0.1:8010",
        START,
        END,
        output,
        UUID(first["run_id"]),
        resource=RESOURCE,
    )
    assert result["status"] == "succeeded" and result["counts"]["completed_days"] == 3
    assert [
        r.url.params["date_from"] for r in requests if r.url.path.endswith("/outgoing-invoices")
    ] == ["2023-03-02", "2023-03-03"]
    with pytest.raises(SyncError, match="scope_mismatch"):
        history.prepare_run(db, source, START, END, UUID(first["run_id"]), "incoming_invoices")


def test_bad_xml_is_not_retried():
    calls = []

    def handle(r):
        calls.append(r)
        return httpx.Response(502, json={"error": {"code": "iiko_outgoing_invalid_response"}})

    with httpx.Client(base_url="http://127.0.0.1:8010", transport=httpx.MockTransport(handle)) as c:
        with pytest.raises(SyncError, match="invalid_response"):
            history.fetch_day(c, START, Event(), RESOURCE)
    assert len(calls) == 1


def test_protected_sync_route_auth_scope_errors_and_result(monkeypatch):
    endpoint = "/api/v1/sync/outgoing-invoices"
    headers = {"X-Sync-Key": "test-key"}

    def success(*args):
        return {
            "run_id": str(uuid4()),
            "status": "succeeded",
            "logout_ok": True,
            "counts": {
                "completed_days": 1,
                "documents_read": 1,
                "items_read": 2,
                "last_day_checks": {},
            },
        }

    monkeypatch.setattr(routes, "synchronize_outgoing", success)
    with TestClient(create_app(config(sync_api_key="test-key"))) as c:
        assert c.post(endpoint, json=PARAMS).status_code == 401
        assert (
            c.post(endpoint, json={**PARAMS, "revision_from": -1}, headers=headers).status_code
            == 422
        )
        r = c.post(endpoint, json=PARAMS, headers=headers)
        assert r.status_code == 200 and r.json()["documents"] == 1 and r.json()["logout_ok"]

        def busy(*args):
            raise SyncError("sync_already_running")

        monkeypatch.setattr(routes, "synchronize_outgoing", busy)
        assert c.post(endpoint, json=PARAMS, headers=headers).status_code == 409

        def fail(*args):
            raise SyncError("private-message")

        monkeypatch.setattr(routes, "synchronize_outgoing", fail)
        r = c.post(endpoint, json=PARAMS, headers=headers)
        assert r.status_code == 503 and "private-message" not in r.text
