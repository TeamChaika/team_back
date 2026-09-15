"""Ingredient price scenarios from saved recipes and sales, never actual writeoffs."""

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext

from fastapi import HTTPException

from app.web.coverage import ZONE, partial_days
from app.web.purchase_prices import read_purchase_prices

ZERO = Decimal(0)


def recent_period(*, today=None):
    """Thirty completed local days, independent of calendar months and page filters."""
    today = today or datetime.now(ZONE).date()
    return today - timedelta(days=30), today - timedelta(days=1)


def applies(spec, department):
    if spec is None:
        return True
    included = str(department) in {str(d) for d in spec["departments"]}
    return not included if spec["inverse"] else included


def recipe_paths(graph, target, dish, department, *, parents=None):
    """Only follow paths containing the selected ingredient; fail closed on ambiguity."""
    if parents is None:
        parents = defaultdict(list)
        for edge in graph:
            parents[str(edge["product_id"])].append(edge)
    visits = 0

    def walk(product, ancestors=()):
        nonlocal visits
        visits += 1
        if visits > 4000 or len(ancestors) >= 24:
            return [], ["Слишком сложная цепочка техкарт"]
        if product == str(target):
            return [(Decimal(1), [])], []
        if product in ancestors:
            return [], ["Циклическая связь техкарт"]
        edges = parents.get(product, [])
        if len({e["chart_id"] for e in edges}) != 1 or any(
            e.get("chart_count", 1) != 1 for e in edges
        ):
            return [], ["Не найдена единственная действующая техкарта"]
        chart = edges[0]["chart_details"]
        direct = chart.get("effective_direct_writeoff_store_specification")
        if direct is None:
            return [], ["Не определено правило списания готовой позиции"]
        if applies(direct, department):
            return [], ["Списывается готовая позиция; расход ингредиента здесь не определён"]
        if chart.get("product_size_assembly_strategy") != "COMMON":
            return [], ["Для расчёта нужен размер блюда"]
        found, issues = [], []
        for edge in edges:
            item = edge["item_details"]
            if not applies(item.get("store_specification"), department):
                continue
            if item.get("product_size_id") is not None:
                issues.append("Для ингредиента задан отдельный размер")
                continue
            output, amount = Decimal(edge["assembled_amount"]), Decimal(edge["amount_in"])
            if not output.is_finite() or not amount.is_finite() or output <= 0 or amount < 0:
                issues.append("Некорректный выход или закладка в техкарте")
                continue
            # iiko's prepared-chart reference formula: 4 decimals per link, 3 per path.
            factor = (amount / output).quantize(Decimal(".0001"), rounding=ROUND_HALF_UP)
            children, child_issues = walk(str(edge["ingredient_id"]), (*ancestors, product))
            issues.extend(child_issues)
            for child_amount, path in children:
                found.append((factor * child_amount, [edge, *path]))
        return found, issues

    with localcontext() as ctx:
        ctx.prec = 70
        return walk(str(dish))


def load_graph(db, target, day):
    return load_graphs(db, [target], day)


