"""Warehouse scope, full-snapshot totals and exact product selection."""

import hashlib
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from test_inventory_sync import inventory as inventory
from test_reference_sync import bundle as bundle
from test_reference_sync import db as db
from test_reference_sync import stage

from app.sync_inventory import publish_inventory
from app.sync_references import append_snapshot, publish
from app.web.balances import product_suggestions, read_balances
from app.web.repository import Repository, Scope


@pytest.fixture
def stock(db, bundle, inventory):
    sources, snapshots = stage(db, bundle)
    publish(db, sources, snapshots)
    run, _, goods = inventory
    publish_inventory(db, goods, date(2026, 9, 9))
    store = db.execute("SELECT id FROM chaika.stores").fetchone()[0]
    department = db.execute("SELECT parent_id FROM chaika.stores").fetchone()[0]
    other, empty = uuid4(), uuid4()
    for key, name in [(other, "Other store"), (empty, "Empty store")]:
        db.execute(
            "INSERT INTO chaika.stores SELECT source_id,%s,parent_id,code,%s,"
            "first_seen_at,last_seen_at,present_in_latest,last_snapshot_id "
            "FROM chaika.stores WHERE id=%s",
            (key, name, store),
        )
    product = db.execute("SELECT id FROM chaika.products").fetchone()[0]
    similar, hidden = uuid4(), uuid4()
    for key, name, code in [
        (similar, "Мясо Бедро куриное с кожей", "ABC_2"),
        (hidden, "Секретное мясо", "SECRET"),
    ]:
        db.execute(
            "INSERT INTO chaika.products SELECT source_id,%s,%s,type,group_id,main_unit_id,"
            "category_id,%s,num,deleted,default_sale_price,unit_weight,unit_capacity,"
            "details,present_in_latest,first_seen_at,last_seen_at,last_snapshot_id "
            "FROM chaika.products WHERE id=%s",
            (key, name, code, product),
        )
    db.execute(
        "UPDATE chaika.products SET name='Мясо Бедро куриное',code='ABC_1' WHERE id=%s", (product,)
    )
    snapshot = uuid4()
    raw = b"[]"
    now = datetime.now(UTC)
    append_snapshot(
        db,
        run,
        dict(
            id=str(snapshot),
            source_id="primary",
            resource="store_balances",
            observed_at=now,
            raw=raw,
            sha256=hashlib.sha256(raw).hexdigest(),
            payload={},
        ),
    )
    lines = [(store, product, Decimal("1"), Decimal("1.123456789123456789"))] * 51
    lines += [
        (store, similar, Decimal("2"), Decimal("-10")),
        (other, hidden, Decimal("3"), Decimal("9999")),
    ]
    db.execute(
        "INSERT INTO chaika.store_balance_reports "
        "VALUES('primary','2026-09-10T23:59:59',%s,%s,%s,%s)",
        (snapshot, len(lines), now, now),
    )
    for n, line in enumerate(lines, 1):
        db.execute(
            "INSERT INTO chaika.store_balance_items VALUES(%s,%s,%s,%s,%s,%s)", (snapshot, n, *line)
        )
    scope = Scope(
        {"role": "manager"},
        ({"id": department, "name": "Restaurant"},),
        department,
        (store, empty),
        (),
    )
    db.row_factory = dict_row
    return scope, store, empty, other, product, similar, hidden


def test_summary_is_whole_warehouse_not_filtered_page(db, stock):
    scope, store, empty, _, product, *_ = stock
    result = read_balances(db, scope, offset=50, product_id=product)
    assert result["total"] == 51 and len(result["rows"]) == 1
    assert result["filtered_value"] == Decimal("57.296296245296296239")
    by_id = {s["id"]: s for s in result["stores"]}
    assert set(by_id) == {store, empty}
    assert by_id[store]["value"] == Decimal("47.296296245296296239")
    assert by_id[store]["product_count"] == 2 and by_id[store]["row_count"] == 52
    assert by_id[store]["department"] == "Restaurant"
    assert by_id[empty]["value"] is None
    assert read_balances(db, scope, offset=999, product_id=product)["total"] == 51


def test_suggestions_search_catalog_and_keep_ids_and_scope(db, stock):
    scope, store, _, other, product, similar, hidden = stock
    found = product_suggestions(db, scope, "куриное мясо", store)["rows"]
    assert {r["id"] for r in found} == {product, similar}
    assert (
        len(product_suggestions(db, scope, "мясо")["rows"]) == 2
    )  # duplicate stock lines don't repeat suggestions
    assert product_suggestions(db, scope, "секрет")["rows"] == []
    assert product_suggestions(db, scope, "Store")["rows"] == []
    assert product_suggestions(db, scope, "ABC_1")["rows"][0]["id"] == product
    assert product_suggestions(db, scope, "%%")["rows"] == []
    assert read_balances(db, scope, product_id=hidden)["total"] == 0
    for call in [
        lambda: read_balances(db, scope, store_id=other),
        lambda: product_suggestions(db, scope, "", other),
    ]:
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 403


def test_store_selection_and_zero_rows(db, stock):
    scope, store, empty, *_ = stock
    assert read_balances(db, scope, store_id=store)["total"] == 52
    result = read_balances(db, scope, store_id=empty)
    assert result["rows"] == [] and result["filtered_value"] is None


def test_writeoff_total_sums_current_costs_without_multiplying_quantity(db, inventory):
    _, _, snapshots = inventory
    publish_inventory(db, snapshots, date(2026, 9, 9))
    doc = db.execute("SELECT id FROM chaika.writeoffs").fetchone()[0]
    db.execute("UPDATE chaika.writeoff_items SET amount=10,cost=380.76123456789123456789")
    db.execute(
        "INSERT INTO chaika.writeoff_items SELECT source_id,document_id,2,product_id,2,249.58,"
        "measure_unit_id,amount_factor,details,true FROM chaika.writeoff_items"
    )
    db.execute(
        "INSERT INTO chaika.writeoff_items SELECT source_id,document_id,3,product_id,1,9999,"
        "measure_unit_id,amount_factor,details,false FROM chaika.writeoff_items WHERE num=1"
    )
    repo = object.__new__(Repository)

    @contextmanager
    def connection(**kwargs):
        yield db

    repo.connection = connection
    db.row_factory = dict_row
    scope = Scope({"role": "owner"}, (), None, (), ())
    result = repo.resources(scope, "writeoffs")
    assert result["rows"][0]["writeoff_sum"] == "630.34123456789123456789"
    assert (
        repo.detail(scope, "writeoffs", doc)["header"]["writeoff_sum"] == "630.34123456789123456789"
    )
    db.execute("UPDATE chaika.writeoff_items SET cost=NULL WHERE num=2")
    assert repo.resources(scope, "writeoffs")["rows"][0]["writeoff_sum"] is None
