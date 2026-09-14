import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from test_iiko_dictionaries import ID, PAYLOADS
from test_iiko_employees import config
from test_reference_sync import db as db

from app import sync_dictionaries as sync
from app.api.routes import sync_jobs as routes
from app.integrations.iiko.dictionaries import BUNDLED_DICTIONARIES, DICTIONARIES
from app.main import create_app
from app.sync_references import Source, SyncError, append_snapshot, register_sources


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    settings = config()
    source = Source(
        "primary",
        "Chain",
        str(settings.iiko_base_url).rstrip("/"),
        hashlib.sha256(
            f"{str(settings.iiko_base_url).rstrip('/')}\n{settings.iiko_login}".encode()
        ).hexdigest(),
    )
    folder = tmp_path / ".local/dictionaries"

    def handle(request):
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text="test-token")
        kind = next(
            k
            for k in BUNDLED_DICTIONARIES
            if request.url.path.endswith("/" + DICTIONARIES[k].endpoint)
        )
        return httpx.Response(200, content=PAYLOADS[kind])

    monkeypatch.setattr(sync, "BACKEND_DIR", tmp_path)
    with TestClient(
        create_app(
            settings, iiko_transport=httpx.MockTransport(handle), dictionaries_directory=folder
        )
    ) as client:
        responses = {
            kind: client.post(f"/api/v1/iiko/dictionaries/{kind}/load").json()
            for kind in BUNDLED_DICTIONARIES
        }
    snapshots = {
        kind: sync.capture_dictionary(settings, source, kind, response)
        for kind, response in responses.items()
    }
    return source, snapshots, responses, folder


@pytest.mark.parametrize("bad", ["source", "scope", "hash", "count"])
def test_capture_requires_complete_verified_source(bundle, bad):
    source, _, responses, folder = bundle
    kind = "measure-units"
    response = responses[kind]
    path = folder / kind / "current.json"
    meta = json.loads(path.read_text())
    if bad == "source":
        meta["source_fingerprint"] = "b" * 64
    elif bad == "scope":
        response["request"]["includeDeleted"] = "false"
        meta["snapshot"] = response
    elif bad == "hash":
        (folder / kind / f"{response['snapshot_id']}.json").write_bytes(b"[]")
    else:
        response["total"] = 99
        meta["snapshot"] = response
    path.write_text(json.dumps(meta))
    with pytest.raises(SyncError):
        sync.capture_dictionary(config(), source, kind, response)


def stage(db, bundle):
    source, snapshots, _, _ = bundle
    register_sources(db, [source])
    run = uuid4()
    db.execute(
        "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'dictionaries','running')", (run,)
    )
    for snapshot in snapshots.values():
        append_snapshot(db, run, snapshot)
    return run, snapshots


def test_publish_preserves_fields_deleted_and_repeat_keys(db, bundle):
    _, snapshots = stage(db, bundle)
    counts, links = sync.publish_dictionaries(db, snapshots)
    assert [counts[DICTIONARIES[k].table] for k in BUNDLED_DICTIONARIES] == [1, 1, 1]
    assert counts["product_categories_deleted"] == 1
    assert links["counteragent_stores_unmatched"] == 1
    sync.publish_dictionaries(db, snapshots)
    assert db.execute(
        "SELECT code,represents_store,represented_store_id FROM chaika.counteragents"
    ).fetchone() == ("", True, UUID(int=2))
    assert db.execute(
        "SELECT count(*),bool_and(first_seen_at=last_seen_at) FROM chaika.counteragents"
    ).fetchone() == (1, True)
    assert db.execute("SELECT code,root_type FROM chaika.measure_units").fetchone() == (
        "001",
        "MeasureUnit",
    )
    assert db.execute(
        "SELECT deleted,code,root_type FROM chaika.product_categories"
    ).fetchone() == (True, None, None)


def test_partial_bundle_rolls_back_all_dictionaries(db, bundle):
    _, snapshots = stage(db, bundle)
    sync.publish_dictionaries(db, snapshots)
    broken = deepcopy(snapshots)
    broken["counteragents"]["payload"]["items"][0]["name"] = "Wrong"
    broken["categories"]["payload"]["total"] = 99
    with pytest.raises(SyncError):
        sync.publish_dictionaries(db, broken)
    assert db.execute("SELECT name FROM chaika.counteragents").fetchone()[0] == "Test supplier"
    with pytest.raises(SyncError):
        sync.publish_dictionaries(db, {"counteragents": snapshots["counteragents"]})