def load_graphs(db, targets, day, *, limit=20000, compact=False):
    # UNION terminates even when source recipes contain a cycle. The Python traversal
    # then reports that cycle rather than inventing a consumption norm.
    chart_details = (
        "jsonb_build_object('effective_direct_writeoff_store_specification',"
        "c.details->'effective_direct_writeoff_store_specification',"
        "'product_size_assembly_strategy',c.details->'product_size_assembly_strategy')"
        if compact
        else "c.details"
    )
    item_details = (
        "jsonb_build_object('store_specification',i.details->'store_specification',"
        "'product_size_id',i.details->'product_size_id')"
        if compact
        else "i.details"
    )
    projection = (
        "a.product_id,a.chart_id,a.assembled_amount,a.chart_details,cc.chart_count,"
        "u.name AS unit,jsonb_agg(jsonb_build_array(a.ingredient_id,a.amount_in::text,"
        "a.item_details) ORDER BY a.item_id) AS ingredients"
        if compact
        else "a.*,cc.chart_count,p.name,p.main_unit_id,u.name AS unit,child.name AS ingredient,"
        "cu.name AS ingredient_unit"
    )
    child_joins = (
        ""
        if compact
        else (
            "LEFT JOIN chaika.products child "
            "ON child.source_id='primary' AND child.id=a.ingredient_id "
            "LEFT JOIN chaika.measure_units cu "
            "ON cu.source_id='primary' AND cu.id=child.main_unit_id"
        )
    )
    grouping = (
        "GROUP BY a.product_id,a.chart_id,a.assembled_amount,a.chart_details,cc.chart_count,u.name "
        "ORDER BY a.product_id,a.chart_id"
        if compact
        else "ORDER BY a.product_id,a.chart_id,a.item_id"
    )
    rows = db.execute(
        f"""WITH RECURSIVE charts AS MATERIALIZED (
            SELECT c.* FROM chaika.assembly_chart_scopes s JOIN chaika.assembly_charts c
                ON (c.source_id,c.id)=(s.source_id,s.chart_id)
            WHERE s.source_id='primary' AND s.business_date=%s AND s.present_in_latest
                AND c.date_from<=%s AND (c.date_to IS NULL OR c.date_to>%s)
        ), active AS MATERIALIZED (
            SELECT c.id AS chart_id,c.product_id,c.date_from,c.date_to,c.assembled_amount,
                {chart_details} AS chart_details,i.id AS item_id,i.product_id AS ingredient_id,
                i.amount_in,{item_details} AS item_details
            FROM charts c JOIN chaika.assembly_chart_items i
                ON (i.source_id,i.chart_id)=(c.source_id,c.id)
            WHERE i.present_in_latest
        ), chart_counts AS (
            SELECT product_id,count(*) AS chart_count FROM charts GROUP BY 1
        ), ancestors(id) AS (
            SELECT unnest(%s::uuid[]) UNION SELECT a.product_id FROM active a
                JOIN ancestors p ON p.id=a.ingredient_id
        ) SELECT {projection}
            FROM active a JOIN ancestors f ON f.id=a.ingredient_id
            JOIN chart_counts cc ON cc.product_id=a.product_id
            LEFT JOIN chaika.products p ON p.source_id='primary' AND p.id=a.product_id
            LEFT JOIN chaika.measure_units u ON u.source_id='primary' AND u.id=p.main_unit_id
            {child_joins} {grouping} LIMIT %s""",
        (day, day, day, list(targets), limit + 1),
    ).fetchall()
    if compact:
        # Transmit each chart's repeated rules once. Numeric ingredient amounts stay
        # decimal strings in JSON, avoiding a float conversion before calculation.
        rows = [
            dict(
                **{k: v for k, v in chart.items() if k != "ingredients"},
                ingredient_id=ingredient,
                amount_in=Decimal(amount),
                item_details=details,
                name=None,
                ingredient=None,
                ingredient_unit=None,
            )
            for chart in rows
            for ingredient, amount, details in chart["ingredients"]
        ]
    if len(rows) > limit:
        raise HTTPException(413, "У товара слишком много вхождений для этого расчёта.")
    return rows


def sales_coverage(coverage, start, end, departments):
    visible = {str(d) for d in departments}
    days = (end - start).days + 1
    loaded = {r["business_date"] for r in coverage}
    missing = [
        start + timedelta(days=i) for i in range(days) if start + timedelta(days=i) not in loaded
    ]
    mismatches = []
    for row in coverage:
        for check in row["checks"]:
            if check["report"] != "dishes" or check["exact_match"]:
                continue
            differences = check.get("differences", [])
            if not differences or any(str(d["department_id"]) in visible for d in differences):
                mismatches.append(row["business_date"])
    partial = partial_days(coverage)
    return dict(
        loaded_days=len(loaded),
        days=days,
        missing_dates=missing,
        partial_dates=[r["date"] for r in partial],
        mismatch_dates=sorted(set(mismatches)),
        complete=not (missing or partial or mismatches),
    )


