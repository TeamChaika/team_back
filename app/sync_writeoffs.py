"""Writeoff history; optionally start only after a complete successful invoice history."""

import argparse
import json
import signal
from datetime import date
from pathlib import Path
from threading import Event
from uuid import UUID

import psycopg

from app.core.config import BACKEND_DIR, Settings
from app.sync_invoices import synchronize_invoices
from app.sync_references import SyncError, configured_sources


def prerequisite_state(row, start: date, end: date, fingerprint: str) -> bool:
    if row is None:
        raise SyncError("invoice_prerequisite_missing")
    status, counts = row
    if (
        counts.get("resource") != "incoming_invoices"
        or counts.get("source_fingerprint") != fingerprint
        or date.fromisoformat(counts["date_from"]) > start
        or date.fromisoformat(counts["date_to"]) < end
    ):
        raise SyncError("invoice_prerequisite_scope_mismatch")
    if status == "failed":
        raise SyncError("invoice_prerequisite_failed")
    if status == "succeeded":
        if counts.get("completed_days") != counts.get("total_days") or date.fromisoformat(
            counts["next_date"]
        ) <= date.fromisoformat(counts["date_to"]):
            raise SyncError("invoice_prerequisite_incomplete")
        return True
    if status != "running":
        raise SyncError("invoice_prerequisite_invalid_status")
    return False


def wait_for_invoices(settings: Settings, run_id: UUID, start: date, end: date, stop: Event):
    fingerprint = configured_sources(settings)[0].fingerprint
    print(json.dumps({"status": "waiting_for_invoices", "invoice_run_id": str(run_id)}), flush=True)
    while not stop.is_set():
        with psycopg.connect(
            settings.database_url.get_secret_value(),
            connect_timeout=10,
            options="-c default_transaction_read_only=on -c statement_timeout=10000",
        ) as db:
            row = db.execute(
                "SELECT status,counts FROM chaika.sync_runs WHERE id=%s", (run_id,)
            ).fetchone()
            if prerequisite_state(row, start, end, fingerprint):
                return
        stop.wait(15)
    raise SyncError("writeoff_history_interrupted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", type=date.fromisoformat, required=True)
    parser.add_argument("--date-to", type=date.fromisoformat, required=True)
    parser.add_argument("--resume-run", type=UUID)
    parser.add_argument("--after-invoice-run", type=UUID)
    parser.add_argument("--api-url", default="http://127.0.0.1:8010")
    parser.add_argument(
        "--output", type=Path, default=BACKEND_DIR / ".local/sync/writeoffs-latest.json"
    )
    args = parser.parse_args()
    stop = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        settings = Settings()
        if args.after_invoice_run:
            wait_for_invoices(settings, args.after_invoice_run, args.date_from, args.date_to, stop)
        synchronize_invoices(
            settings,
            args.api_url,
            args.date_from,
            args.date_to,
            args.output,
            args.resume_run,
            stop,
            resource="writeoffs",
        )
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(
            json.dumps({"status": "failed", "resource": "writeoffs", "error_code": code}),
            flush=True,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
