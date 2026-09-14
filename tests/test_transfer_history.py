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
from test_iiko_transfers import ACCOUNT, PARAMS, STORE, body, config, document, item
from test_reference_sync import db as db

from app import sync_inventory
from app import sync_invoices as history
from app.api.routes import sync_jobs as routes
from app.main import create_app
from app.schemas.iiko_transfers import TransfersQuery, TransfersResponse
from app.services.iiko_transfers import read_transfers
from app.sync_references import Source, SyncError, register_sources
from app.transfer_storage import transfer_link_counts

RESOURCE = "transfers"
START = date(2023, 3, 1)
END = date(2023, 3, 3)


@pytest.fixture
def source(db):
    source = Source("primary", "Chain", "https://iiko.example/resto/api", "a" * 64)
    register_sources(db, [source])
    db.execute("UPDATE chaika.sources SET server_type='CHAIN' WHERE id='primary'")
    return source


def snapshot(tmp_path, source, day, items=None):
    raw = body([document(dateIncoming=f"{day}T23:00", items=[item()] if items is None else items)])
    path = tmp_path / f"{uuid4()}.json"
    path.write_bytes(raw)
    rows = [
        r.model_dump(mode="json")
        for r in read_transfers(path, TransfersQuery(date_from=day, date_to=day)).response
    ]
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
    first = snapshot(tmp_path, source, START, [item(), item(num=2)])
    state = history.commit_day(db, run, state, first)
    assert db.execute(
        "SELECT num,cost FROM chaika.internal_transfer_items ORDER BY num"
    ).fetchall() == [(1, Decimal("101.123456789123456789")), (2, Decimal("101.123456789123456789"))]
    state = history.commit_day(
        db, run, state, snapshot(tmp_path, source, START + timedelta(days=1), [])
    )
    assert (
        db.execute(
            "SELECT count(*) FROM chaika.internal_transfer_items WHERE present_in_latest"
        ).fetchone()[0]
        == 0
    )
    state = history.commit_day(
        db, run, state, snapshot(tmp_path, source, END, [item(), item(num=2)])
    )
    assert (
        db.execute(
            "SELECT count(*) FROM chaika.internal_transfer_items WHERE present_in_latest"
        ).fetchone()[0]
        == 2
    )
    assert db.execute("SELECT count(*) FROM chaika.internal_transfers").fetchone()[0] == 1
    assert (
        db.execute("SELECT first_seen_at FROM chaika.internal_transfers").fetchone()[0]
        == first["observed_at"]
    )
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 3
    assert state["completed_days"] == 3 and state["last_day_checks"]["sender_store_resolved"] == 0


def test_empty_day_does_not_delete_previous_documents(db, source, tmp_path):
    run, state = history.prepare_run(db, source, START, END, None, RESOURCE)
    state = history.commit_day(db, run, state, snapshot(tmp_path, source, START))
    empty = snapshot(tmp_path, source, START + timedelta(days=1))
    empty["payload"].update(items=[], total=0)
    state = history.commit_day(db, run, state, empty)
    assert state["documents_read"] == 1
    assert db.execute("SELECT count(*) FROM chaika.internal_transfers").fetchone()[0] == 1
    assert db.execute("SELECT present_in_latest FROM chaika.internal_transfer_items").fetchone()[0]


def test_warehouse_direction_unmatched_products_and_access_controls(db, source, tmp_path):
    run, state = history.prepare_run(db, source, START, END, None, RESOURCE)
    snap = snapshot(tmp_path, source, START)
    state = history.commit_day(db, run, state, snap)
    assert state["last_day_checks"]["sender_store_resolved"] == 0
    assert state["last_day_checks"]["unknown_products"] == 1
    assert state["last_day_checks"]["unknown_measure_units"] == 1
    assert db.execute(
        "SELECT store_from_id,store_to_id FROM chaika.internal_transfers"
    ).fetchone() == (UUID(STORE), UUID(ACCOUNT))
    db.execute(
        "INSERT INTO chaika.stores(source_id,id,first_seen_at,last_seen_at,last_snapshot_id) "
        "VALUES(%s,%s,%s,%s,%s)",
        (source.id, STORE, snap["observed_at"], snap["observed_at"], snap["id"]),
    )
    counts = transfer_link_counts(db, source.id, [UUID(int=1)])
    assert counts["sender_store_resolved"] == 1 and counts["receiver_store_resolved"] == 0
    for table in ["internal_transfers", "internal_transfer_items"]:
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
        snap["payload"]["items"][0]["items"][0]["amount"] = None
    else:
        db.execute(
            "UPDATE chaika.sync_runs SET counts=jsonb_set(counts,'{next_date}','\"2023-03-02\"') "
            "WHERE id=%s",
            (run,),
        )
    with pytest.raises((SyncError, psycopg.errors.NotNullViolation)):
        history.commit_day(db, run, state, snap)
    for table in ["raw_snapshots", "internal_transfers", "internal_transfer_items"]:
        assert db.execute(f"SELECT count(*) FROM chaika.{table}").fetchone()[0] == 0


