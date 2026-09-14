"""Resumable incoming-invoice history, one sequential calendar day at a time."""

import argparse
import json
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.core.config import BACKEND_DIR, Settings
from app.invoice_lines import numbered_invoice_items
from app.outgoing_storage import publish_outgoing_invoices
from app.sync_inventory import capture_inventory, publish_writeoffs, upsert_rows
from app.sync_references import (
    Source,
    SyncError,
    append_snapshot,
    check_local_api,
    configured_sources,
    reference_lock,
    register_sources,
)
from app.transfer_storage import publish_transfers

RESOURCE = "incoming_invoices"


@dataclass(frozen=True)
class HistoryRequest:
    resource: str
    output: Path
    resume: UUID | None = None
    mirror: Path | None = None


def initial_progress(source: Source, start: date, end: date, resource: str = RESOURCE) -> dict:
    if resource not in {RESOURCE, "writeoffs", "outgoing_invoices", "transfers"}:
        raise SyncError("unsupported_history_resource")
    if start > end or end >= date.max:
        raise SyncError("invoice_history_invalid_period")
    return {
        "resource": resource,
        "mode": "daily_history",
        "source_fingerprint": source.fingerprint,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "next_date": start.isoformat(),
        "completed_days": 0,
        "total_days": (end - start).days + 1,
        "documents_read": 0,
        "items_read": 0,
    }


def prepare_run(
    db, source: Source, start: date, end: date, resume: UUID | None, resource: str = RESOURCE
):
    progress = initial_progress(source, start, end, resource)
    run_id = resume or uuid4()
    with db.transaction():
        if resume:
            row = db.execute(
                "SELECT job,counts FROM chaika.sync_runs WHERE id=%s FOR UPDATE", (resume,)
            ).fetchone()
            scope = ("resource", "mode", "source_fingerprint", "date_from", "date_to", "total_days")
            if not row or row[0] != "inventory" or any(row[1].get(k) != progress[k] for k in scope):
                raise SyncError("invoice_history_resume_scope_mismatch")
            progress = row[1]
            next_day = date.fromisoformat(progress["next_date"])
            if not start <= next_day <= end + timedelta(days=1) or (
                progress["completed_days"] != (next_day - start).days
            ):
                raise SyncError("invoice_history_invalid_checkpoint")
            db.execute(
                "UPDATE chaika.sync_runs SET status='running',finished_at=NULL,error_code=NULL "
                "WHERE id=%s",
                (run_id,),
            )
        else:
            db.execute(
                "INSERT INTO chaika.sync_runs(id,job,status,counts) "
                "VALUES(%s,'inventory','running',%s)",
                (run_id, Jsonb(progress)),
            )
    return run_id, progress


def publish_invoices(db, snapshot: dict, day: date) -> None:
    """Publish only returned documents; absence from an export is not deletion."""
    parents = snapshot["payload"]["items"]
    source_id = snapshot["source_id"]
    header_fields = [
        "id",
        "document_number",
        "date_incoming",
        "incoming_date",
        "status",
        "supplier_id",
        "default_store_id",
        "revision",
        "last_export_date",
        "source_id",
        "first_seen_at",
        "last_seen_at",
        "last_snapshot_id",
        "details",
    ]

    def headers():
        for row in parents:
            details = {k: v for k, v in row.items() if k != "items"}
            yield {
                **details,
                "source_id": source_id,
                "last_export_date": day,
                "first_seen_at": snapshot["observed_at"],
                "last_seen_at": snapshot["observed_at"],
                "last_snapshot_id": snapshot["id"],
                "details": Jsonb(details),
            }

    upsert_rows(db, RESOURCE, header_fields, ["source_id", "id"], headers())
    db.execute(
        "UPDATE chaika.incoming_invoice_items SET present_in_latest=false "
        "WHERE source_id=%s AND document_id=ANY(%s::uuid[])",
        (source_id, [r["id"] for r in parents]),
    )
    upsert_rows(
        db,
        "incoming_invoice_items",
        [
            "source_id",
            "document_id",
            "num",
            "num_occurrence",
            "product_id",
            "store_id",
            "amount",
            "actual_amount",
            "price",
            "sum",
            "amount_unit_id",
            "details",
            "present_in_latest",
        ],
        ["source_id", "document_id", "num", "num_occurrence"],
        (
            {
                **item,
                "source_id": source_id,
                "document_id": parent["id"],
                "num_occurrence": occurrence,
                "details": Jsonb(item),
                "present_in_latest": True,
            }
            for parent in parents
            for item, occurrence in numbered_invoice_items(parent["items"])
        ),
    )


