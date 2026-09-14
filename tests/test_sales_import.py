"""Offline import must preserve precise data and reject incomplete or altered captures."""

import hashlib
import json
from decimal import Decimal

import pytest

from app.import_sales_review import KINDS, parse_review


@pytest.fixture
def review(tmp_path):
    groups = {
        "daily": ["Department"],
        "dishes": ["DishId", "DishName"],
        "payments": ["PayTypes.Group", "PayTypes"],
        "discounts": ["ItemSaleEventDiscountType"],
        "returns": ["Storned"],
        "waiters": ["OrderWaiter.Name"],
        "hours": ["HourOpen"],
    }
    filters = {
        "OpenDate.Typed": {
            "filterType": "DateRange",
            "from": "2026-09-10T00:00:00",
            "to": "2026-09-11T00:00:00",
            "includeLow": True,
            "includeHigh": False,
        }
    }
    manifest = {
        "source_fingerprint": "test-source",
        "business_date": "2026-09-10",
        "logout": "logged_out",
        "reports": {},
    }
    for kind in KINDS:
        fields = ["DishDiscountSumInt"]
        if kind in {"daily", "dishes", "hours"}:
            fields += ["ProductCostBase.ProductCost"]
        if kind in {"daily", "hours", "waiters"}:
            fields += ["UniqOrderId", "GuestNum"]
        row = {
            "OpenDate.Typed": "2026-09-10",
            "Department.Id": "00000000-0000-0000-0000-000000000001",
            **{g: "19" if g == "HourOpen" else "test" for g in groups[kind]},
            **{f: 2 for f in fields},
        }
        raw = json.dumps({"data": [row], "summary": []}).replace(
            '"DishDiscountSumInt": 2', '"DishDiscountSumInt": 123.45000000000000000001'
        )
        path = tmp_path / (kind + ".json")
        path.write_text(raw)
        manifest["reports"][kind] = {
            "raw_file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "rows": 1,
            "received_at": "2026-09-11T00:00:00+00:00",
            "request": {
                "reportType": "SALES",
                "buildSummary": False,
                "groupByRowFields": ["OpenDate.Typed", "Department.Id", *groups[kind]],
                "groupByColFields": [],
                "aggregateFields": fields,
                "filters": filters,
            },
        }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path, manifest


def save(root, manifest):
    (root / "manifest.json").write_text(json.dumps(manifest))


def test_complete_day_is_exact_and_repeatable(review):
    root, _ = review
    result = parse_review(root, "test-source")
    assert result["reports"]["daily"]["rows"][0]["DishDiscountSumInt"] == Decimal(
        "123.45000000000000000001"
    )
    assert result["id"] == parse_review(root, "test-source")["id"]
    assert all(check["exact_match"] for check in result["checks"])


def test_rejects_changed_bytes(review):
    root, _ = review
    with (root / "daily.json").open("a") as f:
        f.write(" ")
    with pytest.raises(ValueError, match="hash_mismatch"):
        parse_review(root, "test-source")


def test_rejects_incomplete_or_wrong_source(review):
    root, manifest = review
    with pytest.raises(ValueError, match="source_mismatch"):
        parse_review(root, "other-source")
    del manifest["reports"]["hours"]
    save(root, manifest)
    with pytest.raises(ValueError, match="incomplete"):
        parse_review(root, "test-source")


def test_rejects_mismatched_totals_even_with_valid_hash(review):
    root, manifest = review
    p = root / "waiters.json"
    p.write_text(p.read_text().replace("123.45000000000000000001", "124.45"))
    manifest["reports"]["waiters"]["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    save(root, manifest)
    with pytest.raises(ValueError, match="reconciliation_failed"):
        parse_review(root, "test-source")


def test_rejects_period_larger_than_one_day(review):
    root, manifest = review
    manifest["reports"]["daily"]["request"]["filters"]["OpenDate.Typed"]["to"] = "2026-10-01"
    save(root, manifest)
    with pytest.raises(ValueError, match="invalid_period"):
        parse_review(root, "test-source")


def test_rejects_raw_path_escape(review):
    root, manifest = review
    manifest["reports"]["daily"]["raw_file"] = "../outside.json"
    save(root, manifest)
    with pytest.raises(ValueError, match="invalid_path"):
        parse_review(root, "test-source")
