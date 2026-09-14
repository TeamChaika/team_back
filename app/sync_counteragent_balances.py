"""Synchronize one unfiltered counteragent-balance report at a specified accounting time."""

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
from app.schemas.iiko_balances import CounteragentBalancesQuery, CounteragentBalancesResponse
from app.schemas.iiko_reports import AccountingReportQuery
from app.services.iiko_balances import read_counteragent_balances
from app.sync_references import (
    Source,
    SyncError,
    append_snapshot,
    check_local_api,
    configured_sources,
    reference_lock,
    register_sources,
)


def capture_counteragent_balances(
    settings: Settings,
    source: Source,
    query: AccountingReportQuery,
    response: dict,
    directory: Path | None = None,
) -> dict:
    folder = directory if directory is not None else BACKEND_DIR / ".local/counteragent-balances"
    key = str(UUID(response["snapshot_id"]))
    metadata = json.loads((folder / f"{key}.meta.json").read_text())
    if metadata.pop("source_fingerprint") != source.fingerprint:
        raise SyncError("counteragent_balances_source_mismatch")
    if metadata.pop("source_endpoint") != "v2/reports/balance/counteragents":
        raise SyncError("counteragent_balances_endpoint_mismatch")
    if metadata != {k: v for k, v in response.items() if k != "items"}:
        raise SyncError("counteragent_balances_snapshot_mismatch")
    parsed = CounteragentBalancesResponse.model_validate(response, by_name=True)
    expected = CounteragentBalancesQuery(timestamp=query.timestamp)
    if parsed.request != expected:
        raise SyncError("counteragent_balances_scope_mismatch")
    path = folder / f"{key}.json"
    if path.stat().st_size > settings.iiko_balances_max_response_bytes:
        raise SyncError("counteragent_balances_raw_too_large")
    raw = path.read_bytes()
    if len(raw) != parsed.source_bytes or hashlib.sha256(raw).hexdigest() != parsed.sha256:
        raise SyncError("counteragent_balances_raw_mismatch")
    rows = read_counteragent_balances(path, expected)
    if rows != parsed.items or parsed.total != len(rows):
        raise SyncError("counteragent_balances_rows_mismatch")
    if parsed.received_at.tzinfo is None:
        raise SyncError("observation_timezone_missing")
    return {
        "id": key,
        "source_id": source.id,
        "resource": "counteragent_balances",
        "observed_at": parsed.received_at,
        "sha256": parsed.sha256,
        "raw": raw,
        "payload": {**parsed.model_dump(mode="json"), "source_fingerprint": source.fingerprint},
    }


def publish_counteragent_balances(db, snapshot: dict) -> dict:
    """Save every row once per observation, then switch only this accounting-time report."""
    report = CounteragentBalancesResponse.model_validate(snapshot["payload"], by_name=True)
    query = report.request
    if (
        query.department_id
        or query.account_id
        or query.counteragent_id
        or report.total != len(report.items)
        or snapshot["resource"] != "counteragent_balances"
        or str(report.snapshot_id) != str(snapshot["id"])
        or report.received_at != snapshot["observed_at"]
        or report.received_at.tzinfo is None
    ):
        raise SyncError("counteragent_balances_incomplete")
    accounts = {r.account_id for r in report.items}
    counterparties = {r.counteragent_id for r in report.items if r.counteragent_id is not None}
    departments = {r.department_id for r in report.items if r.department_id is not None}
    counts = {
        "rows": report.total,
        "accounts": len(accounts),
        "counteragents": len(counterparties),
        "departments": len(departments),
        "negative_sum_rows": sum(r.sum < 0 for r in report.items),
        "zero_sum_rows": sum(r.sum == 0 for r in report.items),
        "null_counteragent_rows": sum(r.counteragent_id is None for r in report.items),
        "null_department_rows": sum(r.department_id is None for r in report.items),
        "duplicate_dimension_rows": sum(
            n - 1
            for n in Counter(
                (r.account_id, r.counteragent_id, r.department_id) for r in report.items
            ).values()
        ),
    }
    with db.transaction():
        known_accounts = {
            r[0]
            for r in db.execute(
                "SELECT id FROM chaika.accounts WHERE source_id=%s AND id=ANY(%s::uuid[])",
                (snapshot["source_id"], list(accounts)),
            )
        }
        counts["accounts_without_dictionary"] = len(accounts - known_accounts)
        with db.cursor() as cursor:
            records = (
                (
                    report.snapshot_id,
                    n,
                    row.account_id,
                    row.counteragent_id,
                    row.department_id,
                    row.sum,
                )
                for n, row in enumerate(report.items, 1)
            )
            for batch in batched(records, 1000):
                cursor.executemany(
                    "INSERT INTO chaika.counteragent_balance_items"
                    "(snapshot_id,line_num,account_id,counteragent_id,department_id,sum) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    batch,
                )
        known_counterparties = {
            r[0]
            for r in db.execute(
                "SELECT id FROM chaika.counteragents WHERE source_id=%s AND id=ANY(%s::uuid[]) "
                "UNION SELECT id FROM chaika.employees WHERE source_id=%s AND id=ANY(%s::uuid[])",
                (
                    snapshot["source_id"],
                    list(counterparties),
                    snapshot["source_id"],
                    list(counterparties),
                ),
            )
        }
        known_departments = {
            r[0]
            for r in db.execute(
                "SELECT id FROM chaika.corporate_nodes WHERE source_id=%s AND id=ANY(%s::uuid[])",
                (snapshot["source_id"], list(departments)),
            )
        }
        counts["unmatched_counteragents"] = len(counterparties - known_counterparties)
        counts["unmatched_departments"] = len(departments - known_departments)
        db.execute(
            "INSERT INTO chaika.counteragent_balance_reports"
            "(source_id,accounting_timestamp,last_snapshot_id,row_count,"
            "first_seen_at,last_seen_at) "
            "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(source_id,accounting_timestamp) DO UPDATE SET "
            "last_snapshot_id=EXCLUDED.last_snapshot_id,row_count=EXCLUDED.row_count,"
            "last_seen_at=EXCLUDED.last_seen_at "
            "WHERE chaika.counteragent_balance_reports.last_seen_at <= EXCLUDED.last_seen_at",
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


def synchronize_counteragent_balances(
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
            application_name="chaika-counteragent-balance-sync",
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
            "WHERE job='counteragent_balances' AND status='running'"
        )
        db.execute(
            "INSERT INTO chaika.sync_runs(id,job,status) "
            "VALUES(%s,'counteragent_balances','running')",
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
                    "/api/v1/iiko/reports/counteragent-balances",
                    params=query.model_dump(mode="json"),
                )
                if response.status_code != 200:
                    raise SyncError(f"counteragent_balances_http_{response.status_code}")
                snapshot = capture_counteragent_balances(settings, source, query, response.json())
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
            failure = SyncError("counteragent_balances_logout_failed")
        if failure is None:
            try:
                with db.transaction():
                    append_snapshot(db, run_id, snapshot)
                    counts = publish_counteragent_balances(db, snapshot)
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
        report = synchronize_counteragent_balances(Settings(), query, args.api_url)
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
