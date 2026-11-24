"""Read prepared ingredient usage; this path never loads graphs or sales rows."""

from datetime import date, datetime
from decimal import Decimal, localcontext

from app.purchase_impact_precompute import sales_revision, source_revision
from app.web.coverage import ZONE
from app.web.purchase_impact import recent_period, sales_coverage
from app.web.purchase_impact_scope import forecast_departments
from app.web.warehouse_analytics import require_department_report


def restored_coverage(rows):
    return [
        dict(
            row,
            business_date=date.fromisoformat(row["business_date"]),
            observed_at=datetime.fromisoformat(row["observed_at"]),
        )
        for row in rows
    ]


def summarize_prepared(price, fact, coverage, start, end, recipe_day, departments, *, quality=None):
    if fact is None:
        return dict(
            weekly_delta=None,
            included_positions=0,
            excluded_positions=0,
            reason="Расчёт обновляется.",
        )
    if not fact["product_exists"]:
        return dict(
            weekly_delta=None,
            included_positions=0,
            excluded_positions=0,
            reason="Товар отсутствует в номенклатуре.",
        )
    if quality is None:
        quality = sales_coverage(coverage, start, end, departments)
    blockers = []
    if fact["main_unit_id"] != price["unit_id"]:
        blockers.append("Единица в накладной отличается от основной единицы товара")
    if price["delta"] is None:
        blockers.append("Нет пригодной предыдущей цены для сравнения")
    if recipe_day is None:
        blockers.append("Нет загруженного снимка техкарт")
    if not quality["complete"]:
        blockers.append("Продажи периода неполные или не прошли сверку; недельный прогноз отключён")
    selected = [fact["departments"][str(d)] for d in departments if str(d) in fact["departments"]]
    monthly = sorted((Decimal(v) for values in selected for v in values["monthly"]), reverse=True)
    excluded = sum(values["excluded"] for values in selected)
    with localcontext() as ctx:
        ctx.prec = 70
        delta = (
            sum((amount * 7 / quality["days"] * price["delta"] for amount in monthly), Decimal(0))
            if monthly and not blockers
            else None
        )
    empty_reason = (
        "Товар не найден в цепочках загруженных техкарт."
        if not fact["has_graph"]
        else "В выбранных заведениях за последние 30 дней не найдено продаж связанных блюд."
        if not selected
        else "Связанные блюда найдены в продажах, но все исключены из расчёта. "
        "Причины указаны ниже."
        if not monthly
        else None
    )
    return dict(
        weekly_delta=delta,
        included_positions=len(monthly),
        excluded_positions=excluded,
        reason="; ".join(blockers) or empty_reason,
    )


def prepared_departments(db, scope, current):
    allowed = {str(d) for d in scope.ids}
    candidates = (
        list(scope.selection_ids)
        if scope.selection_ids
        else sorted(allowed.intersection(current["selling_department_ids"]))
    )
    return candidates, forecast_departments(db, candidates, explicit=bool(scope.selection_ids))


def prepared_window(db, scope):
    """Use an older complete generation during rollover only if all its inputs still match."""
    today = datetime.now(ZONE).date()
    _, target_end = recent_period(today=today)
    revision = source_revision(db, today=today)
    start, end = date.fromisoformat(revision["start"]), date.fromisoformat(revision["end"])
    current = db.execute(
        "SELECT * FROM chaika.purchase_impact_prepared WHERE source_id='primary'"
    ).fetchone()
    period = dict(
        start=start,
        end=end,
        target_end=target_end,
        recipe_day=date.fromisoformat(revision["recipe_day"]) if revision["recipe_day"] else None,
        status="pending",
        prepared_at=current["prepared_at"] if current else None,
    )
    if current is None:
        return None, period
    old_start, old_end = current["period_start"], current["period_end"]
    if (old_start, old_end) == (start, end) and current["revision"] == revision:
        return current, dict(period, status="ready")
    if old_end < end and old_end - old_start == end - start:
        previous_inputs = dict(
            revision,
            start=old_start.isoformat(),
            end=old_end.isoformat(),
            sales=sales_revision(db, old_start, old_end),
        )
        if previous_inputs == current["revision"]:
            _, departments = prepared_departments(db, scope, current)
            quality = sales_coverage(
                restored_coverage(current["coverage"]), old_start, old_end, departments
            )
            if quality["complete"]:
                return current, dict(period, status="ready", start=old_start, end=old_end)
    return None, period


def add_prepared_impacts(db, scope, report):
    require_department_report(scope)
    current, period = prepared_window(db, scope)
    report["impact_period"] = period
    start, end = period["start"], period["end"]
    if current is None:
        for price in report["rows"]:
            price["impact"] = summarize_prepared(price, None, [], start, end, None, [])
        return report
    targets = list({price["product_id"] for price in report["rows"]})
    candidates, departments = prepared_departments(db, scope, current)
    # Filter JSON before it leaves PostgreSQL; facts of other venues cannot be
    # accidentally returned by a later response refactor.
    facts = {
        row["product_id"]: row
        for row in db.execute(
            "SELECT product_id,product_exists,main_unit_id,has_graph,"
            "COALESCE((SELECT jsonb_object_agg(key,value) FROM jsonb_each(p.departments) "
            "WHERE key=ANY(%s::text[])), '{}'::jsonb) AS departments "
            "FROM chaika.purchase_impact_prepared_products p "
            "WHERE generation_id=%s AND product_id=ANY(%s::uuid[])",
            (sorted(str(d) for d in scope.ids), current["id"], targets),
        ).fetchall()
    }
    coverage = restored_coverage(current["coverage"])
    quality = sales_coverage(coverage, start, end, departments)
    for price in report["rows"]:
        price["impact"] = summarize_prepared(
            price,
            facts.get(price["product_id"]),
            coverage,
            start,
            end,
            current["recipe_day"],
            departments,
            quality=quality,
        )
    period.update(
        recipe_day=current["recipe_day"],
        coverage=quality,
        status="ready" if all(target in facts for target in targets) else "pending",
        analysis_department_ids=departments,
        excluded_department_ids=[value for value in candidates if value not in departments],
    )
    return report
