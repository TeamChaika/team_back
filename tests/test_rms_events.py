"""Synthetic event fixtures: no restaurant payloads or credentials in Git."""

import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from test_iiko_connections import config
from test_reference_sync import db as db

from app.api.routes import iiko_events as routes
from app.event_storage import match_transfers, publish_events
from app.main import create_app
from app.services.iiko_events import capture_events, read_event_types, read_events
from app.services.order_topology import build_topology
from app.sync_references import Source, SyncError, register_sources

DAY = date(2026, 8, 2)
NOW = datetime(2026, 9, 11, tzinfo=UTC)
ORDER_A, ORDER_B, ACTOR, TERMINAL = [str(UUID(int=i)) for i in range(10, 14)]
META = (
    b"<groupsList><group><id>orders</id><name>Orders</name><type>"
    b"<id>dishesMovedFrom</id><name>Transfer out</name></type></group></groupsList>"
)
HEADERS = {"X-Sync-Key": "test-sync-key"}


def event(eid=1, kind="dishesMovedFrom", when="2026-08-02T18:01:25.730+03:00", **extra):
    fields = {
        "orderId": ORDER_A,
        "orderNum": "123.000000000",
        "user": ACTOR,
        "terminal": TERMINAL,
        "sum": "3300.000000000",
        "orderSumAfterDiscount": "1720.000000000",
        "dishes": "Drink, 3 x Juice",
        "comment": "124 - номер нового заказа для блюд",
        **extra,
    }
    return {"id": str(UUID(int=eid)), "type": kind, "time": when, "fields": fields}


def pair():
    return [
        event(),
        event(
            2,
            "dishesMovedTo",
            "2026-08-02T18:01:25.733+03:00",
            orderId=ORDER_B,
            orderNum="124",
            comment="123 - номер заказа-источника блюд",
        ),
    ]


def xml(events):
    root = Element("eventsList")
    for row in events:
        e = SubElement(root, "event")
        for key, name in [("id", "id"), ("time", "date"), ("type", "type")]:
            SubElement(e, name).text = row[key]
        for name, value in row["fields"].items():
            a = SubElement(e, "attribute")
            SubElement(a, "name").text = name
            SubElement(a, "value").text = value
            SubElement(a, "type").text = "String"
    return tostring(root)


def parsed(rows=None):
    return read_events(xml(pair() if rows is None else rows), DAY)[0]


def test_three_millisecond_pair_preserves_exact_values_and_redacts_credentials():
    rows, raw, count = read_events(
        xml([event(pin="test-private-pin", sum="3300.123456789123456789")]), DAY
    )
    assert count == 1 and b"test-private-pin" not in raw
    assert "test-private-pin" not in json.dumps(rows)
    assert rows[0]["fields"]["sum"] == "3300.123456789123456789"
    links = match_transfers(parsed())
    assert {link["status"] for link in links} == {"matched"}
    assert {link["evidence"]["time_delta_ms"] for link in links} == {3}


@pytest.mark.parametrize(
    "bad", [b'<!DOCTYPE eventsList [<!ENTITY x "secret">]><eventsList/>', b"<wrong/>"]
)
def test_invalid_xml_rejected(bad):
    with pytest.raises(SyncError):
        read_events(bad, DAY)


@pytest.mark.parametrize("when", ["2026-08-03T00:00:00.000+03:00", "2026-08-02T18:00:00"])
def test_scope_or_missing_timezone_rejected(when):
    with pytest.raises(SyncError):
        parsed([event(when=when)])


def test_duplicates_and_nonfinite_numbers_rejected():
    for rows in ([event(), event()], [event(sum="NaN")], [event(user="not-a-uuid")]):
        with pytest.raises(SyncError):
            parsed(rows)


def test_ambiguous_pairs_never_pick_first_and_unmatched_are_pending():
    a, b = pair()
    c = deepcopy(b)
    c["id"] = str(UUID(int=3))
    assert {x["status"] for x in match_transfers(parsed([a, b, c]))} == {"ambiguous"}
    assert match_transfers(parsed([a]))[0]["status"] == "pending"
    b["fields"]["terminal"] = str(uuid4())
    assert {x["status"] for x in match_transfers(parsed([a, b]))} == {"pending"}


def test_metadata_keeps_multiple_group_memberships():
    raw = META.replace(
        b"</groupsList>", META.replace(b"<groupsList>", b"").replace(b"orders", b"payments")
    )
    assert len(read_event_types(raw)["dishesMovedFrom"]["groups"]) == 2


