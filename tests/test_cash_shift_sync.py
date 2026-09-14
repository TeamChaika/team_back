"""Cash-shift history, UUID binding and failure isolation on a separate PostgreSQL."""

import hashlib
import json
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_iiko_cash_shifts import DEPARTMENT, GROUP, POS, body, config
from test_reference_sync import db as db

from app import sync_cash_shifts as sync
from app.api.routes import sync_jobs as routes
from app.main import create_app
from app.schemas.iiko_cash_shifts import CashShiftsQuery, CashShiftsResponse
from app.schemas.sync_jobs import CashShiftSyncQuery
from app.services.iiko_cash_shifts import read_cash_shifts
from app.sync_references import Source, SyncError, append_snapshot, register_sources
from app.web.repository import Repository, Scope


def groups_xml(department=DEPARTMENT, group=GROUP):
    return (
        f"<groupDtoes><groupDto><id>{group}</id><departmentId>{department}</departmentId>"
        f"<pointOfSaleDtoes><pointOfSaleDto><id>{POS}</id><name>Касса</name>"
        "</pointOfSaleDto></pointOfSaleDtoes></groupDto></groupDtoes>"
    ).encode()


@pytest.fixture
def sample(tmp_path):
    source = Source("primary", "Chain", "https://iiko.example/resto/api", "a" * 64)
    folder = tmp_path / ".local/cash-shifts"
    folder.mkdir(parents=True)
    key, now = uuid4(), datetime.now(UTC)
    raw = body()
    path = folder / f"{key}.json"
    path.write_bytes(raw)
    query = CashShiftsQuery(open_date_from="2026-09-09", open_date_to="2026-09-09")
    response = CashShiftsResponse(
        snapshot_id=key,
        received_at=now,
        request=query,
        total=1,
        items=read_cash_shifts(path),
        source_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    ).model_dump(mode="json")
    meta = {k: v for k, v in response.items() if k != "items"}
    meta.update(source_fingerprint=source.fingerprint, source_endpoint="v2/cashshifts/list")
    (folder / f"{key}.meta.json").write_text(json.dumps(meta))
    group = dict(
        id=str(uuid4()),
        source_id="primary",
        resource="groups",
        observed_at=now,
        raw=groups_xml(),
        payload={},
    )
    group["sha256"] = hashlib.sha256(group["raw"]).hexdigest()
    return source, folder, query, response, group


def capture(sample):
    source, folder, query, response, _ = sample
    return sync.capture_cash_shifts(config(), source, query, response, folder)


def test_capture_keeps_precision_source_time_and_serial(sample):
    item = capture(sample)["payload"]["items"][0]
    assert item["pay_orders"] == "101.123456789123456789"
    assert item["pay_income"] == "-101.123456789123456789"
    assert item["cash_reg_serial"] == "000001 "
    assert item["close_date"].startswith("2026-09-10")


@pytest.mark.parametrize("bad", ["source", "scope", "hash", "rows"])
def test_capture_rejects_mismatched_capture(sample, bad):
    _, folder, _, response, _ = sample
    path = folder / f"{response['snapshot_id']}.meta.json"
    meta = json.loads(path.read_text())
    if bad == "source":
        meta["source_fingerprint"] = "x"
    elif bad == "scope":
        response["request"]["status"] = "OPEN"
        meta["request"] = response["request"]
    elif bad == "hash":
        (folder / f"{response['snapshot_id']}.json").write_bytes(b"[]")
    else:
        response["items"][0]["pay_orders"] = "1"
    path.write_text(json.dumps(meta))
    with pytest.raises(SyncError):
        capture(sample)


def test_mapping_requires_unique_explicit_department():
    known = {UUID(DEPARTMENT)}
    assert sync.point_of_sale_bindings(groups_xml(), known)[UUID(POS)]["department_id"] in known
    assert sync.point_of_sale_bindings(groups_xml(), set())[UUID(POS)] == {
        "mapping_state": "unknown_department"
    }
    conflict = groups_xml().replace(
        b"</groupDtoes>", groups_xml(group=str(uuid4())).replace(b"<groupDtoes>", b"")
    )
    assert sync.point_of_sale_bindings(conflict, known)[UUID(POS)] == {"mapping_state": "ambiguous"}
    assert sync.point_of_sale_bindings(b"<groupDtoes/>", known) == {}


