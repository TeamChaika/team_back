"""Detached worker for a persisted 60-day refresh request."""

import argparse
import json
import os
import signal
from datetime import date
from pathlib import Path
from threading import Event
from uuid import UUID

from app.core.config import Settings
from app.progress import read_json
from app.services.sync_jobs import ROOT, SyncJobsService, now, write_json
from app.sync_invoices import HistoryRequest, synchronize_histories
from app.sync_references import SyncError


def run_job(service: SyncJobsService, job_id: UUID, stop: Event) -> None:
    directory = service.job_directory(job_id)
    path = directory / "job.json"
    with service.launch_lock(wait=True):
        manifest = read_json(path)
        if manifest is None or manifest["status"] != "accepted":
            raise SyncError("sync_job_not_accepted")
        manifest.update(pid=os.getpid(), status="running")
        write_json(path, manifest)
    status, error_code = "failed", None
    try:
        start, end = (
            date.fromisoformat(manifest["date_from"]),
            date.fromisoformat(manifest["date_to"]),
        )
        if (end - start).days != 59:
            raise SyncError("sync_refresh_invalid_period")

        def announce():
            # Publish ownership only after the shared database lock is acquired.
            for name in ("invoices", "writeoffs"):
                write_json(
                    service.directory / f"{name}-process.json",
                    {
                        "pid": os.getpid(),
                        "job_id": str(job_id),
                        "mode": "rolling_60_days",
                        "date_from": start.isoformat(),
                        "date_to": end.isoformat(),
                    },
                )

        synchronize_histories(
            service.settings,
            "http://127.0.0.1:8010",
            start,
            end,
            [
                HistoryRequest(
                    "writeoffs",
                    directory / "writeoffs.json",
                    mirror=service.directory / "writeoffs-latest.json",
                ),
                HistoryRequest(
                    "incoming_invoices",
                    directory / "invoices.json",
                    mirror=service.directory / "invoices-latest.json",
                ),
            ],
            stop,
            on_ready=announce,
        )
        status = "succeeded"
    except Exception as error:
        error_code = str(error) if isinstance(error, SyncError) else type(error).__name__
        raise
    finally:
        manifest.update(status=status, error_code=error_code, finished_at=now().isoformat())
        write_json(path, manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", type=UUID, required=True)
    parser.add_argument("--directory", type=Path, default=ROOT)
    args = parser.parse_args()
    stop = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        run_job(SyncJobsService(Settings(), args.directory), args.job_id, stop)
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