def stage(db, rows=None, seconds=0, source="rms-one", day=DAY):
    register_sources(db, [Source(source, source, f"https://{source}.example/resto/api", "a" * 64)])
    run = uuid4()
    db.execute("INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'events','running')", (run,))
    rows = pair() if rows is None else rows
    snapshot = {
        "id": str(uuid4()),
        "source_id": source,
        "day": day,
        "observed_at": NOW + timedelta(seconds=seconds),
        "raw": xml(rows),
        "metadata_raw": META,
        "expected_total": len(rows),
    }
    return run, snapshot


def publish(db, rows=None, seconds=0, source="rms-one", day=DAY):
    run, snapshot = stage(db, rows, seconds, source, day)
    return publish_events(db, run, snapshot, read_event_types(META))


def test_idempotency_a_b_a_and_immutable_history(db):
    assert publish(db)["new_events"] == 2
    assert publish(db, seconds=1)["unchanged_events"] == 2
    a, b = pair()
    a["fields"]["sum"] = "15.123456789123456789"
    assert publish(db, [a, b], 2)["changed_events"] == 1
    assert publish(db, seconds=3)["changed_events"] == 1
    assert db.execute("SELECT count(*) FROM chaika.rms_events").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM chaika.rms_event_versions").fetchone()[0] == 4
    assert db.execute("SELECT count(*) FROM chaika.rms_event_observations").fetchone()[0] == 8
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("DELETE FROM chaika.rms_event_versions")
    for role in ("anon", "authenticated"):
        assert not db.execute(
            "SELECT has_table_privilege(%s,'chaika.rms_events','SELECT')", (role,)
        ).fetchone()[0]


def test_missing_event_not_deleted_and_failed_batch_rolls_back(db):
    publish(db)
    publish(db, [], 1)
    assert db.execute("SELECT count(*) FROM chaika.rms_events").fetchone()[0] == 2
    run, snapshot = stage(db, seconds=2)
    snapshot["expected_total"] = 20
    before = db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0]
    with pytest.raises(SyncError):
        publish_events(db, run, snapshot, read_event_types(META))
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == before
    assert db.execute("SELECT event_count FROM chaika.rms_event_days").fetchone()[0] == 0
    with pytest.raises(SyncError, match="stale"):
        publish(db, seconds=-1)


def test_pending_pair_is_completed_later_and_graph_uses_both_orders(db):
    a, b = pair()
    publish(db, [a])
    assert db.execute("SELECT status FROM chaika.rms_event_links").fetchone()[0] == "pending"
    publish(db, [b], 1)
    graph = build_topology(db, "rms-one", UUID(ORDER_A))
    assert graph.event_count == 2 and set(map(str, graph.order_ids)) == {ORDER_A, ORDER_B}
    assert len(graph.transfers) == 1 and graph.transfers[0]["status"] == "matched"
    assert len(graph.transfers[0]["event_ids"]) == 2
    assert all(
        e.source in {n.id for n in graph.nodes} and e.target in {n.id for n in graph.nodes}
        for e in graph.edges
    )
    assert not any(n.kind == "position" for n in graph.nodes)


def test_source_isolation_exact_numeric_and_credential_exclusion(db):
    publish(db, [event(pin="test-private-pin", sum="0.123456789123456789")])
    publish(db, source="rms-two")
    assert db.execute(
        "SELECT event_sum FROM chaika.rms_events WHERE source_id='rms-one'"
    ).fetchone()[0] == Decimal("0.123456789123456789")
    graph = build_topology(db, "rms-one", UUID(ORDER_A))
    assert graph.event_count == 1 and len(graph.order_ids) == 1
    assert "test-private-pin" not in graph.model_dump_json()
    raws = db.execute("SELECT raw FROM chaika.raw_snapshots WHERE resource='events'").fetchall()
    assert all(b"test-private-pin" not in bytes(r[0]) for r in raws)


def test_live_capture_uses_configured_host_sequential_requests_and_logout(tmp_path, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.host == "rms-one.iiko.example"
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, text="test-token")
        if request.url.path.endswith("/logout"):
            return httpx.Response(200, text="test-token")
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, content=META)
        assert dict(request.url.params) == {
            "from_time": "2026-08-02T00:00:00.000",
            "to_time": "2026-08-03T00:00:00.000",
        }
        return httpx.Response(200, content=xml(pair()))

    async def capture(connections, source_id, day):
        return await capture_events(connections, source_id, day, tmp_path / "events")

    monkeypatch.setattr(routes, "capture_events", capture)
    with TestClient(
        create_app(
            config(tmp_path, sync_api_key="test-sync-key"),
            iiko_transport=httpx.MockTransport(handle),
        )
    ) as client:
        url = "/api/v1/iiko/connections/rms-one/events?date=2026-08-02"
        assert client.get(url).status_code == 401
        r = client.get(url, headers=HEADERS)
        assert r.status_code == 200 and r.json()["total"] == 2
        assert calls[-1].url.path.endswith("/logout")
        assert len(calls) == 4
        assert "dishes" not in r.text and "test-token" not in r.text


