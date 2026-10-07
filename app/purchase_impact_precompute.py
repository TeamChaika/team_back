"""Prepare price-independent ingredient usage after synchronization, never in HTTP."""

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.web.coverage import ZONE
from app.web.purchase_impact import calculate_impact, load_graphs, recent_period

ALGORITHM_VERSION = 1
LOCK_ID = 7623011071800


def check_stop(stop):
    if stop is not None and stop.is_set():
        raise RuntimeError("purchase_impact_preparation_interrupted")


def ingredient_graph(reverse, target):
    """Keep the existing ancestor-edge selection, including cycles for validation."""
    pending, seen, graph = [str(target)], set(), []
    while pending:
        ingredient = pending.pop()
        if ingredient in seen:
            continue
        seen.add(ingredient)
        edges = reverse.get(ingredient, [])
        graph.extend(edges)
        pending.extend(str(edge["product_id"]) for edge in edges)
    return graph


def json_value(value):
    """Retain Decimal values as strings, including every significant digit."""
    return json.loads(json.dumps(value, default=str))


def source_revision(db, *, today=None):
    """Small publication metadata only; no invoice, recipe or sales-row scans."""
    today = today or datetime.now(ZONE).date()
    start, end = recent_period(today=today)
    runs = db.execute(
        "SELECT job,count(*) AS count,max(finished_at) AS finished_at "
        "FROM chaika.sync_runs WHERE status='succeeded' "
        "AND job IN ('inventory','references','dictionaries') GROUP BY job ORDER BY job"
    ).fetchall()
    days = db.execute(
        "SELECT business_date,current_set_id FROM chaika.sales_report_days "
        "WHERE source_id='primary' AND business_date BETWEEN %s AND %s ORDER BY business_date",
        (start, end),
    ).fetchall()
    return json_value(dict(version=ALGORITHM_VERSION, today=today, runs=runs, sales=days))


def prepare_product_facts(
    targets, graph, sales, products, coverage, start, end, recipe_day, *, stop=None
):
    """Reuse the existing norm/exclusion rules, preserving per-dish Decimal amounts."""
    reverse, by_dish = defaultdict(list), defaultdict(list)
    for edge in graph:
        reverse[str(edge["ingredient_id"])].append(edge)
    for sale in sales:
        by_dish[str(sale["dish_id"])].append(sale)
    result = []
    for target in targets:
        check_stop(stop)
        product = products.get(target)
        relevant = ingredient_graph(reverse, target)
        departments = {}
        if product is not None:
            dish_ids = {str(edge["product_id"]) for edge in relevant}
            sold = [sale for dish in dish_ids for sale in by_dish[dish]]
            # Price blockers do not affect route inclusion or monthly usage. Real
            # price/unit/coverage blockers are applied to the caller's current price.
            technical_price = dict(
                department_id=None, unit_id=product["main_unit_id"], delta=Decimal(1)
            )
            calculated = calculate_impact(
                relevant,
                sold,
                technical_price,
                product,
                coverage,
                start,
                end,
                recipe_day,
                departments=sorted({sale["department_id"] for sale in sold}, key=str),
            )
            for sale in sold:
                departments.setdefault(str(sale["department_id"]), dict(monthly=[], excluded=0))
            for row in calculated["rows"]:
                departments[str(row["department_id"])]["monthly"].append(str(row["monthly_amount"]))
            for row in calculated["excluded"]:
                departments[str(row["department_id"])]["excluded"] += 1
        result.append(
            dict(
                product_id=target,
                product_exists=product is not None,
                main_unit_id=product["main_unit_id"] if product is not None else None,
                has_graph=bool(relevant),
                departments=departments,
            )
        )
    return result


