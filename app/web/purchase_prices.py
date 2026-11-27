"""Compare product receipts across the accessible network, preserving invoice evidence."""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from heapq import nlargest
from operator import itemgetter
from uuid import UUID

from app.web.coverage import ZONE
from app.web.purchase_prices_prepared import prepared_receipts_query
from app.web.purchase_receipts import receipt_lines_query

# Confirmed Chain group "ХОЗНУЖДЫ". Match its identity, including renamed/deleted
# descendants, rather than guessing a product's purpose from its name.
HOUSEHOLD_GROUP_ID = UUID("caf4e221-3d55-400e-b932-961ba139542d")
HISTORY_SIZE = 6
RECENT_DAYS = 60
TOP_INCREASES = 5


def household_product_ids(db):
    return [
        row["id"]
        for row in db.execute(
            "WITH RECURSIVE household_groups(id) AS ("
            "SELECT id FROM chaika.product_groups WHERE source_id='primary' AND id=%s "
            "UNION SELECT g.id FROM chaika.product_groups g JOIN household_groups h "
            "ON g.parent_id=h.id WHERE g.source_id='primary') "
            "SELECT p.id FROM chaika.products p JOIN household_groups h ON p.group_id=h.id "
            "WHERE p.source_id='primary'",
            (HOUSEHOLD_GROUP_ID,),
        ).fetchall()
    ]


JOIN = """chaika.incoming_invoice_items i JOIN chaika.incoming_invoices t
    ON (t.source_id,t.id)=(i.source_id,i.document_id)"""
LINKED = "(NULLIF(t.details->>'linked_outgoing_invoice_id','') IS NOT NULL)"
ELIGIBLE = """t.source_id='primary' AND t.status='PROCESSED' AND i.present_in_latest
    AND i.product_id IS NOT NULL
    AND COALESCE(i.details->>'is_additional_expense','false')<>'true'"""
FIELDS = (
    """i.product_id,coalesce(i.store_id,t.default_store_id) AS store_id,
    i.amount_unit_id AS unit_id,t.date_incoming AS date,t.id AS document_id,
    t.document_number,t.supplier_id,i.num,i.num_occurrence,i.amount,i.sum,i.price,
    t.last_seen_at,"""
    + LINKED
    + " AS linked"
)


def key(row):
    return (row["product_id"], row["store_id"], row["unit_id"], row["linked"])


def observation(lines):
    """Same-time receipts are one weighted observation, never ordered by arbitrary UUID."""
    amount = sum((r["amount"] or Decimal(0) for r in lines), Decimal(0))
    value = sum((r["sum"] for r in lines), Decimal(0))
    valid = all(r["amount"] is not None and r["amount"] > 0 and r["sum"] >= 0 for r in lines)
    return {
        "date": lines[0]["date"],
        "amount": amount,
        "sum": value,
        "price": (value / amount).quantize(Decimal("0.0001")) if valid else None,
        "lines": lines,
    }


def baseline(receipts):
    """The current receipt is never included, and invalid history is not skipped."""
    amount = sum((r["amount"] for r in receipts), Decimal(0))
    value = sum((r["sum"] for r in receipts), Decimal(0))
    valid = all(r["price"] is not None for r in receipts)
    return {
        "date_from": receipts[0]["date"],
        "date": receipts[-1]["date"],
        "amount": amount,
        "sum": value,
        "price": (value / amount).quantize(Decimal("0.0001")) if valid else None,
        "count": len(receipts),
        "receipts": list(receipts),
    }


STAMP = (
    "CASE WHEN length(t.date_incoming)=10 THEN t.date_incoming||'T00:00:00' "
    "ELSE t.date_incoming END"
)


def unit_price(amount, value, valid):
    return (value / amount).quantize(Decimal("0.0001")) if valid else None


