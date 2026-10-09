"""Read prepared ingredient usage; this path never loads graphs or sales rows."""

from datetime import date, datetime
from decimal import Decimal, localcontext

from app.purchase_impact_precompute import source_revision
from app.tenancy.sql import ANALYTICS_SCHEMA as DB
from app.web.purchase_impact import recent_period, sales_coverage
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


def summarize_prepared(price, fact, coverage, start, end, recipe_day, departments):
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


def add_prepared_impacts(db, scope, report):
    require_department_report(scope)
    start, end = recent_period()
    current = db.execute(
        f"SELECT * FROM {DB}.purchase_impact_prepared WHERE source_id='primary'"
    ).fetchone()
    valid = (
        current is not None
        and current["period_start"] == start
        and current["period_end"] == end
        and current["revision"] == source_revision(db)
    )
    if not valid:
        for price in report["rows"]:
            price["impact"] = summarize_prepared(price, None, [], start, end, None, [])
        report["impact_period"] = dict(
            start=start,
            end=end,
            recipe_day=None,
            status="pending",
            prepared_at=current["prepared_at"] if current else None,
        )
        return report
    targets = list({price["product_id"] for price in report["rows"]})
    allowed = {str(d) for d in scope.ids}
    departments = (
        list(scope.selection_ids)
        if scope.selection_ids
        else sorted(allowed.intersection(current["selling_department_ids"]))
    )
    # Filter JSON before it leaves PostgreSQL; facts of other venues cannot be
    # accidentally returned by a later response refactor.
    facts = {
        row["product_id"]: row
        for row in db.execute(
            "SELECT product_id,product_exists,main_unit_id,has_graph,"
            "COALESCE((SELECT jsonb_object_agg(key,value) FROM jsonb_each(p.departments) "
            "WHERE key=ANY(%s::text[])), '{}'::jsonb) AS departments "
            f"FROM {DB}.purchase_impact_prepared_products p "
            "WHERE generation_id=%s AND product_id=ANY(%s::uuid[])",
            (sorted(allowed), current["id"], targets),
        ).fetchall()
    }
    coverage = restored_coverage(current["coverage"])
    for price in report["rows"]:
        price["impact"] = summarize_prepared(
            price,
            facts.get(price["product_id"]),
            coverage,
            start,
            end,
            current["recipe_day"],
            departments,
        )
    report["impact_period"] = dict(
        start=start,
        end=end,
        recipe_day=current["recipe_day"],
        coverage=sales_coverage(coverage, start, end, departments),
        status="ready" if all(target in facts for target in targets) else "pending",
        prepared_at=current["prepared_at"],
    )
    return report
