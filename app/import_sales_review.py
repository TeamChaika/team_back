"""Validate source captures and publish seven reports per accounting day."""

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.services.iiko_olap_sales import _unique_fields
from app.sync_references import configured_sources, reference_lock

KINDS = {"daily", "dishes", "payments", "discounts", "returns", "waiters", "hours"}
METRICS = {
    "revenue": "DishDiscountSumInt",
    "cost": "ProductCostBase.ProductCost",
    "checks": "UniqOrderId",
    "guests": "GuestNum",
    "discount": "DiscountSum",
    "return_sum": "DishReturnSum",
    "quantity": "DishAmountInt",
}


def parse_review(
    root: Path,
    fingerprint: str,
    *,
    business_date: date | None = None,
    allow_discrepancies: bool = False,
) -> dict:
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest["source_fingerprint"] != fingerprint or manifest.get("errors"):
        raise ValueError("sales_source_mismatch")
    if set(manifest["reports"]) != KINDS or manifest.get("logout") != "logged_out":
        raise ValueError("sales_incomplete")
    start = date.fromisoformat(manifest["business_date"])
    end = date.fromisoformat(manifest.get("date_to", manifest["business_date"]))
    day = business_date or start
    if end < start or (end - start).days > 6 or not start <= day <= end:
        raise ValueError("sales_invalid_period")
    capture_id = UUID(manifest["capture_id"]) if "date_to" in manifest else None
    filters = manifest["reports"]["daily"]["request"]["filters"]
    period = filters["OpenDate.Typed"]
    if (
        period.get("filterType") != "DateRange"
        or period.get("includeLow") is not True
        or period.get("includeHigh") is not False
        or datetime.fromisoformat(period["from"]) != datetime.combine(start, datetime.min.time())
        or datetime.fromisoformat(period["to"])
        != datetime.combine(end + timedelta(days=1), datetime.min.time())
    ):
        raise ValueError("sales_invalid_period")
    reports = {}
    for kind, info in manifest["reports"].items():
        path = (root / info.get("raw_file", f"raw/{kind}.json")).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("sales_invalid_path")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != info["sha256"]:
            raise ValueError("sales_hash_mismatch")
        payload = json.loads(raw, parse_float=Decimal, object_pairs_hook=_unique_fields)
        request = info["request"]
        if request["reportType"] != "SALES" or request["buildSummary"] is not False:
            raise ValueError("sales_invalid_request")
        if request["filters"] != manifest["reports"]["daily"]["request"]["filters"]:
            raise ValueError("sales_filter_mismatch")
        rows = payload["data"]
        if len(rows) != info["rows"] or payload["summary"] != []:
            raise ValueError("sales_count_mismatch")
        groups = request["groupByRowFields"]
        if not {"OpenDate.Typed", "Department.Id"}.issubset(groups):
            raise ValueError("sales_missing_date_group")
        if request.get("groupByColFields") or len(groups) + len(request["aggregateFields"]) > 7:
            raise ValueError("sales_too_many_fields")
        seen = set()
        for row in rows:
            try:
                row_date = date.fromisoformat(row["OpenDate.Typed"])
            except (ValueError, TypeError):
                raise ValueError("sales_date_mismatch") from None
            if row_date.isoformat() != row["OpenDate.Typed"] or not start <= row_date <= end:
                raise ValueError("sales_date_mismatch")
            key = tuple(json.dumps(row[k], sort_keys=True, default=str) for k in groups)
            if key in seen:
                raise ValueError("sales_duplicate_group")
            seen.add(key)
            for field in request["aggregateFields"]:
                if isinstance(row[field], bool) or not isinstance(row[field], (int, Decimal)):
                    raise ValueError("sales_invalid_metric")
                if not Decimal(row[field]).is_finite():
                    raise ValueError("sales_invalid_metric")
        reports[kind] = {
            "raw": raw,
            "info": info,
            "capture_id": uuid5(capture_id, kind) if capture_id else None,
            "rows": [row for row in rows if row["OpenDate.Typed"] == day.isoformat()],
        }
    if "HourOpen" not in reports["hours"]["info"]["request"]["groupByRowFields"]:
        raise ValueError("sales_wrong_hour")
    if "PayTypes.Group" not in reports["payments"]["info"]["request"]["groupByRowFields"]:
        raise ValueError("sales_wrong_payment_group")

    def sums(kind, field):
        result = {}
        with localcontext() as ctx:
            ctx.prec = 70
            for row in reports[kind]["rows"]:
                d = row["Department.Id"]
                result[d] = result.get(d, Decimal(0)) + Decimal(row[field])
        return result

    checks = []
    for kind in sorted(KINDS - {"daily"}):
        fields = ["DishDiscountSumInt"]
        if kind in {"dishes", "hours"}:
            fields.append("ProductCostBase.ProductCost")
        if kind in {"hours", "waiters"}:
            fields.extend(["UniqOrderId", "GuestNum"])
        for field in fields:
            expected, actual = sums("daily", field), sums(kind, field)
            with localcontext() as ctx:
                ctx.prec = 70
                differences = [
                    {
                        "department_id": department,
                        "daily": str(expected[department]) if department in expected else None,
                        "actual": str(actual[department]) if department in actual else None,
                        "delta": str(actual[department] - expected[department])
                        if department in expected and department in actual
                        else None,
                    }
                    for department in sorted(expected.keys() | actual.keys())
                    if expected.get(department) != actual.get(department)
                ]
            if differences and not allow_discrepancies:
                raise ValueError("sales_reconciliation_failed")
            checks.append(
                {
                    "report": kind,
                    "field": field,
                    "exact_match": not differences,
                    **({"differences": differences} if differences else {}),
                }
            )
    identity = fingerprint + day.isoformat() + json.dumps(manifest["reports"], sort_keys=True)
    return {
        "id": uuid5(NAMESPACE_URL, identity),
        "manifest": {**manifest, "business_date": day.isoformat(), "date_from": start.isoformat()},
        "reports": reports,
        "checks": checks,
    }


