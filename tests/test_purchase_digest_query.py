"""Exercise the digest's first-seen window and venue boundary with temporary tables."""

import os
from datetime import date
from decimal import Decimal
from io import BytesIO
from uuid import UUID
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import purchase_digest_report as digest

VENUE, SIBLING, KITCHEN, BAR, PRODUCT, UNIT, SUPPLIER = [UUID(int=i) for i in range(1, 8)]
WHOLESALER, TRADER, PLACEHOLDER = [UUID(int=i) for i in range(8, 11)]
GROUP = UUID("231a6b55-3b64-4b80-ad84-b97881bda814")


@pytest.fixture
def digest_db(monkeypatch):
    dsn = os.environ.get("LOW_MARKUP_TEST_DSN")
    if not dsn:
        pytest.skip("loopback PostgreSQL fixture required")
    assert conninfo_to_dict(dsn).get("host") in {"127.0.0.1", "localhost"}
    monkeypatch.setattr(digest, "settings_schema", lambda db: "pg_temp")
    with psycopg.connect(dsn, row_factory=dict_row) as db:
        for name, columns in {
            "corporate_nodes": "id uuid,parent_id uuid,name text,type text",
            "stores": "id uuid,parent_id uuid,name text,present_in_latest boolean DEFAULT true",
            "product_groups": "id uuid,parent_id uuid",
            "products": "id uuid,group_id uuid,name text",
            "counteragents": "id uuid,name text,represents_store boolean DEFAULT false,"
            "represented_store_id uuid",
            "measure_units": "id uuid,name text",
            "purchase_digest_receipt_pairs": "incoming_id uuid,first_paired_at timestamptz",
            "incoming_invoices": "id uuid,document_number text,supplier_id uuid,first_seen_at "
            "timestamptz,status text,details jsonb,date_incoming text,"
            "default_store_id uuid,processed_first_seen_at timestamptz,"
            "paired_first_seen_at timestamptz",
            "outgoing_invoices": "id uuid,document_number text,default_store_id uuid,"
            "counteragent_id uuid,linked_incoming_invoice_id uuid,first_seen_at timestamptz,"
            "processed_first_seen_at timestamptz,paired_first_seen_at timestamptz,status text,"
            "details jsonb,date_incoming text",
            "outgoing_invoice_items": "document_id uuid,store_id uuid,product_id uuid,"
            "amount numeric,sum numeric,details jsonb,present_in_latest boolean",
            "incoming_invoice_items": "document_id uuid,store_id uuid,product_id uuid,"
            "amount_unit_id uuid,amount numeric,sum numeric,"
            "details jsonb,present_in_latest boolean",
        }.items():
            db.execute(f"CREATE TEMP TABLE {name} (source_id text DEFAULT 'primary',{columns})")
        db.execute("CREATE TEMP TABLE operation_settings(singleton boolean,settings jsonb)")
        db.execute(
            "INSERT INTO operation_settings VALUES (true,%s)",
            (Jsonb({"analytics": {"archived_department_ids": []}, "sync": {"mode": "all"}}),),
        )
        yield db