def commit_day(db, run_id: UUID, progress: dict, snapshot: dict) -> dict:
    """RAW, headers, lines and the next-day checkpoint commit or roll back together."""
    day = date.fromisoformat(progress["next_date"])
    payload = snapshot["payload"]
    if (
        snapshot["source_id"] != "primary"
        or snapshot["resource"] != progress["resource"]
        or payload["sync_business_date"] != day.isoformat()
        or payload["source_fingerprint"] != progress["source_fingerprint"]
        or day > date.fromisoformat(progress["date_to"])
    ):
        raise SyncError("invoice_history_snapshot_scope_mismatch")
    updated = {
        **progress,
        "next_date": (day + timedelta(days=1)).isoformat(),
        "completed_through": day.isoformat(),
        "completed_days": progress["completed_days"] + 1,
        "documents_read": progress["documents_read"] + len(payload["items"]),
        "items_read": progress["items_read"] + sum(len(r["items"]) for r in payload["items"]),
        "last_snapshot_id": snapshot["id"],
    }
    with db.transaction():
        append_snapshot(db, run_id, snapshot)
        if progress["resource"] == RESOURCE:
            publish_invoices(db, snapshot, day)
        elif progress["resource"] == "writeoffs":
            publish_writeoffs(db, snapshot, day)
        elif progress["resource"] == "transfers":
            updated["last_day_checks"] = publish_transfers(db, snapshot, day)
        elif progress["resource"] == "outgoing_invoices":
            updated["last_day_checks"] = publish_outgoing_invoices(db, snapshot, day)
        else:
            raise SyncError("unsupported_history_resource")
        result = db.execute(
            "UPDATE chaika.sync_runs SET counts=%s WHERE id=%s AND status='running' "
            "AND counts->>'next_date'=%s",
            (Jsonb(updated), run_id, day.isoformat()),
        )
        if result.rowcount != 1:
            raise SyncError("invoice_history_checkpoint_conflict")
    return updated


def emit(
    output: Path,
    run_id: UUID,
    status: str,
    progress: dict,
    *,
    mirror: Path | None = None,
    **extra,
) -> dict:
    report = {
        "run_id": str(run_id),
        "status": status,
        "counts": progress,
        "updated_at": datetime.now(UTC).isoformat(),
        **extra,
    }
    for destination in [output] + ([mirror] if mirror is not None else []):
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


def fetch_day(client: httpx.Client, day: date, stop: Event, resource: str = RESOURCE) -> dict:
    path = {
        RESOURCE: "incoming-invoices",
        "writeoffs": "writeoffs",
        "outgoing_invoices": "outgoing-invoices",
        "transfers": "transfers",
    }[resource]
    params = {"date_from": day.isoformat(), "date_to": day.isoformat()}
    if resource != "outgoing_invoices":
        params["revision_from"] = -1
    for attempt in range(3):
        if stop.is_set():
            raise SyncError("invoice_history_interrupted")
        try:
            response = client.get(
                "/api/v1/iiko/" + path,
                params=params,
            )
            if response.status_code == 200:
                return response.json()
            code = f"invoice_history_http_{response.status_code}"
            retryable = response.status_code in {502, 503, 504}
            # Repeating a rejected document format cannot repair that format.
            try:
                failure = response.json()
                upstream_code = failure.get("error", {}).get("code")
            except (ValueError, AttributeError):
                upstream_code = None
            if upstream_code in {
                "iiko_invoices_invalid_response",
                "iiko_writeoffs_invalid_response",
                "iiko_outgoing_invalid_response",
                "iiko_transfers_invalid_response",
                "iiko_transfers_export_failed",
            }:
                code, retryable = upstream_code, False
        except httpx.TransportError:
            code, retryable = "invoice_history_api_unavailable", True
        if not retryable or attempt == 2:
            raise SyncError(code)
        print(
            json.dumps({"retry_date": day.isoformat(), "attempt": attempt + 1, "error_code": code}),
            flush=True,
        )
        stop.wait(5 * (attempt + 1))
    raise AssertionError("unreachable")