def change(identity, now, before, stats):
    stats["observations"] += 1
    if 0 < before["count"] < HISTORY_SIZE:
        stats["short_history"] += 1
    if (
        (identity[0] is None or identity[2] is None)
        or now["price"] is None
        or (before["count"] and before["price"] is None)
    ):
        stats["invalid"] += 1
        return None
    delta = now["price"] - before["price"] if before["count"] else None
    if delta is None:
        stats["no_previous"] += 1
    elif delta == 0:
        stats["unchanged"] += 1
    return dict(
        product_id=identity[0],
        store_id=identity[1],
        unit_id=identity[2],
        linked=identity[3],
        current=now,
        previous=before,
        delta=delta,
        percent=delta / before["price"] * 100 if before["price"] else None,
    )


def empty_baseline():
    return dict(
        date_from=None,
        date=None,
        amount=Decimal(0),
        sum=Decimal(0),
        price=None,
        count=0,
        receipts=[],
    )


def recent_start(today: date, period: str | None) -> date:
    if period == "month":
        last_month_end = today.replace(day=1) - timedelta(days=1)
        return last_month_end.replace(day=min(today.day, last_month_end.day))
    return today - timedelta(days=RECENT_DAYS - 1)


def read_purchase_prices(
    db,
    scope,
    spec,
    kind="unlinked",
    exclude_household=True,
    selection=None,
    *,
    recent_only=True,
    recent_period=None,
    as_of: date | None = None,
    top_increases=False,
    require_prepared=False,
):
    """Latest price per product/unit/link across all accessible warehouses.

    The list transfers only aggregate values. Full source rows for the seven
    latest receipt timestamps are loaded when opening a single product's history.
    Recency limits the list's latest receipts, never the comparison history.
    """
    join_sql, fields_sql, linked_sql = JOIN, FIELDS, LINKED
    receipt_prepared = None
    source_params = []
    if kind == "receipts":
        if require_prepared:
            from app.web.purchase_receipts_prepared import prepared_queries

            receipt_prepared = prepared_queries(db, scope)
            if receipt_prepared is None:
                raise ValueError("receipt_preparation_pending")
        source_sql, source_params = (
            receipt_prepared[0] if receipt_prepared else receipt_lines_query(scope, spec)
        )
        join_sql = (
            "(" + source_sql + ") i JOIN chaika.incoming_invoices t "
            "ON (t.source_id,t.id)=(i.source_id,i.document_id)"
        )
        linked_sql = "false"
        fields_sql = (
            FIELDS.replace("t.supplier_id", "i.supplier_id").replace(LINKED, "false")
            + ",i.receipt_route"
        )
    today = as_of or datetime.now(ZONE).date()
    recent_since = recent_start(today, recent_period)
    clauses = [ELIGIBLE, "(" + spec["guard"] + ")"]
    params = [*source_params, *spec["params"]]
    if kind not in {"all", "receipts"}:
        clauses.append(linked_sql + "=%s")
        params.append(kind == "linked")
    excluded_products = household_product_ids(db) if exclude_household else None
    if exclude_household:
        clauses.append("NOT (i.product_id=ANY(%s::uuid[]))")
        params.append(excluded_products)
    if selection:
        product_id, store_id, unit_id, linked = selection
        clauses.extend(["i.product_id=%s", "i.amount_unit_id=%s", linked_sql + "=%s"])
        params.extend([product_id, unit_id, linked])
        # Explicit warehouse detail URLs remain usable; the network UI omits store_id.
        if store_id is not None:
            clauses.append("coalesce(i.store_id,t.default_store_id)=%s")
            params.append(store_id)
    where = " AND ".join(clauses)
    stats = dict(observations=0, no_previous=0, invalid=0, unchanged=0, short_history=0)
    changes = []
    with localcontext() as ctx:
        ctx.prec = 70
        if selection:
            rows = db.execute(
                "WITH history AS (SELECT " + fields_sql + "," + STAMP + " AS stamp,"
                "dense_rank() OVER (ORDER BY "
                + STAMP
                + " DESC) AS position FROM "
                + join_sql
                + " WHERE "
                + where
                + ") SELECT * FROM history WHERE position<=%s "
                "ORDER BY stamp,document_id,num,num_occurrence",
                [*params, HISTORY_SIZE + 1],
            ).fetchall()
            groups = defaultdict(list)
            for row in rows:
                row["date"] = row.pop("stamp")
                row.pop("position")
                groups[row["date"]].append(row)
            receipts = [observation(lines) for _, lines in sorted(groups.items())]
            if receipts:
                before = baseline(receipts[:-1]) if len(receipts) > 1 else empty_baseline()
                item = change(selection, receipts[-1], before, stats)
                if item:
                    changes.append(item)
        else:
            prepared = (
                None
                if kind == "receipts"
                else prepared_receipts_query(db, scope, kind, excluded_products)
            )
            if receipt_prepared:
                prepared_sql, prepared_params = receipt_prepared[1]
                if excluded_products is not None:
                    prepared_sql = (
                        "SELECT * FROM (" + prepared_sql + ") observations "
                        "WHERE NOT (product_id=ANY(%s::uuid[]))"
                    )
                    prepared_params = [*prepared_params, excluded_products]
                prepared = prepared_sql, prepared_params
            receipts_sql, receipt_params = prepared or (
                "SELECT i.product_id,NULL::uuid AS store_id,i.amount_unit_id AS unit_id,"
                + linked_sql
                + " AS linked,"
                + STAMP
                + " AS date,"
                "sum(i.amount) AS amount,sum(i.sum) AS sum,"
                "bool_and(i.amount IS NOT NULL AND i.amount>0 AND i.sum>=0) AS valid FROM "
                + join_sql
                + " WHERE "
                + where
                + " GROUP BY 1,2,3,4,5",
                params,
            )
            summary_params = [*receipt_params, HISTORY_SIZE + 1]
            recency = ""
            if recent_only:
                recency = " HAVING max(date)>=%s AND max(date)<%s"
                summary_params.extend(
                    [recent_since.isoformat(), (today + timedelta(days=1)).isoformat()]
                )
            summaries = db.execute(
                "WITH receipts AS (" + receipts_sql + "), ranked AS ("
                "SELECT *,row_number() OVER (PARTITION BY product_id,unit_id,linked "
                "ORDER BY date DESC) AS position FROM receipts) "
                "SELECT product_id,store_id,unit_id,linked,"
                "max(date) FILTER (WHERE position=1) AS date,"
                "max(amount) FILTER (WHERE position=1) AS amount,"
                "max(sum) FILTER (WHERE position=1) AS sum,"
                "bool_and(valid) FILTER (WHERE position=1) AS valid,"
                "min(date) FILTER (WHERE position>1) AS previous_date_from,"
                "max(date) FILTER (WHERE position>1) AS previous_date,"
                "sum(amount) FILTER (WHERE position>1) AS previous_amount,"
                "sum(sum) FILTER (WHERE position>1) AS previous_sum,"
                "bool_and(valid) FILTER (WHERE position>1) AS previous_valid,"
                "count(*) FILTER (WHERE position>1) AS previous_count "
                "FROM ranked WHERE position<=%s GROUP BY 1,2,3,4" + recency,
                summary_params,
            ).fetchall()
            for row in summaries:
                now = dict(
                    date=row["date"],
                    amount=row["amount"],
                    sum=row["sum"],
                    price=unit_price(row["amount"], row["sum"], row["valid"]),
                    lines=[],
                )
                before = empty_baseline()
                if row["previous_count"]:
                    before.update(
                        date_from=row["previous_date_from"],
                        date=row["previous_date"],
                        amount=row["previous_amount"],
                        sum=row["previous_sum"],
                        price=unit_price(
                            row["previous_amount"], row["previous_sum"], row["previous_valid"]
                        ),
                        count=row["previous_count"],
                    )
                item = change(key(row), now, before, stats)
                if item:
                    changes.append(item)
    if top_increases:
        changes = [r for r in changes if r["percent"] is not None and r["percent"] > 0]
        if len(changes) > TOP_INCREASES:
            # Product names break ties in the full list. Keep every candidate tied at the cutoff.
            cutoff = nlargest(TOP_INCREASES, (r["percent"] for r in changes))[-1]
            changes = [r for r in changes if r["percent"] >= cutoff]
    if changes:
        enrich(db, scope, changes)
    changes.sort(
        key=lambda r: (r["current"]["date"], r["product"], str(r["store_id"])), reverse=True
    )
    if top_increases:
        changes = nlargest(TOP_INCREASES, changes, key=itemgetter("percent"))
    return dict(
        kind=kind,
        exclude_household=exclude_household,
        recent_only=recent_only and selection is None,
        recent_days=(today - recent_since).days + 1,
        recent_since=recent_since.isoformat(),
        history_size=HISTORY_SIZE,
        comparison_method="weighted_previous_receipts",
        mode="latest",
        grouping="product_warehouse"
        if selection and selection[1] is not None
        else "product_network",
        rows=changes,
        stats=stats,
    )


