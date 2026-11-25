import asyncio
import logging
import sys

from psycopg.types.json import Jsonb

from app.core.config import BACKEND_DIR

log = logging.getLogger(__name__)


async def schedule_auto_receipts(service, stop):
    """Queue due waybills even when the iiko sender runs on another host."""
    from app.documents.auto_receipt import queue_due_one

    def scan():
        queued = 0
        while queued < 10 and not stop.is_set() and queue_due_one(service):
            queued += 1
        with service.database.connection() as db:
            db.execute(
                "INSERT INTO native_jobs (name,data) VALUES ('waybill_auto_receipt',%s) "
                "ON CONFLICT (name) DO UPDATE SET data=excluded.data,updated_at=now()",
                (Jsonb({"queued": queued}),),
            )

    while not stop.is_set():
        try:
            await asyncio.to_thread(scan)
        except Exception as error:
            log.warning("Automatic waybill receipt scan failed: %s", type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            pass


async def supervise():
    """Restart an exited worker; durable reservations survive process/container restarts."""
    process = None
    try:
        while True:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.documents.worker",
                cwd=BACKEND_DIR,
            )
            code = await process.wait()
            log.warning("Document worker exited (%s); restarting", code)
            await asyncio.sleep(5)
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=90)
            except TimeoutError:
                process.kill()
                await process.wait()
