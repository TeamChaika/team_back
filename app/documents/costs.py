"""Read-only stock valuation; never retail prices or an outbound iiko request."""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, DecimalException, localcontext
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Simferopol")


def estimate(db, store_id, rows, *, at=None):
    now = datetime.now(UTC)
    point = at or now
    if point.tzinfo is None:
        point = point.replace(tzinfo=ZONE)
    present = db.execute(
        "SELECT to_regclass('chaika.store_balance_reports') AS reports, "
        "to_regclass('chaika.store_balance_items') AS items"
    ).fetchone()
    report = None
    if present["reports"] and present["items"]:
        # last_seen_at prevents using a subsequently replaced historical report.
        report = db.execute(
            "SELECT last_snapshot_id,accounting_timestamp FROM chaika.store_balance_reports "
            "WHERE source_id='primary' AND accounting_timestamp<=%s AND last_seen_at<=%s "
            "ORDER BY accounting_timestamp DESC LIMIT 1",
            (point.astimezone(ZONE).replace(tzinfo=None), point),
        ).fetchone()
    balances = {}
    source_at = report["accounting_timestamp"].replace(tzinfo=ZONE) if report else None
    stale = source_at is not None and point - source_at > timedelta(hours=48)
    if report and not stale:
        balances = {
            str(row["product_id"]): row
            for row in db.execute(
                "SELECT product_id,sum(amount) AS amount,sum(sum) AS value "
                "FROM chaika.store_balance_items WHERE snapshot_id=%s AND store_id=%s "
                "AND product_id=ANY(%s::uuid[]) GROUP BY product_id",
                (report["last_snapshot_id"], store_id, [str(r["product_id"]) for r in rows]),
            ).fetchall()
        }
    result, known, unpriced = [], Decimal(0), 0
    with localcontext() as context:
        context.prec = 50
        for row in rows:
            product = str(row["product_id"])
            stock = balances.get(product)
            reason = "stale_balance" if stale else "missing_balance"
            unit, value = None, None
            if stock:
                quantity, stock_value = Decimal(str(stock["amount"])), Decimal(str(stock["value"]))
                if not quantity.is_finite() or not stock_value.is_finite():
                    reason = "invalid_balance"
                elif quantity == 0 or stock_value == 0 or (quantity > 0) != (stock_value > 0):
                    reason = "nonpositive_balance"
                else:
                    try:
                        cost = stock_value / quantity
                        calculated = (cost * Decimal(str(row["amount"]))).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                        calculated_unit = format(
                            cost.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f"
                        )
                        updated_total = known + calculated
                    except DecimalException:
                        reason = "invalid_balance"
                    else:
                        value, unit, known, reason = (
                            calculated,
                            calculated_unit,
                            updated_total,
                            None,
                        )

            if value is None:
                unpriced += 1
            result.append(
                {
                    "product_id": product,
                    "amount": row["amount"],
                    "unit_cost": unit,
                    "sum": format(value, "f") if value is not None else None,
                    "reason": reason,
                }
            )
    return {
        "source": "store_balance",
        "source_at": source_at.isoformat() if source_at else None,
        "estimated_at": now.isoformat(),
        "items": result,
        "total": format(known, ".2f") if not unpriced else None,
        "known_total": format(known, ".2f"),
        "unpriced_count": unpriced,
    }