def enrich(db, scope, changes):
    labels = db.execute(
        "SELECT id,name,code FROM chaika.products WHERE source_id='primary' AND id=ANY(%s::uuid[])",
        (list({r["product_id"] for r in changes}),),
    )
    stores = db.execute("SELECT id,parent_id,name FROM chaika.stores WHERE source_id='primary'")
    nodes = db.execute(
        "SELECT id,parent_id,name FROM chaika.corporate_nodes WHERE source_id='primary'"
    )
    units = db.execute("SELECT id,name FROM chaika.measure_units WHERE source_id='primary'")
    lines = [
        line
        for r in changes
        for o in [r["current"], *r["previous"]["receipts"]]
        for line in o["lines"]
    ]
    suppliers = db.execute(
        "SELECT id,name FROM chaika.counteragents WHERE source_id='primary' AND id=ANY(%s::uuid[])",
        (list({r["supplier_id"] for r in lines if r["supplier_id"]}),),
    )
    products = {r["id"]: r for r in labels.fetchall()}
    store_map = {r["id"]: r for r in stores.fetchall()}
    parents = {r["id"]: r["parent_id"] for r in nodes.fetchall() + list(store_map.values())}
    unit_map = {r["id"]: r["name"] for r in units.fetchall()}
    supplier_map = {r["id"]: r["name"] for r in suppliers.fetchall()}
    departments = {r["id"]: r.get("name") for r in scope.departments}
    scope_label = (
        departments.get(scope.selection_ids[0], "Выбранное заведение")
        if len(scope.selection_ids) == 1
        else f"Выбрано заведений: {len(scope.selection_ids)}"
        if scope.selection_ids
        else "Вся сеть"
        if scope.unrestricted
        else "Доступные заведения"
    )

    def department_for(store_id):
        ancestor, seen = store_id, set()
        while ancestor and ancestor not in departments and ancestor not in seen:
            seen.add(ancestor)
            ancestor = parents.get(ancestor)
        return ancestor, departments.get(ancestor)

    for row in changes:
        row["product"] = products.get(row["product_id"], {}).get("name") or str(row["product_id"])
        row["code"] = products.get(row["product_id"], {}).get("code")
        row["store"] = (
            store_map.get(row["store_id"], {}).get("name") or str(row["store_id"])
            if row["store_id"] is not None
            else None
        )
        row["unit"] = unit_map.get(row["unit_id"]) or str(row["unit_id"])
        row["department_id"], row["department"] = department_for(row["store_id"])
        row["scope_label"] = scope_label
        for receipt in [row["current"], *row["previous"]["receipts"]]:
            for line in receipt["lines"]:
                line["store"] = store_map.get(line["store_id"], {}).get("name") or str(
                    line["store_id"] or "—"
                )
                line["department_id"], line["department"] = department_for(line["store_id"])
                line["supplier"] = supplier_map.get(line["supplier_id"]) or str(
                    line["supplier_id"] or "—"
                )
