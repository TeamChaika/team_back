"""Bounded OLAP drilldown, immutable captures, and exact UUID links to RMS events."""

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from uuid import UUID, uuid4, uuid5

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.config import BACKEND_DIR
from app.integrations.iiko.client import IikoClient
from app.integrations.iiko.errors import IikoError
from app.schemas.sales_drilldown import DiscountDetailsQuery
from app.services.iiko_auth import IikoAuthService
from app.services.iiko_olap_sales import _unique_fields
from app.services.sync_jobs import SyncJobError, write_json
from app.sync_references import SyncError, configured_sources, reference_lock
from app.sync_sales_history import FixedReport, request_for_day
from app.web.repository import serial

PAGE_SIZE = 100
MAX_ROWS = 10000
DISCOUNT = "ItemSaleEventDiscountType"
NAMESPACE = UUID("6ac7692b-8887-469e-8b31-880b749e34ac")


def query_for(context, order_id=None):
    request = request_for_day(context["request"], context["business_date"]).iiko_body()
    request["filters"]["Department.Id"] = {
        "filterType": "IncludeValues",
        "values": [str(context["department_id"])],
    }
    request["aggregateFields"] = ["DishDiscountSumInt", "DiscountSum"]
    request["groupByColFields"] = []
    if order_id is None:
        request["filters"][DISCOUNT] = {
            "filterType": "IncludeValues",
            "values": [context["discount_name"]],
        }
        request["groupByRowFields"] = [
            "OpenDate.Typed",
            "Department.Id",
            "UniqOrderId.Id",
            "OrderNum",
            DISCOUNT,
        ]
    else:
        request["filters"].pop(DISCOUNT, None)
        request["filters"]["UniqOrderId.Id"] = {
            "filterType": "IncludeValues",
            "values": [str(order_id)],
        }
        request["groupByRowFields"] = ["ItemSaleEvent.Id", "DishId", "DishName", DISCOUNT]
        request["aggregateFields"].append("DishAmountInt")
    return request


def parse_rows(raw: bytes, request: dict, context: dict, order_id=None):
    payload = json.loads(raw, parse_float=Decimal, object_pairs_hook=_unique_fields)
    if (
        not isinstance(payload, dict)
        or payload.get("summary") != []
        or not isinstance(payload.get("data"), list)
    ):
        raise ValueError("drilldown_invalid_data")
    if len(payload["data"]) > MAX_ROWS:
        raise SyncJobError("drilldown_too_large", "В детализации больше 10 000 строк.", 413)
    result, seen = [], set()
    for row in payload["data"]:
        if not isinstance(row, dict):
            raise ValueError("drilldown_invalid_row")
        key = tuple(row[field] for field in request["groupByRowFields"])
        if key in seen:
            raise ValueError("drilldown_duplicate_group")
        seen.add(key)
        for field in request["aggregateFields"]:
            if isinstance(row[field], bool) or not isinstance(row[field], (int, Decimal)):
                raise ValueError("drilldown_invalid_amount")
            if not Decimal(row[field]).is_finite():
                raise ValueError("drilldown_invalid_amount")
        item = dict(
            revenue=Decimal(row["DishDiscountSumInt"]), discount=Decimal(row["DiscountSum"])
        )
        if order_id is None:
            if (
                row["OpenDate.Typed"] != context["business_date"].isoformat()
                or UUID(row["Department.Id"]) != context["department_id"]
                or row[DISCOUNT] != context["discount_name"]
            ):
                raise ValueError("drilldown_scope_mismatch")
            number = row["OrderNum"]
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise ValueError("drilldown_invalid_order_number")
            item.update(order_id=UUID(row["UniqOrderId.Id"]), order_number=number)
        else:
            if not isinstance(row["DishName"], str):
                raise ValueError("drilldown_invalid_product_name")
            if row[DISCOUNT] is not None and not isinstance(row[DISCOUNT], str):
                raise ValueError("drilldown_invalid_discount")
            item.update(
                item_id=UUID(row["ItemSaleEvent.Id"]),
                product_id=UUID(row["DishId"]),
                name=row["DishName"],
                discount_name=row[DISCOUNT],
                quantity=Decimal(row["DishAmountInt"]),
            )
        result.append(item)
    return serial(result)


