"""Conservative saved-recipe valuation in product base units, without iiko calls."""

from datetime import timedelta
from decimal import Decimal, DecimalException

from app.documents.context import analytics_schema


class UnpricedRecipe(Exception):
    def __init__(self, reason):
        self.reason = reason


def number(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except (DecimalException, ValueError):
        raise UnpricedRecipe("invalid_recipe") from None


def applies(spec, department):
    if spec is None:
        return True
    if (
        not isinstance(spec, dict)
        or type(spec.get("inverse")) is not bool
        or not isinstance(spec.get("departments"), list)
    ):
        raise UnpricedRecipe("invalid_recipe")
    included = str(department) in {str(value) for value in spec["departments"]}
    return not included if spec["inverse"] else included


class RecipeCosts:
    def __init__(self, db, store, point, snapshot, stock_cost):
        self.db, self.store, self.point = db, store, point
        self.snapshot, self.stock_cost = snapshot, stock_cost
        self.cache = {}
        self.products = {}
        self.department = None
        self.visits = 0

    def product(self, product):
        if product not in self.products:
            self.products[product] = self.db.execute(
                f"SELECT type,main_unit_id "
                f"FROM {analytics_schema(self.db)}.products WHERE source_id='primary' "
                "AND id=%s AND present_in_latest AND NOT deleted AND last_seen_at<=%s "
                "AND last_seen_at>=%s",
                (product, self.point, self.point - timedelta(hours=48)),
            ).fetchone()
        return self.products[product]

    def resolve_department(self):
        if self.department is not None:
            return self.department
        row = self.db.execute(
            "WITH RECURSIVE parents AS ("
            "SELECT s.id,s.parent_id,'STORE'::text AS type,ARRAY[s.id] AS path "
            f"FROM {analytics_schema(self.db)}.stores s WHERE s.source_id='primary' AND s.id=%s "
            "AND s.present_in_latest AND s.last_seen_at<=%s AND s.last_seen_at>=%s "
            "UNION ALL SELECT n.id,n.parent_id,n.type,p.path||n.id FROM parents p "
            f"JOIN {analytics_schema(self.db)}.corporate_nodes n ON n.source_id='primary' "
            f"AND n.id=p.parent_id "
            "WHERE n.present_in_latest AND n.last_seen_at<=%s AND n.last_seen_at>=%s "
            "AND NOT n.id=ANY(p.path) AND cardinality(p.path)<24 "
            "AND p.type NOT IN ('DEPARTMENT','CENTRALSTORE','MANUFACTURE')) "
            "SELECT id FROM parents WHERE type IN ('DEPARTMENT','CENTRALSTORE','MANUFACTURE')",
            (
                self.store,
                self.point,
                self.point - timedelta(hours=48),
                self.point,
                self.point - timedelta(hours=48),
            ),
        ).fetchall()
        if len(row) != 1:
            raise UnpricedRecipe("missing_recipe_department")
        self.department = row[0]["id"]
        return self.department

    def resolve(self, product, ancestors=()):
        self.visits += 1
        if product in ancestors:
            raise UnpricedRecipe("recipe_cycle")
        if len(ancestors) >= 24 or self.visits > 4000:
            raise UnpricedRecipe("recipe_too_complex")
        if product in self.cache:
            return self.cache[product]
        metadata = self.product(product)
        if not metadata or not metadata["main_unit_id"]:
            raise UnpricedRecipe("missing_recipe_product")
        if metadata["type"] == "GOODS":
            stock = self.db.execute(
                "SELECT product_id,sum(amount) AS amount,sum(sum) AS value "
                f"FROM {analytics_schema(self.db)}.store_balance_items "
                f"WHERE snapshot_id=%s AND store_id=%s "
                "AND product_id=%s GROUP BY product_id",
                (self.snapshot, self.store, product),
            ).fetchone()
            cost, _ = self.stock_cost(stock)
            if cost is None:
                raise UnpricedRecipe("missing_ingredient_price")
            self.cache[product] = cost
            return cost
        if metadata["type"] != "PREPARED":
            raise UnpricedRecipe("unsupported_recipe_product")
        department = self.resolve_department()
        day = self.point.date()
        # Scopes and mutable headers must still describe the SAME observation.
        # A later replacement cannot be used to reconstruct a historical estimate.
        # Header, scope and items share one MVCC statement snapshot: the inventory
        # sync replaces them transactionally, including items without timestamps.
        # Decimal norms travel as text through JSON, never binary floats.
        charts = self.db.execute(
            "SELECT c.id,c.assembled_amount,c.details, "
            "COALESCE((SELECT jsonb_agg(jsonb_build_object('product_id',i.product_id, "
            "'amount_in',i.amount_in::text,'details',i.details) ORDER BY i.id) "
            f"FROM {analytics_schema(self.db)}.assembly_chart_items i "
            f"WHERE i.source_id=c.source_id "
            "AND i.chart_id=c.id AND i.present_in_latest),'[]'::jsonb) AS items "
            f"FROM {analytics_schema(self.db)}.assembly_charts c "
            f"JOIN {analytics_schema(self.db)}.assembly_chart_scopes s ON s.source_id=c.source_id "
            f"AND s.chart_id=c.id "
            "WHERE c.source_id='primary' AND c.product_id=%s AND s.present_in_latest "
            f"AND s.business_date=(SELECT max(business_date) "
            f"FROM {analytics_schema(self.db)}.assembly_chart_scopes "
            "WHERE source_id='primary' AND business_date<=%s AND present_in_latest) "
            "AND s.business_date>=%s AND c.date_from<=%s AND (c.date_to IS NULL OR c.date_to>%s) "
            "AND s.last_snapshot_id=c.last_snapshot_id AND c.last_seen_at<=%s "
            "AND c.last_seen_at>=%s",
            (
                product,
                day,
                day - timedelta(days=2),
                day,
                day,
                self.point,
                self.point - timedelta(hours=48),
            ),
        ).fetchall()
        if len(charts) != 1:
            raise UnpricedRecipe("ambiguous_recipe" if charts else "missing_recipe")
        chart = charts[0]
        details = chart["details"]
        if details.get("product_size_assembly_strategy") != "COMMON":
            raise UnpricedRecipe("unsupported_recipe_size")
        output = number(chart["assembled_amount"])
        if output <= 0:
            raise UnpricedRecipe("invalid_recipe")
        items = chart["items"]
        total, included = Decimal(0), False
        for item in items:
            detail = item["details"]
            if not applies(detail.get("store_specification"), department):
                continue
            if detail.get("product_size_id") is not None:
                raise UnpricedRecipe("unsupported_recipe_size")
            amount = number(item["amount_in"])
            if amount < 0:
                raise UnpricedRecipe("invalid_recipe")
            if amount == 0:
                continue
            included = True
            total += amount / output * self.resolve(str(item["product_id"]), (*ancestors, product))
        if not included or total <= 0 or not total.is_finite():
            raise UnpricedRecipe("invalid_recipe")
        self.cache[product] = total
        return total