def test_backdated_receipt_boundary_and_network_average(digest_db):
    venue, other, kitchen, other_store, product, unit, supplier = [UUID(int=i) for i in range(1, 8)]
    db = digest_db
    db.execute(
        "INSERT INTO corporate_nodes(id,parent_id,name,type) VALUES "
        "(%s,NULL,'Пример','DEPARTMENT'),(%s,%s,'Сосед','DEPARTMENT')",
        (venue, other, venue),
    )
    db.execute(
        "INSERT INTO stores(id,parent_id,name) VALUES (%s,%s,'Кухня'),(%s,%s,'Сосед')",
        (kitchen, venue, other_store, other),
    )
    db.execute("INSERT INTO products(id,name) VALUES (%s,'Товар')", (product,))
    db.execute("INSERT INTO measure_units(id,name) VALUES (%s,'кг')", (unit,))
    db.execute("INSERT INTO counteragents(id,name) VALUES (%s,'Поставщик')", (supplier,))

    def invoice(i, day, observed, warehouse, qty, cost):
        document = UUID(int=100 + i)
        db.execute(
            "INSERT INTO incoming_invoices(id,document_number,supplier_id,first_seen_at,"
            "status,details,date_incoming,default_store_id) "
            "VALUES (%s,%s,%s,%s,'PROCESSED','{}',%s,%s)",
            (document, str(i), supplier, observed, f"2026-01-{day:02d}T09:00:00", warehouse),
        )
        db.execute(
            "INSERT INTO incoming_invoice_items(document_id,product_id,amount_unit_id,amount,"
            "sum,details,present_in_latest) VALUES (%s,%s,%s,%s,%s,'{}',true)",
            (document, product, unit, qty, cost),
        )

    for d in range(1, 8):
        invoice(d, d, f"2026-01-{d:02d}T12:00:00+03", other_store, 1, d * 10)
    invoice(8, 7, "2026-01-07T14:00:00+03", other_store, 2, 50)
    invoice(9, 8, "2026-01-09T09:00:00+03", kitchen, 2, 120)
    invoice(10, 8, "2026-01-09T15:00:00+03", other_store, 4, 400)
    invoice(11, 7, "2026-01-10T09:00:00+03", kitchen, 2, 2000)
    content = digest.build_report(db, venue, date(2026, 1, 10))
    with ZipFile(BytesIO(content)) as book:
        summary = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))

        def value(cell):
            return float(summary.find(f".//{{*}}c[@r='{cell}']/{{*}}v").text)

        assert [value(f"{col}8") for col in "BCDEH"] == [1, 1, 120, 80, 40]
    db.execute(
        "UPDATE operation_settings SET settings=%s",
        (
            Jsonb(
                {
                    "analytics": {"archived_department_ids": [str(venue)]},
                    "sync": {"mode": "all"},
                }
            ),
        ),
    )
    with pytest.raises(ValueError, match="purchase_digest_venue_unavailable"):
        digest.build_report(db, venue, date(2026, 1, 10))


def warehouse_agent(store):
    return UUID(int=100 + store.int)


@pytest.fixture
def routed(digest_db, monkeypatch):
    db = digest_db
    for identifier, name in [
        (VENUE, "Первый ресторан"),
        (SIBLING, "Второй ресторан"),
        (GROUP, "ЯФирма"),
    ]:
        db.execute(
            "INSERT INTO corporate_nodes(id,name,type) VALUES (%s,%s,'DEPARTMENT')",
            (identifier, name),
        )
    for store, department, name in [
        (KITCHEN, VENUE, "Кухня"),
        (BAR, SIBLING, "Бар"),
        (WHOLESALER, GROUP, "ООО Первая компания"),
        (TRADER, GROUP, "ИП Вторая компания"),
        (PLACEHOLDER, GROUP, "Новый склад"),
    ]:
        db.execute(
            "INSERT INTO stores(id,parent_id,name) VALUES (%s,%s,%s)", (store, department, name)
        )
        db.execute(
            "INSERT INTO counteragents(id,name,represents_store,represented_store_id) "
            "VALUES (%s,%s,true,%s)",
            (warehouse_agent(store), name, store),
        )
    db.execute("INSERT INTO counteragents(id,name) VALUES (%s,'Условный поставщик')", (SUPPLIER,))
    db.execute("INSERT INTO products(id,name) VALUES (%s,'Условный продукт')", (PRODUCT,))
    db.execute("INSERT INTO measure_units(id,name) VALUES (%s,'кг')", (UNIT,))
    monkeypatch.setattr(digest, "render_workbook", lambda source, business_date: source)
    return db


def receipt(db, serial, store, day, qty, cost, *, supplier=SUPPLIER, observed=None, details=None):
    doc = UUID(int=1000 + serial)
    db.execute(
        "INSERT INTO incoming_invoices(id,document_number,supplier_id,first_seen_at,"
        "status,details,date_incoming,default_store_id) VALUES (%s,%s,%s,%s,'PROCESSED',%s,%s,%s)",
        (
            doc,
            str(serial),
            supplier,
            observed or f"2026-01-{day:02}T12:00:00+03",
            Jsonb(details or {}),
            f"2026-01-{day:02}T09:00:00",
            store,
        ),
    )
    db.execute(
        "INSERT INTO incoming_invoice_items(document_id,product_id,amount_unit_id,amount,"
        "sum,details,present_in_latest) VALUES (%s,%s,%s,%s,%s,'{}',true)",
        (doc, PRODUCT, UNIT, qty, cost),
    )
    return doc


