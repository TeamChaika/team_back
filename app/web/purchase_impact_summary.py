"""Weekly price impacts for the list, sharing one recipe and sales snapshot."""

from collections import defaultdict
from datetime import datetime

from app.web.coverage import ZONE
from app.web.purchase_impact import calculate_impact, load_graphs, recent_period, sales_coverage


def ingredient_graph(reverse, target):
    """Select the same ancestor edges as the single-product SQL, retaining cycles."""
    pending, seen, graph = [str(target)], set(), []
    while pending:
        ingredient = pending.pop()
        if ingredient in seen:
            continue
        seen.add(ingredient)
        edges = reverse.get(ingredient, [])
        graph.extend(edges)
        pending.extend(str(e["product_id"]) for e in edges)
    return graph


def summarize_prices(prices, graph, sales, products, coverage, start, end, recipe_day, departments):
    reverse, by_dish = defaultdict(list), defaultdict(list)
    for edge in graph:
        reverse[str(edge["ingredient_id"])].append(edge)
    for sale in sales:
        by_dish[str(sale["dish_id"])].append(sale)
    ingredients = {}
    summaries = []
    for price in prices:
        product = products.get(price["product_id"])
        if product is None:
            summaries.append(
                dict(
                    weekly_delta=None,
                    included_positions=0,
                    excluded_positions=0,
                    reason="Товар отсутствует в номенклатуре.",
                )
            )
            continue
        target = price["product_id"]
        if target not in ingredients:
            relevant = ingredient_graph(reverse, target)
            dish_ids = {str(e["product_id"]) for e in relevant}
            ingredients[target] = relevant, [s for dish in dish_ids for s in by_dish[dish]]
        relevant, sold = ingredients[target]
        result = calculate_impact(
            relevant,
            sold,
            price,
            product,
            coverage,
            start,
            end,
            recipe_day,
            departments=departments,
        )
        summaries.append(
            dict(
                weekly_delta=result["totals"]["weekly_delta"],
                included_positions=len(result["rows"]),
                excluded_positions=len(result["excluded"]),
                reason="; ".join(result["blockers"]) or result["empty_reason"],
            )
        )
    return summaries


def add_weekly_impacts(db, scope, report):
    """Enrich authorized price rows without per-product database or iiko requests."""
    start, end = recent_period()
    prices = report["rows"]
    if not prices:
        report["impact_period"] = dict(start=start, end=end, recipe_day=None)
        return report
    targets = {p["product_id"] for p in prices}
    recipe_day = db.execute(
        "SELECT max(business_date) AS day FROM chaika.assembly_chart_scopes "
        "WHERE source_id='primary' AND business_date<=%s",
        (datetime.now(ZONE).date(),),
    ).fetchone()["day"]
    # Independent reads are queued in the repository's pipeline before graph loading.
    product_query = db.execute(
        "SELECT id,main_unit_id FROM chaika.products "
        "WHERE source_id='primary' AND id=ANY(%s::uuid[])",
        (list(targets),),
    )
    coverage_query = db.execute(
        "SELECT d.business_date,r.observed_at,s.checks FROM chaika.sales_report_days d "
        "JOIN chaika.sales_report_sets s ON s.id=d.current_set_id "
        "JOIN chaika.sales_reports r ON r.set_id=s.id AND r.kind='dishes' "
        "WHERE d.source_id='primary' AND d.business_date BETWEEN %s AND %s",
        (start, end),
    )
    sales_query = db.execute(
        """SELECT x.department_id,x.dimensions->>'DishId' AS dish_id,
            max(x.dimensions->>'DishName') AS dish,sum(x.quantity) AS quantity,
            bool_or(x.quantity IS NULL OR x.quantity<0) AS invalid_quantity
        FROM chaika.sales_report_days d JOIN chaika.sales_reports r ON r.set_id=d.current_set_id
        JOIN chaika.sales_report_rows x ON x.report_id=r.id
        WHERE d.source_id='primary' AND d.business_date BETWEEN %s AND %s
            AND r.kind='dishes' AND x.department_id=ANY(%s::uuid[]) GROUP BY 1,2""",
        (start, end, scope.ids),
    )
    graph = load_graphs(db, targets, recipe_day, limit=200000, compact=True) if recipe_day else []
    products = {p["id"]: p for p in product_query.fetchall()}
    coverage, sales = coverage_query.fetchall(), sales_query.fetchall()
    selling_ids = {s["department_id"] for s in sales}
    departments = list(scope.selection_ids) if scope.selection_ids else sorted(selling_ids, key=str)
    summaries = summarize_prices(
        prices,
        graph,
        sales,
        products,
        coverage,
        start,
        end,
        recipe_day,
        departments,
    )
    for price, summary in zip(prices, summaries, strict=True):
        price["impact"] = summary
    report["impact_period"] = dict(
        start=start,
        end=end,
        recipe_day=recipe_day,
        coverage=sales_coverage(coverage, start, end, departments),
    )
    return report