def test_empty_then_return_keeps_source_flags_and_history(db, bundle):
    run, first = stage(db, bundle)
    sync.publish_dictionaries(db, first)
    for seconds, empty in [(1, True), (2, False)]:
        data = deepcopy(first)
        for snap in data.values():
            snap["id"] = str(uuid4())
            snap["observed_at"] += timedelta(seconds=seconds)
            snap["payload"]["snapshot_id"] = snap["id"]
            snap["payload"]["received_at"] = snap["observed_at"].isoformat()
            if empty:
                snap["payload"].update(total=0, items=[])
            append_snapshot(db, run, snap)
        sync.publish_dictionaries(db, data)
        assert db.execute(
            "SELECT present_in_latest,deleted FROM chaika.counteragents"
        ).fetchone() == (not empty, False)
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 9
    assert (
        db.execute("SELECT first_seen_at FROM chaika.counteragents").fetchone()[0]
        == first["counteragents"]["observed_at"]
    )


def test_historical_supplier_and_unit_links_include_unknown_ids(db, bundle):
    _, snapshots = stage(db, bundle)
    for supplier in [ID, str(UUID(int=99))]:
        doc = uuid4()
        db.execute(
            "INSERT INTO chaika.incoming_invoices(source_id,id,supplier_id,last_export_date,"
            "details,first_seen_at,last_seen_at,last_snapshot_id) "
            "VALUES('primary',%s,%s,'2026-09-10','{}',now(),now(),%s)",
            (doc, supplier, snapshots["counteragents"]["id"]),
        )
        db.execute(
            "INSERT INTO chaika.incoming_invoice_items(source_id,document_id,num,sum,"
            "amount_unit_id,details) VALUES('primary',%s,1,0,%s,'{}')",
            (doc, supplier),
        )
    _, links = sync.publish_dictionaries(db, snapshots)
    assert links["invoice_suppliers_ids"] == links["invoice_units_ids"] == 2
    assert links["invoice_suppliers_unmatched"] == links["invoice_units_unmatched"] == 1


def test_private_table_privileges(db, bundle):
    _, snapshots = stage(db, bundle)
    sync.publish_dictionaries(db, snapshots)
    for table in ("counteragents", "measure_units", "product_categories"):
        for role in ("anon", "authenticated"):
            assert not db.execute(
                "SELECT has_table_privilege(%s,%s,'SELECT')", (role, "chaika." + table)
            ).fetchone()[0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute("DELETE FROM chaika." + table)


@pytest.fixture
def runtime(db, bundle, monkeypatch):
    source, _, responses, _ = bundle
    register_sources(db, [source])
    db.execute("UPDATE chaika.sources SET server_type='CHAIN'")
    monkeypatch.setattr(sync, "configured_sources", lambda _: [source])
    monkeypatch.setattr(sync.psycopg, "connect", lambda *a, **k: nullcontext(db))
    calls = []
    states = {"measure-units": 200, "logout": 200}

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/connections"):
            return httpx.Response(
                200, json={"items": [{"connection_id": "primary", "base_url": source.base_url}]}
            )
        if request.url.path.endswith("/logout"):
            return httpx.Response(states["logout"], json={"state": "logged_out"})
        kind = request.url.path.split("/")[-2]
        return httpx.Response(states.get(kind, 200), json=responses[kind])

    client = httpx.Client(base_url="http://127.0.0.1:8010", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(sync.httpx, "Client", lambda **kwargs: client)
    return calls, states


def test_worker_orders_requests_and_publishes_three_snapshots(db, runtime):
    calls, _ = runtime
    result = sync.synchronize_dictionaries(config(database_url="test-only"))
    assert result["status"] == "succeeded" and result["counts"]["logout_ok"] is True
    assert calls == ["/api/v1/iiko/connections"] + [
        f"/api/v1/iiko/dictionaries/{k}/load" for k in BUNDLED_DICTIONARIES
    ] + ["/api/v1/iiko/connections/primary/logout"]
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 3


@pytest.mark.parametrize("failure", ["measure-units", "logout", "publish"])
def test_worker_failure_keeps_all_previous_data(db, runtime, monkeypatch, failure):
    calls, states = runtime
    if failure == "publish":

        def fail(*args):
            raise ValueError("private-source")

        monkeypatch.setattr(sync, "publish_dictionaries", fail)
    else:
        states[failure] = 502
    with pytest.raises(SyncError) as error:
        sync.synchronize_dictionaries(config(database_url="test-only"))
    assert "private-source" not in str(error.value)
    assert calls[-1].endswith("/logout")
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM chaika.counteragents").fetchone()[0] == 0
    assert db.execute("SELECT status FROM chaika.sync_runs").fetchone()[0] == "failed"


@pytest.mark.parametrize(
    "failure,expected", [("sync_already_running", 409), ("private-error", 503)]
)
def test_api_auth_busy_and_redacted_errors(monkeypatch, failure, expected):
    calls = []

    def fail(*args):
        calls.append(1)
        raise SyncError(failure)

    monkeypatch.setattr(routes, "synchronize_dictionaries", fail)
    with TestClient(create_app(config(sync_api_key="test-key"))) as client:
        assert client.post("/api/v1/sync/dictionaries").status_code == 401 and not calls
        response = client.post("/api/v1/sync/dictionaries", headers={"X-Sync-Key": "test-key"})
        assert response.status_code == expected and "private-error" not in response.text
