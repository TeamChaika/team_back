"""A partial OLAP period must not be presented as a complete financial total."""

from contextlib import contextmanager
from datetime import date
from uuid import UUID

from psycopg.rows import dict_row
from test_reference_sync import db as db
from test_sales_import import review as review

from app.core.config import Settings
from app.import_sales_review import parse_review, publish
from app.services.sync_jobs import write_json
from app.sync_references import Source, register_sources
from app.web.repository import Repository, Scope


def repository(db, review):
    register_sources(db, [Source("primary", "Test", "https://test.example/api", "a" * 64)])
    review[1]["source_fingerprint"] = "a" * 64
    write_json(review[0] / "manifest.json", review[1])
    publish(db, parse_review(review[0], "a" * 64))
    repo = Repository(Settings(_env_file=None))

    @contextmanager
    def connection():
        with db.cursor(row_factory=dict_row) as cursor:
            yield cursor

    repo.connection = connection
    scope = Scope({"role": "owner"}, ({"id": UUID(int=1)},), None, (), ())
    return repo, scope


def test_partial_period_withholds_totals_but_keeps_daily_details(db, review):
    repo, scope = repository(db, review)
    result = repo.sales(scope, "daily", date(2026, 9, 1), date(2026, 9, 10))
    assert result["loaded_dates"] == ["2026-09-10"]
    assert result["missing_dates"] == [f"2026-09-{day:02d}" for day in range(1, 10)]
    assert not result["complete"]
    assert all(value is None for value in result["totals"].values())
    assert result["rows"][0]["revenue"] == "123.45000000000000000001"


def test_complete_period_keeps_precise_totals(db, review):
    repo, scope = repository(db, review)
    result = repo.sales(scope, "daily", date(2026, 9, 10), date(2026, 9, 10))
    assert result["complete"] and result["missing_dates"] == []
    assert result["totals"]["revenue"] == "123.45000000000000000001"


def test_loaded_day_without_rows_for_selected_restaurant_is_not_a_missing_day(db, review):
    repo, _ = repository(db, review)
    scope = Scope({"role": "manager"}, ({"id": UUID(int=999)},), None, (), ())
    result = repo.sales(scope, "daily", date(2026, 9, 10), date(2026, 9, 10))
    assert result["rows"] == [] and result["complete"]
    assert result["loaded_dates"] == ["2026-09-10"]


def test_missing_period_does_not_show_hour_or_payment_totals(db, review):
    repo, scope = repository(db, review)
    for kind in ("hours", "payments"):
        result = repo.sales(scope, kind, date(2026, 9, 9), date(2026, 9, 10))
        assert result["totals"]["revenue"] is None
        assert result["hourly"] == []
        assert all(g["totals"]["revenue"] is None for g in result["payment_groups"])


def test_discount_restaurant_groups_keep_exact_totals_scope_and_drilldown_rows(db, review):
    import hashlib
    from decimal import Decimal

    root, manifest = review
    path = root / manifest["reports"]["discounts"]["raw_file"]
    path.write_text(
        path.read_text().replace(
            '"DishDiscountSumInt":',
            '"DiscountSum": 3.00000000000000000001, "DishDiscountSumInt":',
        )
    )
    manifest["reports"]["discounts"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["reports"]["discounts"]["request"]["aggregateFields"].append("DiscountSum")
    repo, _ = repository(db, review)
    report = db.execute("SELECT id FROM chaika.sales_reports WHERE kind='discounts'").fetchone()[0]
    # Multiple rows for one restaurant and a different restaurant with the same label.
    for ordinal, department, discount in [
        (1, 1, "4.00000000000000000002"),
        (2, 2, "5.25"),
        (3, 3, "9999"),
    ]:
        db.execute(
            "INSERT INTO chaika.sales_report_rows"
            "(report_id,ordinal,department_id,revenue,discount,dimensions) "
            'VALUES(%s,%s,%s,10,%s,\'{"ItemSaleEventDiscountType":"test"}\')',
            (report, ordinal, UUID(int=department), Decimal(discount)),
        )
    scope = Scope(
        {"role": "manager"},
        tuple({"id": UUID(int=n), "name": "Same name"} for n in [1, 2]),
        None,
        (),
        (),
    )
    result = repo.sales(scope, "discounts", date(2026, 9, 10), date(2026, 9, 10))
    groups = {g["department_id"]: g for g in result["discount_groups"]}
    assert set(groups) == {str(UUID(int=1)), str(UUID(int=2))}
    assert len(result["rows"]) == 3
    assert groups[str(UUID(int=1))]["totals"]["discount"] == "7.00000000000000000003"
    assert groups[str(UUID(int=2))]["totals"]["discount"] == "5.25"
    assert result["totals"]["discount"] == "12.25000000000000000003"
    assert all(r["report_id"] == str(report) for r in result["rows"])
    assert {r["ordinal"] for r in result["rows"]} == {0, 1, 2}
    partial = repo.sales(scope, "discounts", date(2026, 9, 9), date(2026, 9, 10))
    assert partial["rows"] == result["rows"]
    assert all(
        value is None for group in partial["discount_groups"] for value in group["totals"].values()
    )
    assert (
        repo.sales(scope, "discounts", date(2026, 8, 1), date(2026, 8, 1))["discount_groups"] == []
    )


def test_intraday_observation_stays_preliminary_on_later_dates(db, review):
    for info in review[1]["reports"].values():
        info["received_at"] = "2026-09-10T20:59:59Z"
    repo, scope = repository(db, review)
    result = repo.sales(scope, "daily", date(2026, 9, 10), date(2026, 9, 10))
    assert result["complete"]  # Every requested day has saved rows.
    assert result["partial_days"][0]["date"] == "2026-09-10"
    assert result["totals"]["revenue"] == "123.45000000000000000001"
    for info in review[1]["reports"].values():
        info["received_at"] = "2026-09-10T21:00:00Z"
    write_json(review[0] / "manifest.json", review[1])
    publish(db, parse_review(review[0], "a" * 64))
    refreshed = repo.sales(scope, "daily", date(2026, 9, 10), date(2026, 9, 10))
    assert refreshed["partial_days"] == []
