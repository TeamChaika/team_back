"""Discount drilldown uses source identifiers, exact amounts and fresh restaurant scope."""

import hashlib
import json
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from test_reference_sync import db as db
from test_sales_coverage import repository
from test_sales_import import review as review

from app.schemas.sales_drilldown import DiscountDetailsQuery
from app.services import sales_drilldown as detail
from app.services.sync_jobs import SyncJobError

ORDER, ITEM, PRODUCT = UUID(int=2), UUID(int=3), UUID(int=4)


def source(context, order_id=None):
    row = {"DishDiscountSumInt": 0, "DiscountSum": 900}
    if order_id is None:
        row.update(
            {
                "OpenDate.Typed": str(context["business_date"]),
                "Department.Id": str(context["department_id"]),
                "UniqOrderId.Id": str(ORDER),
                "OrderNum": 42,
                detail.DISCOUNT: context["discount_name"],
            }
        )
    else:
        row.update(
            {
                "ItemSaleEvent.Id": str(ITEM),
                "DishId": str(PRODUCT),
                "DishName": "Test dish",
                detail.DISCOUNT: context["discount_name"],
                "DishAmountInt": 1,
            }
        )
    return json.dumps({"data": [row], "summary": []}).encode()


@pytest.fixture
def context(review):
    return dict(
        report_id=UUID(int=1),
        ordinal=0,
        business_date=date(2026, 9, 10),
        department_id=UUID(int=1),
        discount_name="test",
        request=review[1]["reports"]["discounts"]["request"],
        revenue=Decimal(0),
        discount=Decimal(900),
    )


def test_requests_keep_seven_field_limit_date_and_department(context):
    original = deepcopy(context)
    for oid in (None, ORDER):
        q = detail.query_for(context, oid)
        assert len(q["groupByRowFields"]) + len(q["aggregateFields"]) == 7
        assert q["filters"]["OpenDate.Typed"]["to"] == "2026-09-11T00:00:00"
        assert q["filters"]["Department.Id"]["values"] == [str(UUID(int=1))]
        assert q["buildSummary"] is False
    assert context == original
    assert detail.DISCOUNT not in detail.query_for(context, ORDER)["filters"]


def test_orders_and_items_keep_identifiers_and_exact_amounts(context):
    for oid in (None, ORDER):
        raw = source(context, oid).replace(
            b'"DiscountSum": 900', b'"DiscountSum": 900.000000000000000000001'
        )
        rows = detail.parse_rows(raw, detail.query_for(context, oid), context, oid)
        assert rows[0]["discount"] == "900.000000000000000000001"
        assert rows[0]["order_id" if oid is None else "item_id"] == str(
            ORDER if oid is None else ITEM
        )
    check = detail.reconcile(rows, context, "test", items=True)
    assert not check["matched"] and check["checks"][1]["delta"] == "1E-21"


@pytest.mark.parametrize(
    "field,value",
    [
        ("Department.Id", str(UUID(int=99))),
        ("OpenDate.Typed", "2026-09-09"),
        (detail.DISCOUNT, "other"),
    ],
)
def test_wrong_scope_from_iiko_is_rejected(context, field, value):
    payload = json.loads(source(context))
    payload["data"][0][field] = value
    with pytest.raises(ValueError, match="scope_mismatch"):
        detail.parse_rows(json.dumps(payload).encode(), detail.query_for(context), context)


def test_duplicate_groups_and_non_numeric_values_rejected(context):
    p = json.loads(source(context))
    p["data"] *= 2
    with pytest.raises(ValueError, match="duplicate_group"):
        detail.parse_rows(json.dumps(p).encode(), detail.query_for(context), context)
    p["data"] = p["data"][:1]
    p["data"][0]["DiscountSum"] = True
    with pytest.raises(ValueError, match="invalid_amount"):
        detail.parse_rows(json.dumps(p).encode(), detail.query_for(context), context)


