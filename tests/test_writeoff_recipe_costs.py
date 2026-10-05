"""Synthetic recipe trees: no production documents or external calls."""

from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest

from app.documents.costs import estimate
from tests.test_writeoff_costs import PRODUCT, SOURCE, BalanceDB, Cursor

RAW, NESTED, DEPARTMENT = map(str, (uuid4(), uuid4(), uuid4()))
ROOT = str(PRODUCT)


class RecipeDB(BalanceDB):
    def __init__(self):
        super().__init__()
        self.products = {
            ROOT: {"type": "PREPARED", "main_unit_id": "kg"},
            NESTED: {"type": "PREPARED", "main_unit_id": "piece"},
            RAW: {"type": "GOODS", "main_unit_id": "kg"},
        }
        self.charts = {
            ROOT: [
                {
                    "id": ROOT,
                    "assembled_amount": 2,
                    "details": {"product_size_assembly_strategy": "COMMON"},
                }
            ],
            NESTED: [
                {
                    "id": NESTED,
                    "assembled_amount": 4,
                    "details": {"product_size_assembly_strategy": "COMMON"},
                }
            ],
        }
        self.items = {
            ROOT: [{"product_id": NESTED, "amount_in": 3, "details": {}}],
            NESTED: [{"product_id": RAW, "amount_in": 2, "details": {}}],
        }
        self.leaf = [{"product_id": RAW, "amount": 5, "value": 100}]
        self.departments = [{"id": DEPARTMENT}]

    def execute(self, sql, params=None):
        if "to_regclass" in sql:
            return Cursor(
                [
                    {
                        key: True
                        for key in (
                            "reports",
                            "items",
                            "products",
                            "charts",
                            "chart_items",
                            "scopes",
                            "stores",
                            "nodes",
                        )
                    }
                ]
            )
        if "FROM chaika.products" in sql:
            assert "source_id='primary'" in sql and "last_seen_at<=%s" in sql
            return Cursor([self.products[params[0]]] if params[0] in self.products else [])
        if "WITH RECURSIVE parents" in sql:
            assert params[0] == SOURCE
            return Cursor(self.departments)
        if "FROM chaika.assembly_charts c" in sql:
            assert "s.last_snapshot_id=c.last_snapshot_id" in sql
            assert "c.last_seen_at<=%s" in sql and "c.date_to>%s" in sql
            assert "i.amount_in::text" in sql
            return Cursor(
                [
                    {**deepcopy(chart), "items": deepcopy(self.items.get(chart["id"], []))}
                    for chart in self.charts.get(params[0], [])
                ]
            )
        if "FROM chaika.assembly_chart_items" in sql:
            raise AssertionError("Recipe items must share the header statement snapshot")
        if "AND product_id=%s" in sql:
            assert params == ("snapshot", SOURCE, RAW)
            return Cursor(self.leaf)
        return super().execute(sql, params)


def result(db):
    return estimate(db, SOURCE, [{"product_id": ROOT, "amount": 2}])


def test_nested_base_units_and_output_yield():
    value = result(RecipeDB())
    assert value["total"] == "30.00"  # 2 / 2 * 3 / 4 * 2 * 20
    assert value["items"][0]["unit_cost"] == "15.000000"
    assert value["items"][0]["valuation_method"] == "recipe"


def test_direct_stock_preferred_to_recipe():
    db = RecipeDB()
    db.balances = [{"product_id": ROOT, "amount": 2, "value": 18}]
    db.charts = {}
    assert result(db)["total"] == "18.00"


@pytest.mark.parametrize(
    "case,reason",
    [
        ("missing", "missing_recipe"),
        ("ambiguous", "ambiguous_recipe"),
        ("cycle", "recipe_cycle"),
        ("missing_price", "missing_ingredient_price"),
        ("negative_price", "missing_ingredient_price"),
        ("size", "unsupported_recipe_size"),
        ("item_size", "unsupported_recipe_size"),
        ("zero_output", "invalid_recipe"),
        ("nan", "invalid_recipe"),
        ("negative", "invalid_recipe"),
        ("no_unit", "missing_recipe_product"),
        ("department", "missing_recipe_department"),
        ("empty", "invalid_recipe"),
        ("filter", "invalid_recipe"),
        ("invalid_filter", "invalid_recipe"),
    ],
)
def test_incomplete_recipe_never_partial_or_zero(case, reason):
    db = RecipeDB()
    if case == "missing":
        db.charts.pop(NESTED)
    elif case == "ambiguous":
        db.charts[ROOT].append(deepcopy(db.charts[ROOT][0]))
    elif case == "cycle":
        db.items[NESTED][0]["product_id"] = ROOT
    elif case == "missing_price":
        db.leaf = []
    elif case == "negative_price":
        db.leaf[0]["value"] = -1
    elif case == "size":
        db.charts[ROOT][0]["details"]["product_size_assembly_strategy"] = "SPECIFIC"
    elif case == "item_size":
        db.items[ROOT][0]["details"]["product_size_id"] = "size"
    elif case == "zero_output":
        db.charts[ROOT][0]["assembled_amount"] = 0
    elif case in {"nan", "negative"}:
        db.items[ROOT][0]["amount_in"] = Decimal("NaN" if case == "nan" else "-1")
    elif case == "no_unit":
        db.products[RAW]["main_unit_id"] = None
    elif case == "department":
        db.departments = []
    elif case == "empty":
        db.items[ROOT] = []
    elif case == "filter":
        db.items[ROOT][0]["details"]["store_specification"] = {"departments": [], "inverse": False}
    elif case == "invalid_filter":
        db.items[ROOT][0]["details"]["store_specification"] = {"departments": []}
    value = result(db)
    assert value["total"] is None
    assert value["known_total"] == "0.00"
    assert value["items"][0]["reason"] == reason


def test_department_exclusions_and_duplicate_ingredients():
    db = RecipeDB()
    item = db.items[ROOT][0]
    item["details"]["store_specification"] = {"departments": [DEPARTMENT], "inverse": False}
    excluded = deepcopy(item)
    excluded["details"]["store_specification"]["inverse"] = True
    excluded["product_id"] = "unavailable"
    db.items[ROOT] = [item, deepcopy(item), excluded]
    assert result(db)["total"] == "60.00"


def test_recipe_items_share_header_snapshot_during_concurrent_refresh():
    class RefreshDB(RecipeDB):
        def execute(self, sql, params=None):
            cursor = super().execute(sql, params)
            if "FROM chaika.assembly_charts c" in sql and params[0] == ROOT:
                # Simulate a committed inventory refresh after the statement took
                # its snapshot; a later item query would mix these new norms with
                # the previously read output yield.
                self.items[ROOT][0]["amount_in"] = 999
                self.charts[ROOT][0]["assembled_amount"] = 99
            return cursor

    assert result(RefreshDB())["total"] == "30.00"