def stage(db, sample):
    register_sources(db, [sample[0]])
    db.execute("UPDATE chaika.sources SET server_type='CHAIN'")
    run = uuid4()
    db.execute(
        "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'cash_shifts','running')", (run,)
    )
    group = sample[4]
    append_snapshot(db, run, group)
    db.execute(
        "INSERT INTO chaika.corporate_nodes(source_id,id,type,name,first_seen_at,"
        "last_seen_at,last_snapshot_id) VALUES('primary',%s,'DEPARTMENT','Ресторан',%s,%s,%s)",
        (DEPARTMENT, group["observed_at"], group["observed_at"], group["id"]),
    )
    snapshot = capture(sample)
    append_snapshot(db, run, snapshot)
    return run, snapshot, group


def next_snapshot(db, run, first, seconds, items=None, request=None):
    new = deepcopy(first)
    new.update(id=str(uuid4()), observed_at=first["observed_at"] + timedelta(seconds=seconds))
    new["payload"].update(snapshot_id=new["id"], received_at=new["observed_at"].isoformat())
    if items is not None:
        new["payload"].update(items=items, total=len(items))
    if request is not None:
        new["payload"]["request"] = request
    append_snapshot(db, run, new)
    return new


def test_boundary_shift_visible_by_source_date_and_deduplicated_across_request_days(db, sample):
    run, first, groups = stage(db, sample)
    request = {
        **first["payload"]["request"],
        "open_date_from": "2026-09-10",
        "open_date_to": "2026-09-10",
    }
    boundary = next_snapshot(db, run, first, 1, request=request)
    sync.publish_cash_shifts(db, boundary, groups)
    assert db.execute("SELECT open_date::date::text FROM chaika.cash_shifts").fetchone() == (
        "2026-09-09",
    )
    assert db.execute("SELECT open_day::text FROM chaika.cash_shift_days").fetchone() == (
        "2026-09-10",
    )
    sync.publish_cash_shifts(db, first, groups)
    assert db.execute("SELECT count(*) FROM chaika.cash_shift_observations").fetchone()[0] == 2
    row = db.execute("SELECT id,last_snapshot_id FROM chaika.cash_shifts").fetchall()
    assert row == [(UUID(first["payload"]["items"][0]["id"]), UUID(boundary["id"]))]


def test_history_empty_day_reappearance_stale_and_duplicate(db, sample):
    run, first, groups = stage(db, sample)
    counts = sync.publish_cash_shifts(db, first, groups)
    assert counts["matched"] == 1 and counts["closed"] == 1 and counts["accepted"] == 0
    sync.publish_cash_shifts(db, first, groups)
    assert db.execute("select count(*) from chaika.cash_shift_observations").fetchone()[0] == 1
    assert db.execute("select pay_orders from chaika.cash_shifts").fetchone()[0] == Decimal(
        "101.123456789123456789"
    )
    empty = next_snapshot(db, run, first, 1, [])
    sync.publish_cash_shifts(db, empty, groups)
    assert db.execute("select count(*) from chaika.cash_shifts").fetchone()[0] == 0
    returned = next_snapshot(db, run, first, 2)
    sync.publish_cash_shifts(db, returned, groups)
    sync.publish_cash_shifts(db, next_snapshot(db, run, first, -1, []), groups)
    assert db.execute("select count(*) from chaika.cash_shifts").fetchone()[0] == 1
    assert db.execute("select count(*) from chaika.cash_shift_observations").fetchone()[0] == 2
    for role in ("anon", "authenticated"):
        assert not db.execute(
            "select has_table_privilege(%s,'chaika.cash_shifts','SELECT')", (role,)
        ).fetchone()[0]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("delete from chaika.cash_shift_observations")


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "OPEN"),
        ("revision_from", 1),
        ("department_id", [DEPARTMENT]),
        ("group_id", [GROUP]),
        ("open_date_to", "2026-09-10"),
    ],
)
def test_partial_snapshot_cannot_replace_complete_day(db, sample, field, value):
    _, first, groups = stage(db, sample)
    first["payload"]["request"][field] = value
    with pytest.raises(SyncError, match="cash_shifts_incomplete"):
        sync.publish_cash_shifts(db, first, groups)
    assert db.execute("select count(*) from chaika.cash_shift_days").fetchone()[0] == 0


