"""Price history, provenance and authorization on the isolated test database."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_inventory_sync import inventory as inventory
from test_reference_sync import bundle as bundle
from test_reference_sync import db as db

from app.sync_inventory import publish_inventory
from app.web.purchase_prices import HOUSEHOLD_GROUP_ID, read_purchase_prices
from app.web.repository import Repository, Scope


@pytest.fixture
def data(db, inventory):
    publish_inventory(db, inventory[2], date(2026, 9, 9))
    base = db.execute("SELECT id FROM chaika.incoming_invoices").fetchone()[0]
    product = db.execute("SELECT id FROM chaika.products").fetchone()[0]
    store, unit, hidden = uuid4(), uuid4(), uuid4()

    def receipt(day, amount="1", value="100", **options):
        doc = options.get("document", uuid4())
        if "document" not in options:
            details = {"linked_outgoing_invoice_id": str(uuid4())} if options.get("linked") else {}
            db.execute(
                "INSERT INTO chaika.incoming_invoices "
                "SELECT source_id,%s,%s,%s,incoming_date,%s,supplier_id,%s,revision,"
                "last_export_date,%s,first_seen_at,last_seen_at,last_snapshot_id "
                "FROM chaika.incoming_invoices WHERE id=%s",
                (
                    doc,
                    str(doc),
                    day,
                    options.get("status", "PROCESSED"),
                    options.get("store", store),
                    Jsonb(details),
                    base,
                ),
            )
        db.execute(
            "INSERT INTO chaika.incoming_invoice_items(source_id,document_id,num,num_occurrence,"
            "product_id,store_id,amount,actual_amount,price,sum,amount_unit_id,"
            "details,present_in_latest) "
            "VALUES('primary',%s,%s,%s,%s,%s,%s,999,99999,%s,%s,%s,%s)",
            (
                doc,
                options.get("num", 1),
                options.get("occurrence", 1),
                options.get("product", product),
                options.get("store", store),
                amount,
                value,
                options.get("unit", unit),
                Jsonb({"is_additional_expense": options.get("expense", False)}),
                options.get("present", True),
            ),
        )
        return doc

    def read(
        *,
        kind="unlinked",
        allowed=None,
        exclude_household=True,
        selection=None,
        recent_only=False,
    ):
        scope = Scope({"role": "manager"}, (), None, tuple(allowed or [store]), ())
        repo = object.__new__(Repository)
        original = db.row_factory
        db.row_factory = dict_row
        try:
            return read_purchase_prices(
                db,
                scope,
                repo.resource_query(scope, "invoices"),
                kind,
                exclude_household,
                selection,
                recent_only=recent_only,
                as_of=date(2026, 9, 13),
            )
        finally:
            db.row_factory = original

    return receipt, read, store, unit, hidden


def detail(read, row):
    return read(selection=(row["product_id"], row["store_id"], row["unit_id"], row["linked"]))[
        "rows"
    ][0]


def test_latest_receipt_only_with_weighted_six_prior_receipts_and_sources(data):
    receipt, read, *_ = data
    receipt("2022-12-01", "1", "9999")  # Not one of the latest six previous receipts.
    previous = [
        ("2023-03-01", "1", "100"),
        ("2024-08-01", "2", "220"),
        ("2025-08-01", "3", "360"),
        ("2026-03-01", "4", "520"),
        ("2026-08-10", "5", "700"),
        ("2026-08-30", "6", "900"),
    ]
    ids = {receipt(day, amount, value) for day, amount, value in previous}
    latest = receipt("2026-09-01", "7", "1120")
    result = read()
    assert result["mode"] == "latest" and result["history_size"] == 6
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["current"]["date"] == "2026-09-01T00:00:00"
    assert row["current"]["price"] == 160
    assert row["previous"]["count"] == 6
    assert row["previous"]["sum"] == 2800 and row["previous"]["amount"] == 21
    assert row["previous"]["price"] == Decimal("133.3333")
    assert row["delta"] == Decimal("26.6667")
    assert row["current"]["lines"] == []  # Full document rows are loaded only on drilldown.
    history = detail(read, row)
    assert history["previous"]["price"] == row["previous"]["price"]
    assert {
        line["document_id"] for o in history["previous"]["receipts"] for line in o["lines"]
    } == ids
    assert history["current"]["lines"][0]["document_id"] == latest
    receipt("2026-09-02", "8", "1360")
    newer = read()["rows"][0]
    assert newer["current"]["price"] == 170 and newer["previous"]["price"] == Decimal("141.4815")
    assert newer["previous"]["count"] == 6


def test_unchanged_latest_does_not_surface_an_older_change(data):
    receipt, read, *_ = data
    receipt("2023-03-01", value="50")
    for day in range(1, 9):
        receipt(f"2026-09-{day:02d}", value="100")
    result = read()
    assert len(result["rows"]) == 1
    assert result["stats"]["unchanged"] == 1
    assert result["rows"][0]["current"]["date"] == "2026-09-08T00:00:00"
    assert result["rows"][0]["delta"] == 0


def test_old_or_first_receipt_is_listed_without_a_date_filter(data):
    receipt, read, *_ = data
    receipt("2023-03-01", "2", "240")
    result = read()
    assert result["stats"]["no_previous"] == 1
    row = result["rows"][0]
    assert row["current"]["price"] == 120
    assert row["previous"]["count"] == 0 and row["delta"] is None and row["percent"] is None
    assert len(detail(read, row)["current"]["lines"]) == 1


def test_recency_uses_latest_network_receipt_but_preserves_six_old_receipts(data):
    receipt, read, store, unit, old_store = data
    for month in range(1, 7):
        receipt(f"2023-{month:02d}-01", amount=str(month), value=str(month * 100))
    receipt("2026-09-13", amount="2", value="240")
    receipt("2023-09-09", store=old_store)
    receipt("2023-09-09", unit=uuid4())
    receipt("2023-09-09", linked=True)
    options = dict(kind="all", allowed=[store, old_store])
    recent = read(**options, recent_only=True)
    assert recent["recent_only"] is True and recent["recent_since"] == "2026-07-16"
    assert recent["stats"]["observations"] == 1
    assert len(recent["rows"]) == 1
    row = recent["rows"][0]
    assert row["store_id"] is None and row["unit_id"] == unit and row["linked"] is False
    assert row["previous"]["count"] == 6
    assert row["previous"]["amount"] == 21 and row["previous"]["sum"] == 2100
    assert row["previous"]["price"] == 100 and row["percent"] == 20
    archive = read(**options, recent_only=False)
    assert len(archive["rows"]) == 3 and archive["stats"]["observations"] == 3
    assert row == next(r for r in archive["rows"] if r["current"]["date"].startswith("2026"))
    history = read(
        **options,
        recent_only=True,
        selection=(row["product_id"], old_store, unit, False),
    )
    assert history["recent_only"] is False  # History is accessible independently of recency.
    assert history["rows"][0]["current"]["date"] == "2023-09-09T00:00:00"
    assert len(history["rows"][0]["current"]["lines"]) == 1


def test_network_combines_warehouses_before_weighting_and_preserves_access(data):
    receipt, read, a, unit, hidden = data
    b = uuid4()
    previous = {
        receipt(f"2026-08-{day:02d}", str(day), str(day * 100), store=a if day % 2 else b)
        for day in range(1, 7)
    }
    first = receipt("2026-09-01", "2", "240", store=a)
    second = receipt("2026-09-01", "8", "1280", store=b)
    receipt("2026-09-12", "100", "90000", store=hidden)
    def read_network(**kw):
        return read(allowed=[a, b], **kw)

    result = read_network()
    assert result["grouping"] == "product_network" and len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["store_id"] is None and row["unit_id"] == unit
    assert row["current"]["price"] == 152 and row["current"]["amount"] == 10
    assert row["previous"]["price"] == 100 and row["previous"]["count"] == 6
    assert row["previous"]["amount"] == 21 and row["percent"] == 52
    history = detail(read_network, row)
    assert history["current"]["price"] == row["current"]["price"]
    assert {line["document_id"] for line in history["current"]["lines"]} == {first, second}
    assert {line["store_id"] for line in history["current"]["lines"]} == {a, b}
    assert all(line["store"] for line in history["current"]["lines"])
    assert {
        line["document_id"] for r in history["previous"]["receipts"] for line in r["lines"]
    } == previous
    assert read_network(selection=(row["product_id"], hidden, unit, False))["rows"] == []


def test_recency_includes_sixty_calendar_days_and_excludes_future_receipts(data):
    receipt, read, *_ = data
    for stamp in ("2026-07-15T23:59:59", "2026-07-16", "2026-09-13T23:59:59", "2026-09-14"):
        receipt(stamp, product=uuid4())
    recent = read(recent_only=True)
    assert {r["current"]["date"] for r in recent["rows"]} == {
        "2026-07-16T00:00:00",
        "2026-09-13T23:59:59",
    }
    assert recent["stats"]["no_previous"] == 2
    assert len(read(recent_only=False)["rows"]) == 4


def test_same_time_groups_preserve_duplicate_lines_and_all_documents(data):
    receipt, read, *_ = data
    for day in range(25, 31):
        doc = receipt(f"2026-08-{day}T00:00:00", "1", "100")
        receipt(f"2026-08-{day}", "2", "400", document=doc, occurrence=2)
        receipt(f"2026-08-{day}", "3", "900")
    receipt("2026-09-01", "1", "300")
    row = read()["rows"][0]
    assert row["previous"]["count"] == 6
    assert row["previous"]["price"] == Decimal("233.3333")
    history = detail(read, row)
    assert sum(len(o["lines"]) for o in history["previous"]["receipts"]) == 18
    assert history["previous"]["price"] == row["previous"]["price"]


def test_scope_units_status_removed_lines_and_expenses_apply_to_list_and_history(data):
    receipt, read, store, unit, hidden = data
    old = receipt("2026-08-25", value="100")
    receipt("2026-09-01", value="120")
    for status in ("NEW", "DELETED"):
        receipt("2026-09-02", value="900", status=status)
    receipt("2026-09-02", value="900", present=False)
    receipt("2026-09-02", value="900", expense=True)
    receipt("2026-09-02", value="900", store=hidden)
    mixed = receipt("2026-09-03", value="900")
    receipt("2026-09-03", value="900", store=hidden, document=mixed, num=2)
    result = read()
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["current"]["price"] == 120 and row["previous"]["price"] == 100
    assert detail(read, row)["previous"]["receipts"][0]["lines"][0]["document_id"] == old
    assert read(selection=(row["product_id"], hidden, unit, False))["rows"] == []
    other_unit = uuid4()
    receipt("2026-09-04", value="999", unit=other_unit)
    rows = read()["rows"]
    assert len(rows) == 2
    assert next(r for r in rows if r["unit_id"] == other_unit)["previous"]["count"] == 0
    assert next(r for r in rows if r["unit_id"] == unit)["previous"]["price"] == 100


def test_linked_documents_are_separate_even_in_all_mode(data):
    receipt, read, *_ = data
    receipt("2026-08-29", value="100")
    receipt("2026-08-30", value="200", linked=True)
    receipt("2026-09-01", value="120")
    receipt("2026-09-02", value="220", linked=True)
    assert read()["rows"][0]["percent"] == 20
    rows = read(kind="all")["rows"]
    assert len(rows) == 2
    linked = next(r for r in rows if r["linked"])
    assert linked["percent"] == 10
    assert detail(lambda **kw: read(kind="all", **kw), linked)["previous"]["price"] == 200


def test_invalid_latest_or_recent_history_does_not_fall_back_to_older_prices(data):
    receipt, read, *_ = data
    receipt("2026-08-25", value="9999")
    receipt("2026-08-26", amount="0", value="100")
    for day in range(27, 32):
        receipt(f"2026-08-{day}", value="100")
    receipt("2026-09-01", value="100")
    result = read()
    assert result["stats"]["invalid"] == 1 and result["rows"] == []
    receipt("2026-09-02", value="120")
    row = read()["rows"][0]
    assert row["previous"]["price"] == 100 and row["previous"]["count"] == 6
    receipt("2026-09-03", amount="0", value="120")
    assert read()["rows"] == []


def test_short_history_zero_baseline(data):
    receipt, read, *_ = data
    receipt("2026-08-31", value="0")
    receipt("2026-09-01", value="100")
    result = read()
    row = result["rows"][0]
    assert row["percent"] is None and row["delta"] == 100
    assert result["stats"]["short_history"] == 1


def test_subprecision_difference_remains_unchanged_in_list_and_detail(data):
    receipt, read, *_ = data
    receipt("2026-08-31", amount="3", value="100.000000001")
    receipt("2026-09-01", amount="3", value="100")
    row = read()["rows"][0]
    assert row["delta"] == 0
    assert detail(read, row)["delta"] == 0


def test_household_subtree_is_optional_and_filters_observations_before_comparison(db, data):
    receipt, read, *_ = data
    original = db.execute("SELECT id,group_id FROM chaika.products LIMIT 1").fetchone()
    product, original_group = original
    child, grandchild = uuid4(), uuid4()
    for group_id, parent in [
        (HOUSEHOLD_GROUP_ID, grandchild),
        (child, HOUSEHOLD_GROUP_ID),
        (grandchild, child),
    ]:
        # Deliberately renamed groups and a cycle: UUID ancestry remains authoritative
        # and the recursive UNION must terminate. Historical deleted groups still count.
        db.execute(
            "INSERT INTO chaika.product_groups SELECT source_id,%s,'Renamed',%s,code,num,true,"
            "details,present_in_latest,first_seen_at,last_seen_at,last_snapshot_id "
            "FROM chaika.product_groups WHERE id=%s",
            (group_id, parent, original_group),
        )
    db.execute("UPDATE chaika.products SET group_id=%s WHERE id=%s", (grandchild, product))
    food, unknown = uuid4(), uuid4()
    db.execute(
        "INSERT INTO chaika.products SELECT source_id,%s,'Хоз название продукта',type,NULL,"
        "main_unit_id,category_id,code,num,deleted,default_sale_price,unit_weight,unit_capacity,"
        "details,present_in_latest,first_seen_at,last_seen_at,last_snapshot_id "
        "FROM chaika.products WHERE id=%s",
        (food, product),
    )
    for pid in (product, food, unknown):
        receipt("2023-03-01", value="100", product=pid)
        receipt("2026-09-01", value="120", product=pid)
    filtered = read()
    all_goods = read(exclude_household=False)
    assert filtered["exclude_household"] is True
    assert {r["product_id"] for r in filtered["rows"]} == {food, unknown}
    assert filtered["stats"]["observations"] == 2
    assert {r["product_id"] for r in all_goods["rows"]} == {food, unknown, product}
    assert all_goods["stats"]["observations"] == 3
    assert filtered["rows"] == [r for r in all_goods["rows"] if r["product_id"] != product]
