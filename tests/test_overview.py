"""Overview totals, aligned periods and drilldowns on an isolated database only."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_reference_sync import db as db

from app.sync_references import Source, register_sources
from app.web.overview import buckets, change, read_overview, totals
from app.web.repository import Scope

A, B, HIDDEN = UUID(int=1), UUID(int=2), UUID(int=3)


def scope(*ids):
    return Scope(
        {"role": "manager"}, tuple({"id": i, "name": "Same name"} for i in ids), None, (), ()
    )


def seed_day(db, day, daily, dishes=(), kinds=("daily", "dishes"), checks=(), observed=None):
    observed = observed or datetime.now(UTC)
    register_sources(db, [Source("primary", "Test", "https://test.example/api", "a" * 64)])
    sid = uuid4()
    db.execute(
        "INSERT INTO chaika.sales_report_sets(id,source_id,business_date,observed_at,checks) "
        "VALUES(%s,'primary',%s,%s,%s)",
        (sid, day, observed, Jsonb(checks)),
    )
    db.execute(
        "INSERT INTO chaika.sales_report_days(source_id,business_date,current_set_id) "
        "VALUES('primary',%s,%s)",
        (day, sid),
    )
    for kind in kinds:
        rid = uuid4()
        rows = daily if kind == "daily" else dishes
        db.execute(
            "INSERT INTO chaika.sales_reports"
            "(id,set_id,kind,request,observed_at,raw,sha256,row_count) "
            "VALUES(%s,%s,%s,'{}',%s,''::bytea,encode(sha256(''::bytea),'hex'),%s)",
            (rid, sid, kind, observed, len(rows)),
        )
        for ordinal, r in enumerate(rows):
            db.execute(
                "INSERT INTO chaika.sales_report_rows(report_id,ordinal,department_id,revenue,"
                "cost,checks,guests,quantity,dimensions) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    rid,
                    ordinal,
                    r.get("department_id", A),
                    r["revenue"],
                    r.get("cost", 1),
                    r.get("checks", 1),
                    r.get("guests", 1),
                    r.get("quantity", 1),
                    Jsonb(r.get("dimensions", {})),
                ),
            )


def read(db, start, end, grain="day", ids=(A, B)):
    # psycopg.Connection.execute returns independent cursors, as the production pool does.
    original = db.row_factory
    db.row_factory = dict_row
    try:
        return read_overview(
            db, scope(*ids), date.fromisoformat(start), date.fromisoformat(end), grain
        )
    finally:
        db.row_factory = original


def test_weighted_averages_and_precise_changes():
    result = totals(
        [
            {
                "revenue": Decimal("100.00000000000000000001"),
                "cost": Decimal("40"),
                "checks": 1,
                "guests": 2,
            },
            {"revenue": Decimal("300"), "cost": Decimal("80"), "checks": 9, "guests": 18},
        ],
        2,
    )
    assert result["revenue"] == Decimal("400.00000000000000000001")
    assert result["average_check"] == Decimal("40.000000000000000000001")
    assert result["gross_profit"] == Decimal("280.00000000000000000001")
    assert result["guests_per_day"] == 10
    assert change(Decimal("100.00000000000000000001"), Decimal("100"))["absolute"] == Decimal(
        "0.00000000000000000001"
    )
    assert change(1, 0) == {"absolute": 1, "percent": None}
    assert change(1, -1) == {"absolute": 2, "percent": None}
    assert change(None, 1) == {"absolute": None, "percent": None}
    assert all(v is None for v in totals([], 1).values())
    unknown = totals([{"revenue": Decimal(100), "cost": None, "checks": 0, "guests": 1}], 1)
    assert unknown["cost"] is None and unknown["gross_profit"] is None
    assert unknown["margin"] is None and unknown["average_check"] is None


def test_periods_and_restaurants_use_same_scope_and_never_merge_names(db):
    for day, amount in [(8, "10.00000000000000000001"), (9, "20"), (10, "30"), (11, "40")]:
        seed_day(
            db,
            f"2026-09-{day:02d}",
            [
                {"department_id": A, "revenue": Decimal(amount), "checks": 2},
                {"department_id": B, "revenue": 100, "checks": 10},
                {"department_id": HIDDEN, "revenue": 99999},
            ],
        )
    result = read(db, "2026-09-10", "2026-09-11")
    assert result["previous"]["start"] == date(2026, 9, 8)
    assert result["previous"]["end"] == date(2026, 9, 9)
    assert result["current"]["totals"]["revenue"] == 270
    assert result["previous"]["totals"]["revenue"] == Decimal("230.00000000000000000001")
    assert result["current"]["totals"]["average_check"] == Decimal("11.25")
    assert len(result["restaurants"]) == 2
    assert {r["department_id"] for r in result["restaurants"]} == {A, B}
    assert sum(r["totals"]["revenue"] for r in result["restaurants"]) == 270
    assert read(db, "2026-09-10", "2026-09-11", ids=(A,))["current"]["totals"]["revenue"] == 70


def test_missing_days_keep_gaps_and_disable_period_totals(db):
    seed_day(db, "2026-09-08", [{"revenue": 100}])
    seed_day(db, "2026-09-10", [{"revenue": 300}])
    result = read(db, "2026-09-10", "2026-09-11")
    assert result["current"]["missing_dates"] == [date(2026, 9, 11)]
    assert result["previous"]["missing_dates"] == [date(2026, 9, 9)]
    assert all(v is None for v in result["current"]["totals"].values())
    assert all(v["percent"] is None for v in result["changes"].values())
    assert result["trend"][0]["current"]["totals"]["revenue"] == 300
    assert result["trend"][1]["current"]["totals"]["revenue"] is None
    assert result["restaurants"][0]["totals"]["revenue"] is None
    assert (
        read(db, "2026-09-10", "2026-09-11", "week")["trend"][0]["current"]["totals"]["revenue"]
        is None
    )


def test_missing_previous_does_not_hide_current_and_empty_reports_are_loaded(db):
    seed_day(db, "2026-09-10", [{"revenue": 300}])
    result = read(db, "2026-09-10", "2026-09-10")
    assert result["current"]["totals"]["revenue"] == 300
    assert result["changes"]["revenue"]["percent"] is None
    selected = read(db, "2026-09-10", "2026-09-10", ids=(B,))
    assert selected["current"]["complete"]
    assert selected["current"]["totals"]["revenue"] is None
    assert selected["restaurants"] == []


def test_top_dishes_group_uuid_across_renames_rank_whole_period_and_scope(db):
    def dish(key, name, revenue, department=A):
        return {
            "department_id": department,
            "revenue": Decimal(revenue),
            "dimensions": {"DishId": key, "DishName": name},
        }

    seed_day(db, "2026-09-08", [], [dish("one", "Old name", "50")])
    seed_day(db, "2026-09-09", [], [dish("one", "Old name", "50")])
    seed_day(db, "2026-09-10", [], [dish("one", "Old name", "60.00000000000000000001")])
    rows = [dish("one", "New name", "60"), dish("one", "New name", "60", B)]
    rows += [dish(str(n), "Identical name", str(n)) for n in range(1, 8)]
    rows += [dish("hidden", "Hidden dish", "99999", HIDDEN)]
    seed_day(db, "2026-09-11", [], rows)
    result = read(db, "2026-09-10", "2026-09-11")
    top = result["top_dishes"]
    assert len(top) == 5
    assert top[0]["dish_id"] == "one" and top[0]["dish_name"] == "New name"
    assert top[0]["revenue"] == Decimal("180.00000000000000000001")
    assert top[0]["previous_revenue"] == 100
    assert top[0]["quantity"] == 3
    assert [d["dish_id"] for d in top[1:]] == ["7", "6", "5", "4"]
    assert all(d["change"]["percent"] is None for d in top[1:])
    selected = read(db, "2026-09-10", "2026-09-11", ids=(A,))
    assert selected["top_dishes"][0]["revenue"] == Decimal("120.00000000000000000001")
    assert read(db, "2026-09-10", "2026-09-12")["top_dishes"] == []


def test_dish_coverage_independent_and_reconciliation_does_not_leak_other_venues(db):
    issues = [
        {
            "exact_match": False,
            "report": "payments",
            "field": "revenue",
            "differences": [
                {"department_id": str(A), "daily": "20", "actual": "19", "delta": "-1"},
                {
                    "department_id": str(HIDDEN),
                    "daily": "123456",
                    "actual": "0",
                    "delta": "-123456",
                },
            ],
        }
    ]
    seed_day(db, "2026-09-10", [{"revenue": 20}], kinds=("daily",), checks=issues)
    result = read(db, "2026-09-10", "2026-09-10")
    assert result["current"]["complete"]
    assert not result["dish_current"]["complete"]
    assert len(result["current"]["reconciliation_issues"]) == 1
    assert result["current"]["reconciliation_issues"][0]["department_id"] == str(A)


@pytest.mark.parametrize(
    "grain,lengths", [("day", [1] * 10), ("week", [2, 7, 1]), ("month", [3, 7])]
)
def test_calendar_buckets_clipped_at_period_edges(grain, lengths):
    result = buckets(date(2026, 8, 29), date(2026, 9, 7), grain)
    assert [len(group) for group in result] == lengths
    assert result[0][0] == date(2026, 8, 29)
    assert result[-1][-1] == date(2026, 9, 7)


def test_dish_drilldown_keeps_exact_filter_and_restaurant_scope(db):
    from contextlib import contextmanager

    from app.core.config import Settings
    from app.web.repository import Repository

    rows = [
        {"revenue": 100, "dimensions": {"DishId": "one", "DishName": "Same"}},
        {"revenue": 200, "dimensions": {"DishId": "two", "DishName": "Same"}},
        {
            "revenue": 99999,
            "department_id": HIDDEN,
            "dimensions": {"DishId": "one", "DishName": "Same"},
        },
        {"revenue": 50, "dimensions": {"DishName": "No ID"}},
        {"revenue": 7, "dimensions": {}},
        {"revenue": 8, "dimensions": {"DishId": "", "DishName": ""}},
    ]
    seed_day(db, "2026-09-10", [], rows)
    repo = Repository(Settings(_env_file=None))

    @contextmanager
    def connection():
        with db.cursor(row_factory=dict_row) as cursor:
            yield cursor

    repo.connection = connection
    day = date(2026, 9, 10)
    exact = repo.sales(scope(A), "dishes", day, day, dish_id="one")
    assert exact["totals"]["revenue"] == "100"
    assert len(exact["rows"]) == 1
    assert exact["rows"][0]["dimensions"]["DishId"] == "one"
    fallback = repo.sales(scope(A), "dishes", day, day, dish_name="No ID")
    assert fallback["totals"]["revenue"] == "50"
    unnamed = repo.sales(scope(A), "dishes", day, day, dish_name="")
    assert unnamed["totals"]["revenue"] == "15"
    top = read(db, "2026-09-10", "2026-09-10", ids=(A,))["top_dishes"]
    assert [r["revenue"] for r in top if not r["dish_name"]] == [Decimal(15)]
    assert repo.sales(scope(A), "dishes", day, day, dish_id="' OR true --")["rows"] == []


def test_overview_marks_saved_intraday_totals_as_preliminary(db):
    seed_day(db, date(2026, 9, 10), [{"revenue": 100}], observed="2026-09-10T12:00:00Z")
    result = read(db, "2026-09-10", "2026-09-10")
    assert result["current"]["partial_days"][0]["date"] == date(2026, 9, 10)
    assert result["current"]["totals"]["revenue"] == 100
    assert result["dish_current"]["partial_days"]
    assert result["previous"]["partial_days"] == []
