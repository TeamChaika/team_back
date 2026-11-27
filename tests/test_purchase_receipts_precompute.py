"""Prepared economics must exactly preserve the canonical matcher and warehouse boundary."""

from pathlib import Path
from uuid import uuid4

import pytest
from test_purchase_digest_query import BAR, KITCHEN, PRODUCT, UNIT, WHOLESALER, receipt, transfer
from test_purchase_receipts import TemporarySchema
from test_purchase_receipts import digest_db as digest_db
from test_purchase_receipts import prices as prices
from test_purchase_receipts import routed as routed

from app.purchase_receipts_precompute import publish_prepared, source_revision


@pytest.fixture
def prepared(routed, prices):
    routed.execute("CREATE TEMP TABLE sources(id text PRIMARY KEY)")
    routed.execute("INSERT INTO sources VALUES ('primary')")
    routed.execute(
        "CREATE TEMP TABLE sync_runs(id uuid,job text,status text,finished_at timestamptz,"
        "counts jsonb DEFAULT '{}')"
    )
    migration = Path(
        "supabase/migrations/20261127010000_purchase_receipts_prepared.sql"
    ).read_text()
    TemporarySchema(routed).execute(migration.split("DO $permissions$")[0])

    def publish():
        db = TemporarySchema(routed)
        return publish_prepared(db, source_revision(db))

    return publish


@pytest.mark.parametrize("stores", [(KITCHEN,), (BAR,), (KITCHEN, BAR, WHOLESALER)])
def test_prepared_list_and_history_match_canonical(routed, prices, prepared, stores):
    receipt(routed, 1, KITCHEN, 1, 1, 100)
    receipt(routed, 30, WHOLESALER, 8, 3, 100, details={"invoice": "A-12"})
    transfer(routed, 40, WHOLESALER, KITCHEN, 1, "33.33")
    transfer(routed, 41, WHOLESALER, BAR, 2, "66.67")
    raw = prices(stores)
    detail = prices(stores, selection=(PRODUCT, None, UNIT, False))
    prepared()
    assert prices(stores, require_prepared=True) == raw
    assert prices(stores, selection=(PRODUCT, None, UNIT, False), require_prepared=True) == detail


def test_visible_origin_with_hidden_line_does_not_suppress_delivery(routed, prices, prepared):
    origin = receipt(routed, 30, WHOLESALER, 8, 2, 300, details={"invoice": "A-12"})
    routed.execute(
        "INSERT INTO incoming_invoice_items(document_id,store_id,product_id,amount_unit_id,"
        "amount,sum,details,present_in_latest,num) VALUES (%s,%s,%s,%s,1,99,"
        "'{\"is_additional_expense\":true}',true,2)",
        (origin, BAR, PRODUCT, UNIT),
    )
    transfer(routed, 40, WHOLESALER, KITCHEN, 2, 300)
    stores = (KITCHEN, WHOLESALER)
    raw = prices(stores)
    assert raw["rows"][0]["current"]["sum"] == 300
    prepared()
    assert prices(stores, require_prepared=True) == raw
    assert prices((WHOLESALER,), require_prepared=True)["rows"] == []


@pytest.mark.parametrize(
    "job,resource",
    [
        ("inventory", "incoming_invoices"),
        ("inventory", "outgoing_invoices"),
        ("references", None),
        ("dictionaries", None),
        ("products", None),
    ],
)
def test_changed_publication_fails_closed_until_refresh(routed, prices, prepared, job, resource):
    receipt(routed, 1, KITCHEN, 1, 1, 100)
    prepared()
    assert prices(require_prepared=True)["rows"]
    routed.execute(
        "INSERT INTO sync_runs VALUES (%s,%s,%s,now(),jsonb_build_object("
        "'resource',%s::text,'last_snapshot_id',%s::text))",
        (uuid4(), job, "running" if resource else "succeeded", resource, str(uuid4())),
    )
    with pytest.raises(ValueError, match="receipt_preparation_pending"):
        prices(require_prepared=True)
    with pytest.raises(ValueError, match="receipt_preparation_pending"):
        prices(selection=(PRODUCT, None, UNIT, False), require_prepared=True)
    prepared()
    assert prices(require_prepared=True)["rows"]


def test_missing_cache_is_pending_and_unrelated_job_does_not_invalidate(routed, prices, prepared):
    with pytest.raises(ValueError, match="receipt_preparation_pending"):
        prices(require_prepared=True)
    prepared()
    routed.execute(
        "INSERT INTO sync_runs(id,job,status,finished_at) VALUES "
        "(%s,'store_balances','succeeded',now()),(%s,'inventory','running',NULL)",
        (uuid4(), uuid4()),
    )
    assert prices(require_prepared=True)["rows"] == []


def test_owner_uses_direct_economics_once_and_revision_is_timezone_independent(
    routed, prices, prepared
):
    receipt(routed, 30, WHOLESALER, 8, 2, 300, details={"invoice": "A-12"})
    transfer(routed, 40, WHOLESALER, KITCHEN, 2, 300)
    routed.execute(
        "INSERT INTO sync_runs(id,job,status,finished_at) VALUES "
        "(%s,'inventory','succeeded',now())",
        (uuid4(),),
    )
    raw = prices(unrestricted=True)
    prepared()
    routed.execute("SET LOCAL TIME ZONE 'Europe/Moscow'")
    assert prices(unrestricted=True, require_prepared=True) == raw
    assert raw["rows"][0]["current"]["amount"] == 2


def test_moved_receipt_invalidates_then_refreshes_warehouse_signature(routed, prices, prepared):
    receipt(routed, 1, KITCHEN, 1, 1, 100)
    prepared()
    routed.execute("UPDATE incoming_invoices SET default_store_id=%s", (BAR,))
    routed.execute(
        "INSERT INTO sync_runs(id,job,status,finished_at) VALUES "
        "(%s,'inventory','succeeded',now())",
        (uuid4(),),
    )
    with pytest.raises(ValueError, match="receipt_preparation_pending"):
        prices(require_prepared=True)
    prepared()
    assert prices(require_prepared=True)["rows"] == []
    assert prices((BAR,), require_prepared=True) == prices((BAR,))