def transfer(db, serial, origin_store, to_store, qty, cost, ref="A-12", *, day=10, observed=None):
    outgoing = UUID(int=2000 + serial)
    observed = observed or f"2026-01-{day:02}T12:00:00+03"
    target = receipt(
        db,
        serial,
        to_store,
        day,
        qty,
        cost,
        supplier=warehouse_agent(origin_store),
        observed=observed,
        details={"linked_outgoing_invoice_id": str(outgoing)},
    )
    db.execute(
        "INSERT INTO outgoing_invoices(id,default_store_id,counteragent_id,"
        "linked_incoming_invoice_id,status,details,date_incoming,first_seen_at,document_number) "
        "VALUES (%s,%s,%s,%s,'PROCESSED',%s,%s,%s,%s)",
        (
            outgoing,
            origin_store,
            warehouse_agent(to_store),
            target,
            Jsonb({"comment": f"  Условный поставщик\n   {ref.lower()}  "}),
            f"2026-01-{day:02}T09:00:00",
            observed,
            f"out-{serial}",
        ),
    )
    db.execute(
        "INSERT INTO outgoing_invoice_items(document_id,product_id,amount,sum,details,"
        "present_in_latest) VALUES (%s,%s,%s,%s,'{}',true)",
        (outgoing, PRODUCT, qty, cost),
    )
    return outgoing


def get_report(db, venue=VENUE, day=11):
    return digest.build_report(db, venue, date(2026, 1, day))


def test_routed_split_companies_and_direct_receipt_use_external_history(routed):
    for day in range(1, 7):
        receipt(routed, day, BAR, day, 1, 100)
    same_ref = {"incoming_document_number": "A-12"}
    origin = receipt(routed, 30, WHOLESALER, 8, 5, 750, details=same_ref)
    second = receipt(routed, 31, TRADER, 8, 2, 400, details=same_ref)
    # An unlinked invoice from another internal warehouse is not an external purchase.
    receipt(routed, 29, KITCHEN, 7, 1, 3333, supplier=warehouse_agent(TRADER))
    # These newer external receipts are not prior history for the Jan 8 procurement.
    receipt(routed, 32, BAR, 9, 1, 900)
    receipt(routed, 33, KITCHEN, 10, 1, 220)
    transfer(routed, 40, WHOLESALER, KITCHEN, 2, 300)
    transfer(routed, 41, WHOLESALER, BAR, 3, 450)
    transfer(routed, 42, TRADER, KITCHEN, 2, 400)
    report = get_report(routed)
    assert report["direct_invoices"] == 1
    assert report["routed_invoices"] == 2
    assert report["review_rows"] == []
    rows = {r["number"]: r for r in report["rows"]}
    assert set(rows) == {"33", "40", "42"}
    assert (rows["40"]["via"], rows["40"]["origin_doc"], rows["40"]["origin_number"]) == (
        "ООО Первая компания",
        str(origin),
        "30",
    )
    assert (rows["42"]["via"], rows["42"]["origin_doc"]) == ("ИП Вторая компания", str(second))
    for number in ("40", "42"):
        row = rows[number]
        assert (row["supplier"], row["store"]) == ("Условный поставщик", "Кухня")
        assert Decimal(row["prev_qty"]) == 6 and Decimal(row["prev_cost"]) == 600
    assert Decimal(rows["40"]["qty"]) == 2 and Decimal(rows["40"]["cost"]) == 300
    assert (Decimal(rows["33"]["prev_qty"]), Decimal(rows["33"]["prev_cost"])) == (12, 2450)
    # Across the two stores the accepted quantities equal the original five.
    sibling = get_report(routed, SIBLING)
    assert sibling["direct_invoices"] == 0 and sibling["routed_invoices"] == 1
    assert len(sibling["rows"]) == 1
    assert Decimal(sibling["rows"][0]["qty"]) == 3 and sibling["rows"][0]["store"] == "Бар"
    assert get_report(routed, day=12)["routed_invoices"] == 0


