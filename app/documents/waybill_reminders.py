"""One private reminder per receiving approver before a waybill's automatic receipt."""

from uuid import uuid4

import httpx

from app.documents.auto_receipt_deadlines import deadlines_query
from app.documents.policy import warehouse_grant_guard
from app.documents.reads import ZONE
from app.documents.telegram_cleanup import TelegramError, record_sent


def reminder_candidates(db):
    deadlines, params = deadlines_query(db)
    query = f"""
        SELECT w.id,w.version,e.due_at,u.id AS recipient_id,u.telegram_id,l.supabase_id,
               s.name AS source,t.name AS target
        FROM ({deadlines}) e JOIN waybills w ON w.id=e.id AND w.version=e.version
        JOIN stores s ON s.id=w.store_id JOIN stores t ON t.id=w.counteragent_id
        JOIN portal_documents_grant g ON g.kind='waybill' AND g.store_id=w.counteragent_id
            AND g.actions @> '["approve"]'::jsonb
        JOIN authentication_user u ON u.id=g.user_id AND u.is_active AND u.telegram_id>0
        LEFT JOIN portal_documents_userlink l ON l.user_id=u.id
        LEFT JOIN portal_access p ON p.id=l.supabase_id
        WHERE (l.user_id IS NULL OR (p.active AND to_jsonb(p.sections) ? 'transfers'))
          AND e.due_at>clock_timestamp() AND e.due_at<=now()+interval '12 hours'
    """ + warehouse_grant_guard("portal_documents_grant", alias="g")
    return query, params


def claim_reminder(service):
    with service.database.connection() as db:
        db.execute(
            """UPDATE waybill_auto_receipt_reminders SET state='unknown',updated_at=now()
               WHERE state='sending' AND updated_at<now()-interval '5 minutes'"""
        )
        query, params = reminder_candidates(db)
        candidate = db.execute(
            query
            + """
              AND NOT EXISTS (SELECT 1 FROM waybill_auto_receipt_reminders r
                  WHERE r.document_id=w.id AND r.recipient_id=u.id AND
                    (r.state NOT IN ('reserved','retry') OR r.retry_at>now()))
            ORDER BY e.due_at,w.id,u.id LIMIT 1 FOR UPDATE OF w SKIP LOCKED
            """,
            params,
        ).fetchone()
        if not candidate:
            return None
        claim_id = uuid4()
        reserved = db.execute(
            """INSERT INTO waybill_auto_receipt_reminders AS r
              (document_id,recipient_id,claim_id,state,retry_at)
              VALUES (%s,%s,%s,'reserved',now()+interval '2 minutes')
            ON CONFLICT(document_id,recipient_id) DO UPDATE SET claim_id=excluded.claim_id,
              state='reserved',updated_at=now(),retry_at=excluded.retry_at
            WHERE r.state IN ('reserved','retry') AND r.retry_at<=now() RETURNING claim_id""",
            (candidate["id"], candidate["recipient_id"], claim_id),
        ).fetchone()
        return {**candidate, "claim_id": claim_id} if reserved else None


def send_reminder(service, bot, job):
    identity = (job["id"], job["recipient_id"], job["claim_id"])
    message_id = None
    with service.database.connection() as db:
        # Only a reservation that never started delivery may be taken over after a crash.
        if not db.execute(
            """UPDATE waybill_auto_receipt_reminders SET
               state='sending',updated_at=now(),retry_at=NULL
               WHERE document_id=%s AND recipient_id=%s AND claim_id=%s AND state='reserved'
               RETURNING document_id""",
            identity,
        ).fetchone():
            return
    with service.database.connection() as db:
        query, params = reminder_candidates(db)
        # Protect the decision through the one Telegram call. A concurrent approval or edit
        # must complete before this recheck or wait until after the reminder is issued.
        doc = db.execute(
            query
            + """ AND w.id=%s AND w.version=%s AND u.id=%s AND u.telegram_id=%s
                        AND e.due_at=%s FOR SHARE OF w,u,g SKIP LOCKED""",
            (
                *params,
                job["id"],
                job["version"],
                job["recipient_id"],
                job["telegram_id"],
                job["due_at"],
            ),
        ).fetchone()
        if not doc:
            # No send was attempted. A changed document or newly granted recipient can
            # qualify again at its actual deadline.
            db.execute(
                """DELETE FROM waybill_auto_receipt_reminders
                   WHERE document_id=%s AND recipient_id=%s AND claim_id=%s""",
                identity,
            )
            return
        when = doc["due_at"].astimezone(ZONE).strftime("%d.%m.%Y в %H:%M")
        payload = {}
        if doc["supabase_id"]:
            base = service.settings.dashboard_url.rstrip("/")
            button = {
                "text": "Открыть перемещение",
                "url": f"{base}/transfers/documents/{doc['id']}",
            }
            payload["reply_markup"] = {"inline_keyboard": [[button]]}
            action = "Проверьте товар и укажите фактическое количество в карточке."
        else:
            action = (
                "Проверьте товар. Откройте /pending, чтобы согласовать или отклонить перемещение."
            )
        retry = None
        try:
            result = bot.call(
                "sendMessage",
                request_timeout=3,
                chat_id=doc["telegram_id"],
                text=(
                    f"Перемещение DJ{doc['id']:06d}\n{doc['source']} → {doc['target']}\n\n"
                    f"Если перемещение останется без изменений, автоприёмка пройдёт {when} МСК. "
                    f"{action}"
                ),
                **payload,
            )
            if (
                not isinstance(result, dict)
                or type(result.get("message_id")) is not int
                or result["message_id"] <= 0
                or not isinstance(result.get("chat"), dict)
                or result["chat"].get("id") != doc["telegram_id"]
            ):
                raise ValueError("Unexpected Telegram receipt")
            message_id = result["message_id"]
            state = "sent"
        except TelegramError as exc:
            if exc.code == 429:
                state, retry = "retry", exc.retry_after
            else:
                state = "rejected" if 400 <= exc.code < 500 else "unknown"
        except (httpx.ConnectTimeout, httpx.ConnectError):
            state, retry = "retry", 60
        except Exception:
            state = "unknown"
        db.execute(
            """UPDATE waybill_auto_receipt_reminders SET state=%s,updated_at=now(),
               retry_at=now()+%s*interval '1 second'
               WHERE document_id=%s AND recipient_id=%s AND claim_id=%s""",
            (state, retry, *identity),
        )
    if message_id is not None:
        record_sent(service, bot, "waybill", doc, doc["telegram_id"], message_id)


def deliver_reminder(service, bot):
    job = claim_reminder(service)
    if job is None:
        return False
    send_reminder(service, bot, job)
    return True
