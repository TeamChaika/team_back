"""Prepared summaries retain scoped Decimal math and publish as one DB generation."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from test_reference_sync import bundle as bundle
from test_reference_sync import db as db

from app.purchase_impact_precompute import (
    build_prepared,
    json_value,
    prepare_product_facts,
    publish_prepared,
    source_revision,
)
from app.sync_references import register_sources
from app.web.purchase_impact import calculate_impact, recent_period
from app.web.purchase_impact_prepared import add_prepared_impacts, summarize_prepared
from app.web.repository import Scope

TARGET, DISH, DEPT, UNIT = [UUID(int=i) for i in range(11, 15)]


def edge(parent, child, amount, output="1"):
    return dict(
        product_id=parent,
        ingredient_id=child,
        chart_id=parent,
        name=str(parent),
        ingredient=str(child),
        unit="порц",
        ingredient_unit="кг",
        amount_in=Decimal(amount),
        assembled_amount=Decimal(output),
        chart_details={
            "effective_direct_writeoff_store_specification": {"inverse": False, "departments": []},
            "product_size_assembly_strategy": "COMMON",
        },
        item_details={"store_specification": None, "product_size_id": None},
    )


def inputs():
    start, end = recent_period()
    graph = [edge(DISH, TARGET, "0.137", "3")]
    sales = [
        dict(
            dish_id=DISH,
            dish="Блюдо",
            department_id=DEPT,
            quantity=Decimal("137.57"),
            invalid_quantity=False,
        )
    ]
    price = dict(product_id=TARGET, department_id=DEPT, unit_id=UNIT, delta=Decimal("93.5018"))
    product = dict(id=TARGET, main_unit_id=UNIT)
    coverage = [
        dict(business_date=start + timedelta(days=i), observed_at=datetime.now(UTC), checks=[])
        for i in range((end - start).days + 1)
    ]
    return graph, sales, price, product, coverage, start, end, end


@pytest.mark.parametrize(
    "failure",
    [
        "none",
        "unit",
        "price",
        "missing_day",
        "mismatch",
        "returns",
        "cycle",
        "no_sales",
        "no_graph",
        "zero",
        "negative",
    ],
)
def test_prepared_summary_equals_existing_calculation(failure):
    graph, sales, price, product, coverage, start, end, recipe_day = inputs()
    if failure == "unit":
        price["unit_id"] = uuid4()
    elif failure == "price":
        price["delta"] = None
    elif failure == "missing_day":
        coverage.pop()
    elif failure == "mismatch":
        coverage[0]["checks"] = [
            dict(report="dishes", exact_match=False, differences=[dict(department_id=str(DEPT))])
        ]
    elif failure == "returns":
        sales[0]["invalid_quantity"] = True
    elif failure == "cycle":
        graph.append(edge(DISH, DISH, "1"))
    elif failure == "no_sales":
        sales = []
    elif failure == "no_graph":
        graph, sales, recipe_day = [], [], None
    elif failure == "zero":
        price["delta"] = Decimal(0)
    elif failure == "negative":
        price["delta"] = Decimal("-25.577")
    facts = prepare_product_facts(
        [TARGET], graph, sales, {TARGET: product}, coverage, start, end, recipe_day
    )
    actual = summarize_prepared(price, facts[0], coverage, start, end, recipe_day, [DEPT])
    expected = calculate_impact(
        graph, sales, price, product, coverage, start, end, recipe_day, departments=[DEPT]
    )
    assert actual == dict(
        weekly_delta=expected["totals"]["weekly_delta"],
        included_positions=len(expected["rows"]),
        excluded_positions=len(expected["excluded"]),
        reason="; ".join(expected["blockers"]) or expected["empty_reason"],
    )


def test_arbitrary_department_combinations_preserve_scope_and_decimal_order():
    graph, sales, price, product, coverage, start, end, recipe_day = inputs()
    other, third = uuid4(), uuid4()
    sales.extend(
        [
            dict(sales[0], department_id=other, quantity=Decimal("27.998")),
            dict(sales[0], department_id=third, quantity=Decimal("30000")),
        ]
    )
    facts = prepare_product_facts(
        [TARGET], graph, sales, {TARGET: product}, coverage, start, end, recipe_day
    )
    for selected in ([DEPT], [other], [DEPT, other], [DEPT, third]):
        actual = summarize_prepared(price, facts[0], coverage, start, end, recipe_day, selected)
        expected = calculate_impact(
            graph,
            [sale for sale in sales if sale["department_id"] in selected],
            price,
            product,
            coverage,
            start,
            end,
            recipe_day,
            departments=selected,
        )
        assert actual["weekly_delta"] == expected["totals"]["weekly_delta"]
        assert actual["included_positions"] == len(selected)


def test_missing_product_is_distinct_from_unprepared_product():
    _, _, price, _, coverage, start, end, recipe_day = inputs()
    facts = prepare_product_facts([TARGET], [], [], {}, coverage, start, end, recipe_day)
    missing = summarize_prepared(price, facts[0], coverage, start, end, recipe_day, [DEPT])
    pending = summarize_prepared(price, None, coverage, start, end, recipe_day, [DEPT])
    assert missing["weekly_delta"] is pending["weekly_delta"] is None
    assert missing["reason"] == "Товар отсутствует в номенклатуре."
    assert pending["reason"] == "Расчёт обновляется."


@pytest.fixture
def prepared_db(db, bundle):
    register_sources(db, bundle[0])
    db.row_factory = dict_row
    return db


def prepared_fixture(db):
    graph, sales, price, product, coverage, start, end, recipe_day = inputs()
    hidden = uuid4()
    sales.append(dict(sales[0], department_id=hidden, quantity=Decimal("99999")))
    prepared = dict(
        id=uuid4(),
        source_id="primary",
        prepared_at=datetime.now(UTC),
        period_start=start,
        period_end=end,
        recipe_day=recipe_day,
        revision=source_revision(db),
        coverage=json_value(coverage),
        selling_department_ids=[str(DEPT), str(hidden)],
        products=prepare_product_facts(
            [TARGET], graph, sales, {TARGET: product}, coverage, start, end, recipe_day
        ),
    )
    scope = Scope({"role": "manager"}, ({"id": DEPT, "name": "Visible"},), None, (), ())
    return prepared, scope, price


def test_reader_is_scoped_and_never_queries_source_graphs_or_sales_rows(prepared_db):
    prepared, scope, price = prepared_fixture(prepared_db)
    publish_prepared(prepared_db, prepared)
    statements = []

    class GuardedDB:
        def execute(self, query, params=None):
            assert "sales_report_rows" not in query and "assembly_chart" not in query
            statements.append((query, params))
            return prepared_db.execute(query, params)

    report = add_prepared_impacts(GuardedDB(), scope, {"rows": [price]})
    expected = summarize_prepared(
        price,
        prepared["products"][0],
        inputs()[4],
        prepared["period_start"],
        prepared["period_end"],
        prepared["recipe_day"],
        [DEPT],
    )
    assert report["rows"][0]["impact"] == expected
    assert report["impact_period"]["status"] == "ready"
    facts_query = next(item for item in statements if "jsonb_each" in item[0])
    assert facts_query[1][0] == [str(DEPT)]


def test_unavailable_and_stale_preparation_never_returns_old_delta(prepared_db):
    prepared, scope, price = prepared_fixture(prepared_db)
    cold = add_prepared_impacts(prepared_db, scope, {"rows": [deepcopy(price)]})
    assert cold["impact_period"]["status"] == "pending"
    assert cold["rows"][0]["impact"]["weekly_delta"] is None
    prepared["revision"]["version"] = -1
    publish_prepared(prepared_db, prepared)
    stale = add_prepared_impacts(prepared_db, scope, {"rows": [price]})
    assert stale["impact_period"]["status"] == "pending"
    assert stale["rows"][0]["impact"]["weekly_delta"] is None


def test_warehouse_restriction_is_checked_before_any_prepared_read():
    scope = Scope({"role": "owner", "warehouse_scope_mode": "selected"}, (), None, (), ())

    class NoDB:
        def execute(self, *args):
            pytest.fail("The authorization guard must run before database access")

    with pytest.raises(HTTPException) as error:
        add_prepared_impacts(NoDB(), scope, {"rows": []})
    assert error.value.status_code == 403


def test_failed_publication_retains_complete_previous_generation(prepared_db):
    prepared, _, _ = prepared_fixture(prepared_db)
    publish_prepared(prepared_db, prepared)
    broken = deepcopy(prepared)
    broken["id"] = uuid4()
    broken["products"].append(deepcopy(broken["products"][0]))
    with pytest.raises(psycopg.errors.UniqueViolation):
        publish_prepared(prepared_db, broken)
    row = prepared_db.execute("SELECT id FROM chaika.purchase_impact_prepared").fetchone()
    assert row["id"] == prepared["id"]
    assert (
        prepared_db.execute(
            "SELECT count(*) AS n FROM chaika.purchase_impact_prepared_products"
        ).fetchone()["n"]
        == 1
    )


def test_revision_changes_on_successful_sync_and_local_day(prepared_db):
    before = source_revision(prepared_db)
    prepared_db.execute(
        "INSERT INTO chaika.sync_runs (id,job,status,finished_at) "
        "VALUES (%s,'inventory','succeeded',now())",
        (uuid4(),),
    )
    assert source_revision(prepared_db) != before
    today = datetime.fromisoformat(before["today"]).date()
    assert source_revision(prepared_db, today=today + timedelta(days=1))["today"] != before["today"]


def test_revision_changes_when_existing_sales_day_is_republished(prepared_db):
    day = recent_period()[1]
    original, replacement = uuid4(), uuid4()
    for key in (original, replacement):
        prepared_db.execute(
            "INSERT INTO chaika.sales_report_sets "
            "(id,source_id,business_date,observed_at,checks) "
            "VALUES (%s,'primary',%s,now(),'[]'::jsonb)",
            (key, day),
        )
    prepared_db.execute(
        "INSERT INTO chaika.sales_report_days (source_id,business_date,current_set_id) "
        "VALUES ('primary',%s,%s)",
        (day, original),
    )
    before = source_revision(prepared_db)
    prepared_db.execute(
        "UPDATE chaika.sales_report_days SET current_set_id=%s "
        "WHERE source_id='primary' AND business_date=%s",
        (replacement, day),
    )
    assert source_revision(prepared_db) != before


def test_prepared_tables_are_not_readable_by_anonymous_role(prepared_db):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), prepared_db.transaction():
        prepared_db.execute("SET LOCAL ROLE anon")
        prepared_db.execute("SELECT * FROM chaika.purchase_impact_prepared")


def test_shutdown_interrupts_product_preparation():
    graph, sales, _, product, coverage, start, end, recipe_day = inputs()
    stop = Event()
    stop.set()
    with pytest.raises(RuntimeError, match="purchase_impact_preparation_interrupted"):
        prepare_product_facts(
            [TARGET], graph, sales, {TARGET: product}, coverage, start, end, recipe_day, stop=stop
        )


def test_empty_database_build_is_a_complete_empty_generation(prepared_db):
    prepared = build_prepared(prepared_db)
    assert prepared["products"] == []
    publish_prepared(prepared_db, prepared)
    scope = Scope({"role": "owner"}, ({"id": DEPT},), None, (), ())
    result = add_prepared_impacts(prepared_db, scope, {"rows": []})
    assert result["impact_period"]["status"] == "ready"
