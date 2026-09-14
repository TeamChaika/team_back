"""Synchronize the current Chain employee dictionary via the local FastAPI API."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.core.config import BACKEND_DIR, Settings
from app.schemas.iiko_employees import IikoEmployee, IikoEmployeesSnapshot
from app.services.iiko_employees import read_employees, summarize_employees
from app.sync_inventory import upsert_rows
from app.sync_references import (
    Source,
    SyncError,
    append_snapshot,
    check_local_api,
    configured_sources,
    reference_lock,
    register_sources,
)


def capture_employees(
    settings: Settings, source: Source, response: dict, directory: Path | None = None
) -> dict:
    """Only publish a complete, source-matched export whose RAW hash is verified."""
    folder = directory if directory is not None else BACKEND_DIR / ".local/employees"
    metadata = json.loads((folder / "current.json").read_text())
    if metadata["source_fingerprint"] != source.fingerprint:
        raise SyncError("employees_source_mismatch")
    if metadata["snapshot"] != response:
        raise SyncError("employees_snapshot_changed")
    snapshot = IikoEmployeesSnapshot.model_validate(response)
    if snapshot.include_deleted or snapshot.revision_from != -1:
        raise SyncError("employees_scope_mismatch")
    path = folder / f"{snapshot.snapshot_id}.xml"
    maximum = settings.iiko_employees_max_response_bytes
    if path.stat().st_size > maximum:
        raise SyncError("employees_raw_too_large")
    raw = path.read_bytes()
    if len(raw) != snapshot.source_bytes or hashlib.sha256(raw).hexdigest() != snapshot.sha256:
        raise SyncError("employees_raw_mismatch")
    rows = read_employees(path, maximum)
    if len(rows) != snapshot.total or any(
        response[key] != value for key, value in summarize_employees(rows).items()
    ):
        raise SyncError("employees_count_mismatch")
    if not rows:
        raise SyncError("empty_employee_catalog")
    if snapshot.received_at.tzinfo is None:
        raise SyncError("observation_timezone_missing")
    return {
        "id": str(snapshot.snapshot_id),
        "source_id": source.id,
        "resource": "employees",
        "observed_at": snapshot.received_at,
        "sha256": snapshot.sha256,
        "raw": raw,
        "payload": {
            **response,
            "source_fingerprint": source.fingerprint,
            "items": [row.model_dump(mode="json") for row in rows],
        },
    }


def publish_employees(db, snapshot: dict) -> dict:
    """Upsert the full dictionary; absent records retain their original source flags."""
    payload = snapshot["payload"]
    rows = [IikoEmployee.model_validate(row, by_name=True) for row in payload["items"]]
    if not rows:
        raise SyncError("empty_employee_catalog")
    if (
        payload["include_deleted"] is not False
        or payload["revision_from"] != -1
        or payload["total"] != len(rows)
        or len({row.id for row in rows}) != len(rows)
        or any(row.deleted is True for row in rows)
    ):
        raise SyncError("employees_incomplete")
    source_id = snapshot["source_id"]

    def records():
        for employee in rows:
            # Keep native UUID arrays for psycopg; JSON uses their original text representation.
            yield {
                **employee.model_dump(),
                "source_id": source_id,
                "present_in_latest": True,
                "first_seen_at": snapshot["observed_at"],
                "last_seen_at": snapshot["observed_at"],
                "last_snapshot_id": snapshot["id"],
                "details": Jsonb(employee.model_dump(mode="json")),
            }

    with db.transaction():
        db.execute(
            "UPDATE chaika.employees SET present_in_latest=false WHERE source_id=%s",
            (source_id,),
        )
        count = upsert_rows(
            db,
            "employees",
            list(IikoEmployee.model_fields)
            + [
                "source_id",
                "present_in_latest",
                "first_seen_at",
                "last_seen_at",
                "last_snapshot_id",
                "details",
            ],
            ["source_id", "id"],
            records(),
        )
        absent = db.execute(
            "SELECT count(*) FROM chaika.employees WHERE source_id=%s AND NOT present_in_latest",
            (source_id,),
        ).fetchone()[0]
    return {"employees": count, "absent_from_latest": absent, **summarize_employees(rows)}


def synchronize_employees(settings: Settings, api_url: str = "http://127.0.0.1:8010") -> dict:
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
            application_name="chaika-employee-sync",
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
            "error_code='interrupted' WHERE job='employees' AND status='running'"
        )
        db.execute(
            "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'employees','running')",
            (run_id,),
        )
        touched, logout_ok, failure, counts = False, True, None, {}
        with httpx.Client(base_url=api_url, timeout=180, trust_env=False) as client:
            try:
                response = client.get("/api/v1/iiko/connections")
                response.raise_for_status()
                primary = [r for r in response.json()["items"] if r["connection_id"] == source.id]
                if len(primary) != 1 or primary[0]["base_url"] != source.base_url:
                    raise SyncError("backend_source_config_mismatch")
                touched = True
                response = client.post("/api/v1/iiko/employees/load")
                if response.status_code != 200:
                    raise SyncError(f"employees_http_{response.status_code}")
                snapshot = capture_employees(settings, source, response.json())
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
            failure = SyncError("employees_logout_failed")
        if failure is None:
            try:
                with db.transaction():
                    append_snapshot(db, run_id, snapshot)
                    counts = publish_employees(db, snapshot)
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8010")
    args = parser.parse_args()
    try:
        report = synchronize_employees(Settings(), args.api_url)
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
