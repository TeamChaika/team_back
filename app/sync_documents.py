"""Interleave document histories under one session lock, with separate daily checkpoints."""

import argparse
import json
import signal
from datetime import date
from threading import Event
from uuid import UUID

from app.core.config import BACKEND_DIR, Settings
from app.sync_invoices import HistoryRequest, synchronize_histories
from app.sync_references import SyncError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", type=date.fromisoformat, required=True)
    parser.add_argument("--date-to", type=date.fromisoformat, required=True)
    parser.add_argument("--invoice-run", type=UUID)
    parser.add_argument("--writeoff-run", type=UUID)
    parser.add_argument("--outgoing-run", type=UUID)
    parser.add_argument("--transfer-run", type=UUID)
    parser.add_argument(
        "--resources",
        nargs="+",
        choices=["writeoffs", "invoices", "outgoing", "transfers"],
        default=["writeoffs", "invoices"],
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8010")
    args = parser.parse_args()
    stop = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        synchronize_histories(
            Settings(),
            args.api_url,
            args.date_from,
            args.date_to,
            [
                HistoryRequest(
                    {"invoices": "incoming_invoices", "outgoing": "outgoing_invoices"}.get(
                        key, key
                    ),
                    BACKEND_DIR / f".local/sync/{key}-latest.json",
                    {
                        "writeoffs": args.writeoff_run,
                        "invoices": args.invoice_run,
                        "outgoing": args.outgoing_run,
                        "transfers": args.transfer_run,
                    }[key],
                )
                for key in args.resources
            ],
            stop,
        )
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
