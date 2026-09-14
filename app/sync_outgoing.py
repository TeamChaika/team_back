"""Resumable outgoing-invoice history with one sequential request per day."""

import argparse
import json
import signal
from datetime import date
from pathlib import Path
from threading import Event
from uuid import UUID

from app.core.config import BACKEND_DIR, Settings
from app.schemas.iiko_outgoing import OutgoingInvoicesQuery
from app.sync_invoices import synchronize_invoices
from app.sync_references import SyncError


def synchronize_outgoing(settings: Settings, query: OutgoingInvoicesQuery) -> dict:
    return synchronize_invoices(
        settings,
        "http://127.0.0.1:8010",
        query.date_from,
        query.date_to,
        BACKEND_DIR / ".local/sync/outgoing-manual.json",
        resource="outgoing_invoices",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", required=True, type=date.fromisoformat)
    parser.add_argument("--date-to", required=True, type=date.fromisoformat)
    parser.add_argument("--resume-run", type=UUID)
    parser.add_argument("--api-url", default="http://127.0.0.1:8010")
    parser.add_argument(
        "--output", type=Path, default=BACKEND_DIR / ".local/sync/outgoing-latest.json"
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
            resource="outgoing_invoices",
        )
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
