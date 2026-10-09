import asyncio
import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import date, timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from test_sales_import import review as review

from app import sync_sales_history as history
from app.import_sales_review import KINDS, parse_review
from app.integrations.iiko.errors import IikoError
from app.sync_references import SyncError


def days(count=3):
    return [date(2026, 8, 1) + timedelta(days=i) for i in range(count)]


def result():
    return {"status": "imported", "rows": {kind: 1 for kind in KINDS}}


def test_resume_only_requests_missing_days_and_counts_committed_reports():
    calls, checkpoints = [], []

    def sync(day):
        calls.append(day)
        return result()

    report = history.run_history(
        days(),
        {days()[1]: result()["rows"]},
        sync,
        lambda report: checkpoints.append(deepcopy(report)),
        Event(),
    )
    assert calls == [days()[0], days()[2]]
    assert report["status"] == "succeeded"
    assert report["counts"]["completed_days"] == 3
    assert report["counts"]["documents_read"] == 21
    assert report["counts"]["resumed_days"] == 1
    assert checkpoints[0]["counts"]["completed_through"] is None
    assert report["counts"]["next_date"] is None


@pytest.mark.parametrize("failure", [ValueError("private password"), SyncError("sales_failed")])
def test_failed_day_is_not_counted_and_can_be_resumed_without_reloading_prior_days(failure):
    def sync(day):
        if day == days()[1]:
            raise failure
        return result()

    report = history.run_history(days(), {}, sync, lambda _: None, Event())
    assert report["status"] == "failed"
    assert report["counts"]["completed_days"] == 1
    assert report["counts"]["next_date"] == "2026-08-02"
    assert "private" not in json.dumps(report)


def test_stop_after_commit_preserves_the_day():
    stop = Event()

    def sync(day):
        stop.set()
        return result()

    report = history.run_history(days(), {}, sync, lambda _: None, stop)
    assert report["status"] == "interrupted"
    assert report["counts"]["completed_days"] == 1


def test_partial_result_cannot_advance_coverage():
    report = history.run_history(
        days(),
        {},
        lambda _: {"status": "imported", "rows": {"daily": 4}},
        lambda _: None,
        Event(),
    )
    assert report["status"] == "failed"
    assert report["counts"]["completed_days"] == 0


@pytest.mark.parametrize(
    "start,end",
    [
        (date(1999, 1, 1), date(2026, 8, 1)),
        (date(2026, 8, 2), date(2026, 8, 1)),
        (date(2026, 8, 1), date(9999, 1, 1)),
    ],
)
def test_invalid_or_open_period_rejected(start, end):
    with pytest.raises(SyncError, match="invalid_sales_history_period"):
        history.history_days(start, end)


def test_new_date_preserves_all_approved_groupings_and_filters(review):
    _, manifest = review
    template = manifest["reports"]["hours"]["request"]
    saved = deepcopy(template)
    request = history.request_for_day(template, date(2023, 3, 1)).iiko_body()
    assert template == saved
    assert request["groupByRowFields"] == template["groupByRowFields"]
    assert request["filters"]["OpenDate.Typed"]["to"] == "2023-03-02T00:00:00"
    assert request["buildSummary"] is False


def fake_collection(monkeypatch, review, failure=None, logout_failure=False):
    original, manifest = review
    calls = []
    templates = {kind: info["request"] for kind, info in manifest["reports"].items()}

    class Client:
        def __init__(self, settings):
            pass

        async def aclose(self):
            calls.append("closed")

    class Auth:
        def __init__(self, settings, client):
            pass

        async def download_daily_sales(self, path, *, query):
            calls.append(path.stem)
            if failure and path.stem == "payments":
                raise IikoError("iiko_test_failure", "private")
            raw = (original / path.name).read_bytes()
            path.write_bytes(raw)
            return SimpleNamespace(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))

        async def logout(self):
            calls.append("logout")
            if logout_failure:
                raise IikoError("iiko_logout_failed", "private")
            return SimpleNamespace(state="logged_out")

    monkeypatch.setattr(history, "IikoClient", Client)
    monkeypatch.setattr(history, "IikoAuthService", Auth)
    return templates, calls


