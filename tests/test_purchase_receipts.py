"""Receipt-mode authorization and parity using the digest's synthetic routing fixture."""

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from test_purchase_digest_query import (
    BAR,
    KITCHEN,
    PRODUCT,
    SIBLING,
    SUPPLIER,
    TRADER,
    UNIT,
    VENUE,
    WHOLESALER,
    get_report,
    receipt,
    transfer,
)
from test_purchase_digest_query import (
    digest_db as digest_db,
)
from test_purchase_digest_query import (
    routed as routed,
)

from app.web.purchase_prices import HOUSEHOLD_GROUP_ID, read_purchase_prices
from app.web.repository import Repository, Scope


class TemporarySchema:
    """Run production SQL against the isolated digest fixture's temporary tables."""

    def __init__(self, db):
        self.db = db

    def execute(self, query, params=None):
        if not isinstance(query, str):
            query = query.as_string(self.db)
        return self.db.execute(query.replace("chaika.", "pg_temp."), params)

    def transaction(self):
        return self.db.transaction()


@pytest.fixture
def prices(routed, monkeypatch):
    for table, columns in {
        "incoming_invoices": "last_seen_at timestamptz DEFAULT now()",
        "incoming_invoice_items": "num integer DEFAULT 1, ADD num_occurrence integer DEFAULT 1, "
        "ADD price numeric DEFAULT 99999",
        "products": "code text",
    }.items():
        routed.execute(f"ALTER TABLE {table} ADD {columns}")

    def read(stores=(KITCHEN,), *, selection=None, kind="receipts", unrestricted=False, **options):
        recent_only = options.pop("recent_only", False)
        as_of = options.pop("as_of", date(2026, 1, 11))
        scope = Scope(
            {"role": "owner" if unrestricted else "manager"},
            ({"id": VENUE, "name": "Первый ресторан"}, {"id": SIBLING, "name": "Второй"}),
            VENUE if stores == (KITCHEN,) and not unrestricted else None,
            tuple(stores),
            (),
        )
        return read_purchase_prices(
            TemporarySchema(routed),
            scope,
            object.__new__(Repository).resource_query(scope, "invoices"),
            kind=kind,
            selection=selection,
            recent_only=recent_only,
            as_of=as_of,
            **options,
        )

    return read


def lines(report):
    return [
        line
        for row in report["rows"]
        for observation in [row["current"], *row["previous"]["receipts"]]
        for line in observation["lines"]
    ]


def test_direct_and_two_legal_entities_match_digest(routed, prices):
    for day in range(1, 7):
        receipt(routed, day, BAR, day, 1, 100)
    origins = {
        receipt(routed, 30, WHOLESALER, 8, 5, 750, details={"invoice": "A-12"}),
        receipt(routed, 31, TRADER, 8, 2, 400, details={"invoice": "A-12"}),
    }
    receipt(routed, 33, KITCHEN, 10, 1, 220)
    transfer(routed, 40, WHOLESALER, KITCHEN, 2, 300)
    transfer(routed, 41, WHOLESALER, BAR, 3, 450)
    transfer(routed, 42, TRADER, KITCHEN, 2, 400)
    xlsx = get_report(routed)
    [summary] = prices()["rows"]
    assert summary["linked"] is False
    assert summary["current"]["amount"] == 5
    assert summary["current"]["sum"] == sum(Decimal(r["cost"]) for r in xlsx["rows"])
    detail = prices(selection=(PRODUCT, None, UNIT, False))
    evidence = lines(detail)
    assert {r["document_number"] for r in evidence} == {"33", "40", "42"}
    assert {r["store_id"] for r in evidence} == {KITCHEN}
    assert {r["supplier_id"] for r in evidence} == {SUPPLIER}
    assert {r["receipt_route"] for r in evidence} == {"direct", "via_procurement"}
    assert all(str(origin) not in str(detail) for origin in origins)
    assert all(str(UUID(int=2000 + serial)) not in str(detail) for serial in (40, 41, 42))
    assert summary["current"]["price"] == detail["rows"][0]["current"]["price"]


def test_source_visible_in_all_venues_is_not_counted_twice(routed, prices):
    receipt(routed, 30, WHOLESALER, 8, 5, 750, details={"invoice": "A-12"})
    transfer(routed, 40, WHOLESALER, KITCHEN, 2, 300)
    transfer(routed, 41, WHOLESALER, BAR, 3, 450)
    detail = prices((KITCHEN, BAR, WHOLESALER), selection=(PRODUCT, None, UNIT, False))
    assert [r["document_number"] for r in lines(detail)] == ["30"]
    assert detail["rows"][0]["current"]["amount"] == 5
    assert detail["rows"][0]["current"]["sum"] == 750


