"""One native worker for iiko dispatch, catalogs and the existing Telegram bot."""

import logging
import signal
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic

import httpx
from psycopg.types.json import Jsonb

from app.core.logging import configure_http_logging
from app.documents.config import DocumentSettings
from app.documents.dispatch import deliver_one, recover_uncertain
from app.documents.policy import identifier
from app.documents.reads import ZONE
from app.documents.service import DocumentService
from app.documents.telegram import Telegram, deliver_notification, handle_update
from app.documents.telegram_cleanup import deliver_cleanup
from app.documents.telegram_link import queued_update

log = logging.getLogger(__name__)
LEADER_LOCK = 7623011102049


@contextmanager
def worker_leader(database):
    with database.connection() as leader:
        # Network calls deliberately run while this transaction owns the lock.
        # The legacy role's idle timeout must not release it mid-delivery and
        # allow another replica to start polling Telegram concurrently.
        leader.execute("SET LOCAL idle_in_transaction_session_timeout='0'")
        acquired = leader.execute(
            "SELECT pg_try_advisory_xact_lock(%s) AS ok", (LEADER_LOCK,)
        ).fetchone()["ok"]
        yield leader if acquired else None


def refresh_catalogs(service):
    products, stores = service.provider.catalogs()
    with service.database.connection() as db:
        for row in stores:
            key = identifier(row["id"])
            if not isinstance(row["name"], str) or not 1 <= len(row["name"]) <= 128:
                raise ValueError("Invalid store name")
            # Preserve historic names/IDs and existing grants, like the legacy importer.
            db.execute(
                "INSERT INTO stores (id,name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (key, row["name"]),
            )
        for name, data in (("products", products), ("stores", stores)):
            db.execute(
                "INSERT INTO native_catalog (name,data,updated_at) VALUES (%s,%s,now()) "
                "ON CONFLICT (name) DO UPDATE SET data=excluded.data,updated_at=now()",
                (name, Jsonb(data)),
            )


def catalog_due(service, now):
    local = now.astimezone(ZONE)
    slot = local.replace(hour=6, minute=0, second=0, microsecond=0)
    if slot > local:
        slot -= timedelta(days=1)
    with service.database.connection(readonly=True) as db:
        row = db.execute("SELECT updated_at FROM native_catalog WHERE name='products'").fetchone()
    return not row or row["updated_at"] < slot


def recover_bot_jobs(service):
    with service.database.connection() as db:
        # Callback mutations use a stable request UUID, so replay after a crash
        # can complete the existing operation without a second approval.
        # Plain messages and notifications have no Telegram idempotency key.
        db.execute(
            "UPDATE native_bot_updates SET state=CASE WHEN data ? 'callback_query' "
            "THEN 'pending' ELSE 'unknown' END,updated_at=now() "
            "WHERE state='processing' AND updated_at<now()-interval '5 minutes'"
        )
        db.execute(
            "UPDATE portal_documents_notification SET state='unknown',updated_at=now() "
            "WHERE state='sending' AND updated_at<now()-interval '5 minutes'"
        )


