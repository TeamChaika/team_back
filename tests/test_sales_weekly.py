"""Weekly RAW must remain exact, shared, scoped and resumable by committed day."""

import hashlib
import json
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from threading import Event
from uuid import UUID, uuid4

import pytest
from test_reference_sync import db as db
from test_sales_coverage import repository
from test_sales_import import review as review

from app.import_sales_review import KINDS, parse_review, publish
from app.sync_references import Source, SyncError, register_sources
from app.sync_sales_history import missing_windows, run_prefetched_history
from app.web.repository import Scope


@pytest.fixture
def week(review):
    root, manifest = review
    manifest.update(date_to="2026-09-16", capture_id=str(uuid4()))
    for info in manifest["reports"].values():
        info["request"]["filters"]["OpenDate.Typed"]["to"] = "2026-09-17T00:00:00"
        path = root / info["raw_file"]
        original = path.read_text()
        row = original[original.index("[{", 0) + 1 : original.index("}]") + 1]
        rows = [row.replace("2026-09-10", f"2026-09-{day:02d}") for day in range(10, 17)]
        path.write_text('{"data":[' + ",".join(rows) + '],"summary":[]}')
        info.update(rows=7, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root, manifest


def mutate_report(root, manifest, kind, old, new):
    path = root / manifest["reports"][kind]["raw_file"]
    path.write_text(path.read_text().replace(old, new))
    manifest["reports"][kind]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest))


def test_week_splits_by_date_without_rounding_and_shares_source_identity(week):
    root, _ = week
    bundles = [
        parse_review(root, "test-source", business_date=date(2026, 9, d)) for d in range(10, 17)
    ]
    assert len({b["id"] for b in bundles}) == 7
    assert len({b["reports"]["daily"]["capture_id"] for b in bundles}) == 1
    for day, bundle in zip(range(10, 17), bundles, strict=True):
        for report in bundle["reports"].values():
            assert len(report["rows"]) == 1
            assert report["rows"][0]["OpenDate.Typed"] == f"2026-09-{day:02d}"
            assert report["rows"][0]["DishDiscountSumInt"] == Decimal("123.45000000000000000001")
            assert len(json.loads(report["raw"])["data"]) == 7


@pytest.mark.parametrize("bad_date", ["2026-09-17", "20260910", "2026-09-10T12:00:00"])
def test_week_rejects_rows_outside_period_or_noncanonical_dates(week, bad_date):
    root, manifest = week
    mutate_report(root, manifest, "daily", '"2026-09-10"', json.dumps(bad_date))
    with pytest.raises(ValueError, match="sales_date_mismatch"):
        parse_review(root, "test-source")


def test_discrepancy_is_explicit_exact_and_never_silently_fixed(week):
    root, manifest = week
    mutate_report(
        root, manifest, "payments", "123.45000000000000000001", "1.000000000000000000000000000001"
    )
    with pytest.raises(ValueError, match="sales_reconciliation_failed"):
        parse_review(root, "test-source")
    bundle = parse_review(root, "test-source", allow_discrepancies=True)
    issue = next(c for c in bundle["checks"] if not c["exact_match"])["differences"][0]
    assert issue["delta"] == "-122.450000000000000000009999999999"
    assert issue["daily"] == "123.45000000000000000001"
    assert bundle["reports"]["payments"]["rows"][0]["DishDiscountSumInt"] == Decimal(
        issue["actual"]
    )


