import asyncio
import logging
import sys

from app.core.config import BACKEND_DIR

log = logging.getLogger(__name__)


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
