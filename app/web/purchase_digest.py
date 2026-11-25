"""Primary Dashboard administration of morning procurement reports."""

from datetime import datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.documents.config import DocumentSettings
from app.operation_settings import load_settings
from app.web.low_markup_reports import DestinationWrite, _verified_chat
from app.web.repository import serial

MOSCOW = ZoneInfo("Europe/Moscow")


def next_report_date(now):
    local = now.astimezone(MOSCOW)
    return local.date() if local.time() < time(9) else local.date() + timedelta(days=1)


def sync_enabled(policy, department_id):
    selection = policy["sync"]
    return selection["mode"] == "all" or str(department_id) in selection["department_ids"]


def read_settings(administration):
    with administration.repo.connection(repeatable=True) as db:
        policy = load_settings(db)
        rows = db.execute(
            """
            SELECT n.id,n.name,d.chat_id::text,d.chat_title,coalesce(d.enabled,false) AS enabled,
                   coalesce(d.revision,0) AS revision,d.start_date,
                   (SELECT jsonb_build_object('business_date',l.business_date,'state',l.state,
                           'error_code',l.error_code,'updated_at',l.updated_at)
                    FROM chaika.purchase_digest_deliveries l WHERE l.department_id=n.id
                    ORDER BY l.business_date DESC LIMIT 1) AS delivery
            FROM chaika.corporate_nodes n
            LEFT JOIN chaika.purchase_digest_destinations d ON d.department_id=n.id
            WHERE n.source_id='primary' AND n.type='DEPARTMENT' ORDER BY n.name,n.id
            """
        ).fetchall()
    archived = set(policy["analytics"]["archived_department_ids"])
    for row in rows:
        row["archived"] = str(row["id"]) in archived
        row["sync_enabled"] = sync_enabled(policy, row["id"])
    bot = DocumentSettings()
    return serial(
        {
            "send_time": "09:00",
            "timezone": str(MOSCOW),
            "bot_username": bot.bot_username,
            "bot_configured": bool(bot.bot_token.get_secret_value()),
            "departments": rows,
        }
    )


def current_destination(db, department_id, payload):
    row = db.execute(
        "SELECT * FROM chaika.purchase_digest_destinations WHERE department_id=%s FOR UPDATE",
        (department_id,),
    ).fetchone()
    if payload.revision != (row["revision"] if row else 0):
        raise HTTPException(409, "Настройки рассылки изменились. Обновите страницу.")
    if not db.execute(
        "SELECT 1 FROM chaika.corporate_nodes WHERE id=%s AND source_id='primary' "
        "AND type='DEPARTMENT'",
        (department_id,),
    ).fetchone():
        raise HTTPException(404, "Заведение не найдено.")
    if payload.enabled:
        policy = load_settings(db)
        if str(department_id) in policy["analytics"]["archived_department_ids"]:
            raise HTTPException(422, "Заведение в архиве.")
        if not sync_enabled(policy, department_id):
            raise HTTPException(422, "Для рассылки включите синхронизацию заведения.")
    return row


def save_destination(administration, actor, department_id: UUID, payload: DestinationWrite):
    with administration.write(actor) as db:
        previous = current_destination(db, department_id, payload)
    if previous and not payload.enabled and str(previous["chat_id"]) == payload.chat_id:
        bot_id, title = previous["bot_id"], previous["chat_title"]
    else:
        bot_id, title = _verified_chat(DocumentSettings(), payload.chat_id)

    with administration.write(actor) as db:
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("chaika:operation-settings",)
        )
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"chaika:purchase-digest:{department_id}",),
        )
        previous = current_destination(db, department_id, payload)
        same_route = (
            previous
            and previous["enabled"]
            and previous["bot_id"] == bot_id
            and str(previous["chat_id"]) == payload.chat_id
        )
        start = previous["start_date"] if same_route else next_report_date(datetime.now(MOSCOW))
        db.execute(
            """
            INSERT INTO chaika.purchase_digest_destinations
                (department_id,chat_id,chat_title,bot_id,enabled,revision,updated_by,start_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(department_id) DO UPDATE SET
                chat_id=excluded.chat_id,chat_title=excluded.chat_title,
                bot_id=excluded.bot_id,enabled=excluded.enabled,revision=excluded.revision,
                updated_at=now(),updated_by=excluded.updated_by,start_date=excluded.start_date
            """,
            (
                department_id,
                int(payload.chat_id),
                title,
                bot_id,
                payload.enabled,
                payload.revision + 1,
                actor,
                start,
            ),
        )
        administration.audit(db, actor, "purchase_digest.destination.update", department_id)
    return read_settings(administration)
