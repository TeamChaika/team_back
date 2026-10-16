"""Published values and original provenance remain frozen across policy changes."""

import hashlib
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_cash_shift_sync import next_snapshot
from test_cash_shift_sync import sample as _cash_fixture
from test_cash_shift_sync import stage as cash_stage
from test_inventory_sync import inventory as inventory
from test_reference_sync import bundle as bundle
from test_reference_sync import db as db
from test_reference_sync import stage

from app.import_sales_review import KINDS
from app.import_sales_review import publish as publish_sales
from app.sync_cash_shifts import publish_cash_shifts
from app.sync_counteragent_balances import publish_counteragent_balances
from app.sync_inventory import publish_inventory, restore_snapshots
from app.sync_invoices import publish_invoices
from app.sync_references import append_snapshot
from app.sync_references import publish as publish_references
from app.sync_scope import active_sources, filter_documents, load_scope, source_enabled
from app.sync_store_balances import publish_store_balances
from app.web.balances import read_balances


@pytest.fixture
def cash_sample(tmp_path):
    return _cash_fixture.__wrapped__(tmp_path)


def policy(db, departments):
    settings = {
        "sync": {
            "mode": "all" if departments is None else "selected",
            "department_ids": departments or [],
        },
        "writeoff": {"mode": "all", "store_ids": []},
        "waybill": {"mode": "all", "store_ids": []},
    }
    db.execute(
        "INSERT INTO "
        "chaika.operation_settings(singleton,revision,settings,updated_by) "
        "VALUES(true,1,%s,%s) ON CONFLICT(singleton) DO UPDATE SET "
        "revision=operation_settings.revision+1,settings=EXCLUDED.settings",
        (Jsonb(settings), uuid4()),
    )


def topology(db, bundle):
    sources, snapshots = stage(db, bundle)
    publish_references(db, sources, snapshots)
    dept = snapshots["primary", "departments"]["payload"]["items"][0]["id"]
    store = snapshots["primary", "stores"]["payload"]["items"][0]["id"]
    other, other_store = str(uuid4()), str(uuid4())
    db.execute(
        "INSERT INTO "
        "chaika.corporate_nodes(source_id,id,name,type,first_seen_at,la"
        "st_seen_at,last_snapshot_id) SELECT 'primary',%s,'Other','DEPA"
        "RTMENT',first_seen_at,last_seen_at,last_snapshot_id FROM chaik"
        "a.corporate_nodes WHERE id=%s",
        (other, dept),
    )
    db.execute(
        "INSERT INTO "
        "chaika.stores(source_id,id,parent_id,name,first_seen_at,last_s"
        "een_at,last_snapshot_id) SELECT 'primary',%s,%s,'Other store',"
        "first_seen_at,last_seen_at,last_snapshot_id FROM chaika.stores"
        " WHERE id=%s",
        (other_store, other, store),
    )
    return sources, snapshots, dept, store, other, other_store


def raw_snapshot(db, resource, payload, seconds=0):
    key, run = uuid4(), uuid4()
    now = datetime.now(UTC) + timedelta(seconds=seconds)
    payload = {
        **payload,
        "snapshot_id": str(key),
        "received_at": now.isoformat(),
        "source_bytes": 2,
        "sha256": hashlib.sha256(b"[]").hexdigest(),
    }
    snapshot = dict(
        id=str(key),
        source_id="primary",
        resource=resource,
        observed_at=now,
        raw=b"[]",
        sha256=hashlib.sha256(b"[]").hexdigest(),
        payload=payload,
    )
    db.execute("INSERT INTO chaika.sync_runs(id,status) VALUES(%s,'running')", (run,))
    append_snapshot(db, run, snapshot)
    return snapshot


