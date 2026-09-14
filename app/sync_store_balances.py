"""Synchronize one unfiltered store-balance report at a specified accounting time."""

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.core.config import BACKEND_DIR, Settings
from app.schemas.iiko_reports import AccountingReportQuery
from app.schemas.iiko_store_balances import StoreBalancesQuery, StoreBalancesResponse
from app.services.iiko_store_balances import read_store_balances
from app.sync_references import (
    Source,
    SyncError,
    append_snapshot,
    check_local_api,
    configured_sources,
    reference_lock,
    register_sources,
)


def capture_store_balances(
    settings: Settings,
    source: Source,
    query: AccountingReportQuery,
    response: dict,
    directory: Path | None = None,
) -> dict:
    folder = directory if directory is not None else BACKEND_DIR / ".local/store-balances"
    key = str(UUID(response["snapshot_id"]))
    metadata = json.loads((folder / f"{key}.meta.json").read_text())
    if metadata.pop("source_fingerprint") != source.fingerprint:
        raise SyncError("store_balances_source_mismatch")
    if metadata.pop("source_endpoint") != "v2/reports/balance/stores":
        raise SyncError("store_balances_endpoint_mismatch")
    if metadata != {k: v for k, v in response.items() if k != "items"}:
        raise SyncError("store_balances_snapshot_mismatch")
    parsed = StoreBalancesResponse.model_validate(response, by_name=True)
    expected = StoreBalancesQuery(timestamp=query.timestamp)
    if parsed.request != expected:
        raise SyncError("store_balances_scope_mismatch")
    path = folder / f"{key}.json"
    if path.stat().st_size > settings.iiko_store_balances_max_response_bytes:
        raise SyncError("store_balances_raw_too_large")
    raw = path.read_bytes()
    if len(raw) != parsed.source_bytes or hashlib.sha256(raw).hexdigest() != parsed.sha256:
        raise SyncError("store_balances_raw_mismatch")
    rows = read_store_balances(path, expected)
    if rows != parsed.items or parsed.total != len(rows):
        raise SyncError("store_balances_rows_mismatch")
    if parsed.received_at.tzinfo is None:
        raise SyncError("observation_timezone_missing")
    return {
        "id": key,
        "source_id": source.id,
        "resource": "store_balances",
        "observed_at": parsed.received_at,
        "sha256": parsed.sha256,
        "raw": raw,
        "payload": {**parsed.model_dump(mode="json"), "source_fingerprint": source.fingerprint},
    }


def publish_store_balances(db, snapshot: dict) -> dict:
    """Save every row once per observation, then switch only this accounting-time report."""
    report = StoreBalancesResponse.model_validate(snapshot["payload"], by_name=True)
    query = report.request
    if (
        query.department_id
        or query.store_id
        or query.product_id
        or report.total != len(report.items)
        or str(report.snapshot_id) != str(snapshot["id"])
        or report.received_at != snapshot["observed_at"]
    ):
        raise SyncError("store_balances_incomplete")
    counts = {
        "rows": report.total,
        "stores": len({r.store_id for r in report.items}),
        "products": len({r.product_id for r in report.items}),
        "negative_amount_rows": sum(r.amount < 0 for r in report.items),
        "negative_sum_rows": sum(r.sum < 0 for r in report.items),
        "duplicate_pair_rows": sum(
            n - 1 for n in Counter((r.store_id, r.product_id) for r in report.items).values()
        ),
    }
    with db.transaction():
        with db.cursor() as cursor:
            records = (
                (report.snapshot_id, n, row.store_id, row.product_id, row.amount, row.sum)
                for n, row in enumerate(report.items, 1)
            )
            for batch in batched(records, 1000):
                cursor.executemany(
                    "INSERT INTO chaika.store_balance_items"
                    "(snapshot_id,line_num,store_id,product_id,amount,sum) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    batch,
                )
        for field, table in [("store_id", "stores"), ("product_id", "products")]:
            ids = {getattr(r, field) for r in report.items}
            known = {
                r[0]
                for r in db.execute(
                    f"SELECT id FROM chaika.{table} WHERE source_id=%s AND id=ANY(%s::uuid[])",
                    (snapshot["source_id"], list(ids)),
                )
            }
            counts[f"unmatched_{table}"] = len(ids - known)
        db.execute(
            "INSERT INTO chaika.store_balance_reports"
            "(source_id,accounting_timestamp,last_snapshot_id,row_count,"
            "first_seen_at,last_seen_at) "
            "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(source_id,accounting_timestamp) DO UPDATE SET "
            "last_snapshot_id=EXCLUDED.last_snapshot_id,row_count=EXCLUDED.row_count,"
            "last_seen_at=EXCLUDED.last_seen_at "
            "WHERE chaika.store_balance_reports.last_seen_at <= EXCLUDED.last_seen_at",
            (
                snapshot["source_id"],
                query.timestamp,
                report.snapshot_id,
                report.total,
                snapshot["observed_at"],
                snapshot["observed_at"],
            ),
        )
    return counts