def calculate_impact(
    graph, sales, price, product, coverage, start, end, recipe_day, *, departments=None
):
    departments = departments if departments is not None else [price["department_id"]]
    quality = sales_coverage(coverage, start, end, departments)
    rows, excluded, blockers = [], [], []
    if product["main_unit_id"] != price["unit_id"]:
        blockers.append("Единица в накладной отличается от основной единицы товара")
    if price["delta"] is None:
        blockers.append("Нет пригодной предыдущей цены для сравнения")
    if recipe_day is None:
        blockers.append("Нет загруженного снимка техкарт")
    # A unit price and recipe determine one portion independently of sales coverage.
    can_price_portion = not blockers
    if not quality["complete"]:
        blockers.append("Продажи периода неполные или не прошли сверку; недельный прогноз отключён")
    parents = defaultdict(list)
    for edge in graph:
        parents[str(edge["product_id"])].append(edge)
    with localcontext() as ctx:
        ctx.prec = 70
        for sale in sales:
            dish = str(sale["dish_id"])
            department = sale.get("department_id", price["department_id"])
            routes, issues = recipe_paths(graph, product["id"], dish, department, parents=parents)
            source = parents.get(dish, [])
            # The current OLAP slice has no portion-size/modifier breakdown. Do not
            # silently equate weighed dishes to a standard portion.
            if source and source[0]["unit"] not in {"порц", "шт"}:
                issues.append("Единица продаваемого блюда требует отдельного пересчёта")
            if sale["quantity"] is None or sale["invalid_quantity"]:
                issues.append("В продажах отсутствует количество или есть возвратные строки")
            base = dict(
                dish_id=dish,
                department_id=department,
                department=sale.get("department", price.get("department")),
                dish=sale["dish"],
                quantity=sale["quantity"],
                chart_ids=sorted({str(e["chart_id"]) for e in source}),
                sales_sources=sale.get("sales_sources", []),
            )
            if issues or not routes:
                excluded.append(
                    {**base, "reasons": sorted(set(issues or ["Нет применимой нормы"]))}
                )
                continue
            norm = sum(
                (a.quantize(Decimal(".001"), rounding=ROUND_HALF_UP) for a, _ in routes), ZERO
            )
            monthly = norm * sale["quantity"]
            weekly = monthly * 7 / quality["days"] if quality["complete"] else None
            portion_delta = norm * price["delta"] if can_price_portion else None
            rows.append(
                dict(
                    **base,
                    amount_per_portion=norm,
                    monthly_amount=monthly,
                    weekly_amount=weekly,
                    portion_delta=portion_delta,
                    weekly_delta=weekly * price["delta"]
                    if weekly is not None and not blockers
                    else None,
                    paths=[
                        [
                            dict(
                                chart_id=e["chart_id"],
                                product_id=e["product_id"],
                                product=e["name"],
                                unit=e["unit"],
                                output=e["assembled_amount"],
                                ingredient_id=e["ingredient_id"],
                                ingredient=e["ingredient"],
                                ingredient_unit=e.get("ingredient_unit"),
                                amount=e["amount_in"],
                            )
                            for e in path
                        ]
                        for _, path in routes
                    ],
                )
            )
        rows.sort(key=lambda r: r["monthly_amount"], reverse=True)
        monthly = sum((r["monthly_amount"] for r in rows), ZERO)
        quantity = sum((r["quantity"] for r in rows), ZERO)
        totals = dict(
            quantity=quantity,
            average_portion_delta=sum((r["portion_delta"] * r["quantity"] for r in rows), ZERO)
            / quantity
            if can_price_portion and quantity > 0
            else None,
            monthly_amount=monthly,
            weekly_amount=monthly * 7 / quality["days"] if quality["complete"] and rows else None,
            weekly_delta=sum((r["weekly_delta"] for r in rows), ZERO)
            if not blockers and rows
            else None,
        )
    return dict(
        price=price,
        start=start,
        end=end,
        recipe_day=recipe_day,
        coverage=quality,
        rows=rows,
        excluded=excluded,
        blockers=blockers,
        totals=totals,
        standard_portion_factor=1,
        norm_unit=product.get("unit"),
        sold_dishes=len(sales),
        empty_reason=(
            "Товар не найден в цепочках загруженных техкарт."
            if not graph
            else "В выбранных заведениях за последние 30 дней не найдено продаж связанных блюд."
            if not sales
            else (
                "Связанные блюда найдены в продажах, но все исключены из расчёта. "
                "Причины указаны ниже."
            )
            if not rows
            else None
        ),
    )


def analysis_scope(scope, price_department, selling_ids, selected=None, all_departments=False):
    """Purchase ownership is not a sales location; never extend the user's grants."""
    allowed = {d["id"] for d in scope.departments}
    if selected is not None and selected not in allowed:
        raise HTTPException(403, "Нет доступа к выбранному заведению продаж.")
    if selected is not None and all_departments:
        raise HTTPException(422, "Выберите одно заведение или все заведения.")
    selling = allowed.intersection(selling_ids)
    if selected is not None:
        return [selected]
    if not all_departments and price_department in selling:
        return [price_department]
    return sorted(selling, key=str)