@pytest.mark.parametrize("bad", ["fingerprint", "endpoint", "date", "hash", "rows"])
def test_capture_verifies_scope_and_raw(db, source, tmp_path, monkeypatch, bad):
    snap = snapshot(tmp_path, source, START)
    folder = tmp_path / ".local/transfers"
    folder.mkdir(parents=True)
    path = folder / f"{snap['id']}.json"
    path.write_bytes(snap["raw"])
    response = TransfersResponse(
        snapshot_id=snap["id"],
        received_at=snap["observed_at"],
        request=TransfersQuery(date_from=START, date_to=START),
        total=1,
        items_count=1,
        documents=read_transfers(path, TransfersQuery(date_from=START, date_to=START)).response,
        revision=987654,
        source_bytes=len(snap["raw"]),
        sha256=snap["sha256"],
    ).model_dump(mode="json")
    meta = {k: v for k, v in response.items() if k != "documents"}
    meta.update(
        source_fingerprint=source.fingerprint, source_endpoint="v2/documents/internalTransfer"
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
        response["documents"][0]["items"][0]["amount"] = "0"
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
        assert r.url.path.endswith("/transfers")
        day = r.url.params["date_from"]
        assert dict(r.url.params) == {"date_from": day, "date_to": day, "revision_from": "-1"}
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
    assert [r.url.params["date_from"] for r in requests if r.url.path.endswith("/transfers")] == [
        "2023-03-02",
        "2023-03-03",
    ]
    with pytest.raises(SyncError, match="scope_mismatch"):
        history.prepare_run(db, source, START, END, UUID(first["run_id"]), "incoming_invoices")


def test_bad_json_is_not_retried():
    calls = []

    def handle(r):
        calls.append(r)
        return httpx.Response(502, json={"error": {"code": "iiko_transfers_invalid_response"}})

    with httpx.Client(base_url="http://127.0.0.1:8010", transport=httpx.MockTransport(handle)) as c:
        with pytest.raises(SyncError, match="invalid_response"):
            history.fetch_day(c, START, Event(), RESOURCE)
    assert len(calls) == 1


def test_protected_sync_route_auth_scope_errors_and_result(monkeypatch):
    endpoint = "/api/v1/sync/transfers"
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

    monkeypatch.setattr(routes, "synchronize_transfers", success)
    with TestClient(create_app(config(sync_api_key="test-key"))) as c:
        assert c.post(endpoint, json=PARAMS).status_code == 401
        assert (
            c.post(endpoint, json={**PARAMS, "revision_from": 123}, headers=headers).status_code
            == 422
        )
        r = c.post(endpoint, json=PARAMS, headers=headers)
        assert r.status_code == 200 and r.json()["documents"] == 1 and r.json()["logout_ok"]

        def busy(*args):
            raise SyncError("sync_already_running")

        monkeypatch.setattr(routes, "synchronize_transfers", busy)
        assert c.post(endpoint, json=PARAMS, headers=headers).status_code == 409

        def fail(*args):
            raise SyncError("private-message")

        monkeypatch.setattr(routes, "synchronize_transfers", fail)
        r = c.post(endpoint, json=PARAMS, headers=headers)
        assert r.status_code == 503 and "private-message" not in r.text


def test_outgoing_and_transfers_interleave_from_independent_checkpoints(
    db, source, tmp_path, monkeypatch
):
    from test_outgoing_history import snapshot as outgoing_snapshot

    calls = []
    factory = httpx.Client

    def handle(r):
        path = r.url.path.rsplit("/", 1)[-1]
        if path == "connections":
            return httpx.Response(
                200, json={"items": [{"connection_id": "primary", "base_url": source.base_url}]}
            )
        if path == "logout":
            calls.append((path, None))
            return httpx.Response(200, json={"state": "logged_out"})
        day = r.url.params["date_from"]
        assert r.url.params.get("revision_from") == ("-1" if path == "transfers" else None)
        calls.append((path, day))
        return httpx.Response(200, json={})

    monkeypatch.setattr(history.psycopg, "connect", lambda *a, **k: nullcontext(db))
    monkeypatch.setattr(history, "configured_sources", lambda _: [source])
    monkeypatch.setattr(
        history.httpx, "Client", lambda **k: factory(**k, transport=httpx.MockTransport(handle))
    )
    monkeypatch.setattr(
        history,
        "capture_inventory",
        lambda s, src, res, response, day: (snapshot if res == "transfers" else outgoing_snapshot)(
            tmp_path, src, day
        ),
    )
    run, state = history.prepare_run(db, source, START, END, None, "outgoing_invoices")
    history.commit_day(db, run, state, outgoing_snapshot(tmp_path, source, START))
    reports = history.synchronize_histories(
        config(database_url="unused"),
        "http://127.0.0.1:8010",
        START,
        END,
        [
            history.HistoryRequest("transfers", tmp_path / "transfers.json"),
            history.HistoryRequest("outgoing_invoices", tmp_path / "outgoing.json", run),
        ],
    )
    assert calls == [
        ("transfers", "2023-03-01"),
        ("outgoing-invoices", "2023-03-02"),
        ("transfers", "2023-03-02"),
        ("outgoing-invoices", "2023-03-03"),
        ("transfers", "2023-03-03"),
        ("logout", None),
    ]
    assert all(r["status"] == "succeeded" and r["counts"]["completed_days"] == 3 for r in reports)