def poll(service, bot):
    with service.database.connection(readonly=True) as db:
        row = db.execute("SELECT data FROM native_jobs WHERE name='telegram-offset'").fetchone()
        offset = row["data"].get("offset", 0) if row else 0
    updates = bot.call(
        "getUpdates",
        offset=offset,
        timeout=2,
        limit=20,
        allowed_updates=["message", "callback_query"],
    )
    if not isinstance(updates, list):
        raise ValueError("Invalid Telegram updates")
    if updates:
        with service.database.connection() as db:
            for update in updates:
                db.execute(
                    "INSERT INTO native_bot_updates (id,data) VALUES (%s,%s) ON CONFLICT DO "
                    "NOTHING",
                    (update["update_id"], Jsonb(queued_update(update))),
                )
            db.execute(
                "INSERT INTO native_jobs (name,data) VALUES ('telegram-offset',%s) "
                "ON CONFLICT (name) DO UPDATE SET data=excluded.data,updated_at=now()",
                (Jsonb({"offset": max(u["update_id"] for u in updates) + 1}),),
            )
    with service.database.connection() as db:
        job = db.execute(
            "SELECT * FROM native_bot_updates WHERE state='pending' ORDER BY id "
            "FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()
        if job:
            db.execute(
                "UPDATE native_bot_updates SET state='processing',updated_at=now() WHERE id=%s",
                (job["id"],),
            )
    if job:
        state = "done"
        try:
            handle_update(service, bot, job["data"])
        except Exception:
            state = "unknown"
            log.warning("Document bot update needs review")
        with service.database.connection() as db:
            db.execute(
                "UPDATE native_bot_updates SET state=%s,updated_at=now() WHERE id=%s",
                (state, job["id"]),
            )


def drain_cleanup(service, bot):
    """Drain a small recipient group promptly without starving dispatch or polling."""
    deadline = monotonic() + 2
    for _ in range(20):
        if monotonic() >= deadline or not deliver_cleanup(service, bot):
            break


def process_jobs(service, bot, leader):
    recover_uncertain(service.database)
    health = {}
    if bot:
        try:
            recover_bot_jobs(service)
            drain_cleanup(service, bot)
            deliver_notification(service, bot)
            poll(service, bot)
            # Bot approval committed during poll: clean every due recipient in this cycle.
            drain_cleanup(service, bot)
        except Exception as error:
            # Telegram availability must not invalidate iiko worker liveness.
            # Only fixed diagnostic categories are persisted; URLs/tokens are not.
            health["telegram_error"] = type(error).__name__
            for marker, category in (
                ("CERTIFICATE_VERIFY_FAILED", "tls_certificate"),
                ("Name or service not known", "dns"),
                ("nodename nor servname", "dns"),
                ("Network is unreachable", "network_unreachable"),
                ("Connection refused", "connection_refused"),
                ("Connection reset", "connection_reset"),
            ):
                if marker in str(error):
                    health["telegram_reason"] = category
                    break
            log.warning("Document Telegram unavailable (%s)", health)
    # Existing cleanup and approvals received in this cycle precede slow iiko calls.
    deliver_one(service)
    leader.execute(
        "INSERT INTO native_jobs (name,data) VALUES ('heartbeat',%s) "
        "ON CONFLICT (name) DO UPDATE SET data=excluded.data,updated_at=now()",
        (Jsonb(health),),
    )
    return not health


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-catalogs", action="store_true")
    args = parser.parse_args()
    configure_http_logging()
    settings = DocumentSettings()
    if not args.refresh_catalogs and (not settings.native_enabled or not settings.worker_enabled):
        raise SystemExit("Native document worker is disabled")
    service = DocumentService(settings)
    if args.refresh_catalogs:
        try:
            refresh_catalogs(service)
            print("Document catalogs refreshed")
        finally:
            service.close()
        return
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    bot = (
        Telegram(
            settings.bot_token.get_secret_value(),
            transport=httpx.HTTPTransport(local_address=settings.telegram_local_address)
            if settings.telegram_local_address
            else None,
        )
        if settings.bot_token.get_secret_value()
        else None
    )
    next_catalog_check = datetime.min.replace(tzinfo=UTC)
    try:
        while not stop.is_set():
            try:
                # Transaction advisory lock also works through transaction poolers.
                # Held for one bounded cycle; protects Telegram polling across app replicas.
                with worker_leader(service.database) as leader:
                    if leader is None:
                        stop.wait(3)
                        continue
                    now = datetime.now(UTC)
                    if now >= next_catalog_check:
                        next_catalog_check = now + timedelta(minutes=10)
                        try:
                            if catalog_due(service, now):
                                refresh_catalogs(service)
                        except Exception as error:
                            log.warning(
                                "Document catalog refresh failed (%s)", type(error).__name__
                            )
                    healthy = process_jobs(service, bot, leader)
                if not healthy:
                    stop.wait(5)
            except Exception as error:
                log.warning("Document worker cycle failed (%s)", type(error).__name__)
                stop.wait(5)
            stop.wait(1)
    finally:
        if bot:
            bot.close()
        service.close()


if __name__ == "__main__":
    main()