def read_purchase_impact(
    db, scope, spec, selection, analysis_department_id=None, all_departments=False
):
    start, end = recent_period()
    product_id, store_id, _, _ = selection
    if store_id is not None and store_id not in scope.store_ids:
        raise HTTPException(404, "Товар или склад недоступен.")
    # Validate the independently selected sales venue before doing expensive reads.
    analysis_scope(scope, None, [], analysis_department_id, all_departments)
    history = read_purchase_prices(db, scope, spec, "all", False, selection, recent_only=False)
    if not history["rows"]:
        raise HTTPException(404, "История цены не найдена или недоступна.")
    price = history["rows"][0]
    department = price["department_id"]
    if store_id is not None and department not in scope.ids:
        raise HTTPException(422, "Склад не сопоставлен с доступным заведением.")
    product = db.execute(
        "SELECT p.id,p.main_unit_id,u.name AS unit FROM chaika.products p "
        "LEFT JOIN chaika.measure_units u ON u.source_id=p.source_id AND u.id=p.main_unit_id "
        "WHERE p.source_id='primary' AND p.id=%s",
        (product_id,),
    ).fetchone()
    if not product:
        raise HTTPException(422, "Товар отсутствует в номенклатуре.")
    recipe_day = db.execute(
        "SELECT max(business_date) AS day FROM chaika.assembly_chart_scopes "
        "WHERE source_id='primary' AND business_date<=%s",
        (datetime.now(ZONE).date(),),
    ).fetchone()["day"]
    graph = load_graph(db, product_id, recipe_day) if recipe_day else []
    selling_ids = [
        r["department_id"]
        for r in db.execute(
            "SELECT DISTINCT x.department_id FROM chaika.sales_report_days d "
            "JOIN chaika.sales_reports r ON r.set_id=d.current_set_id "
            "JOIN chaika.sales_report_rows x ON x.report_id=r.id "
            "WHERE d.source_id='primary' AND d.business_date BETWEEN %s AND %s "
            "AND r.kind='dishes' AND x.department_id=ANY(%s::uuid[])",
            (start, end, [d["id"] for d in scope.departments]),
        ).fetchall()
    ]
    selected_group = (
        store_id is None
        and bool(scope.selection_ids)
        and analysis_department_id is None
        and not all_departments
    )
    analysis_ids = (
        list(scope.selection_ids)
        if selected_group
        else analysis_scope(scope, department, selling_ids, analysis_department_id, all_departments)
    )
    coverage = db.execute(
        "SELECT d.business_date,r.observed_at,s.checks FROM chaika.sales_report_days d "
        "JOIN chaika.sales_report_sets s ON s.id=d.current_set_id "
        "JOIN chaika.sales_reports r ON r.set_id=s.id AND r.kind='dishes' "
        "WHERE d.source_id='primary' AND d.business_date BETWEEN %s AND %s",
        (start, end),
    ).fetchall()
    sales = db.execute(
        """SELECT x.department_id,x.dimensions->>'DishId' AS dish_id,
            max(x.dimensions->>'DishName') AS dish,sum(x.quantity) AS quantity,
            bool_or(x.quantity IS NULL OR x.quantity<0) AS invalid_quantity,
            jsonb_agg(jsonb_build_object('date',d.business_date,'report_id',r.id,
                'ordinal',x.ordinal,'quantity',x.quantity::text)
                ORDER BY d.business_date,x.ordinal) AS sales_sources
        FROM chaika.sales_report_days d JOIN chaika.sales_reports r ON r.set_id=d.current_set_id
        JOIN chaika.sales_report_rows x ON x.report_id=r.id
        WHERE d.source_id='primary' AND d.business_date BETWEEN %s AND %s
            AND r.kind='dishes' AND x.department_id=ANY(%s::uuid[])
            AND x.dimensions->>'DishId'=ANY(%s::text[]) GROUP BY 1,2""",
        (start, end, analysis_ids, list({str(e["product_id"]) for e in graph})),
    ).fetchall()
    labels = {d["id"]: d["name"] for d in scope.departments}
    for row in sales:
        row["department"] = labels.get(row["department_id"])
    report = calculate_impact(
        graph, sales, price, product, coverage, start, end, recipe_day, departments=analysis_ids
    )
    report["analysis_departments"] = [dict(id=d, name=labels[d]) for d in analysis_ids]
    report["available_departments"] = [
        dict(id=d["id"], name=d["name"])
        for d in scope.departments
        if d["id"] in selling_ids or d["id"] == analysis_department_id
    ]
    report["analysis_selection"] = str(analysis_ids[0]) if len(analysis_ids) == 1 else "all"
    report["price_department_has_sales"] = department in selling_ids
    return report