def test_balance_freeze_same_time_and_reenable(db, bundle):
    _, _, dept, store, other, other_store = topology(db, bundle)
    payload = dict(
        request={"timestamp": "2026-09-09T23:59:59"},
        total=2,
        items=[
            dict(store_id=s, product_id=str(uuid4()), amount="1", sum="10")
            for s in [store, other_store]
        ],
    )
    first = raw_snapshot(db, "store_balances", payload)
    publish_store_balances(db, first)
    policy(db, [dept])
    changed = deepcopy(payload)
    for row in changed["items"]:
        row.update(amount="2", sum="20")
    second = raw_snapshot(db, "store_balances", changed, 1)
    result = publish_store_balances(db, second)
    rows = {
        str(s): (amount, str(origin) if origin else None)
        for s, amount, origin in db.execute(
            "SELECT store_id,amount,origin_snapshot_id FROM "
            "chaika.store_balance_items WHERE snapshot_id=%s",
            (second["id"],),
        )
    }
    assert rows[store] == (Decimal("2"), None)
    assert rows[other_store] == (Decimal("1"), first["id"])
    assert result["frozen_rows"] == 1
    changed["request"]["timestamp"] = "2026-09-10T23:59:59"
    third = raw_snapshot(db, "store_balances", changed, 2)
    publish_store_balances(db, third)
    assert (
        db.execute(
            "SELECT count(*) FROM chaika.store_balance_items WHERE snapshot_id=%s", (third["id"],)
        ).fetchone()[0]
        == 1
    )
    original_factory = db.row_factory
    db.row_factory = dict_row
    try:
        result = read_balances(db, SimpleNamespace(unrestricted=True, departments=[], store_ids=[]))
    finally:
        db.row_factory = original_factory
    frozen = next(row for row in result["rows"] if str(row["store_id"]) == other_store)
    assert frozen["frozen"] and frozen["accounting_timestamp"].isoformat() == "2026-09-09T23:59:59"
    assert frozen["amount"] == Decimal(1) and frozen["last_synced_at"] == first["observed_at"]
    policy(db, None)
    fourth = raw_snapshot(db, "store_balances", changed, 3)
    publish_store_balances(db, fourth)
    assert (
        db.execute(
            "SELECT count(*) FROM chaika.store_balance_items WHERE "
            "snapshot_id=%s AND amount=2 AND origin_snapshot_id IS NULL",
            (fourth["id"],),
        ).fetchone()[0]
        == 2
    )


def test_counteragent_freezes_null_and_disabled_department(db, bundle):
    _, _, dept, _, other, _ = topology(db, bundle)
    payload = dict(
        request={"timestamp": "2026-09-09T23:59:59"},
        total=3,
        items=[
            dict(account_id=str(uuid4()), counteragent_id=None, department_id=d, sum="10")
            for d in [dept, other, None]
        ],
    )
    first = raw_snapshot(db, "counteragent_balances", payload)
    publish_counteragent_balances(db, first)
    policy(db, [dept])
    changed = deepcopy(payload)
    for row in changed["items"]:
        row["sum"] = "99"
    second = raw_snapshot(db, "counteragent_balances", changed, 1)
    assert publish_counteragent_balances(db, second)["frozen_rows"] == 2
    assert (
        db.execute(
            "SELECT count(*) FROM chaika.counteragent_balance_items WHERE "
            "snapshot_id=%s AND sum=10 AND origin_snapshot_id=%s",
            (second["id"], first["id"]),
        ).fetchone()[0]
        == 2
    )


def sales_bundle(day, departments, value):
    return dict(
        id=uuid4(),
        manifest={"business_date": day},
        checks=[],
        reports={
            kind: dict(
                capture_id=None,
                raw=b"[]",
                info={
                    "request": {"groupByRowFields": ["Department.Id"]},
                    "received_at": datetime.now(UTC).isoformat(),
                    "sha256": hashlib.sha256(b"[]").hexdigest(),
                },
                rows=[{"Department.Id": d, "DishDiscountSumInt": value} for d in departments],
            )
            for kind in KINDS
        },
    )


def test_sales_carry_same_day_origin_and_no_invented_next_day(db, bundle):
    _, _, dept, _, other, _ = topology(db, bundle)
    publish_sales(db, sales_bundle("2026-09-09", [dept, other], 10))
    policy(db, [dept])
    result = publish_sales(db, sales_bundle("2026-09-09", [dept, other], 99))
    assert result["frozen_rows"] == 7 and result["partial_scope"]
    rows = db.execute(
        "SELECT x.department_id,x.revenue,x.origin_report_id FROM "
        "chaika.sales_report_rows x JOIN chaika.sales_reports r ON "
        "r.id=x.report_id JOIN chaika.sales_report_days d ON "
        "d.current_set_id=r.set_id WHERE r.kind='daily'"
    ).fetchall()
    assert {(str(d), value) for d, value, origin in rows} == {
        (dept, Decimal(99)),
        (other, Decimal(10)),
    }
    assert next(origin for d, value, origin in rows if str(d) == other) is not None
    assert publish_sales(db, sales_bundle("2026-09-10", [dept, other], 99))["frozen_rows"] == 0
    policy(db, None)
    assert publish_sales(db, sales_bundle("2026-09-09", [dept, other], 101))["frozen_rows"] == 0


