"""Read-only stock valuation; never retail prices or an outbound iiko request."""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, DecimalException, localcontext
from zoneinfo import ZoneInfo

from app.documents.recipe_costs import RecipeCosts, UnpricedRecipe

ZONE = ZoneInfo("Europe/Simferopol")


def stock_cost(stock):
    if not stock:
        return None, "missing_balance"
    try:
        quantity, value = Decimal(str(stock["amount"])), Decimal(str(stock["value"]))
        if not quantity.is_finite() or not value.is_finite():
            return None, "invalid_balance"
        if quantity == 0 or value == 0 or (quantity > 0) != (value > 0):
            return None, "nonpositive_balance"
        return value / quantity, None
    except (DecimalException, ValueError):
        return None, "invalid_balance"


def estimate(db, store_id, rows, *, at=None):
    now = datetime.now(UTC)
    point = at or now
    if point.tzinfo is None:
        point = point.replace(tzinfo=ZONE)
    present = db.execute(
        "SELECT to_regclass('chaika.store_balance_reports') AS reports, "
        "to_regclass('chaika.store_balance_items') AS items, "
        "to_regclass('chaika.products') AS products, "
        "to_regclass('chaika.assembly_charts') AS charts, "
        "to_regclass('chaika.assembly_chart_items') AS chart_items, "
        "to_regclass('chaika.assembly_chart_scopes') AS scopes, "
        "to_regclass('chaika.stores') AS stores, "
        "to_regclass('chaika.corporate_nodes') AS nodes"
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
    recipes = None
    if (
        report
        and not stale
        and all(
            present.get(key)
            for key in ("products", "charts", "chart_items", "scopes", "stores", "nodes")
        )
    ):
        recipes = RecipeCosts(
            db, store_id, point.astimezone(ZONE), report["last_snapshot_id"], stock_cost
        )
    result, known, unpriced = [], Decimal(0), 0
    with localcontext() as context:
        context.prec = 50
        for row in rows:
            product = str(row["product_id"])
            stock = balances.get(product)
            reason = "stale_balance" if stale else "missing_balance"
            unit, value = None, None
            cost = None
            method = "store_balance"
            if stock:
                cost, reason = stock_cost(stock)
            if cost is None and recipes:
                metadata = recipes.product(product)
                if metadata and metadata["type"] == "PREPARED":
                    try:
                        cost = recipes.resolve(product)
                        method = "recipe"
                    except UnpricedRecipe as exc:
                        reason = exc.reason
                    except DecimalException:
                        reason = "invalid_recipe"
            if cost is not None:
                try:
                    calculated = (cost * Decimal(str(row["amount"]))).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    calculated_unit = format(
                        cost.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f"
                    )
                    updated_total = known + calculated
                except DecimalException:
                    reason = "invalid_recipe" if method == "recipe" else "invalid_balance"
                else:
                    value, unit, known, reason = calculated, calculated_unit, updated_total, None

            if value is None:
                unpriced += 1
            result.append(
                {
                    "product_id": product,
                    "amount": row["amount"],
                    "unit_cost": unit,
                    "sum": format(value, "f") if value is not None else None,
                    "reason": reason,
                    "valuation_method": method if value is not None else None,
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


def displayed_estimate(stored):
    """An approval quote is frozen; original creation fields stay intact in storage."""
    return stored.get("approval_estimate", stored) if stored else stored


def needs_current_estimate(doc):
    stored = doc.get("cost_estimate")
    return bool(
        stored
        and stored.get("total") is None
        and "approval_estimate" not in stored
        and doc["status"] == "Created"
        and doc["submission_state"] in {"idle", "failed"}
    )


def improved_estimate(db, doc, rows):
    """Never replace an incomplete quote with equal or worse price coverage."""
    current = estimate(db, doc["store_id"], rows)
    if current["unpriced_count"] >= doc["cost_estimate"]["unpriced_count"]:
        return doc["cost_estimate"]
    return {
        **current,
        "refreshed": True,
        "original_estimated_at": doc["cost_estimate"].get("estimated_at"),
    }