def test_unconfirmed_sources_and_non_procurement_transfers(routed):
    receipt(routed, 1, BAR, 1, 1, 100)
    for serial in (30, 31):
        receipt(routed, serial, WHOLESALER, 8, 2, 300, details={"invoice": "reused"})
    receipt(routed, 32, WHOLESALER, 8, 2, 430, details={"invoice": "mismatch"})
    transfer(routed, 40, WHOLESALER, KITCHEN, 2, 300, "reused")
    transfer(routed, 41, WHOLESALER, KITCHEN, 2, 300, "unknown")
    transfer(routed, 42, WHOLESALER, KITCHEN, 2, 300, "mismatch")
    transfer(routed, 43, BAR, KITCHEN, 2, 300)
    transfer(routed, 44, PLACEHOLDER, KITCHEN, 2, 300)
    report = get_report(routed)
    assert report["rows"] == []
    review = {r["number"]: r for r in report["review_rows"]}
    assert set(review) == {"40", "41", "42"}
    assert report["direct_invoices"] == 0 and report["routed_invoices"] == 3
    assert all(r["reason"] and not r.get("origin_doc") for r in review.values())
    assert all(Decimal(r["cost"]) == 300 for r in review.values())


@pytest.mark.parametrize("same_time", [True, False])
def test_overallocated_supplier_invoice_is_not_counted_per_venue(routed, same_time):
    receipt(routed, 1, BAR, 1, 1, 100)
    receipt(
        routed,
        30,
        WHOLESALER,
        8,
        4,
        400,
        details={
            "invoice": "A-12",
            "transport_invoice_number": "transport-7",
        },
    )
    transfer(routed, 40, WHOLESALER, KITCHEN, 3, 300)
    transfer(
        routed,
        41,
        WHOLESALER,
        BAR,
        3,
        300,
        ref="transport-7",
        observed="2026-01-10T12:00:00+03" if same_time else "2026-01-10T12:01:00+03",
    )
    first, second = get_report(routed, VENUE), get_report(routed, SIBLING)
    if same_time:
        assert first["rows"] == [] and [r["number"] for r in first["review_rows"]] == ["40"]
    else:
        assert [r["number"] for r in first["rows"]] == ["40"] and first["review_rows"] == []
    assert second["rows"] == []
    assert [r["number"] for r in second["review_rows"]] == ["41"]
    assert second["review_rows"][0]["reason"]


def test_backdated_pair_uses_first_ready_window_once(routed):
    receipt(routed, 1, BAR, 1, 1, 100)
    receipt(routed, 30, TRADER, 7, 1, 150, details={"transport_invoice_number": "A-12"})
    outgoing = transfer(routed, 40, TRADER, KITCHEN, 1, 150, day=8)
    # The backdated receiving document was imported as a draft before its outgoing link was ready.
    routed.execute(
        "UPDATE incoming_invoices SET processed_first_seen_at='2026-01-09T08:30:00+03',"
        "paired_first_seen_at='2026-01-09T10:00:00+03' WHERE document_number='40'"
    )
    routed.execute(
        "UPDATE outgoing_invoices SET processed_first_seen_at='2026-01-09T12:00:00+03',"
        "paired_first_seen_at='2026-01-10T09:00:00+03' WHERE id=%s",
        (outgoing,),
    )
    assert get_report(routed, day=10)["routed_invoices"] == 0
    ready = get_report(routed, day=11)
    assert [r["number"] for r in ready["rows"]] == ["40"]
    assert ready["review_rows"] == []
    assert get_report(routed, day=12)["routed_invoices"] == 0


def test_confirmed_receipt_without_baseline_keeps_supplier_for_review(routed):
    origin = receipt(routed, 30, WHOLESALER, 8, 2, 300, details={"invoice": "A-12"})
    # The supplier reused the number on a different document with different goods.
    wrong = receipt(routed, 31, WHOLESALER, 8, 2, 300, details={"invoice": "A-12"})
    routed.execute(
        "UPDATE incoming_invoice_items SET product_id=%s WHERE document_id=%s",
        (UUID(int=9999), wrong),
    )
    transfer(routed, 40, WHOLESALER, KITCHEN, 2, 300)
    report = get_report(routed)
    assert not report["rows"]
    [row] = report["review_rows"]
    assert (row["number"], row["supplier"], row["origin_doc"]) == (
        "40",
        "Условный поставщик",
        str(origin),
    )