def test_api_auth_scope_error_redaction(tmp_path, monkeypatch):
    def fail(*a):
        raise SyncError("private-payload")

    monkeypatch.setattr(routes, "synchronize_events", fail)
    with TestClient(create_app(config(tmp_path, sync_api_key="test-sync-key"))) as client:
        assert (
            client.post(
                "/api/v1/sync/events", json={"source_id": "rms-one", "date": "2026-08-02"}
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v1/events/orders/topology?source_id=rms-one&date=2026-08-02&order_number=123"
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/v1/sync/events",
                json={"source_id": "rms-one", "date": "invalid"},
                headers=HEADERS,
            ).status_code
            == 422
        )
        response = client.post(
            "/api/v1/sync/events",
            json={"source_id": "rms-one", "date": "2026-08-02"},
            headers=HEADERS,
        )
        assert response.status_code == 503 and "private-payload" not in response.text


def test_failed_projection_rolls_back_all_saved_events(db, monkeypatch):
    from app import event_storage

    def fail(*args):
        raise RuntimeError("simulated projection failure")

    monkeypatch.setattr(event_storage, "rebuild_links", fail)
    run, snapshot = stage(db)
    with pytest.raises(RuntimeError):
        publish_events(db, run, snapshot, read_event_types(META))
    for table in (
        "rms_events",
        "rms_event_versions",
        "rms_event_observations",
        "rms_event_days",
        "raw_snapshots",
    ):
        assert db.execute(f"SELECT count(*) FROM chaika.{table}").fetchone()[0] == 0


def test_cross_midnight_transfer_and_equal_time_actions(db):
    a, b = pair()
    a["time"] = "2026-08-02T23:59:59.999+03:00"
    b["time"] = "2026-08-03T00:00:00.002+03:00"
    publish(db, [a])
    publish(db, [b], 1, day=DAY + timedelta(days=1))
    graph = build_topology(db, "rms-one", UUID(ORDER_A))
    assert graph.event_count == 2 and len(graph.coverage) == 2
    assert graph.transfers[0]["evidence"]["time_delta_ms"] == 3
    equal = event(3, kind="orderPaid", when=a["time"])
    publish(db, [a, equal], 2)
    graph = build_topology(db, "rms-one", UUID(ORDER_A))
    assert not any(e.kind == "later_in_time" for e in graph.edges)


