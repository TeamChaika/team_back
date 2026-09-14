"""Publish the full Chain role catalogue, immutable RAW and UUID reconciliation atomically."""

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
from app.schemas.iiko_employee_roles import EmployeeRolesSnapshot, IikoEmployeeRole
from app.services.iiko_employee_roles import read_employee_roles, summarize_employee_roles
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


def capture_employee_roles(
    settings: Settings,
    source: Source,
    response: dict,
    directory: Path | None = None,
) -> dict:
    folder = directory if directory is not None else BACKEND_DIR / ".local/employee-roles"
    metadata = json.loads((folder / "current.json").read_text())
    if metadata["source_fingerprint"] != source.fingerprint or metadata["snapshot"] != response:
        raise SyncError("employee_roles_snapshot_mismatch")
    snapshot = EmployeeRolesSnapshot.model_validate(response)
    path = folder / f"{snapshot.snapshot_id}.xml"
    maximum = settings.iiko_employees_max_response_bytes
    if path.stat().st_size > maximum:
        raise SyncError("employee_roles_too_large")
    raw = path.read_bytes()
    if len(raw) != snapshot.source_bytes or hashlib.sha256(raw).hexdigest() != snapshot.sha256:
        raise SyncError("employee_roles_raw_mismatch")
    rows = read_employee_roles(path, maximum)
    if (
        not rows
        or len(rows) != snapshot.total
        or any(response[key] != value for key, value in summarize_employee_roles(rows).items())
    ):
        raise SyncError("employee_roles_incomplete")
    if snapshot.received_at.tzinfo is None:
        raise SyncError("observation_timezone_missing")
    return {
        "id": str(snapshot.snapshot_id),
        "source_id": source.id,
        "resource": "employee_roles",
        "observed_at": snapshot.received_at,
        "sha256": snapshot.sha256,
        "raw": raw,
        "payload": {**response, "items": [row.model_dump(mode="json") for row in rows]},
    }


def publish_employee_roles(db, snapshot: dict) -> dict:
    payload = snapshot["payload"]
    rows = [IikoEmployeeRole.model_validate(row, by_name=True) for row in payload["items"]]
    if (
        not rows
        or payload["revision_from"] != -1
        or payload["total"] != len(rows)
        or (len({row.id for row in rows}) != len(rows))
    ):
        raise SyncError("employee_roles_incomplete")
    source, observed = snapshot["source_id"], snapshot["observed_at"]
    with db.transaction():
        latest = db.execute(
            "SELECT max(last_seen_at) FROM chaika.employee_roles WHERE source_id=%s",
            (source,),
        ).fetchone()[0]
        if latest and latest > observed:
            raise SyncError("employee_roles_stale_observation")
        db.execute(
            "UPDATE chaika.employee_roles SET present_in_latest=false WHERE source_id=%s", (source,)
        )
        count = upsert_rows(
            db,
            "employee_roles",
            list(IikoEmployeeRole.model_fields)
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
                    **role.model_dump(),
                    "source_id": source,
                    "present_in_latest": True,
                    "first_seen_at": observed,
                    "last_seen_at": observed,
                    "last_snapshot_id": snapshot["id"],
                    "details": Jsonb(role.model_dump(mode="json")),
                }
                for role in rows
            ),
        )
        absent = db.execute(
            "SELECT count(*) FROM chaika.employee_roles "
            "WHERE source_id=%s AND NOT present_in_latest",
            (source,),
        ).fetchone()[0]
        employees, main, unresolved_main = db.execute(
            "SELECT count(*),count(e.main_role_id),"
            "count(*) FILTER(WHERE e.main_role_id IS NOT NULL AND r.id IS NULL) "
            "FROM chaika.employees e LEFT JOIN chaika.employee_roles r "
            "ON r.source_id=e.source_id AND r.id=e.main_role_id "
            "WHERE e.source_id=%s AND e.present_in_latest",
            (source,),
        ).fetchone()
        refs, missing, absent_refs, deleted_refs = db.execute(
            "SELECT count(DISTINCT role_id),"
            "count(DISTINCT role_id) FILTER(WHERE NOT role_resolved),"
            "count(DISTINCT role_id) FILTER(WHERE role_resolved AND NOT role_present_in_latest),"
            "count(DISTINCT role_id) FILTER(WHERE role_deleted) "
            "FROM chaika.employee_role_assignments "
            "WHERE source_id=%s AND employee_present_in_latest",
            (source,),
        ).fetchone()
    return {
        "employee_roles": count,
        "absent_from_latest": absent,
        **summarize_employee_roles(rows),
        "employees_checked": employees,
        "employees_with_main_role": main,
        "employees_main_role_resolved": main - unresolved_main,
        "employees_main_role_unresolved": unresolved_main,
        "referenced_role_ids": refs,
        "unresolved_role_ids": missing,
        "absent_referenced_role_ids": absent_refs,
        "deleted_referenced_role_ids": deleted_refs,
    }


def synchronize_employee_roles(settings: Settings, api_url: str = "http://127.0.0.1:8010") -> dict:
    api_url = check_local_api(api_url)
    if not settings.database_url.get_secret_value():
        raise SyncError("database_not_configured")
    key = settings.sync_api_key.get_secret_value()
    if not key:
        raise SyncError("sync_key_not_configured")
    source = configured_sources(settings)[0]
    with (
        psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            application_name="chaika-employee-roles-sync",
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
            "error_code='interrupted' WHERE job='employee_roles' AND status='running'"
        )
        run_id = uuid4()
        db.execute(
            "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'employee_roles','running')",
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
                response = client.post(
                    "/api/v1/iiko/employee-roles/load", headers={"X-Sync-Key": key}
                )
                if response.status_code != 200:
                    raise SyncError(f"employee_roles_http_{response.status_code}")
                snapshot = capture_employee_roles(settings, source, response.json())
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
            failure = SyncError("employee_roles_logout_failed")
        if failure is None:
            try:
                with db.transaction():
                    append_snapshot(db, run_id, snapshot)
                    counts = publish_employee_roles(db, snapshot)
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
        report = synchronize_employee_roles(Settings(), args.api_url)
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
