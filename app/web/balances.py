"""Warehouse summaries and scoped, exact-product stock browsing."""

from fastapi import HTTPException

LATEST = """WITH latest AS (
    SELECT source_id,last_snapshot_id,accounting_timestamp,last_seen_at
    FROM chaika.store_balance_reports WHERE source_id='primary'
    ORDER BY accounting_timestamp DESC LIMIT 1
) """
BASE = """latest r JOIN chaika.store_balance_items i ON i.snapshot_id=r.last_snapshot_id
    LEFT JOIN chaika.products p ON p.source_id=r.source_id AND p.id=i.product_id
    LEFT JOIN chaika.stores s ON s.source_id=r.source_id AND s.id=i.store_id
    LEFT JOIN chaika.measure_units u ON u.source_id=p.source_id AND u.id=p.main_unit_id"""


def filters(scope, store_id=None, product_id=None, q=""):
    clauses, params = ["true"], []
    if store_id and not scope.unrestricted and store_id not in scope.store_ids:
        raise HTTPException(403, "Нет доступа к выбранному складу.")
    if not scope.unrestricted:
        clauses.append("i.store_id=ANY(%s::uuid[])")
        params.append(list(scope.store_ids))
    if store_id:
        clauses.append("i.store_id=%s")
        params.append(store_id)
    if product_id:
        clauses.append("i.product_id=%s")
        params.append(product_id)
    else:
        # Literal words, in any order; never search warehouse names as products.
        for word in q.lower().replace("ё", "е").split():
            clauses.append("replace(lower(concat_ws(' ',p.name,p.code,p.num)), 'ё','е') LIKE %s")
            params.append(
                "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
    return " AND ".join(clauses), params


def summary_queries(db, scope):
    where, params = filters(scope)
    # Totals describe the whole warehouse, independent of product search/pagination.
    stats = db.execute(
        LATEST + "SELECT i.store_id AS id,count(*) AS row_count,"
        "count(DISTINCT i.product_id) AS product_count,sum(i.sum) AS value "
        "FROM latest r JOIN chaika.store_balance_items i ON i.snapshot_id=r.last_snapshot_id "
        "WHERE " + where + " GROUP BY i.store_id",
        params,
    )
    store_clause = "" if scope.unrestricted else " AND id=ANY(%s::uuid[])"
    stores = db.execute(
        "SELECT id,parent_id,name FROM chaika.stores WHERE source_id='primary'" + store_clause,
        [] if scope.unrestricted else [list(scope.store_ids)],
    )
    nodes = db.execute(
        "SELECT id,parent_id,name FROM chaika.corporate_nodes WHERE source_id='primary'"
    )
    return stats, stores, nodes


def summarize(scope, stats, stores, nodes):
    parents = {n["id"]: n["parent_id"] for n in nodes + stores}
    departments = {n["id"]: n.get("name") for n in scope.departments}
    by_id = {s["id"]: s for s in stats}
    catalog = {s["id"]: s for s in stores}
    result = []
    for key in catalog.keys() | by_id.keys():
        store = catalog.get(key, {"id": key, "name": str(key)})
        ancestor, visited = key, set()
        while ancestor is not None and ancestor not in departments and ancestor not in visited:
            visited.add(ancestor)
            ancestor = parents.get(ancestor)
        result.append(
            {
                "id": key,
                "name": store["name"],
                "department": departments.get(ancestor),
                **by_id.get(key, {"row_count": 0, "product_count": 0, "value": None}),
            }
        )
    return sorted(result, key=lambda s: (s["department"] or "", s["name"] or ""))


def read_balances(
    db, scope, q="", offset=0, store_id=None, product_id=None, sort="sum", direction="desc"
):
    order_columns = {"amount": "i.amount", "sum": "i.sum"}
    order_directions = {"asc": "ASC", "desc": "DESC"}
    if sort not in order_columns or direction not in order_directions:
        raise HTTPException(422, "Выберите сортировку по количеству или стоимости.")
    where, params = filters(scope, store_id, product_id, q)
    # Caller pins all reads to one repeatable-read snapshot. Queue independent
    # queries before fetching so the website's pipeline shares a network round trip.
    totals = db.execute(
        LATEST + "SELECT count(*) AS total,sum(i.sum) AS value,"
        "CASE WHEN count(u.id)=count(*) AND count(DISTINCT u.id)=1 "
        "THEN sum(i.amount) END AS amount,"
        "CASE WHEN count(u.id)=count(*) AND count(DISTINCT u.id)=1 "
        "THEN min(u.name) END AS unit FROM " + BASE + " WHERE " + where,
        params,
    )
    rows = db.execute(
        LATEST
        + "SELECT i.product_id AS id,r.source_id,COALESCE(p.name,i.product_id::text) AS title,"
        "i.store_id,COALESCE(s.name,i.store_id::text) AS store,u.name AS unit,"
        "i.amount,i.sum,r.accounting_timestamp FROM "
        + BASE
        + " WHERE "
        + where
        + f" ORDER BY {order_columns[sort]} {order_directions[direction]},"
        "s.name NULLS LAST,p.name NULLS LAST,i.line_num LIMIT 50 OFFSET %s",
        [*params, offset],
    )
    stamp = db.execute(LATEST + "SELECT accounting_timestamp,last_seen_at FROM latest")
    stats, stores, nodes = summary_queries(db, scope)
    totals, rows, stamp = totals.fetchone(), rows.fetchall(), stamp.fetchone()
    warehouses = summarize(scope, stats.fetchall(), stores.fetchall(), nodes.fetchall())
    return {
        "rows": rows,
        "total": totals["total"],
        "filtered_value": totals["value"],
        "filtered_amount": totals["amount"],
        "filtered_unit": totals["unit"],
        "sort": sort,
        "direction": direction,
        "offset": offset,
        "limit": 50,
        "accounting_timestamp": stamp["accounting_timestamp"] if stamp else None,
        "last_synced_at": stamp["last_seen_at"] if stamp else None,
        "stores": warehouses,
        "columns": [
            {"key": k, "label": label}
            for k, label in [
                ("title", "Номенклатура"),
                ("store", "Склад"),
                ("unit", "Ед."),
                ("amount", "Количество"),
                ("sum", "Стоимость, ₽"),
            ]
        ],
    }


def product_suggestions(db, scope, q, store_id=None):
    where, params = filters(scope, store_id, q=q)
    if len(q.strip()) < 2:
        return {"rows": [], "has_more": False}
    rows = db.execute(
        LATEST
        + "SELECT DISTINCT p.id,p.name,p.code,p.num,u.name AS unit FROM "
        + BASE
        + " WHERE p.id IS NOT NULL AND "
        + where
        + " ORDER BY p.name,p.id LIMIT 21",
        params,
    ).fetchall()
    return {"rows": rows[:20], "has_more": len(rows) > 20}