def test_collection_preserves_sequential_raw_and_confirmed_logout(review, monkeypatch):
    templates, calls = fake_collection(monkeypatch, review)
    root = review[0] / "capture"
    asyncio.run(
        history.collect_day(
            None,
            SimpleNamespace(fingerprint="test-source"),
            date(2026, 9, 10),
            templates,
            "approved",
            root,
            Event(),
        )
    )
    assert calls == [*history.REPORT_ORDER, "logout", "closed"]
    assert set(parse_review(root, "test-source")["reports"]) == KINDS


@pytest.mark.parametrize("logout_failure", [False, True])
def test_failed_collection_still_logs_out_and_retains_raw(review, monkeypatch, logout_failure):
    templates, calls = fake_collection(
        monkeypatch, review, failure=True, logout_failure=logout_failure
    )
    root = review[0] / "capture"
    with pytest.raises((IikoError, SyncError)):
        asyncio.run(
            history.collect_day(
                None,
                SimpleNamespace(fingerprint="test-source"),
                date(2026, 9, 10),
                templates,
                "approved",
                root,
                Event(),
            )
        )
    assert calls == ["daily", "dishes", "payments", "logout", "closed"]
    assert (root / "raw/daily.json").exists()
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["errors"]
    assert "private" not in json.dumps(manifest)
    with pytest.raises(ValueError):
        parse_review(root, "test-source")


def test_today_requires_explicit_opt_in_and_future_stays_rejected():
    today = history.datetime.now(history.ZONE).date()
    with pytest.raises(SyncError, match="invalid_sales_history_period"):
        history.history_days(today, today)
    assert history.history_days(today, today, include_today=True) == [today]
    with pytest.raises(SyncError, match="invalid_sales_history_period"):
        history.history_days(today, today + timedelta(days=1), include_today=True)


class TemplateDatabase:
    def __init__(self, row=None, templates=None):
        self.row = row
        self.templates = templates

    def execute(self, query, parameters=None):
        return self

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self.templates.items())


def test_fresh_and_subsequent_tenant_use_code_schema_without_inherited_approval(monkeypatch):
    monkeypatch.setattr(history, "load_runtime", lambda: SimpleNamespace(mode="tenant"))
    # Neither an empty schema nor own unreviewed captures provide a reviewed row.
    for _ in range(2):
        template_id, templates = history.approved_templates(TemplateDatabase())
        assert template_id == history.CODE_TEMPLATE_ID
        assert set(templates) == KINDS
        daily = history.DailySalesQuery(business_date=date(2000, 1, 1)).iiko_body()
        assert templates["daily"] == daily
        assert all(request["filters"] == daily["filters"] for request in templates.values())
        assert all(
            len(request["groupByRowFields"]) + len(request["aggregateFields"]) <= 7
            for request in templates.values()
        )
        assert "DishAmountInt" in templates["dishes"]["aggregateFields"]
        assert "DiscountSum" in templates["discounts"]["aggregateFields"]
        assert "DishReturnSum" in templates["returns"]["aggregateFields"]
        templates["hours"]["filters"]["OrderDeleted"]["values"].append("bad")
        assert "bad" not in templates["daily"]["filters"]["OrderDeleted"]["values"]


def test_legacy_missing_templates_still_rejected(monkeypatch):
    monkeypatch.setattr(history, "load_runtime", lambda: SimpleNamespace(mode="legacy"))
    with pytest.raises(SyncError, match="sales_approved_templates_missing"):
        history.approved_templates(TemplateDatabase())