def reconcile(rows, expected, discount_name=None, *, items=False):
    selected = [r for r in rows if not items or r["discount_name"] == discount_name]
    with localcontext() as ctx:
        ctx.prec = 70
        comparisons = [
            dict(
                field=field,
                expected=Decimal(expected[field]),
                actual=sum((Decimal(row[field]) for row in selected), Decimal(0)),
            )
            for field in ("revenue", "discount")
        ]
        for check in comparisons:
            check["delta"] = check["actual"] - check["expected"]
    return serial(dict(matched=all(c["delta"] == 0 for c in comparisons), checks=comparisons))


async def capture(settings, request, root):
    root.mkdir(parents=True, mode=0o700)
    client = IikoClient(settings)
    auth = IikoAuthService(settings, client)
    manifest = dict(request=request, started_at=datetime.now(UTC).isoformat())
    try:
        download = await auth.download_daily_sales(root / "raw.json", query=FixedReport(request))
        manifest.update(sha256=download.sha256, received_at=datetime.now(UTC).isoformat())
    finally:
        try:
            manifest["logout"] = (await auth.logout()).state
            if manifest["logout"] != "logged_out":
                raise SyncJobError("drilldown_logout_failed", "Не удалось завершить сессию iiko.")
        finally:
            await client.aclose()
            write_json(root / "manifest.json", manifest)
    raw = (root / "raw.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
        raise ValueError("drilldown_hash_mismatch")
    return raw, manifest


def fetch_one(db, sql, params):
    with db.cursor(row_factory=dict_row) as cursor:
        return cursor.execute(sql, params).fetchone()


def read_context(db, query, allowed_departments):
    row = fetch_one(
        db,
        "SELECT r.id AS report_id,x.ordinal,s.business_date,x.department_id,n.name AS department,"
        "x.dimensions->>'ItemSaleEventDiscountType' AS discount_name,x.revenue,x.discount,"
        "r.request,src.fingerprint FROM chaika.sales_reports r "
        "JOIN chaika.sales_report_sets s ON s.id=r.set_id "
        "JOIN chaika.sales_report_rows x ON x.report_id=r.id "
        "JOIN chaika.sources src ON src.id=s.source_id "
        "LEFT JOIN chaika.corporate_nodes n ON n.source_id=s.source_id AND n.id=x.department_id "
        "WHERE r.id=%s AND x.ordinal=%s AND r.kind='discounts' AND s.source_id='primary' "
        "AND (%s OR x.department_id=ANY(%s::uuid[]))",
        (query.report_id, query.ordinal, allowed_departments is None, allowed_departments or []),
    )
    if row is None:
        raise SyncJobError("drilldown_not_found", "Строка скидки не найдена или недоступна.", 404)
    return row


def stored_capture(db, key):
    return fetch_one(
        db, "SELECT id,rows,observed_at FROM chaika.sales_drilldown_captures WHERE id=%s", (key,)
    )


def load_capture(db, settings, context, order_id=None):
    key = uuid5(NAMESPACE, f"{context['report_id']}:{context['ordinal']}:{order_id or 'orders'}:v1")
    saved = stored_capture(db, key)
    if saved:
        return saved
    with reference_lock(db):
        saved = stored_capture(db, key)
        if saved:
            return saved
        if context["fingerprint"] != configured_sources(settings)[0].fingerprint:
            raise SyncJobError("drilldown_source_mismatch", "Настройки источника изменились.")
        with httpx.Client(timeout=30, trust_env=False) as http:
            response = http.post("http://127.0.0.1:8010/api/v1/iiko/connections/primary/logout")
            if response.status_code != 200 or response.json().get("state") != "logged_out":
                raise SyncJobError(
                    "drilldown_api_logout_failed", "Не удалось освободить сессию iiko."
                )
        request = query_for(context, order_id)
        root = BACKEND_DIR / ".local/discount-details" / str(uuid4())
        raw, manifest = asyncio.run(capture(settings, request, root))
        rows = parse_rows(raw, request, context, order_id)
        with db.transaction():
            db.execute(
                "INSERT INTO chaika.sales_drilldown_captures(id,report_id,ordinal,order_id,"
                "request,observed_at,raw,sha256,rows) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    key,
                    context["report_id"],
                    context["ordinal"],
                    order_id,
                    Jsonb(request),
                    manifest["received_at"],
                    raw,
                    manifest["sha256"],
                    Jsonb(rows),
                ),
            )
        return dict(id=key, rows=rows, observed_at=manifest["received_at"])