@pytest.fixture
def runtime(db, tmp_path, monkeypatch):
    import hashlib
    from contextlib import nullcontext

    from app import sync_events as sync
    from app.schemas.iiko_events import EventsCapture, EventsSyncQuery
    from app.sync_references import append_snapshot

    source = Source("rms-one", "RMS", "https://rms-one.example/resto/api", "a" * 64)
    primary = Source("primary", "Chain", "https://primary.example/resto/api", "b" * 64)
    register_sources(db, [source, primary])
    db.execute("UPDATE chaika.sources SET server_type='REPLICATED_RMS' WHERE id='rms-one'")
    seed = uuid4()
    raw_id = uuid4()
    department = uuid4()
    db.execute(
        "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'references','running')", (seed,)
    )
    append_snapshot(
        db,
        seed,
        {
            "id": raw_id,
            "source_id": "primary",
            "resource": "departments",
            "observed_at": NOW,
            "raw": b"fixture",
            "sha256": hashlib.sha256(b"fixture").hexdigest(),
            "payload": {},
        },
    )
    db.execute(
        "INSERT INTO chaika.corporate_nodes"
        "(source_id,id,type,first_seen_at,last_seen_at,last_snapshot_id) "
        "VALUES('primary',%s,'DEPARTMENT',%s,%s,%s)",
        (department, NOW, NOW, raw_id),
    )
    db.execute(
        "INSERT INTO chaika.rms_bindings(source_id,chain_source_id,department_id,state,"
        "details,observed_at,chain_snapshot_id,rms_snapshot_id,groups_snapshot_id) "
        "VALUES('rms-one','primary',%s,'matched','{}',%s,%s,%s,%s)",
        (department, NOW, raw_id, raw_id, raw_id),
    )
    folder = tmp_path / ".local/events/rms-one"
    folder.mkdir(parents=True)
    key = uuid4()
    raw = xml(pair())
    payload = EventsCapture(
        snapshot_id=key,
        source_id="rms-one",
        date=DAY,
        received_at=NOW,
        total=2,
        sha256=hashlib.sha256(raw).hexdigest(),
        source_bytes=len(raw),
        metadata_sha256=hashlib.sha256(META).hexdigest(),
    ).model_dump(mode="json")
    (folder / f"{key}.xml").write_bytes(raw)
    (folder / f"{key}.metadata.xml").write_bytes(META)
    (folder / f"{key}.meta.json").write_text(
        json.dumps(payload | {"source_fingerprint": source.fingerprint})
    )
    calls = []
    statuses = {"load": 200, "logout": 200}

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/connections"):
            return httpx.Response(
                200, json={"items": [{"connection_id": "rms-one", "base_url": source.base_url}]}
            )
        if request.url.path.endswith("/events"):
            assert request.headers["X-Sync-Key"] == "test-sync-key"
            return httpx.Response(statuses["load"], json=payload)
        return httpx.Response(statuses["logout"], json={"state": "logged_out"})

    client = httpx.Client(base_url="http://127.0.0.1:8010", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(sync.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(sync.psycopg, "connect", lambda *a, **k: nullcontext(db))
    monkeypatch.setattr(sync, "configured_sources", lambda _: [source])
    monkeypatch.setattr(sync, "BACKEND_DIR", tmp_path)
    settings = config(tmp_path, sync_api_key="test-sync-key", database_url="test-only")
    return (
        settings,
        EventsSyncQuery(source_id="rms-one", date=DAY),
        calls,
        statuses,
        folder,
        payload,
    )


def test_worker_success(db, runtime):
    from app.sync_events import synchronize_events

    settings, query, calls, *_ = runtime
    result = synchronize_events(settings, query)
    assert result["counts"]["events"] == 2 and result["counts"]["logout_ok"]
    assert calls[-1].url.path.endswith("/logout")
    assert (
        db.execute("SELECT status FROM chaika.sync_runs WHERE job='events'").fetchone()[0]
        == "succeeded"
    )


@pytest.mark.parametrize("failure", ["load", "logout", "publish", "hash", "source"])
def test_worker_failure_logs_out_and_keeps_day_uncommitted(db, runtime, monkeypatch, failure):
    from app import sync_events as sync

    settings, query, calls, statuses, folder, payload = runtime
    if failure == "publish":

        def fail(*args):
            raise RuntimeError("private-source-error")

        monkeypatch.setattr(sync, "publish_events", fail)
    elif failure == "hash":
        (folder / f"{payload['snapshot_id']}.xml").write_bytes(b"broken")
    elif failure == "source":
        path = folder / f"{payload['snapshot_id']}.meta.json"
        path.write_text(json.dumps(payload | {"source_fingerprint": "c" * 64}))
    else:
        statuses[failure] = 502
    with pytest.raises(SyncError) as error:
        sync.synchronize_events(settings, query)
    assert "private-source-error" not in str(error.value)
    assert calls[-1].url.path.endswith("/logout")
    assert db.execute("SELECT count(*) FROM chaika.rms_event_days").fetchone()[0] == 0
    assert (
        db.execute("SELECT status FROM chaika.sync_runs WHERE job='events'").fetchone()[0]
        == "failed"
    )


def test_attribute_permutation_is_not_a_business_change(db):
    rows = pair()
    publish(db, rows)
    for event_row in rows:
        event_row["fields"] = dict(reversed(list(event_row["fields"].items())))
    result = publish(db, rows, 1)
    assert result["unchanged_events"] == 2 and result["changed_events"] == 0
    assert db.execute("SELECT count(*) FROM chaika.rms_event_versions").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM chaika.rms_event_observations").fetchone()[0] == 4


def test_legacy_hash_comparison_ignores_attribute_permutation():
    from app.services.iiko_events import event_content_hash

    record = parsed()[0]
    legacy = deepcopy(record)
    legacy.pop("hash_method")
    legacy["hash"] = "a" * 64
    legacy["attributes"].reverse()
    assert event_content_hash(legacy) == record["hash"]


def test_worker_marks_abandoned_run_after_acquiring_lock(db, runtime):
    from app.sync_events import synchronize_events

    old = uuid4()
    db.execute("INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'events','running')", (old,))
    settings, query, *_ = runtime
    synchronize_events(settings, query)
    assert db.execute(
        "SELECT status,error_code FROM chaika.sync_runs WHERE id=%s", (old,)
    ).fetchone() == ("failed", "interrupted")