def test_document_atomicity_disabled_moved_and_cross_store(db, bundle):
    _, _, dept, store, other, other_store = topology(db, bundle)
    rows = [
        {
            "id": str(uuid4()),
            "default_store_id": s,
            "items": [{"num": 1, "store_id": s, "sum": "10"}],
        }
        for s in [store, other_store]
    ]
    publish_invoices(db, raw_snapshot(db, "incoming_invoices", {"items": rows}), date(2026, 9, 9))
    policy(db, [dept])
    rows[1]["default_store_id"] = rows[1]["items"][0]["store_id"] = store
    rows[0]["items"][0]["sum"] = "99"
    second = raw_snapshot(db, "incoming_invoices", {"items": rows}, 1)
    publish_invoices(db, second, date(2026, 9, 9))
    values = {
        str(key): amount
        for key, amount in db.execute("SELECT document_id,sum FROM chaika.incoming_invoice_items")
    }
    assert values[rows[0]["id"]] == Decimal(99)
    assert values[rows[1]["id"]] == Decimal(10)
    mixed = {
        "resource": "transfers",
        "source_id": "primary",
        "payload": {
            "items": [
                {
                    "id": str(uuid4()),
                    "store_from_id": store,
                    "store_to_id": other_store,
                    "items": [],
                }
            ]
        },
    }
    assert filter_documents(db, mixed) == []
    policy(db, [])
    assert load_scope(db).empty and filter_documents(db, second) == []


def test_disabled_reference_source_is_physical_and_mapping_stays(db, bundle):
    sources, snapshots, dept, _, other, _ = topology(db, bundle)
    policy(db, [other])
    assert not source_enabled(db, "rms-a")
    assert [s.id for s in active_sources(db, sources, keep_primary=True)] == ["primary"]
    changed = deepcopy(snapshots)
    changed["primary", "departments"]["payload"]["items"][0]["name"] = "Changed disabled"
    publish_references(db, sources, changed)
    assert db.execute("SELECT configured FROM chaika.sources WHERE id='rms-a'").fetchone() == (
        True,
    )
    assert db.execute(
        "SELECT state,department_id FROM chaika.rms_bindings WHERE source_id='rms-a'"
    ).fetchone() == ("matched", UUID(dept))
    assert db.execute(
        "SELECT name FROM chaika.corporate_nodes WHERE id=%s", (dept,)
    ).fetchone() == ("Restaurant",)


def test_cash_freezes_original_values_and_observation(db, cash_sample):
    run, first, groups = cash_stage(db, cash_sample)
    publish_cash_shifts(db, first, groups)
    policy(db, [str(uuid4())])
    second = next_snapshot(db, run, first, 1)
    second["payload"]["items"][0]["pay_orders"] = "999"
    result = publish_cash_shifts(db, second, groups)
    assert result["frozen_rows"] == 1
    row = db.execute(
        "SELECT pay_orders,origin_snapshot_id,last_seen_at FROM chaika.cash_shifts"
    ).fetchone()
    assert row[0] == Decimal(first["payload"]["items"][0]["pay_orders"])
    assert str(row[1]) == first["id"] and row[2] == first["observed_at"]


def test_reference_admits_new_active_store_but_never_moves_frozen_store(db, bundle):
    sources, snapshots, dept, store, other, other_store = topology(db, bundle)
    policy(db, [dept])
    changed = deepcopy(snapshots)
    items = changed["primary", "stores"]["payload"]["items"]
    new_store = str(uuid4())
    items[0]["parent_id"] = other
    items.append({"id": new_store, "parent_id": dept, "name": "New active store"})
    items.append({"id": other_store, "parent_id": dept, "name": "Moved frozen store"})
    publish_references(db, sources, changed)
    assert db.execute("SELECT parent_id FROM chaika.stores WHERE id=%s", (store,)).fetchone() == (
        UUID(dept),
    )
    assert db.execute(
        "SELECT parent_id FROM chaika.stores WHERE id=%s", (new_store,)
    ).fetchone() == (UUID(dept),)
    assert db.execute(
        "SELECT parent_id FROM chaika.stores WHERE id=%s", (other_store,)
    ).fetchone() == (UUID(other),)


def test_legacy_inventory_resume_respects_selected_scope(db, inventory):
    run, source, snapshots = inventory
    db.execute("UPDATE chaika.sync_runs SET status='failed',finished_at=now() WHERE id=%s", (run,))
    restored = restore_snapshots(db, run, source, date(2026, 9, 9))
    policy(db, [str(uuid4())])
    result = publish_inventory(db, restored, date(2026, 9, 9))
    assert result["incoming_invoices"] == 0 and result["writeoffs"] == 0
    assert result["products"] == 1