def test_cache_is_immutable_and_authorization_precedes_reads_or_fetches(db, review, monkeypatch):
    root, manifest = review
    path = root / "discounts.json"
    path.write_text(path.read_text().replace('"data": [{', '"data": [{"DiscountSum": 900, ', 1))
    manifest["reports"]["discounts"]["request"]["aggregateFields"].append("DiscountSum")
    manifest["reports"]["discounts"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    repository(db, review)
    report_id = db.execute("SELECT id FROM chaika.sales_reports WHERE kind='discounts'").fetchone()[
        0
    ]
    query = DiscountDetailsQuery(report_id=report_id, ordinal=0)
    calls = []

    @contextmanager
    def connection(*args, **kwargs):
        yield db

    monkeypatch.setattr(detail.psycopg, "connect", connection)
    monkeypatch.setattr(
        detail, "configured_sources", lambda _: [SimpleNamespace(fingerprint="a" * 64)]
    )
    factory = httpx.Client
    monkeypatch.setattr(
        detail.httpx,
        "Client",
        lambda **kwargs: factory(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"state": "logged_out"})
            )
        ),
    )

    async def capture(settings, request, root):
        ctx = detail.read_context(db, query, [UUID(int=1)])
        oid = ORDER if "UniqOrderId.Id" in request["filters"] else None
        raw = source(ctx, oid)
        calls.append(oid)
        return raw, dict(
            sha256=hashlib.sha256(raw).hexdigest(), received_at="2026-09-11T01:00:00+00:00"
        )

    monkeypatch.setattr(detail, "capture", capture)
    settings = SimpleNamespace(database_url=SimpleNamespace(get_secret_value=lambda: "isolated"))
    first = detail.discount_details(settings, query, [UUID(int=1)])
    assert first["rows"][0]["order_id"] == str(ORDER)
    assert first["rows"][0]["topology"] is None
    assert (
        detail.discount_details(settings, query, [UUID(int=1)])["capture_id"] == first["capture_id"]
    )
    assert calls == [None]
    with pytest.raises(SyncJobError) as denied:
        detail.discount_details(settings, query, [UUID(int=999)])
    assert denied.value.status_code == 404 and calls == [None]
    with pytest.raises(SyncJobError) as wrong_order:
        detail.discount_details(
            settings, query.model_copy(update={"order_id": UUID(int=999)}), [UUID(int=1)]
        )
    assert wrong_order.value.status_code == 404 and calls == [None]
    items = detail.discount_details(
        settings, query.model_copy(update={"order_id": ORDER}), [UUID(int=1)]
    )
    assert (
        items["rows"][0]["item_id"] == str(ITEM) and items["selected_order"]["order_number"] == 42
    )
    assert calls == [None, ORDER]
    assert db.execute("SELECT count(*) FROM chaika.sales_drilldown_captures").fetchone()[0] == 2


def test_capture_logs_out_even_if_download_fails(context, monkeypatch, tmp_path):
    import asyncio

    calls = []

    class Client:
        def __init__(self, _):
            pass

        async def aclose(self):
            calls.append("closed")

    class Auth:
        def __init__(self, *_):
            pass

        async def download_daily_sales(self, *args, **kwargs):
            calls.append("request")
            raise ValueError("failed")

        async def logout(self):
            calls.append("logout")
            return SimpleNamespace(state="logged_out")

    monkeypatch.setattr(detail, "IikoClient", Client)
    monkeypatch.setattr(detail, "IikoAuthService", Auth)
    with pytest.raises(ValueError):
        asyncio.run(detail.capture(None, detail.query_for(context), tmp_path / "capture"))
    assert calls == ["request", "logout", "closed"]
    assert json.loads((tmp_path / "capture/manifest.json").read_text())["logout"] == "logged_out"


@pytest.mark.parametrize("raw", [b"[]", b'{"data":[null],"summary":[]}'])
def test_malformed_envelopes_are_rejected(context, raw):
    with pytest.raises(ValueError, match="drilldown_invalid"):
        detail.parse_rows(raw, detail.query_for(context), context)
