import json
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from test_iiko_assembly import config, response_body
from test_reference_sync import bundle as bundle
from test_reference_sync import db as db

from app.main import create_app
from app.services.iiko_assembly import read_all_assembly
from app.sync_inventory import publish_inventory, restore_snapshots
from app.sync_references import SyncError, append_snapshot, register_sources


def test_bulk_assembly_uses_one_day_and_shared_auth(tmp_path):
    calls = []
    body = response_body()
    body["knownRevision"] = 456

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, text="assembly-bulk-token")
        if request.url.path.endswith("/logout"):
            return httpx.Response(200, text="assembly-bulk-token")
        assert request.url.path.endswith("/v2/assemblyCharts/getAll")
        assert dict(request.url.params) == {
            "dateFrom": "2026-09-09",
            "dateTo": "2026-09-10",
            "includeDeletedProducts": "true",
            "includePreparedCharts": "false",
        }
        return httpx.Response(200, json=body)

    app = create_app(
        config(), iiko_transport=httpx.MockTransport(handle), assembly_directory=tmp_path
    )
    with TestClient(app) as client:
        r = client.get("/api/v1/iiko/assembly-charts/all", params={"date": "2026-09-09"})
        assert r.status_code == 200
        value = r.json()
        assert value["total"] == 1 and value["items_count"] == 1 and value["known_revision"] == 456
        assert value["include_prepared_charts"] is False
        assert len(calls) == 2
        export = read_all_assembly(
            tmp_path / f"{value['snapshot_id']}.json", date(2026, 9, 9), 100000
        )
        assert export.assembly_charts[0].items[0].amount_in == Decimal("0.123456789123456789")


@pytest.mark.parametrize("kind", ["duplicate", "expired", "prepared"])
def test_bulk_assembly_rejects_bad_export(tmp_path, kind):
    body = response_body()
    if kind == "duplicate":
        body["assemblyCharts"] *= 2
    if kind == "expired":
        body["assemblyCharts"][0]["dateTo"] = "2026-09-09"
    if kind == "prepared":
        body["preparedCharts"] = [{"unexpected": True}]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(body))
    from app.integrations.iiko.errors import IikoError

    with pytest.raises(IikoError):
        read_all_assembly(path, date(2026, 9, 9), 100000)


@pytest.fixture
def inventory(db, bundle):
    sources, _ = bundle
    register_sources(db, sources)
    run = uuid4()
    db.execute(
        "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'inventory','running')", (run,)
    )
    source = sources[0]
    product, group, invoice, writeoff, chart, line = [str(uuid4()) for _ in range(6)]
    data = {
        "product_groups": [{"id": group, "name": "Group", "parent_id": None, "deleted": False}],
        "products": [
            {
                "id": product,
                "name": "Product",
                "type": "GOODS",
                "parent_id": group,
                "main_unit_id": str(uuid4()),
                "deleted": False,
                "default_sale_price": "123456789.123456789123",
                "unit_weight": "0.000001",
                "unit_capacity": "1",
            }
        ],
        "incoming_invoices": [
            {
                "id": invoice,
                "date_incoming": "2026-09-09",
                "status": "NEW",
                "items": [
                    {
                        "num": 1,
                        "product_id": product,
                        "amount": "1.0000000000001",
                        "sum": "99.123456789123",
                        "price": "99.123456789123",
                    }
                ],
            }
        ],
        "writeoffs": [
            {
                "id": writeoff,
                "document_number": "W1",
                "date_incoming": "2026-09-09T22:30:00",
                "status": "PROCESSED",
                "store_id": str(uuid4()),
                "account_id": str(uuid4()),
                "items": [
                    {"num": 1, "product_id": product, "amount": "0.123456789123", "cost": None}
                ],
            }
        ],
        "assembly_charts": [
            {
                "id": chart,
                "assembled_product_id": product,
                "date_from": "2026-01-01",
                "date_to": None,
                "assembled_amount": "1",
                "items": [
                    {
                        "id": line,
                        "product_id": product,
                        "sort_weight": "0",
                        "amount_in": "0.123456789123",
                        "amount_middle": "0.1",
                        "amount_out": "0.09",
                    }
                ],
            }
        ],
    }
    result = {}
    import hashlib

    for resource, items in data.items():
        raw = json.dumps(items).encode()
        snapshot = {
            "id": str(uuid4()),
            "source_id": "primary",
            "resource": resource,
            "observed_at": datetime.now(UTC),
            "raw": raw,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "payload": {
                "items": items,
                "total": len(items),
                "sync_business_date": "2026-09-09",
                "source_fingerprint": source.fingerprint,
            },
        }
        append_snapshot(db, run, snapshot)
        result[resource] = snapshot
    return run, source, result


def test_inventory_idempotence_decimal_and_line_replacement(db, inventory):
    run, source, snapshots = inventory
    first = publish_inventory(db, snapshots, date(2026, 9, 9))
    assert publish_inventory(db, snapshots, date(2026, 9, 9)) == first
    assert db.execute("SELECT default_sale_price FROM chaika.products").fetchone()[0] == Decimal(
        "123456789.123456789123"
    )
    assert db.execute("SELECT cost FROM chaika.writeoff_items").fetchone()[0] is None
    assert db.execute("SELECT count(*) FROM chaika.incoming_invoice_items").fetchone()[0] == 1
    changed = deepcopy(snapshots)
    changed["incoming_invoices"]["payload"]["items"][0]["items"] = []
    publish_inventory(db, changed, date(2026, 9, 9))
    assert (
        db.execute("SELECT present_in_latest FROM chaika.incoming_invoice_items").fetchone()[0]
        is False
    )


def test_invalid_chart_rolls_back_all_datasets(db, inventory):
    _, _, snapshots = inventory
    bad = deepcopy(snapshots)
    bad["assembly_charts"]["payload"]["items"][0]["date_to"] = "2025-01-01"
    with pytest.raises(psycopg.errors.CheckViolation):
        publish_inventory(db, bad, date(2026, 9, 9))
    assert db.execute("SELECT count(*) FROM chaika.products").fetchone()[0] == 0


def test_resume_uses_validated_raw_and_rejects_other_day(db, inventory):
    run, source, snapshots = inventory
    db.execute("UPDATE chaika.sync_runs SET status='failed',finished_at=now() WHERE id=%s", (run,))
    restored = restore_snapshots(db, run, source, date(2026, 9, 9))
    assert set(restored) == set(snapshots)
    with pytest.raises(SyncError, match="inventory_resume_scope_mismatch"):
        restore_snapshots(db, run, source, date(2026, 9, 8))