def synchronize_histories(
    settings: Settings,
    api_url: str,
    start: date,
    end: date,
    requests: list[HistoryRequest],
    stop: Event | None = None,
    *,
    on_ready: Callable[[], None] | None = None,
) -> list[dict]:
    """Round-robin days under one lock and one shared iiko session."""
    stop = stop if stop is not None else Event()
    api_url = check_local_api(api_url)
    source = configured_sources(settings)[0]
    if (
        not requests
        or len({job.resource for job in requests}) != len(requests)
        or len({job.output.resolve() for job in requests}) != len(requests)
    ):
        raise SyncError("invalid_history_requests")
    for job in requests:
        initial_progress(source, start, end, job.resource)
    if not settings.database_url.get_secret_value():
        raise SyncError("database_not_configured")
    with (
        psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            application_name="chaika-document-history",
            options="-c statement_timeout=120000",
        ) as db,
        reference_lock(db),
        httpx.Client(base_url=api_url, timeout=180, trust_env=False) as client,
    ):
        register_sources(db, [source])
        if not db.execute(
            "SELECT 1 FROM chaika.sources WHERE id='primary' AND server_type='CHAIN'"
        ).fetchone():
            raise SyncError("reference_sync_required")
        response = client.get("/api/v1/iiko/connections")
        response.raise_for_status()
        primary = [r for r in response.json()["items"] if r["connection_id"] == "primary"]
        if len(primary) != 1 or primary[0]["base_url"] != source.base_url:
            raise SyncError("backend_source_config_mismatch")
        jobs = []
        with db.transaction():
            for job in requests:
                run_id, progress = prepare_run(db, source, start, end, job.resume, job.resource)
                jobs.append((job, run_id, progress))
        failure, touched, logout_ok = None, False, None
        try:
            if on_ready is not None:
                on_ready()
            for job, run_id, progress in jobs:
                emit(job.output, run_id, "running", progress, mirror=job.mirror)
            while any(date.fromisoformat(state["next_date"]) <= end for _, _, state in jobs):
                for job, run_id, progress in jobs:
                    day = date.fromisoformat(progress["next_date"])
                    if day > end:
                        continue
                    if stop.is_set():
                        raise SyncError("invoice_history_interrupted")
                    touched = True
                    result = fetch_day(client, day, stop, job.resource)
                    snapshot = capture_inventory(settings, source, job.resource, result, day)
                    progress.update(commit_day(db, run_id, progress, snapshot))
                    status = "succeeded" if day == end else "running"
                    if status == "succeeded":
                        db.execute(
                            "UPDATE chaika.sync_runs SET status='succeeded',finished_at=now() "
                            "WHERE id=%s",
                            (run_id,),
                        )
                    emit(job.output, run_id, status, progress, mirror=job.mirror)
        except BaseException as error:
            failure = error
        finally:
            if touched or any(job.resume for job in requests):
                try:
                    response = client.post("/api/v1/iiko/connections/primary/logout")
                    logout_ok = response.status_code == 200 and (
                        response.json()["state"] == "logged_out"
                    )
                except Exception:
                    logout_ok = False
        if logout_ok is False and failure is None:
            failure = SyncError("invoice_history_logout_failed")
        code = (
            (str(failure) if isinstance(failure, SyncError) else type(failure).__name__)
            if failure
            else None
        )
        reports = []
        for job, run_id, progress in jobs:
            incomplete = date.fromisoformat(progress["next_date"]) <= end
            status = "failed" if failure and (incomplete or logout_ok is False) else "succeeded"
            job_code = code if status == "failed" else None
            try:
                db.execute(
                    "UPDATE chaika.sync_runs SET status=%s,finished_at=coalesce(finished_at,now()),"
                    "error_code=%s WHERE id=%s",
                    (status, job_code, run_id),
                )
            except psycopg.Error:
                status, job_code = "failed", "invoice_history_database_disconnected"
                code = job_code
            reports.append(
                emit(
                    job.output,
                    run_id,
                    status,
                    progress,
                    mirror=job.mirror,
                    error_code=job_code,
                    logout_ok=logout_ok,
                )
            )
        if failure or any(report["status"] == "failed" for report in reports):
            raise SyncError(code) from None
        return reports


def synchronize_invoices(
    settings: Settings,
    api_url: str,
    start: date,
    end: date,
    output: Path,
    resume: UUID | None = None,
    stop: Event | None = None,
    *,
    resource: str = RESOURCE,
) -> dict:
    return synchronize_histories(
        settings, api_url, start, end, [HistoryRequest(resource, output, resume)], stop
    )[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--date-to",
        type=date.fromisoformat,
        default=datetime.now(ZoneInfo("Europe/Simferopol")).date() - timedelta(days=1),
    )
    parser.add_argument("--resume-run", type=UUID)
    parser.add_argument("--api-url", default="http://127.0.0.1:8010")
    parser.add_argument(
        "--output", type=Path, default=BACKEND_DIR / ".local/sync/invoices-latest.json"
    )
    args = parser.parse_args()
    stop = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        synchronize_invoices(
            Settings(),
            args.api_url,
            args.date_from,
            args.date_to,
            args.output,
            args.resume_run,
            stop,
        )
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