def test_week_database_preserves_one_raw_per_report_and_idempotent_daily_commits(db, week):
    root, _ = week
    register_sources(db, [Source("primary", "Test", "https://test.example/api", "a" * 64)])
    for day in range(10, 17):
        bundle = parse_review(root, "test-source", business_date=date(2026, 9, day))
        assert publish(db, bundle)["status"] == "imported"
        assert publish(db, bundle)["status"] == "already_imported"
    assert db.execute("SELECT count(*) FROM chaika.sales_report_captures").fetchone()[0] == 7
    assert db.execute(
        "SELECT count(*),bool_and(raw IS NULL AND source_capture_id IS NOT NULL) "
        "FROM chaika.sales_reports"
    ).fetchone() == (49, True)
    assert db.execute(
        "SELECT count(*),bool_or(reviewed) FROM chaika.sales_report_sets"
    ).fetchone() == (7, False)
    assert db.execute("SELECT revenue FROM chaika.sales_report_rows LIMIT 1").fetchone()[
        0
    ] == Decimal("123.45000000000000000001")
    captures = dict(db.execute("SELECT kind,raw FROM chaika.sales_report_captures").fetchall())
    for kind, raw in captures.items():
        assert raw == (root / f"{kind}.json").read_bytes()


def test_reconciliation_details_respect_restaurant_scope(db, review):
    root, manifest = review
    mutate_report(root, manifest, "payments", "123.45000000000000000001", "100")
    # Build the usual repository with a strict, reconciled initial day.
    mutate_report(
        root,
        manifest,
        "payments",
        '"DishDiscountSumInt": 100',
        '"DishDiscountSumInt": 123.45000000000000000001',
    )
    repo, allowed = repository(db, review)
    mutate_report(root, manifest, "payments", "123.45000000000000000001", "100")
    publish(db, parse_review(root, "a" * 64, allow_discrepancies=True))
    day = date(2026, 9, 10)
    assert len(repo.sales(allowed, "daily", day, day)["reconciliation_issues"]) == 1
    foreign = Scope({"role": "manager"}, ({"id": UUID(int=999)},), None, (), ())
    result = repo.sales(foreign, "daily", day, day)
    assert result["rows"] == [] and result["reconciliation_issues"] == []


def test_prefetch_overlaps_publication_but_uses_one_collector_and_committed_progress():
    days = [date(2023, 7, 14) + timedelta(days=i) for i in range(15)]
    next_capture, finish_capture = Event(), Event()
    calls, writes, checkpoints = [], [], []
    stop = Event()

    def collect(first, last):
        calls.append((first, last))
        if len(calls) == 2:
            next_capture.set()
            assert finish_capture.wait(5)
        return first

    def save(root, day):
        if day == days[0]:
            assert next_capture.wait(5)  # next request started while this day's DB write runs
            finish_capture.set()
        writes.append(day)
        return {
            "status": "imported",
            "rows": dict.fromkeys(KINDS, 1),
            "warning_count": int(day == days[0]),
        }

    report = run_prefetched_history(
        days, {}, collect, save, lambda r: checkpoints.append(deepcopy(r)), stop
    )
    assert report["status"] == "succeeded"
    assert writes == days and calls == [
        (days[0], days[6]),
        (days[7], days[13]),
        (days[14], days[14]),
    ]
    assert report["counts"]["warning_days"] == 1
    assert [r["counts"]["completed_days"] for r in checkpoints] == [0, *range(1, 16), 15]


def test_prefetch_database_failure_drains_logout_and_resume_skips_only_committed_days():
    days = [date(2023, 7, 14) + timedelta(days=i) for i in range(9)]
    started, logged_out, stop = Event(), Event(), Event()

    def collect(first, last):
        if first == days[7]:
            started.set()
            assert stop.wait(5)
            logged_out.set()
            raise SyncError("sales_history_interrupted")
        return first

    def save(root, day):
        if day == days[1]:
            assert started.wait(5)
            raise RuntimeError("private failure")
        return {"status": "imported", "rows": dict.fromkeys(KINDS, 1)}

    report = run_prefetched_history(days, {}, collect, save, lambda _: None, stop)
    assert logged_out.is_set() and report["status"] == "failed"
    assert report["counts"]["completed_days"] == 1 and report["counts"]["next_date"] == str(days[1])
    assert "private" not in json.dumps(report)
    assert missing_windows(days, {days[0]: {}, days[7]: {}}) == [
        (days[1], days[6]),
        (days[8], days[8]),
    ]