def test_zero_stock_before_disabling_is_not_resurrected(db, bundle):
    _, _, dept, store, _, other_store = topology(db, bundle)
    payload = dict(
        request={"timestamp": "2026-09-09T23:59:59"},
        total=2,
        items=[
            dict(store_id=s, product_id=str(uuid4()), amount="1", sum="10")
            for s in [store, other_store]
        ],
    )
    publish_store_balances(db, raw_snapshot(db, "store_balances", payload))
    empty = dict(request={"timestamp": "2026-09-10T23:59:59"}, total=0, items=[])
    publish_store_balances(db, raw_snapshot(db, "store_balances", empty, 1))
    policy(db, [dept])
    payload["request"]["timestamp"] = "2026-09-11T23:59:59"
    publish_store_balances(db, raw_snapshot(db, "store_balances", payload, 2))
    factory = db.row_factory
    db.row_factory = dict_row
    try:
        result = read_balances(db, SimpleNamespace(unrestricted=True, departments=[], store_ids=[]))
    finally:
        db.row_factory = factory
    assert all(str(row["store_id"]) != other_store for row in result["rows"])


def test_cash_active_shift_moved_to_disabled_pos_keeps_old_observation(db, cash_sample):
    run, first, groups = cash_stage(db, cash_sample)
    publish_cash_shifts(db, first, groups)
    active = db.execute("SELECT id FROM chaika.corporate_nodes LIMIT 1").fetchone()[0]
    paused = uuid4()
    db.execute(
        "INSERT INTO chaika.corporate_nodes(source_id,id,name,type,firs"
        "t_seen_at,last_seen_at,last_snapshot_id) "
        "SELECT source_id,%s,'Paused',type,first_seen_at,last_seen_at,last_snapshot_id "
        "FROM chaika.corporate_nodes WHERE id=%s",
        (paused, active),
    )
    policy(db, [str(active)])
    moved = deepcopy(groups)
    moved["id"] = str(uuid4())
    moved["raw"] = moved["raw"].replace(str(active).encode(), str(paused).encode())
    moved["sha256"] = hashlib.sha256(moved["raw"]).hexdigest()
    append_snapshot(db, run, moved)
    second = next_snapshot(db, run, first, 1)
    result = publish_cash_shifts(db, second, moved)
    assert result["frozen_rows"] == 1
    assert db.execute(
        "SELECT department_id,origin_snapshot_id FROM chaika.cash_shifts"
    ).fetchone() == (active, UUID(first["id"]))


def test_indicator_scope_expansion_rejects_partial_capture_and_preserves_checkpoint(db, bundle):
    import pytest

    from app.sync_indicator_filters import publish_filter
    from app.sync_references import SyncError
    from app.web.indicators import FILTERS

    _, _, dept, _, other, _ = topology(db, bundle)
    field = next(iter(FILTERS))
    rows = [
        {"Department.Id": d, FILTERS[field][1]: value} for d, value in [(dept, "A"), (other, "B")]
    ]
    publish_filter(db, field, date(2026, 1, 1), date(2026, 2, 1), rows, replace=True)
    policy(db, [dept])
    expected = load_scope(db)
    publish_filter(
        db, field, date(2026, 1, 1), date(2026, 3, 1), rows, replace=True, expected_scope=expected
    )
    assert db.execute(
        "SELECT period_end FROM chaika.indicator_filter_sync WHERE field=%s", (field,)
    ).fetchone() == (date(2026, 2, 1),)
    policy(db, None)
    with pytest.raises(SyncError, match="sync_scope_changed"):
        publish_filter(
            db,
            field,
            date(2026, 1, 1),
            date(2026, 3, 1),
            rows[:1],
            replace=True,
            expected_scope=expected,
        )
    assert (
        db.execute(
            "SELECT count(*) FROM chaika.indicator_filter_values WHERE field=%s", (field,)
        ).fetchone()[0]
        == 2
    )


def test_rms_remap_quarantine_retains_historical_binding_until_reverified(db, bundle):
    sources, snapshots, dept, _, other, _ = topology(db, bundle)
    policy(db, [dept])
    changed = deepcopy(snapshots)
    other_node = {"id": other, "name": "Other", "type": "DEPARTMENT", "parent_id": None}
    changed["primary", "departments"]["payload"]["items"].append(other_node)
    changed["rms-a", "departments"]["payload"]["items"] = [other_node]
    changed["rms-a", "groups"]["payload"]["items"][0]["department_id"] = other
    result = publish_references(db, sources, changed)
    assert result["policy_blocked_sources"] == ["rms-a"]
    assert db.execute(
        "SELECT state,department_id,sync_blocked FROM chaika.rms_bindings WHERE source_id='rms-a'"
    ).fetchone() == ("matched", UUID(dept), True)
    assert not source_enabled(db, "rms-a")
    policy(db, None)
    assert not source_enabled(db, "rms-a")
    publish_references(db, sources, snapshots)
    assert source_enabled(db, "rms-a")
