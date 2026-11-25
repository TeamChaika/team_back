"""Primary Dashboard's 09:00 Moscow purchase workbooks and durable Telegram receipts."""

import logging
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from threading import Thread
from zoneinfo import ZoneInfo

import httpx

from app.documents.config import DocumentSettings
from app.documents.telegram import Telegram
from app.documents.telegram_cleanup import TelegramError
from app.low_markup_delivery import MIME, bot_id, connect
from app.web.sync_policy import read_policy

log = logging.getLogger(__name__)
ZONE = ZoneInfo("Europe/Moscow")
CUTOFF = time(9)
LOCK = "chaika:purchase-digest"


def through_date(now):
    local = now.astimezone(ZONE)
    return local.date() if local.time() >= CUTOFF else local.date() - timedelta(days=1)


@contextmanager
def _lock(db, key):
    locked = db.execute(
        "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired", (key,)
    ).fetchone()["acquired"]
    try:
        yield locked
    finally:
        if locked:
            db.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))


def _pending(db, day):
    # Failed days do not hold back other days or restaurants. Date arithmetic on
    # integers is independent of the database session's timezone.
    rows = db.execute(
        "SELECT d.*,n.name,d.start_date+slot AS business_date "
        "FROM chaika.purchase_digest_destinations d JOIN chaika.corporate_nodes n "
        "ON n.id=d.department_id AND n.source_id='primary' AND n.type='DEPARTMENT' "
        "CROSS JOIN LATERAL generate_series(0,%s::date-d.start_date) AS days(slot) "
        "LEFT JOIN chaika.purchase_digest_deliveries l ON l.department_id=d.department_id "
        "AND l.business_date=d.start_date+slot WHERE d.enabled AND "
        "(l.state IS NULL OR (l.state='failed' AND l.next_attempt_at<=now())) "
        "ORDER BY business_date,d.department_id",
        (day,),
    ).fetchall()
    if not rows:
        return []
    policy = read_policy(db)
    return [r for r in rows if _eligible(policy, r)]


def _eligible(policy, row):
    department = str(row["department_id"])
    return department not in policy.archived_departments and policy.permits([department])


def _current(db, target):
    # The settings API takes this venue's advisory lock before changing the route.
    return db.execute(
        "SELECT 1 FROM chaika.purchase_digest_destinations WHERE department_id=%s "
        "AND enabled AND revision=%s AND chat_id=%s AND bot_id=%s AND start_date<=%s",
        (target["department_id"], target["revision"], target["chat_id"],
         target["bot_id"], target["business_date"]),
    ).fetchone() and _eligible(read_policy(db), target)


def _reserve(db, target, *, error=None):
    # A committed 'sending' row survives process death between HTTP and receipt.
    return db.execute(
        "INSERT INTO chaika.purchase_digest_deliveries AS l "
        "(department_id,business_date,chat_id,bot_id,department_name,state,attempts,error_code,"
        "next_attempt_at) VALUES(%s,%s,%s,%s,%s,%s,1,%s,now()+%s*interval '1 second') "
        "ON CONFLICT(department_id,business_date) DO UPDATE SET chat_id=excluded.chat_id,"
        "bot_id=excluded.bot_id,department_name=excluded.department_name,state=excluded.state,"
        "attempts=l.attempts+1,error_code=excluded.error_code,next_attempt_at=excluded.next_attempt_at,"
        "updated_at=now() WHERE l.state='failed' AND l.next_attempt_at<=now() "
        "RETURNING department_id",
        (target["department_id"], target["business_date"], target["chat_id"], target["bot_id"],
         target["name"], "failed" if error else "sending", error, 300 if error else None),
    ).fetchone()


def _send(db, bot, target, workbook):
    if not _reserve(db, target):
        return
    state, message_id, error, delay = "unknown", None, "telegram_uncertain", None
    try:
        day = target["business_date"]
        filename = f"{target['name']} — закупки {day:%d.%m.%Y}.xlsx"
        # Telegram.call validates the destination, document size, file ID and message ID.
        result = bot.call(
            "sendDocument", chat_id=target["chat_id"], document=(filename, workbook, MIME),
            caption=f"{target['name']} · Закупки",
        )
        message_id = result["message_id"]
        state, error = "sent", None
    except TelegramError as exc:
        if exc.code == 429:
            state, error, delay = "failed", "telegram_rate_limited", exc.retry_after
        elif 400 <= exc.code < 500 and exc.description:
            state, error, delay = "failed", "telegram_rejected", 600
    except Exception:
        # Telegram provides no idempotency key; a timeout can follow acceptance.
        log.warning("Purchase digest delivery outcome is unknown")
    db.execute(
        "UPDATE chaika.purchase_digest_deliveries SET state=%s,message_id=%s,error_code=%s,"
        "next_attempt_at=now()+%s*interval '1 second',updated_at=now() "
        "WHERE department_id=%s AND business_date=%s AND state='sending'",
        (state, message_id, error, delay, target["department_id"], target["business_date"]),
    )


def _process(db, bot, identity, target, stop):
    from app.purchase_digest_report import build_report

    error = None
    workbook = None
    if not identity:
        error = "bot_not_configured"
    elif target["bot_id"] != identity:
        error = "bot_mismatch"
    else:
        try:
            with db.transaction():
                db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                workbook = build_report(db, target["department_id"], target["business_date"])
            if not workbook:
                raise ValueError("empty_workbook")
        except Exception as exc:
            log.warning("Purchase digest preparation failed: %s", type(exc).__name__)
            error = "report_failed"
    if stop.is_set():
        return
    # Hold this lock through HTTP and journal update. An admin's route change
    # either takes effect before the final recheck or waits for this delivery.
    with _lock(db, f"{LOCK}:{target['department_id']}") as acquired:
        if not acquired or not _current(db, target):
            return
        if error:
            _reserve(db, target, error=error)
        else:
            _send(db, bot, target, workbook)


def run_reports(settings, day, stop):
    with connect(settings) as db, _lock(db, LOCK + ":worker") as acquired:
        if not acquired:
            return
        # Only this worker can have an in-flight request while it holds the lock.
        db.execute(
            "UPDATE chaika.purchase_digest_deliveries SET state='unknown',"
            "error_code='interrupted',updated_at=now() WHERE state='sending'"
        )
        targets = _pending(db, min(day, through_date(datetime.now(UTC))))
        if not targets:
            return
        config = DocumentSettings()
        token = config.bot_token.get_secret_value()
        identity = bot_id(token) if token else None
        transport = httpx.HTTPTransport(local_address=config.telegram_local_address) if (
            token and config.telegram_local_address
        ) else None
        bot = Telegram(token, transport=transport) if token else None
        try:
            for row in targets:
                if stop.is_set():
                    return
                _process(db, bot, identity, row, stop)
        finally:
            if bot:
                bot.close()


def _loop(settings, stop):
    while not stop.is_set():
        try:
            run_reports(settings, through_date(datetime.now(UTC)), stop)
        except Exception as exc:
            log.warning("Purchase digest worker failed: %s", type(exc).__name__)
        now = datetime.now(ZONE)
        cutoff = datetime.combine(now.date(), CUTOFF, ZONE)
        delay = (cutoff - now).total_seconds()
        stop.wait(min(60, delay) if delay > 0 else 60)


def start_worker(settings, stop):
    """Keep a long iiko sync from delaying the daily purchase report."""
    worker = Thread(target=_loop, args=(settings, stop), daemon=True, name="purchase-digest")
    worker.start()
    return worker