def add_event_links(db, context, rows):
    linked = {}
    if rows:
        for source, order_id, count in db.execute(
            "SELECT e.source_id,e.order_id,count(*) FROM chaika.rms_events e "
            "JOIN chaika.rms_bindings b ON b.source_id=e.source_id "
            "WHERE b.chain_source_id='primary' AND b.state='matched' AND b.department_id=%s "
            "AND e.order_id=ANY(%s::uuid[]) GROUP BY e.source_id,e.order_id",
            (context["department_id"], [r["order_id"] for r in rows]),
        ).fetchall():
            linked.setdefault(str(order_id), []).append((source, count))
    for row in rows:
        matches = linked.get(row["order_id"], [])
        row["topology"] = (
            dict(
                source_id=matches[0][0],
                event_count=matches[0][1],
                day=context["business_date"],
                order_id=row["order_id"],
                order_number=row["order_number"],
            )
            if len(matches) == 1
            else None
        )
        row["events_state"] = (
            "matched" if len(matches) == 1 else "ambiguous" if matches else "missing"
        )


def discount_details(settings, query: DiscountDetailsQuery, allowed_departments):
    """Authorization must supply explicit department UUIDs; None is reserved for technical API."""
    try:
        with psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            prepare_threshold=None,
        ) as db:
            context = read_context(db, query, allowed_departments)
            orders = load_capture(db, settings, context)
            selected = None
            data = orders
            if query.order_id is not None:
                selected = next(
                    (r for r in orders["rows"] if r["order_id"] == str(query.order_id)), None
                )
                if selected is None:
                    raise SyncJobError(
                        "drilldown_order_not_found", "Заказ отсутствует в этой скидке.", 404
                    )
                selected = deepcopy(selected)
                add_event_links(db, context, [selected])
                data = load_capture(db, settings, context, query.order_id)
            check = reconcile(
                data["rows"],
                selected or context,
                context["discount_name"],
                items=selected is not None,
            )
            rows = deepcopy(data["rows"][query.offset : query.offset + PAGE_SIZE])
            if selected is None:
                add_event_links(db, context, rows)
            return serial(
                dict(
                    kind="items" if selected else "orders",
                    capture_id=data["id"],
                    observed_at=data["observed_at"],
                    department=context["department"],
                    business_date=context["business_date"],
                    discount_name=context["discount_name"],
                    selected_order=selected,
                    rows=rows,
                    total=len(data["rows"]),
                    offset=query.offset,
                    limit=PAGE_SIZE,
                    reconciliation=check,
                )
            )
    except SyncError as error:
        if str(error) == "sync_already_running":
            raise SyncJobError(
                "drilldown_busy",
                "Сейчас идёт синхронизация iiko. Повторите после её завершения.",
                409,
            ) from None
        raise
    except IikoError as error:
        raise SyncJobError(
            error.code, "Не удалось получить детализацию из iiko.", error.status_code
        ) from None
    except (ValueError, KeyError, TypeError, OSError, httpx.HTTPError):
        raise SyncJobError(
            "drilldown_invalid_response",
            "Не удалось проверить или сохранить детализацию iiko.",
            502,
        ) from None
