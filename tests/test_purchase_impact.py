"""Norms, partial data, source links and warehouse isolation for price scenarios."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_inventory_sync import inventory as inventory
from test_overview import seed_day
from test_purchase_prices import data as data
from test_reference_sync import bundle as bundle
from test_reference_sync import db as db

from app.web.purchase_impact import (
    analysis_scope,
    calculate_impact,
    load_graph,
    read_purchase_impact,
    recent_period,
    recipe_paths,
)
from app.web.purchase_impact_summary import add_weekly_impacts, summarize_prices
from app.web.repository import Repository, Scope

TARGET, DISH, PREP, DEPT, UNIT = [UUID(int=i) for i in range(11, 16)]
START, END = date(2026, 8, 1), date(2026, 8, 31)


def edge(parent, child, amount, output="1"):
    return dict(
        product_id=parent,
        ingredient_id=child,
        chart_id=parent,
        name=str(parent),
        ingredient=str(child),
        unit="порц" if parent == DISH else "кг",
        ingredient_unit="кг",
        amount_in=Decimal(amount),
        assembled_amount=Decimal(output),
        chart_details={
            "effective_direct_writeoff_store_specification": {"inverse": False, "departments": []},
            "product_size_assembly_strategy": "COMMON",
        },
        item_details={"store_specification": None, "product_size_id": None},
    )


def sample():
    graph = [edge(DISH, PREP, ".02"), edge(PREP, TARGET, "1", ".5")]
    sales = [dict(dish_id=DISH, dish="Бургер", quantity=Decimal(310), invalid_quantity=False)]
    price = dict(department_id=DEPT, unit_id=UNIT, delta=Decimal("93.5018"))
    product = dict(id=TARGET, main_unit_id=UNIT, unit="кг")
    coverage = [
        dict(
            business_date=START + timedelta(days=i),
            observed_at=datetime(2026, 9, 1, tzinfo=UTC),
            checks=[],
        )
        for i in range(31)
    ]
    return [graph, sales, price, product, coverage, START, END, date(2026, 9, 13)]


def test_nested_yield_and_weekly_impact_uses_calendar_days_and_decimal():
    r = calculate_impact(*sample())
    assert r["blockers"] == [] and r["coverage"]["complete"]
    row = r["rows"][0]
    assert row["amount_per_portion"] == Decimal(".040")
    assert row["monthly_amount"] == Decimal("12.4")
    assert row["weekly_amount"] == Decimal("2.8")
    assert row["weekly_delta"] == Decimal("261.80504")
    assert [p["chart_id"] for p in row["paths"][0]] == [DISH, PREP]
    assert r["totals"]["weekly_delta"] == row["weekly_delta"]
    assert r["totals"]["average_portion_delta"] == row["portion_delta"]


def test_parallel_ingredient_paths_are_added_once_each():
    s = sample()
    s[0].append(edge(DISH, TARGET, ".01"))
    r = calculate_impact(*s)
    assert r["rows"][0]["amount_per_portion"] == Decimal(".050")
    assert len(r["rows"][0]["paths"]) == 2


@pytest.mark.parametrize("negative", [False, True])
def test_zero_or_negative_price_change_is_not_hidden(negative):
    s = sample()
    s[2]["delta"] = Decimal(-10 if negative else 0)
    r = calculate_impact(*s)
    assert r["totals"]["weekly_delta"] == (-28 if negative else 0)


@pytest.mark.parametrize("failure", ["missing", "partial", "mismatch", "unit", "price"])
def test_unknown_inputs_do_not_create_a_weekly_forecast(failure):
    s = sample()
    if failure == "missing":
        s[4].pop()
    if failure == "partial":
        s[4][0]["observed_at"] = datetime(2026, 8, 1, tzinfo=UTC)
    if failure == "mismatch":
        s[4][0]["checks"] = [
            dict(report="dishes", exact_match=False, differences=[dict(department_id=str(DEPT))])
        ]
    if failure == "unit":
        s[2]["unit_id"] = uuid4()
    if failure == "price":
        s[2]["delta"] = None
    r = calculate_impact(*s)
    assert r["blockers"] and r["totals"]["weekly_delta"] is None
    assert r["rows"][0]["weekly_delta"] is None
    if failure in {"missing", "partial", "mismatch"}:
        assert r["rows"][0]["portion_delta"] == Decimal("3.740072")
        assert r["totals"]["average_portion_delta"] == Decimal("3.740072")
    else:
        assert r["rows"][0]["portion_delta"] is None
        assert r["totals"]["average_portion_delta"] is None


def test_total_portion_change_is_weighted_by_sales_and_excludes_unusable_dishes():
    s = sample()
    second_dish = uuid4()
    second_recipe = edge(second_dish, TARGET, ".1")
    second_recipe["unit"] = "порц"
    s[0].append(second_recipe)
    s[1][0]["quantity"] = Decimal(10)
    s[1].append(
        dict(dish_id=second_dish, dish="Второе блюдо", quantity=Decimal(30), invalid_quantity=False)
    )
    s[2]["delta"] = Decimal(100)
    r = calculate_impact(*s)
    assert r["totals"]["quantity"] == 40
    assert r["totals"]["average_portion_delta"] == Decimal("8.5")  # (10×4 + 30×10) / 40
    assert r["totals"]["weekly_delta"].quantize(Decimal(".0001")) == Decimal("76.7742")
    s[1][1]["invalid_quantity"] = True
    r = calculate_impact(*s)
    assert r["totals"]["quantity"] == 10 and r["totals"]["average_portion_delta"] == 4
    assert len(r["excluded"]) == 1


def test_zero_sales_does_not_produce_a_misleading_average_portion_change():
    s = sample()
    s[1][0]["quantity"] = Decimal(0)
    r = calculate_impact(*s)
    assert r["rows"][0]["portion_delta"] == Decimal("3.740072")
    assert r["totals"]["average_portion_delta"] is None
    assert r["totals"]["weekly_delta"] == 0


@pytest.mark.parametrize("delta", ["93.5018", "-25", "0", None])
def test_list_weekly_impact_matches_detail_with_other_ingredients_and_price_directions(delta):
    graph, sales, price, product, coverage, start, end, day = sample()
    price.update(product_id=TARGET, delta=Decimal(delta) if delta is not None else None)
    # Other products' edges returned by the bulk read must not contaminate this norm.
    unrelated = uuid4()
    bulk_graph = [*graph, edge(DISH, unrelated, "5")]
    expected = calculate_impact(graph, sales, price, product, coverage, start, end, day)
    summaries = summarize_prices(
        [price], bulk_graph, sales, {TARGET: product}, coverage, start, end, day, [DEPT]
    )
    assert summaries[0]["weekly_delta"] == expected["totals"]["weekly_delta"]
    assert summaries[0]["included_positions"] == len(expected["rows"])
    assert bool(summaries[0]["reason"]) is (delta is None)


@pytest.mark.parametrize("failure", ["coverage", "unit", "recipes", "product", "sales"])
def test_list_weekly_impact_does_not_turn_unknown_inputs_into_zero(failure):
    graph, sales, price, product, coverage, start, end, day = sample()
    price["product_id"] = TARGET
    products = {TARGET: product}
    if failure == "coverage":
        coverage.pop()
    elif failure == "unit":
        price["unit_id"] = uuid4()
    elif failure == "recipes":
        graph = []
    elif failure == "product":
        products = {}
    elif failure == "sales":
        sales = []
    result = summarize_prices([price], graph, sales, products, coverage, start, end, day, [DEPT])[0]
    assert result["weekly_delta"] is None and result["reason"]


def test_other_venue_reconciliation_does_not_leak_or_block_this_venue():
    s = sample()
    s[4][0]["checks"] = [
        dict(report="dishes", exact_match=False, differences=[dict(department_id=str(uuid4()))])
    ]
    r = calculate_impact(*s)
    assert r["coverage"]["complete"] and r["coverage"]["mismatch_dates"] == []


@pytest.mark.parametrize(
    "failure",
    ["direct", "unknown", "size", "item_size", "cycle", "ambiguous", "zero_output", "quantity"],
)
def test_unsupported_recipes_remain_visible_as_exclusions(failure):
    s = sample()
    if failure == "direct":
        s[0][1]["chart_details"]["effective_direct_writeoff_store_specification"]["departments"] = [
            str(DEPT)
        ]
    if failure == "unknown":
        s[0][1]["chart_details"]["effective_direct_writeoff_store_specification"] = None
    if failure == "size":
        s[0][0]["chart_details"]["product_size_assembly_strategy"] = "SPECIFIC"
    if failure == "item_size":
        s[0][0]["item_details"]["product_size_id"] = str(uuid4())
    if failure == "cycle":
        s[0].append(edge(PREP, DISH, "1"))
    if failure == "ambiguous":
        s[0][0]["chart_count"] = 2  # Other active chart need not contain this ingredient.
    if failure == "zero_output":
        s[0][1]["assembled_amount"] = 0
    if failure == "quantity":
        s[1][0]["invalid_quantity"] = True
    r = calculate_impact(*s)
    assert r["rows"] == [] and len(r["excluded"]) == 1
    assert r["excluded"][0]["chart_ids"] == [str(DISH)]
    assert r["totals"]["weekly_delta"] is None


def test_department_filters_and_inverse_apply_before_paths_are_summed():
    s = sample()
    extra = edge(DISH, TARGET, ".6")
    extra["item_details"]["store_specification"] = dict(inverse=True, departments=[str(DEPT)])
    s[0].append(extra)
    r = calculate_impact(*s)
    assert r["rows"][0]["amount_per_portion"] == Decimal(".040")
    s[0][0]["item_details"]["store_specification"] = dict(inverse=False, departments=[str(uuid4())])
    assert calculate_impact(*s)["rows"] == []


@pytest.mark.parametrize("today", [date(2026, 1, 3), date(2024, 3, 1), date(2026, 9, 13)])
def test_period_is_thirty_completed_local_days(today):
    start, end = recent_period(today=today)
    assert start == today - timedelta(days=30) and end == today - timedelta(days=1)
    assert (end - start).days + 1 == 30


def test_central_purchase_uses_selling_venues_within_user_grants():
    central, a, b, hidden = [uuid4() for _ in range(4)]
    scope = Scope({"role": "manager"}, tuple({"id": d} for d in [central, a, b]), central, (), ())
    # Global warehouse scope must not be used as the dish sales location.
    assert set(analysis_scope(scope, central, [a, b, hidden])) == {a, b}
    assert analysis_scope(scope, a, [a, b, hidden]) == [a]
    assert set(analysis_scope(scope, a, [a, b, hidden], all_departments=True)) == {a, b}
    assert analysis_scope(scope, central, [a, b, hidden], selected=b) == [b]
    with pytest.raises(HTTPException) as exc:
        analysis_scope(scope, central, [a, b, hidden], selected=hidden)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        analysis_scope(scope, central, [a, b], selected=a, all_departments=True)
    assert exc.value.status_code == 422


def test_same_dish_in_two_restaurants_keeps_distinct_sales_and_recipe_rules():
    s = sample()
    a, b = uuid4(), uuid4()
    s[1] = [
        {**s[1][0], "department_id": a, "department": "A"},
        {**s[1][0], "department_id": b, "department": "B", "quantity": Decimal(620)},
    ]
    r = calculate_impact(*s, departments=[a, b])
    assert len(r["rows"]) == 2 and r["totals"]["quantity"] == 930
    assert {r["department_id"] for r in r["rows"]} == {a, b}
    s[0][1]["chart_details"]["effective_direct_writeoff_store_specification"]["departments"] = [
        str(b)
    ]
    r = calculate_impact(*s, departments=[a, b])
    assert len(r["rows"]) == 1 and r["rows"][0]["department_id"] == a
    assert len(r["excluded"]) == 1 and r["excluded"][0]["department_id"] == b


def test_no_sales_is_not_zero_consumption_and_explains_the_cause():
    s = sample()
    s[1] = []
    r = calculate_impact(*s)
    assert r["totals"]["weekly_amount"] is None
    assert r["totals"]["weekly_delta"] is None
    assert r["empty_reason"] and r["sold_dishes"] == 0


def test_path_reference_rounding():
    graph = [edge(DISH, TARGET, "1", "3")]
    paths, issues = recipe_paths(graph, TARGET, DISH, DEPT)
    assert not issues and paths[0][0] == Decimal(".3333")


def test_sql_graph_uses_latest_scope_excludes_removed_items_and_keeps_cycles(db, inventory):
    from app.sync_inventory import publish_inventory

    snapshots = deepcopy(inventory[2])
    chart = snapshots["assembly_charts"]["payload"]["items"][0]
    target = UUID(chart["assembled_product_id"])
    chart["items"][0]["product_id"] = str(target)  # Cycle must terminate in SQL.
    publish_inventory(db, snapshots, date(2026, 9, 9))
    db.row_factory = dict_row
    rows = load_graph(db, target, date(2026, 9, 9))
    assert len(rows) == 1 and rows[0]["chart_count"] == 1
    assert load_graph(db, target, date(2026, 9, 10)) == []
    db.execute("UPDATE chaika.assembly_chart_items SET present_in_latest=false")
    assert load_graph(db, target, date(2026, 9, 9)) == []


def test_unavailable_warehouse_rejected_before_querying_any_data():
    scope = Scope({"role": "manager"}, (), None, (), ())
    with pytest.raises(HTTPException) as exc:
        read_purchase_impact(None, scope, {}, (TARGET, uuid4(), UNIT, False))
    assert exc.value.status_code == 404


def test_real_reader_scopes_prices_and_sales(db, data):
    receipt, read, store, unit, hidden = data
    receipt("2026-08-01", "1", "100")
    receipt("2026-09-01", "1", "120")
    product = read()["rows"][0]["product_id"]
    department = uuid4()
    # Source provenance and constraints come from the inventory fixture, not a fake DB.
    db.execute("UPDATE chaika.products SET main_unit_id=%s WHERE id=%s", (unit, product))
    db.execute(
        "INSERT INTO chaika.stores(source_id,id,name,parent_id,first_seen_at,"
        "last_seen_at,last_snapshot_id) "
        "SELECT 'primary',%s,'Кухня',%s,now(),now(),last_snapshot_id FROM chaika.products LIMIT 1",
        (store, department),
    )
    scope = Scope(
        {"role": "manager"}, ({"id": department, "name": "Ресторан"},), None, (store,), ()
    )
    repo = object.__new__(Repository)
    db.row_factory = dict_row
    r = read_purchase_impact(
        db, scope, repo.resource_query(scope, "invoices"), (product, store, unit, False)
    )
    assert r["price"]["department_id"] == department
    assert r["price"]["delta"] == 20
    assert r["coverage"]["loaded_days"] == 0 and r["totals"]["weekly_delta"] is None
    with pytest.raises(HTTPException) as exc:
        read_purchase_impact(
            db, scope, repo.resource_query(scope, "invoices"), (product, hidden, unit, False)
        )
    assert exc.value.status_code == 404


def test_central_warehouse_reader_finds_restaurant_dishes_without_leaking_sales(
    db, data, monkeypatch
):
    receipt, read, store, unit, _ = data
    receipt("2026-08-01", "1", "100")
    receipt("2026-09-01", "1", "120")
    product = read()["rows"][0]["product_id"]
    central, a, b, hidden, dish, portion = [uuid4() for _ in range(6)]
    db.execute("UPDATE chaika.products SET main_unit_id=%s", (unit,))
    db.execute(
        "INSERT INTO chaika.stores(source_id,id,name,parent_id,first_seen_at,"
        "last_seen_at,last_snapshot_id) "
        "SELECT 'primary',%s,'Закупки',%s,now(),now(),last_snapshot_id "
        "FROM chaika.products LIMIT 1",
        (store, central),
    )
    db.execute(
        "INSERT INTO chaika.measure_units(source_id,id,root_type,name,deleted,first_seen_at,"
        "last_seen_at,last_snapshot_id,details) SELECT 'primary',%s,'MeasureUnit','порц',false,"
        "now(),now(),last_snapshot_id,'{}' FROM chaika.products LIMIT 1",
        (portion,),
    )
    db.execute(
        "INSERT INTO chaika.products(source_id,id,name,type,main_unit_id,deleted,"
        "default_sale_price,"
        "unit_weight,unit_capacity,details,first_seen_at,last_seen_at,last_snapshot_id) "
        "SELECT 'primary',%s,'Блюдо','DISH',%s,false,0,0,0,'{}',now(),now(),last_snapshot_id "
        "FROM chaika.products LIMIT 1",
        (dish, portion),
    )
    db.execute(
        "UPDATE chaika.assembly_charts SET product_id=%s,assembled_amount=1,details=%s",
        (dish, Jsonb(edge(dish, product, ".2")["chart_details"])),
    )
    db.execute("UPDATE chaika.assembly_chart_items SET product_id=%s,amount_in=.2", (product,))
    start, end = recent_period()
    # The inventory fixture already registered this source with its original URL.
    monkeypatch.setattr("test_overview.register_sources", lambda *_: None)
    for i in range(30):
        seed_day(
            db,
            start + timedelta(days=i),
            [],
            dishes=[
                dict(
                    department_id=d,
                    revenue=100,
                    quantity=q,
                    dimensions={"DishId": str(dish), "DishName": "Блюдо"},
                )
                for d, q in [(a, 10), (b, 20), (hidden, 9000)]
            ],
        )
    scope = Scope(
        {"role": "manager"},
        tuple(
            dict(id=d, name=n)
            for d, n in [(central, "ЯФирма"), (a, "Ресторан А"), (b, "Ресторан Б")]
        ),
        central,
        (store,),
        (),
    )
    repo = object.__new__(Repository)
    db.row_factory = dict_row
    spec = repo.resource_query(scope, "invoices")
    result = read_purchase_impact(db, scope, spec, (product, store, unit, False))
    assert result["price_department_has_sales"] is False
    assert result["start"] == start and result["end"] == end
    assert len(result["rows"]) == 2 and result["sold_dishes"] == 2
    assert result["totals"]["quantity"] == 900 and result["totals"]["weekly_delta"] == 840
    assert {r["department_id"] for r in result["rows"]} == {a, b}
    assert {r["id"] for r in result["available_departments"]} == {a, b}
    one = read_purchase_impact(
        db, scope, spec, (product, store, unit, False), analysis_department_id=a
    )
    assert one["totals"]["quantity"] == 300 and one["totals"]["weekly_delta"] == 280
    assert one["price"]["current"] == result["price"]["current"]
    network_scope = replace(scope, selected=None)
    network = read_purchase_impact(
        db,
        network_scope,
        repo.resource_query(network_scope, "invoices"),
        (product, None, unit, False),
    )
    assert network["price"]["store_id"] is None
    assert network["price"]["scope_label"] == "Доступные заведения"
    assert {r["department_id"] for r in network["rows"]} == {a, b}
    assert network["totals"]["quantity"] == 900 and network["totals"]["weekly_delta"] == 840
    summary = add_weekly_impacts(db, network_scope, {"rows": [deepcopy(network["price"])]})
    impact = summary["rows"][0]["impact"]
    assert impact["weekly_delta"] == network["totals"]["weekly_delta"]
    assert impact["included_positions"] == 2 and impact["excluded_positions"] == 0
    assert summary["impact_period"]["start"] == start
    # A manager with fewer grants must never receive the network owner's sales volume.
    restricted = replace(
        network_scope, departments=tuple(d for d in network_scope.departments if d["id"] != b)
    )
    limited = add_weekly_impacts(db, restricted, {"rows": [deepcopy(network["price"])]})
    assert limited["rows"][0]["impact"]["weekly_delta"] == 280
    selected = add_weekly_impacts(
        db, replace(network_scope, selected=b), {"rows": [deepcopy(network["price"])]}
    )
    assert selected["rows"][0]["impact"]["weekly_delta"] == 560
    with pytest.raises(HTTPException) as exc:
        read_purchase_impact(
            db, scope, spec, (product, store, unit, False), analysis_department_id=hidden
        )
    assert exc.value.status_code == 403