def build_prepared(db, *, today=None, stop=None):
    """Read a coherent source snapshot; the caller supplies REPEATABLE READ."""
    today = today or datetime.now(ZONE).date()
    check_stop(stop)
    start, end = recent_period(today=today)
    revision = source_revision(db, today=today)
    targets = [
        r["product_id"]
        for r in db.execute(
            "SELECT DISTINCT product_id FROM chaika.incoming_invoice_items "
            "WHERE source_id='primary' AND present_in_latest AND product_id IS NOT NULL"
        ).fetchall()
    ]
    recipe_day = db.execute(
        "SELECT max(business_date) AS day FROM chaika.assembly_chart_scopes "
        "WHERE source_id='primary' AND business_date<=%s",
        (today,),
    ).fetchone()["day"]
    products = {
        row["id"]: row
        for row in db.execute(
            "SELECT id,main_unit_id FROM chaika.products "
            "WHERE source_id='primary' AND id=ANY(%s::uuid[])",
            (targets,),
        ).fetchall()
    }
    coverage = db.execute(
        "SELECT d.business_date,r.observed_at,s.checks FROM chaika.sales_report_days d "
        "JOIN chaika.sales_report_sets s ON s.id=d.current_set_id "
        "JOIN chaika.sales_reports r ON r.set_id=s.id AND r.kind='dishes' "
        "WHERE d.source_id='primary' AND d.business_date BETWEEN %s AND %s",
        (start, end),
    ).fetchall()
    sales = db.execute(
        "SELECT x.department_id,x.dimensions->>'DishId' AS dish_id,"
        "max(x.dimensions->>'DishName') AS dish,sum(x.quantity) AS quantity,"
        "bool_or(x.quantity IS NULL OR x.quantity<0) AS invalid_quantity "
        "FROM chaika.sales_report_days d "
        "JOIN chaika.sales_reports r ON r.set_id=d.current_set_id "
        "JOIN chaika.sales_report_rows x ON x.report_id=r.id "
        "WHERE d.source_id='primary' AND d.business_date BETWEEN %s AND %s "
        "AND r.kind='dishes' AND x.department_id IS NOT NULL GROUP BY 1,2",
        (start, end),
    ).fetchall()
    graph = (
        load_graphs(db, targets, recipe_day, limit=200000, compact=True)
        if recipe_day and targets
        else []
    )
    facts = prepare_product_facts(
        targets, graph, sales, products, coverage, start, end, recipe_day, stop=stop
    )
    return dict(
        id=uuid4(),
        source_id="primary",
        prepared_at=datetime.now(UTC),
        period_start=start,
        period_end=end,
        recipe_day=recipe_day,
        revision=revision,
        coverage=json_value(coverage),
        selling_department_ids=sorted({str(row["department_id"]) for row in sales}),
        products=facts,
    )


def publish_prepared(db, prepared):
    """Replace metadata and every product atomically, retaining the old set on error."""
    with db.transaction():
        db.execute(
            "DELETE FROM chaika.purchase_impact_prepared WHERE source_id=%s",
            (prepared["source_id"],),
        )
        db.execute(
            "INSERT INTO chaika.purchase_impact_prepared "
            "(id,source_id,prepared_at,period_start,period_end,recipe_day,revision,coverage,"
            "selling_department_ids) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                prepared["id"],
                prepared["source_id"],
                prepared["prepared_at"],
                prepared["period_start"],
                prepared["period_end"],
                prepared["recipe_day"],
                Jsonb(prepared["revision"]),
                Jsonb(prepared["coverage"]),
                Jsonb(prepared["selling_department_ids"]),
            ),
        )
        with db.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO chaika.purchase_impact_prepared_products "
                "(generation_id,product_id,product_exists,main_unit_id,has_graph,departments) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                [
                    (
                        prepared["id"],
                        p["product_id"],
                        p["product_exists"],
                        p["main_unit_id"],
                        p["has_graph"],
                        Jsonb(p["departments"]),
                    )
                    for p in prepared["products"]
                ],
            )


def ensure_prepared(settings, stop=None):
    """Background entry point; unchanged sources skip all heavy calculations."""
    check_stop(stop)
    with psycopg.connect(
        settings.database_url.get_secret_value(),
        autocommit=True,
        connect_timeout=10,
        row_factory=dict_row,
        prepare_threshold=None,
    ) as db:
        # Transaction locks also work with the deployment's transaction pooler.
        # The read/build/publish transaction owns the lock until publication commits.
        with db.transaction():
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            locked = db.execute(
                "SELECT pg_try_advisory_xact_lock(%s) AS locked", (LOCK_ID,)
            ).fetchone()["locked"]
            if not locked:
                return dict(status="busy")
            revision = source_revision(db)
            current = db.execute(
                "SELECT id,revision,prepared_at FROM chaika.purchase_impact_prepared "
                "WHERE source_id='primary'"
            ).fetchone()
            if current and current["revision"] == revision:
                return dict(
                    status="unchanged",
                    generation_id=str(current["id"]),
                    prepared_at=current["prepared_at"],
                )
            prepared = build_prepared(db, stop=stop)
            check_stop(stop)
            publish_prepared(db, prepared)
            return dict(
                status="ready",
                generation_id=str(prepared["id"]),
                prepared_at=prepared["prepared_at"],
            )


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps(ensure_prepared(Settings()), default=str))


if __name__ == "__main__":
    main()