def test_source_received_after_cutoff_is_not_used_and_does_not_repeat_transfer(routed):
    receipt(routed, 1, BAR, 1, 1, 100)
    receipt(
        routed,
        30,
        WHOLESALER,
        8,
        2,
        300,
        observed="2026-01-11T11:30:00+03",
        details={"invoice": "A-12"},
    )
    transfer(routed, 40, WHOLESALER, KITCHEN, 2, 300)
    original, next_day = get_report(routed), get_report(routed, day=12)
    assert original["rows"] == [] and [r["number"] for r in original["review_rows"]] == ["40"]
    assert original["review_rows"][0]["origin_doc"] is None
    assert next_day["rows"] == next_day["review_rows"] == []


def test_mixed_outgoing_stores_and_nonreciprocal_links_are_excluded(routed):
    receipt(routed, 1, BAR, 1, 1, 100)
    receipt(routed, 30, WHOLESALER, 8, 2, 300, details={"invoice": "A-12"})
    mixed = transfer(routed, 40, WHOLESALER, KITCHEN, 2, 300)
    routed.execute(
        "INSERT INTO outgoing_invoice_items(document_id,store_id,present_in_latest) "
        "VALUES (%s,%s,true)",
        (mixed, BAR),
    )
    dangling = transfer(routed, 41, WHOLESALER, KITCHEN, 2, 300)
    routed.execute(
        "UPDATE outgoing_invoices SET linked_incoming_invoice_id=NULL WHERE id=%s", (dangling,)
    )
    report = get_report(routed)
    assert report["routed_invoices"] == 0 and report["review_rows"] == [] and report["rows"] == []


@pytest.mark.parametrize(
    "amounts,accepted",
    [
        (["33.33", "33.33", "33.34"], True),
        (["33.34", "33.34", "33.34"], False),
        (["33.33", "33.33", "33.33"], False),
    ],
)
def test_allocated_amounts_conserve_original_kopecks(routed, amounts, accepted):
    receipt(routed, 1, BAR, 1, 1, 10)
    receipt(routed, 30, WHOLESALER, 8, 3, 100, details={"invoice": "A-12"})
    for serial, cost in enumerate(amounts, start=40):
        transfer(routed, serial, WHOLESALER, KITCHEN, 1, cost)
    report = get_report(routed)
    if accepted:
        assert len(report["rows"]) == 3 and report["review_rows"] == []
        assert sum(Decimal(r["cost"]) for r in report["rows"]) == 100
    else:
        assert report["rows"] == [] and len(report["review_rows"]) == 3


def test_disagreeing_destination_costs_cannot_offset_within_one_receipt(routed):
    receipt(routed, 1, BAR, 1, 1, 10)
    receipt(routed, 30, WHOLESALER, 8, 2, 200, details={"invoice": "A-12"})
    transfer(routed, 40, WHOLESALER, KITCHEN, 2, 200)
    doc = UUID(int=1040)
    routed.execute("UPDATE incoming_invoice_items SET amount=1,sum=20 WHERE document_id=%s", (doc,))
    routed.execute(
        "INSERT INTO incoming_invoice_items(document_id,store_id,product_id,amount_unit_id,amount,"
        "sum,details,present_in_latest) VALUES(%s,%s,%s,%s,1,180,'{}',true)",
        (doc, BAR, PRODUCT, UNIT),
    )
    first, second = get_report(routed, VENUE), get_report(routed, SIBLING)
    assert first["rows"] == second["rows"] == []
    assert [Decimal(r["cost"]) for r in first["review_rows"]] == [20]
    assert [Decimal(r["cost"]) for r in second["review_rows"]] == [180]


def test_immutable_eligibility_does_not_replay_or_reconstruct_a_future_replacement(routed):
    receipt(routed, 1, BAR, 1, 1, 10)
    receipt(routed, 30, WHOLESALER, 8, 2, 200, details={"invoice": "A-12"})
    outgoing = transfer(routed, 40, WHOLESALER, KITCHEN, 2, 200)
    routed.execute(
        "INSERT INTO purchase_digest_receipt_pairs(incoming_id,first_paired_at) "
        "VALUES(%s,'2026-01-10T12:00:00+03')",
        (UUID(int=1040),),
    )
    routed.execute(
        "UPDATE outgoing_invoices SET paired_first_seen_at='2026-01-11T12:00:00+03' WHERE id=%s",
        (outgoing,),
    )
    for day in (11, 12):
        report = get_report(routed, day=day)
        assert report["routed_invoices"] == 0 and report["rows"] == report["review_rows"] == []
