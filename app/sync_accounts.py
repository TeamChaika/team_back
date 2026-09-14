"""Full Chain account catalogue with RAW history and source-scoped financial references."""

import argparse
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.integrations.iiko.dictionaries import DICTIONARIES
from app.schemas.iiko_dictionaries import Account, DictionarySnapshot
from app.services.iiko_dictionaries import summarize_dictionary
from app.sync_dictionaries import capture_dictionary
from app.sync_inventory import upsert_rows
from app.sync_references import (
    SyncError,
    append_snapshot,
    check_local_api,
    configured_sources,
    reference_lock,
    register_sources,
)


def account_links(db, source: str) -> dict:
    checks = {
        "balance_accounts": "SELECT DISTINCT i.account_id AS id "
        "FROM chaika.counteragent_balance_reports r "
        "JOIN chaika.counteragent_balance_items i ON i.snapshot_id=r.last_snapshot_id "
        "WHERE r.source_id=%s",
        "writeoff_accounts": "SELECT DISTINCT account_id AS id FROM chaika.writeoffs "
        "WHERE source_id=%s",
        "parent_accounts": "SELECT DISTINCT account_parent_id AS id FROM chaika.accounts "
        "WHERE source_id=%s AND present_in_latest",
    }
    result = {}
    for label, query in checks.items():
        total, missing, absent, deleted = db.execute(
            "WITH refs AS (" + query + ") SELECT count(*),"
            "count(*) FILTER(WHERE a.id IS NULL),"
            "count(*) FILTER(WHERE a.id IS NOT NULL AND NOT a.present_in_latest),"
            "count(*) FILTER(WHERE a.deleted) "
            "FROM refs r LEFT JOIN chaika.accounts a ON a.source_id=%s AND a.id=r.id "
            "WHERE r.id IS NOT NULL",
            (source, source),
        ).fetchone()
        result.update(
            {
                f"{label}_ids": total,
                f"{label}_unresolved": missing,
                f"{label}_absent": absent,
                f"{label}_deleted": deleted,
            }
        )
    total, missing = db.execute(
        "SELECT count(DISTINCT a.parent_corporate_id),"
        "count(DISTINCT a.parent_corporate_id) FILTER(WHERE c.id IS NULL) "
        "FROM chaika.accounts a LEFT JOIN chaika.corporate_nodes c "
        "ON c.source_id=a.source_id AND c.id=a.parent_corporate_id "
        "WHERE a.source_id=%s AND a.present_in_latest",
        (source,),
    ).fetchone()
    result.update(corporate_parent_ids=total, corporate_parents_unresolved=missing)
    return result


def publish_accounts(db, snapshot: dict) -> dict:
    info = DictionarySnapshot.model_validate(snapshot["payload"])
    rows = [Account.model_validate(row, by_name=True) for row in snapshot["payload"]["items"]]
    spec = DICTIONARIES["accounts"]
    summary = summarize_dictionary(rows, kind="accounts")
    if (
        not rows
        or len(rows) != info.total
        or len({r.id for r in rows}) != len(rows)
        or any(info.model_dump()[k] != v for k, v in summary.items())
        or str(info.snapshot_id) != str(snapshot["id"])
        or snapshot["resource"] != spec.table
        or info.received_at != snapshot["observed_at"]
        or info.received_at.tzinfo is None
    ):
        raise SyncError("accounts_incomplete")
    source, observed = snapshot["source_id"], snapshot["observed_at"]
    with db.transaction():
        latest = db.execute(
            "SELECT max(last_seen_at) FROM chaika.accounts WHERE source_id=%s", (source,)
        ).fetchone()[0]
        if latest and latest > observed:
            raise SyncError("accounts_stale_observation")
        db.execute(
            "UPDATE chaika.accounts SET present_in_latest=false WHERE source_id=%s", (source,)
        )
        count = upsert_rows(
            db,
            "accounts",
            list(Account.model_fields)
            + [
                "source_id",
                "present_in_latest",
                "first_seen_at",
                "last_seen_at",
                "last_snapshot_id",
                "details",
            ],
            ["source_id", "id"],
            (
                {
                    **row.model_dump(),
                    "source_id": source,
                    "present_in_latest": True,
                    "first_seen_at": observed,
                    "last_seen_at": observed,
                    "last_snapshot_id": snapshot["id"],
                    "details": Jsonb(row.model_dump(mode="json")),
                }
                for row in rows
            ),
        )
        absent = db.execute(
            "SELECT count(*) FROM chaika.accounts WHERE source_id=%s AND NOT present_in_latest",
            (source,),
        ).fetchone()[0]
        links = account_links(db, source)
    return {
        "accounts": count,
        "deleted": summary["deleted_count"],
        "without_code": summary["without_code"],
        "absent_from_latest": absent,
        "with_account_parent": sum(r.account_parent_id is not None for r in rows),
        **links,
    }


def synchronize_accounts(settings: Settings, api_url: str = "http://127.0.0.1:8010") -> dict:
    api_url = check_local_api(api_url)
    if not settings.database_url.get_secret_value():
        raise SyncError("database_not_configured")
    source = configured_sources(settings)[0]
    with (
        psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            application_name="chaika-accounts-sync",
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
            "error_code='interrupted' WHERE job='accounts' AND status='running'"
        )
        run_id = uuid4()
        db.execute(
            "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'accounts','running')",
            (run_id,),
        )
        failure, touched, logout_ok = None, False, True
        with httpx.Client(base_url=api_url, timeout=180, trust_env=False) as client:
            try:
                response = client.get("/api/v1/iiko/connections")
                response.raise_for_status()
                primary = [r for r in response.json()["items"] if r["connection_id"] == source.id]
                if len(primary) != 1 or primary[0]["base_url"] != source.base_url:
                    raise SyncError("backend_source_config_mismatch")
                touched = True
                response = client.post("/api/v1/iiko/dictionaries/accounts/load")
                if response.status_code != 200:
                    raise SyncError(f"accounts_http_{response.status_code}")
                snapshot = capture_dictionary(settings, source, "accounts", response.json())
            except BaseException as error:
                failure = error
            finally:
                if touched:
                    try:
                        r = client.post("/api/v1/iiko/connections/primary/logout")
                        logout_ok = r.status_code == 200 and r.json()["state"] == "logged_out"
                    except Exception:
                        logout_ok = False
        if failure is None and not logout_ok:
            failure = SyncError("accounts_logout_failed")
        if failure is None:
            try:
                with db.transaction():
                    append_snapshot(db, run_id, snapshot)
                    counts = publish_accounts(db, snapshot)
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
            "observed_at": snapshot["observed_at"].isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "counts": counts,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8010")
    args = parser.parse_args()
    try:
        report = synchronize_accounts(Settings(), args.api_url)
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