def synchronize_store_balances(
    settings: Settings, query: AccountingReportQuery, api_url: str = "http://127.0.0.1:8010"
) -> dict:
    api_url = check_local_api(api_url)
    if not settings.database_url.get_secret_value():
        raise SyncError("database_not_configured")
    source = configured_sources(settings)[0]
    run_id = uuid4()
    with (
        psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            application_name="chaika-store-balance-sync",
        ) as db,
        reference_lock(db),
    ):
        register_sources(db, [source])
        if not db.execute(
            "SELECT 1 FROM chaika.sources WHERE id=%s AND server_type='CHAIN'", (source.id,)
        ).fetchone():
            raise SyncError("reference_sync_required")
        db.execute(
            "UPDATE chaika.sync_runs SET status='failed',finished_at=now(),"
            "error_code='interrupted' "
            "WHERE job='store_balances' AND status='running'"
        )
        db.execute(
            "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'store_balances','running')",
            (run_id,),
        )
        touched, logout_ok, failure = False, True, None
        with httpx.Client(base_url=api_url, timeout=180, trust_env=False) as client:
            try:
                response = client.get("/api/v1/iiko/connections")
                response.raise_for_status()
                primary = [r for r in response.json()["items"] if r["connection_id"] == source.id]
                if len(primary) != 1 or primary[0]["base_url"] != source.base_url:
                    raise SyncError("backend_source_config_mismatch")
                touched = True
                response = client.get(
                    "/api/v1/iiko/reports/store-balances", params=query.model_dump(mode="json")
                )
                if response.status_code != 200:
                    raise SyncError(f"store_balances_http_{response.status_code}")
                snapshot = capture_store_balances(settings, source, query, response.json())
            except BaseException as error:
                failure = error
            finally:
                if touched:
                    try:
                        response = client.post("/api/v1/iiko/connections/primary/logout")
                        logout_ok = (
                            response.status_code == 200 and response.json()["state"] == "logged_out"
                        )
                    except Exception:
                        logout_ok = False
        if failure is None and not logout_ok:
            failure = SyncError("store_balances_logout_failed")
        if failure is None:
            try:
                with db.transaction():
                    append_snapshot(db, run_id, snapshot)
                    counts = publish_store_balances(db, snapshot)
                    counts["logout_ok"] = logout_ok
                    db.execute(
                        "UPDATE chaika.sync_runs SET status='succeeded',finished_at=now(),"
                        "counts=%s WHERE id=%s",
                        (Jsonb(counts), run_id),
                    )
            except BaseException as error:
                failure = error
        if failure is not None:
            code = str(failure) if isinstance(failure, SyncError) else type(failure).__name__
            db.execute(
                "UPDATE chaika.sync_runs SET status='failed',finished_at=now(),error_code=%s,"
                "counts=%s WHERE id=%s",
                (code, Jsonb({"logout_ok": logout_ok}), run_id),
            )
            raise SyncError(code) from None
        return {
            "run_id": str(run_id),
            "status": "succeeded",
            "source_id": source.id,
            "snapshot_id": snapshot["id"],
            "timestamp": query.timestamp.isoformat(),
            "observed_at": snapshot["observed_at"].isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "counts": counts,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timestamp", required=True, help="YYYY-MM-DDTHH:MM:SS (iiko accounting time)"
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8010")
    args = parser.parse_args()
    try:
        query = AccountingReportQuery(timestamp=args.timestamp)
        report = synchronize_store_balances(Settings(), query, args.api_url)
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