@pytest.mark.parametrize("mode", ["legacy", "tenant"])
def test_existing_reviewed_templates_preferred_and_invalid_not_replaced(review, monkeypatch, mode):
    monkeypatch.setattr(history, "load_runtime", lambda: SimpleNamespace(mode=mode))
    templates = {kind: info["request"] for kind, info in review[1]["reports"].items()}
    db = TemplateDatabase(("own-reviewed",), templates)
    assert history.approved_templates(db) == ("own-reviewed", templates)
    del templates["returns"]
    with pytest.raises(SyncError, match="sales_approved_templates_incomplete"):
        history.approved_templates(db)
    templates["returns"] = deepcopy(templates["daily"])
    templates["hours"]["buildSummary"] = True
    with pytest.raises(SyncError, match="sales_approved_templates_invalid"):
        history.approved_templates(db)


@pytest.mark.parametrize("fault", [None, "hash", "source", "shape", "logout", "discrepancy"])
def test_tenant_bootstrap_uses_own_capture_and_only_valid_capture_advances(
    tmp_path, monkeypatch, fault
):
    monkeypatch.setattr(history, "load_runtime", lambda: SimpleNamespace(mode="tenant"))
    template_id, templates = history.approved_templates(TemplateDatabase())
    calls, published, checkpoints = [], [], []
    root = tmp_path / "own-capture"
    source = SimpleNamespace(fingerprint="own-company")
    day = date(2026, 8, 1)
    writes = []

    class PublicationDatabase(TemplateDatabase):
        def transaction(self):
            return nullcontext()

        def cursor(self):
            return nullcontext(self)

        def execute(self, query, parameters=None):
            if query.startswith("INSERT"):
                writes.append((query, parameters))
            return self

        def executemany(self, query, parameters):
            writes.append((query, parameters))

    db = PublicationDatabase()

    class Client:
        def __init__(self, settings):
            pass

        async def aclose(self):
            calls.append("closed")

    class Auth:
        def __init__(self, settings, client):
            pass

        async def download_daily_sales(self, path, *, query):
            calls.append(path.stem)
            request = query.iiko_body()
            row = {
                **{field: "own" for field in request["groupByRowFields"]},
                **{field: 2 for field in request["aggregateFields"]},
                "OpenDate.Typed": day.isoformat(),
                "Department.Id": "00000000-0000-0000-0000-000000000002",
            }
            if fault == "shape" and path.stem == "hours":
                row["UniqOrderId"] = True
            if fault == "discrepancy" and path.stem == "payments":
                row["DishDiscountSumInt"] = 3
            raw = json.dumps({"data": [row], "summary": []}).encode()
            path.write_bytes(raw)
            return SimpleNamespace(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))

        async def logout(self):
            calls.append("logout")
            return SimpleNamespace(state="failed" if fault == "logout" else "logged_out")

    monkeypatch.setattr(history, "IikoClient", Client)
    monkeypatch.setattr(history, "IikoAuthService", Auth)

    def sync(capture_day):
        asyncio.run(
            history.collect_day(None, source, capture_day, templates, template_id, root, Event())
        )
        if fault == "hash":
            (root / "raw" / "daily.json").write_text("{}")
        bundle = parse_review(
            root,
            "wrong-company" if fault == "source" else source.fingerprint,
            allow_discrepancies=True,
        )
        assert bundle["manifest"]["template_set_id"] == history.CODE_TEMPLATE_ID
        assert bundle["manifest"]["source_fingerprint"] == "own-company"
        assert all(check["exact_match"] for check in bundle["checks"]) == (fault != "discrepancy")
        # This is the publication boundary; invalid RAW never reaches it.
        published.append(bundle)
        return history.publish(db, bundle)

    report = history.run_history(
        [day],
        {},
        sync,
        lambda value: checkpoints.append(deepcopy(value)),
        Event(),
        template_id=template_id,
    )
    assert calls == [*history.REPORT_ORDER, "logout", "closed"]
    invalid = fault not in {None, "discrepancy"}
    assert report["counts"]["completed_days"] == (0 if invalid else 1)
    assert report["status"] == ("failed" if invalid else "succeeded")
    assert len(published) == (0 if invalid else 1)
    assert bool(writes) == (not invalid)
    assert all("reviewed" not in query for query, _ in writes)
    assert report["counts"]["warning_days"] == (1 if fault == "discrepancy" else 0)
    assert checkpoints[0]["counts"]["completed_days"] == 0
