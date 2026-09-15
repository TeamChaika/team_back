"""Read-only overview of published OLAP days; missing days never become zero sales."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, localcontext

from app.web.coverage import partial_days

FIELDS = ("revenue", "cost", "checks", "guests")


def dates(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def totals(rows, days, *, complete=True):
    with localcontext() as ctx:
        ctx.prec = 70
        result = {
            key: sum((r[key] for r in rows), Decimal(0))
            if complete and rows and all(r[key] is not None for r in rows)
            else None
            for key in FIELDS
        }
        revenue, cost, checks, guests = (result[k] for k in FIELDS)
        result["gross_profit"] = (
            revenue - cost if revenue is not None and cost is not None else None
        )
        result["average_check"] = revenue / checks if revenue is not None and checks else None
        result["cost_share"] = (
            cost / revenue * 100 if cost is not None and revenue and revenue > 0 else None
        )
        result["margin"] = 100 - result["cost_share"] if result["cost_share"] is not None else None
        result["guests_per_day"] = guests / days if guests is not None else None
        return result


def change(current, previous):
    with localcontext() as ctx:
        ctx.prec = 70
        if current is None or previous is None:
            return {"absolute": None, "percent": None}
        difference = current - previous
        return {
            "absolute": difference,
            # A zero or negative baseline has no useful relative growth percentage.
            "percent": difference / previous * 100 if previous > 0 else None,
        }


def changes(current, previous):
    return {key: change(value, previous[key]) for key, value in current.items()}


def period(coverage, rows, scope, start, end, kind="daily"):
    expected = dates(start, end)
    reports = [r for r in coverage if r["kind"] == kind and start <= r["business_date"] <= end]
    loaded = {r["business_date"] for r in reports}
    missing = sorted(set(expected) - loaded)
    names = {str(d["id"]): d.get("name") for d in scope.departments}
    visible = {str(i) for i in scope.ids}
    issues = [
        {
            "date": report["business_date"],
            "report": check["report"],
            "field": check["field"],
            "department": names.get(difference["department_id"]),
            **difference,
        }
        for report in reports
        for check in report["checks"]
        if not check["exact_match"]
        for difference in check.get("differences", [])
        if difference["department_id"] in visible
    ]
    return {
        "start": start,
        "end": end,
        "days": len(expected),
        "loaded_dates": sorted(loaded),
        "missing_dates": missing,
        "partial_days": partial_days(reports),
        "complete": not missing,
        "reviewed": bool(reports) and all(r["reviewed"] for r in reports),
        "reconciliation_issues": issues,
        "totals": totals(rows, len(expected), complete=not missing),
    }


def buckets(start, end, grain):
    groups = []
    for day in dates(start, end):
        key = day
        if grain == "week":
            key = day - timedelta(days=day.weekday())
        elif grain == "month":
            key = day.replace(day=1)
        if not groups or groups[-1][0] != key:
            groups.append((key, []))
        groups[-1][1].append(day)
    return [days for _, days in groups]


def build_overview(scope, start, end, grain, coverage, daily, dishes):
    days = (end - start).days + 1
    previous_start, previous_end = start - timedelta(days=days), start - timedelta(days=1)
    current_rows = [r for r in daily if r["business_date"] >= start]
    previous_rows = [r for r in daily if r["business_date"] < start]
    current = period(coverage, current_rows, scope, start, end)
    previous = period(coverage, previous_rows, scope, previous_start, previous_end)
    trends = {}
    for option in ("day", "week", "month"):
        trend = []
        for group in buckets(start, end, option):
            a, b = group[0], group[-1]
            pa, pb = a - timedelta(days=days), b - timedelta(days=days)
            now = period(coverage, [r for r in daily if a <= r["business_date"] <= b], scope, a, b)
            before = period(
                coverage, [r for r in daily if pa <= r["business_date"] <= pb], scope, pa, pb
            )
            trend.append({"current": now, "previous": before})
        trends[option] = trend

    by_department = defaultdict(lambda: {"current": [], "previous": []})
    for row in daily:
        by_department[row["department_id"]][
            "current" if row["business_date"] >= start else "previous"
        ].append(row)
    names = {d["id"]: d.get("name") for d in scope.departments}
    restaurants = []
    for department_id, rows in by_department.items():
        now = totals(rows["current"], days, complete=current["complete"])
        before = totals(rows["previous"], days, complete=previous["complete"])
        restaurants.append(
            {
                "department_id": department_id,
                "department": names.get(department_id) or str(department_id),
                "totals": now,
                "previous_totals": before,
                "changes": changes(now, before),
            }
        )
    restaurants.sort(
        key=lambda r: (
            r["totals"]["revenue"] is None,
            (r["totals"]["revenue"] or Decimal(0)).copy_negate(),
            r["department"],
        )
    )

    dish_current = period(coverage, [], scope, start, end, "dishes")
    dish_previous = period(coverage, [], scope, previous_start, previous_end, "dishes")
    dish_before = {(r["dish_id"], r["fallback_name"]): r for r in dishes if not r["is_current"]}
    ranked = sorted(
        (r for r in dishes if r["is_current"]),
        key=lambda r: (r["revenue"].copy_negate(), r["dish_name"] or "", r["dish_id"] or ""),
    )
    top_dishes = []
    if dish_current["complete"]:
        for row in ranked[:5]:
            before = dish_before.get((row["dish_id"], row["fallback_name"]))
            prior_revenue = before["revenue"] if before and dish_previous["complete"] else None
            top_dishes.append(
                {
                    "dish_id": row["dish_id"],
                    "dish_name": row["dish_name"],
                    "revenue": row["revenue"],
                    "quantity": row["quantity"],
                    "previous_revenue": prior_revenue,
                    "change": change(row["revenue"], prior_revenue),
                }
            )
    return {
        "current": current,
        "previous": previous,
        "changes": changes(current["totals"], previous["totals"]),
        "granularity": grain,
        "trend": trends[grain],
        "trends": trends,
        "restaurants": restaurants,
        "top_dishes": top_dishes,
        "dish_current": dish_current,
        "dish_previous": dish_previous,
    }


def read_overview(db, scope, start: date, end: date, grain: str, *, live_bundle=None):
    previous_start = start - timedelta(days=(end - start).days + 1)
    base = (
        " FROM chaika.sales_report_days d "
        "JOIN chaika.sales_report_sets s ON s.id=d.current_set_id "
        "JOIN chaika.sales_reports r ON r.set_id=s.id "
    )
    bounds = "d.source_id='primary' AND d.business_date BETWEEN %s AND %s"
    live_day = date.fromisoformat(live_bundle["manifest"]["business_date"]) if live_bundle else None
    bounds += " AND (%s::date IS NULL OR d.business_date<>%s)"
    coverage = db.execute(
        "SELECT d.business_date,r.kind,s.reviewed,s.checks,r.observed_at"
        + base
        + "WHERE "
        + bounds
        + " AND r.kind IN ('daily','dishes') ORDER BY d.business_date",
        (previous_start, end, live_day, live_day),
    )
    daily = db.execute(
        "SELECT d.business_date,x.department_id,x.revenue,x.cost,x.checks,x.guests"
        + base
        + "JOIN chaika.sales_report_rows x ON x.report_id=r.id WHERE "
        + bounds
        + " AND r.kind='daily' AND x.department_id=ANY(%s::uuid[]) ORDER BY d.business_date",
        (previous_start, end, live_day, live_day, scope.ids),
    )
    # Group in PostgreSQL: a monthly dish report can exceed the ordinary row endpoint limit.
    # Dish UUID is stable across renames; a missing UUID is grouped only by its exact source name.
    top_filter = (
        " SELECT * FROM grouped"
        if live_bundle is not None
        else ", top AS (SELECT dish_id,fallback_name FROM grouped WHERE is_current "
        "ORDER BY revenue DESC,dish_name,dish_id LIMIT 5) "
        "SELECT g.* FROM grouped g JOIN top t ON g.dish_id IS NOT DISTINCT FROM t.dish_id "
        "AND g.fallback_name IS NOT DISTINCT FROM t.fallback_name"
    )
    dishes = db.execute(
        "WITH scoped AS (SELECT d.business_date >= %s AS is_current,d.business_date,x.ordinal,"
        "NULLIF(x.dimensions->>'DishId','') AS dish_id,x.dimensions->>'DishName' AS dish_name,"
        "x.revenue,x.quantity"
        + base
        + "JOIN chaika.sales_report_rows x ON x.report_id=r.id WHERE "
        + bounds
        + " AND r.kind='dishes' AND x.department_id=ANY(%s::uuid[])), grouped AS ("
        "SELECT is_current,dish_id,"
        "CASE WHEN dish_id IS NULL THEN COALESCE(dish_name,'') END AS fallback_name,"
        "(array_agg(dish_name ORDER BY business_date DESC,ordinal DESC))[1] AS dish_name,"
        "SUM(revenue) AS revenue,"
        "CASE WHEN COUNT(quantity)=COUNT(*) THEN SUM(quantity) END AS quantity "
        "FROM scoped GROUP BY is_current,dish_id,"
        "CASE WHEN dish_id IS NULL THEN COALESCE(dish_name,'') END) " + top_filter,
        (start, previous_start, end, live_day, live_day, scope.ids),
    )
    coverage, daily, dishes = coverage.fetchall(), daily.fetchall(), dishes.fetchall()
    if live_bundle is not None:
        from app.web.live_sales import report_coverage, report_rows

        coverage += report_coverage(live_bundle, ("daily", "dishes"))
        daily += report_rows(live_bundle, "daily", scope)
        grouped = {(r["is_current"], r["dish_id"], r["fallback_name"]): r for r in dishes}
        with localcontext() as ctx:
            ctx.prec = 70
            for row in report_rows(live_bundle, "dishes", scope):
                dish_id = row["dimensions"].get("DishId") or None
                name = row["dimensions"].get("DishName")
                fallback = (name or "") if dish_id is None else None
                key = (True, dish_id, fallback)
                if key not in grouped:
                    grouped[key] = {
                        "is_current": True,
                        "dish_id": dish_id,
                        "dish_name": name,
                        "fallback_name": fallback,
                        "revenue": row["revenue"],
                        "quantity": row["quantity"],
                    }
                else:
                    target = grouped[key]
                    target["dish_name"] = name
                    target["revenue"] += row["revenue"]
                    target["quantity"] = (
                        target["quantity"] + row["quantity"]
                        if target["quantity"] is not None and row["quantity"] is not None
                        else None
                    )
        dishes = list(grouped.values())
    return build_overview(scope, start, end, grain, coverage, daily, dishes)