def test_destination_and_whole_document_access_are_required(routed, prices):
    receipt(routed, 30, WHOLESALER, 8, 5, 750, details={"invoice": "A-12"})
    transfer(routed, 40, WHOLESALER, KITCHEN, 2, 300)
    assert prices((BAR,))["rows"] == []
    assert prices(selection=(PRODUCT, BAR, UNIT, False))["rows"] == []
    routed.execute(
        "INSERT INTO incoming_invoice_items(document_id,store_id,product_id,amount_unit_id,"
        "amount,sum,details,present_in_latest,num) VALUES (%s,%s,%s,%s,1,150,'{}',true,2)",
        (UUID(int=1040), BAR, PRODUCT, UNIT),
    )
    assert prices()["rows"] == []
    assert prices(selection=(PRODUCT, None, UNIT, False))["rows"] == []


@pytest.mark.parametrize("reason", ["missing", "ambiguous", "overallocated"])
def test_unconfirmed_routes_are_excluded(routed, prices, reason):
    if reason != "missing":
        receipt(routed, 30, WHOLESALER, 8, 2, 300, details={"invoice": "A-12"})
    if reason == "ambiguous":
        receipt(routed, 31, WHOLESALER, 8, 2, 300, details={"invoice": "A-12"})
    transfer(routed, 40, WHOLESALER, KITCHEN, 2, 300)
    if reason == "overallocated":
        transfer(routed, 41, WHOLESALER, BAR, 2, 300)
    assert prices()["rows"] == []
    assert prices(selection=(PRODUCT, None, UNIT, False))["rows"] == []


def test_legacy_linked_history_is_unchanged_and_receipts_bypass_prepared(
    routed, prices, monkeypatch
):
    receipt(routed, 30, WHOLESALER, 8, 2, 300, details={"invoice": "A-12"})
    transfer(routed, 40, WHOLESALER, KITCHEN, 2, 300)

    def unexpected_cache(*args, **kwargs):
        pytest.fail("Receipt semantics must not use the legacy raw receipt cache")

    monkeypatch.setattr("app.web.purchase_prices.prepared_receipts_query", unexpected_cache)
    [row] = prices()["rows"]
    assert row["linked"] is False and row["current"]["price"] == 150
    legacy = prices(kind="linked", selection=(PRODUCT, KITCHEN, UNIT, True))
    assert legacy["rows"][0]["linked"] is True
    assert {r["document_number"] for r in lines(legacy)} == {"40"}
    assert prices(kind="unlinked", selection=(PRODUCT, KITCHEN, UNIT, False))["rows"] == []


def test_household_recency_and_top_filters_apply_to_receipts(routed, prices):
    receipt(routed, 1, KITCHEN, 1, 1, 100)
    receipt(routed, 30, WHOLESALER, 8, 2, 300, details={"invoice": "A-12"})
    transfer(routed, 40, WHOLESALER, KITCHEN, 2, 300)
    [row] = prices(top_increases=True)["rows"]
    assert row["percent"] == 50
    assert prices(recent_only=True, as_of=date(2026, 5, 1))["rows"] == []
    assert prices(selection=(PRODUCT, None, UNIT, False), as_of=date(2026, 5, 1))["rows"]
    routed.execute("INSERT INTO product_groups(id) VALUES (%s)", (HOUSEHOLD_GROUP_ID,))
    routed.execute("UPDATE products SET group_id=%s", (HOUSEHOLD_GROUP_ID,))
    assert prices()["rows"] == []
    assert prices(exclude_household=False)["rows"]


def test_nonprimary_origin_cannot_complete_primary_receipt(routed, prices):
    origin = receipt(routed, 30, WHOLESALER, 8, 2, 300, details={"invoice": "A-12"})
    transfer(routed, 40, WHOLESALER, KITCHEN, 2, 300)
    routed.execute("UPDATE incoming_invoices SET source_id='other' WHERE id=%s", (origin,))
    routed.execute(
        "UPDATE incoming_invoice_items SET source_id='other' WHERE document_id=%s", (origin,)
    )
    assert prices()["rows"] == []
    assert prices(selection=(PRODUCT, None, UNIT, False))["rows"] == []
