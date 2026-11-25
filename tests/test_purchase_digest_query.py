"""Exercise the digest's first-seen window and venue boundary with temporary tables."""

import os
from datetime import date
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


def test_backdated_receipt_boundary_and_network_average(monkeypatch):
    dsn = os.environ.get("LOW_MARKUP_TEST_DSN")
    if not dsn:
        pytest.skip("loopback PostgreSQL fixture required")
    assert conninfo_to_dict(dsn).get("host") in {"127.0.0.1", "localhost"}
    venue, other, kitchen, other_store, product, unit, supplier = [UUID(int=i) for i in range(1, 8)]
    monkeypatch.setattr(digest, "settings_schema", lambda db: "pg_temp")
    with psycopg.connect(dsn, row_factory=dict_row) as db:
        for name, columns in {
            "corporate_nodes": "id uuid,parent_id uuid,name text,type text",
            "stores": "id uuid,parent_id uuid,name text",
            "product_groups": "id uuid,parent_id uuid",
            "products": "id uuid,group_id uuid,name text",
            "counteragents": "id uuid,name text",
            "measure_units": "id uuid,name text",
            "incoming_invoices": "id uuid,document_number text,supplier_id uuid,first_seen_at "
            "timestamptz,status text,details jsonb,date_incoming text,"
            "default_store_id uuid",
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