def publish(db, bundle: dict) -> dict:
    manifest = bundle["manifest"]
    observed = max(
        datetime.fromisoformat(r["info"]["received_at"]) for r in bundle["reports"].values()
    )
    with db.transaction():
        if db.execute(
            "SELECT 1 FROM chaika.sales_report_sets WHERE id=%s", (bundle["id"],)
        ).fetchone():
            return {"status": "already_imported", "id": str(bundle["id"])}
        captures = [
            (
                report["capture_id"],
                kind,
                manifest["date_from"],
                manifest["date_to"],
                Jsonb(report["info"]["request"]),
                report["info"]["received_at"],
                report["raw"],
                report["info"]["sha256"],
                report["info"]["rows"],
            )
            for kind, report in bundle["reports"].items()
            if report.get("capture_id")
        ]
        if captures:
            existing = {
                row[0]
                for row in db.execute(
                    "SELECT id FROM chaika.sales_report_captures WHERE id=ANY(%s::uuid[])",
                    ([record[0] for record in captures],),
                ).fetchall()
            }
            captures = [record for record in captures if record[0] not in existing]
        if captures:
            with db.cursor() as cur:
                cur.executemany(
                    "INSERT INTO chaika.sales_report_captures(id,source_id,kind,date_from,date_to,"
                    "request,observed_at,raw,sha256,row_count) VALUES(%s,'primary',%s,%s,%s,%s,%s,"
                    "%s,%s,%s) ON CONFLICT(id) DO NOTHING",
                    captures,
                )
        db.execute(
            "INSERT INTO chaika.sales_report_sets(id,source_id,business_date,observed_at,checks) "
            "VALUES(%s,'primary',%s,%s,%s)",
            (bundle["id"], manifest["business_date"], observed, Jsonb(bundle["checks"])),
        )
        counts, report_records, row_records = {}, [], []
        for kind, report in bundle["reports"].items():
            info = report["info"]
            report_id = uuid5(bundle["id"], kind)
            report_records.append(
                (
                    report_id,
                    bundle["id"],
                    kind,
                    Jsonb(info["request"]),
                    info["received_at"],
                    None if report.get("capture_id") else report["raw"],
                    info["sha256"],
                    len(report["rows"]),
                    report.get("capture_id"),
                ),
            )
            row_records.extend(
                [
                    (
                        report_id,
                        i,
                        row["Department.Id"],
                        *[row.get(k) for k in METRICS.values()],
                        Jsonb({k: row[k] for k in info["request"]["groupByRowFields"]}),
                    )
                    for i, row in enumerate(report["rows"])
                ],
            )
            counts[kind] = len(report["rows"])
        with db.cursor() as cur:
            cur.executemany(
                "INSERT INTO chaika.sales_reports(id,set_id,kind,request,observed_at,raw,"
                "sha256,row_count,source_capture_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                report_records,
            )
            cur.executemany(
                "INSERT INTO chaika.sales_report_rows(report_id,ordinal,department_id,revenue,"
                "cost,checks,guests,discount,return_sum,quantity,dimensions) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                row_records,
            )
        db.execute(
            "INSERT INTO chaika.sales_report_days(source_id,business_date,current_set_id) "
            "VALUES('primary',%s,%s) ON CONFLICT(source_id,business_date) DO UPDATE "
            "SET current_set_id=EXCLUDED.current_set_id WHERE "
            "(SELECT observed_at FROM chaika.sales_report_sets WHERE "
            "id=chaika.sales_report_days.current_set_id)<=%s",
            (manifest["business_date"], bundle["id"], observed),
        )
    return {
        "status": "imported",
        "id": str(bundle["id"]),
        "date": manifest["business_date"],
        "rows": counts,
        "warning_count": sum(not check["exact_match"] for check in bundle["checks"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    settings = Settings()
    bundle = parse_review(args.directory, configured_sources(settings)[0].fingerprint)
    with (
        psycopg.connect(settings.database_url.get_secret_value(), autocommit=True) as db,
        reference_lock(db),
    ):
        print(json.dumps(publish(db, bundle)))


if __name__ == "__main__":
    main()