def test_restaurant_scope_applies_to_list_detail_and_unknown(db, sample):
    _, first, groups = stage(db, sample)
    sync.publish_cash_shifts(db, first, groups)
    repo = Repository(config())

    @contextmanager
    def connection():
        with db.cursor(row_factory=psycopg.rows.dict_row) as cursor:
            yield cursor

    repo.connection = connection
    own = Scope({"role": "manager"}, ({"id": UUID(DEPARTMENT), "code": "1"},), None, (), ())
    other = Scope({"role": "manager"}, ({"id": uuid4(), "code": "2"},), None, (), ())
    assert repo.resources(own, "cash-shifts")["total"] == 1
    assert (
        repo.detail(own, "cash-shifts", UUID(int=1))["provenance"]["groups_snapshot_id"]
        == groups["id"]
    )
    assert repo.resources(other, "cash-shifts")["total"] == 0
    with pytest.raises(HTTPException) as error:
        repo.detail(other, "cash-shifts", UUID(int=1))
    assert error.value.status_code == 404


@pytest.mark.parametrize("failure", [None, "load", "logout", "publish"])
def test_worker_logs_out_and_records_only_committed_days(db, sample, monkeypatch, failure):
    source, folder, _, response, groups = sample
    register_sources(db, [source])
    db.execute("UPDATE chaika.sources SET server_type='CHAIN'")
    monkeypatch.setattr(sync, "configured_sources", lambda _: [source])
    monkeypatch.setattr(sync, "BACKEND_DIR", folder.parent.parent)
    monkeypatch.setattr(sync, "capture_snapshot", lambda *args: groups)
    monkeypatch.setattr(sync.psycopg, "connect", lambda *a, **k: nullcontext(db))
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/connections"):
            return httpx.Response(
                200, json={"items": [{"connection_id": "primary", "base_url": source.base_url}]}
            )
        if request.url.path.endswith("/corporate-groups"):
            return httpx.Response(200, json={})
        if request.url.path.endswith("/cash-shifts"):
            return httpx.Response(502 if failure == "load" else 200, json=response)
        assert request.url.path.endswith("/logout")
        return httpx.Response(502 if failure == "logout" else 200, json={"state": "logged_out"})

    client = httpx.Client(base_url="http://127.0.0.1:8010", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(sync.httpx, "Client", lambda **kwargs: client)
    if failure == "publish":

        def fail(*args):
            raise ValueError("private detail")

        monkeypatch.setattr(sync, "publish_cash_shifts", fail)
    query = CashShiftSyncQuery(open_date_from="2026-09-09", open_date_to="2026-09-09")
    saved_days = []

    def on_day_saved(value):
        assert db.execute("select count(*) from chaika.cash_shift_days").fetchone()[0] == 1
        saved_days.append(value)

    if failure:
        with pytest.raises(SyncError) as error:
            sync.synchronize_cash_shifts(
                config(database_url="test"), query, on_day_saved=on_day_saved
            )
        assert "private detail" not in str(error.value)
    else:
        assert sync.synchronize_cash_shifts(
            config(database_url="test"), query, on_day_saved=on_day_saved
        )["counts"]["logout_ok"]
    assert calls[-1].endswith("/logout")
    assert len(saved_days) == (1 if failure in (None, "logout") else 0)
    assert db.execute("select count(*) from chaika.cash_shift_days").fetchone()[0] == (
        1 if failure in (None, "logout") else 0
    )


def test_sync_route_is_protected_and_validates_dates(monkeypatch):
    called = []
    monkeypatch.setattr(routes, "synchronize_cash_shifts", lambda *a: called.append(a))
    with TestClient(create_app(config(sync_api_key="test-sync-key"))) as c:
        data = {"open_date_from": "2026-09-01", "open_date_to": "2026-09-09"}
        assert c.post("/api/v1/sync/cash-shifts", json=data).status_code == 401
        assert (
            c.post(
                "/api/v1/sync/cash-shifts", json=data, headers={"X-Sync-Key": "test-sync-key"}
            ).status_code
            == 422
        )
    assert not called
