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


PRIMARY_LOADERS = (
    "inventory",
    "employees",
    "employee_roles",
    "dictionaries",
    "invoices",
    "store_balances",
    "accounts",
    "counteragent_balances",
    "cash_shifts",
)


@pytest.fixture
def primary_source_database(monkeypatch):
    """Exercise loader SQL in a rollback-only schema on the disposable test server."""
    import os
    from urllib.parse import urlsplit

    import app.source_capabilities as capabilities
    import app.sync_references as references

    url = os.environ.get("CHAIKA_PRIMARY_SOURCE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set CHAIKA_PRIMARY_SOURCE_TEST_DATABASE_URL for isolated primary SQL tests")
    target = urlsplit(url)
    if target.hostname != "127.0.0.1" or target.port != 55483:
        pytest.fail("Primary SQL tests require isolated localhost:55483")
    schema = f"primary_loader_test_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as connection, connection.transaction():
        connection.execute(f"CREATE SCHEMA {schema}")
        connection.execute(
            f"CREATE TABLE {schema}.sources (id text PRIMARY KEY, label text, "
            "base_url text, fingerprint text, server_type text, "
            "configured boolean DEFAULT true, verified_at timestamptz)"
        )
        connection.execute(
            f"CREATE TABLE {schema}.rms_bindings (source_id text PRIMARY KEY, "
            "chain_source_id text, state text, details jsonb, "
            "chain_snapshot_id uuid, rms_snapshot_id uuid)"
        )
        monkeypatch.setattr(capabilities, "ANALYTICS_SCHEMA", schema)
        monkeypatch.setattr(references, "ANALYTICS_SCHEMA", schema)
        yield connection, schema
        raise psycopg.Rollback()


@pytest.mark.parametrize("loader", PRIMARY_LOADERS)
@pytest.mark.parametrize("server_type", ["CHAIN", "STANDALONE_RMS", "REPLICATED_RMS", None])
def test_primary_loader_requires_persisted_primary_server_type(
    primary_source_database, monkeypatch, tmp_path, loader, server_type
):
    from contextlib import nullcontext
    from importlib import import_module

    from app.sync_references import Source

    db, schema = primary_source_database
    source = Source("primary", "Own server", "https://own.example/resto/api", "b" * 64)
    register_sources(db, [source])
    db.execute(f"UPDATE {schema}.sources SET server_type=%s WHERE id='primary'", (server_type,))
    if server_type == "STANDALONE_RMS":
        db.execute(f"UPDATE {schema}.sources SET verified_at=now() WHERE id='primary'")
        snapshot = uuid4()
        db.execute(
            f"INSERT INTO {schema}.rms_bindings VALUES "
            "('primary','primary','matched','{\"server_type\":\"STANDALONE_RMS\"}',%s,%s)",
            (snapshot, snapshot),
        )
    module = import_module(f"app.sync_{loader}")
    monkeypatch.setattr(module, "ANALYTICS_SCHEMA", schema)
    monkeypatch.setattr(module, "configured_sources", lambda _: [source])

    class PassedPrimaryPrecondition(Exception):
        pass

    class BoundaryDatabase:
        def execute(self, statement, parameters=None):
            if "sync_runs" in statement:
                raise PassedPrimaryPrecondition
            return db.execute(statement, parameters)

        def transaction(self):
            return db.transaction()

    class BoundaryCollector:
        def get(self, *args, **kwargs):
            raise PassedPrimaryPrecondition

    monkeypatch.setattr(module, "tenant_connect", lambda *a, **k: nullcontext(BoundaryDatabase()))
    monkeypatch.setattr(module, "reference_lock", lambda _: nullcontext())
    monkeypatch.setattr(module, "collector_client", lambda **k: nullcontext(BoundaryCollector()))
    monkeypatch.setattr(module, "validate_runtime_path", lambda path: path, raising=False)
    settings = config(database_url="isolated-test-only", sync_api_key="test-sync-key")
    day = date(2026, 10, 10)
    api_url = "http://127.0.0.1:8010"
    monkeypatch.setattr(module, "initial_progress", lambda *a: {}, raising=False)

    def invoke():
        if loader == "inventory":
            return module.synchronize_inventory(settings, api_url, day, tmp_path)
        if loader == "invoices":
            return module.synchronize_invoices(settings, api_url, day, day, tmp_path)
        if loader in {"store_balances", "counteragent_balances", "cash_shifts"}:
            return getattr(module, f"synchronize_{loader}")(settings, None, api_url)
        return getattr(module, f"synchronize_{loader}")(settings, api_url)

    if server_type in {"CHAIN", "STANDALONE_RMS"}:
        with pytest.raises(PassedPrimaryPrecondition):
            invoke()
    else:
        with pytest.raises(SyncError, match="^reference_sync_required$"):
            invoke()

    if server_type == "STANDALONE_RMS":
        invalid_evidence = (
            f"UPDATE {schema}.sources SET configured=false",
            f"UPDATE {schema}.sources SET verified_at=NULL",
            f"UPDATE {schema}.rms_bindings SET state='unmatched'",
            f"UPDATE {schema}.rms_bindings SET chain_source_id='other'",
            f"UPDATE {schema}.rms_bindings SET details='{{}}'",
            f"UPDATE {schema}.rms_bindings SET rms_snapshot_id='{uuid4()}'",
            f"DELETE FROM {schema}.rms_bindings",
        )
        for statement in invalid_evidence:
            with db.transaction():
                db.execute(statement)
                with pytest.raises(SyncError, match="^reference_sync_required$"):
                    invoke()
                raise psycopg.Rollback()
        with db.transaction():
            db.execute(f"INSERT INTO {schema}.sources (id,configured) VALUES ('rms-extra',true)")
            with pytest.raises(SyncError, match="^standalone_additional_sources_forbidden$"):
                invoke()
            raise psycopg.Rollback()
        extra = Source("rms-extra", "Extra", "https://extra.example/resto/api", "d" * 64)
        monkeypatch.setattr(module, "configured_sources", lambda _: [source, extra])
        with pytest.raises(SyncError, match="^standalone_additional_sources_forbidden$"):
            invoke()

    # Admitting standalone never permits replacement of a registered source's identity.
    monkeypatch.setattr(
        module,
        "configured_sources",
        lambda _: [Source(source.id, source.label, source.base_url, "c" * 64)],
    )
    with pytest.raises(SyncError, match="^source_identity_changed$"):
        invoke()
