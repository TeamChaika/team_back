"""Synchronize one RMS event day through the existing API session and global sync lock."""

import argparse
import json
from uuid import uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.core.config import BACKEND_DIR, Settings
from app.event_storage import capture_snapshot, publish_events
from app.schemas.iiko_events import EventsSyncQuery
from app.services.iiko_events import read_event_types
from app.sync_references import (
    SyncError,
    check_local_api,
    configured_sources,
    reference_lock,
    register_sources,
)


def synchronize_events(
    settings: Settings, query: EventsSyncQuery, api_url: str = "http://127.0.0.1:8010"
) -> dict:
    api_url = check_local_api(api_url)
    if not settings.database_url.get_secret_value():
        raise SyncError("database_not_configured")
    source = next((s for s in configured_sources(settings) if s.id == query.source_id), None)
    if source is None or source.id == "primary":
        raise SyncError("events_rms_required")
    key = settings.sync_api_key.get_secret_value()
    if not key:
        raise SyncError("sync_key_not_configured")
    with (
        psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            application_name="chaika-events-sync",
        ) as db,
        reference_lock(db),
    ):
        register_sources(db, [source])
        if not db.execute(
            "SELECT 1 FROM chaika.sources s JOIN chaika.rms_bindings b ON b.source_id=s.id "
            "WHERE s.id=%s AND s.server_type='REPLICATED_RMS' AND b.state='matched'",
            (source.id,),
        ).fetchone():
            raise SyncError("events_rms_mapping_required")
        db.execute(
            "UPDATE chaika.sync_runs SET status='failed',finished_at=now(),"
            "error_code='interrupted' WHERE job='events' AND status='running'"
        )
        run_id = uuid4()
        db.execute(
            "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'events','running')", (run_id,)
        )
        failure, touched, logout_ok = None, False, True
        with httpx.Client(base_url=api_url, timeout=180, trust_env=False) as client:
            try:
                response = client.get("/api/v1/iiko/connections")
                response.raise_for_status()
                found = [s for s in response.json()["items"] if s["connection_id"] == source.id]
                if len(found) != 1 or found[0]["base_url"] != source.base_url:
                    raise SyncError("backend_source_config_mismatch")
                touched = True
                response = client.get(
                    f"/api/v1/iiko/connections/{source.id}/events",
                    params={"date": query.date.isoformat()},
                    headers={"X-Sync-Key": key},
                )
                if response.status_code != 200:
                    raise SyncError(f"events_http_{response.status_code}")
                snapshot = capture_snapshot(
                    BACKEND_DIR / ".local/events" / source.id, source, response.json()
                )
                if snapshot["day"] != query.date:
                    raise SyncError("events_snapshot_scope_mismatch")
            except BaseException as error:
                failure = error
            finally:
                if touched:
                    try:
                        r = client.post(f"/api/v1/iiko/connections/{source.id}/logout")
                        logout_ok = r.status_code == 200 and r.json()["state"] == "logged_out"
                    except Exception:
                        logout_ok = False
        if failure is None and not logout_ok:
            failure = SyncError("events_logout_failed")
        if failure is None:
            try:
                with db.transaction():
                    counts = publish_events(
                        db, run_id, snapshot, read_event_types(snapshot["metadata_raw"])
                    )
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
            "snapshot_id": snapshot["id"],
            "source_id": source.id,
            "date": query.date.isoformat(),
            "status": "succeeded",
            "counts": counts,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    try:
        result = synchronize_events(
            Settings(), EventsSyncQuery(source_id=args.source_id, date=args.date)
        )
    except (SyncError, ValueError, psycopg.Error) as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
