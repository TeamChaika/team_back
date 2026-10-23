"""Administrator-owned venue destinations for the daily markup report."""

import re
from uuid import UUID

import httpx
from fastapi import HTTPException
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.documents.config import DocumentSettings
from app.documents.telegram import Telegram
from app.low_markup_delivery import bot_id, table
from app.operation_settings import load_settings, settings_schema
from app.web.coverage import ZONE
from app.web.repository import serial


class DestinationWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int = Field(ge=0, strict=True)
    chat_id: str
    enabled: bool = Field(strict=True)

    @field_validator("chat_id")
    @classmethod
    def validate_chat(cls, value):
        if not re.fullmatch(r"-[1-9][0-9]{0,18}", value) or not -(2**63) < int(value) < 0:
            raise ValueError("Укажите отрицательный ID группы или канала.")
        return value


def read_settings(administration):
    config = DocumentSettings()
    with administration.repo.connection(repeatable=True) as db:
        selection = load_settings(db)["sync"]
        rows = db.execute(
            sql.SQL(
                "SELECT n.id,n.name,d.chat_id,d.chat_title,COALESCE(d.enabled,false) AS enabled,"
                "COALESCE(d.revision,0) AS revision,"
                "(SELECT jsonb_build_object('business_date',l.business_date,'state',l.state,"
                "'error_code',l.error_code,'updated_at',l.updated_at) FROM {} l "
                "WHERE l.department_id=n.id ORDER BY l.business_date DESC LIMIT 1) AS delivery "
                "FROM {} n LEFT JOIN {} d ON d.department_id=n.id "
                "WHERE n.source_id='primary' AND n.type='DEPARTMENT' ORDER BY n.name,n.id"
            ).format(
                table(db, "low_markup_deliveries"),
                table(db, "corporate_nodes"),
                table(db, "low_markup_destinations"),
            )
        ).fetchall()
    for row in rows:
        row["sync_enabled"] = (
            selection["mode"] == "all" or str(row["id"]) in selection["department_ids"]
        )
        row["chat_id"] = str(row["chat_id"]) if row["chat_id"] is not None else None
    return serial(
        {
            "send_time": "09:00",
            "timezone": str(ZONE),
            "bot_username": config.bot_username,
            "bot_configured": bool(config.bot_token.get_secret_value()),
            "departments": rows,
        }
    )


def _check_destination(db, department_id, payload):
    current = db.execute(
        sql.SQL("SELECT * FROM {} WHERE department_id=%s FOR UPDATE").format(
            table(db, "low_markup_destinations")
        ),
        (department_id,),
    ).fetchone()
    if payload.revision != (current["revision"] if current else 0):
        raise HTTPException(409, "Настройки уже изменены. Обновите страницу.")
    venue = db.execute(
        sql.SQL(
            "SELECT 1 FROM {} WHERE source_id='primary' AND id=%s AND type='DEPARTMENT'"
        ).format(table(db, "corporate_nodes")),
        (department_id,),
    ).fetchone()
    if not venue:
        raise HTTPException(404, "Заведение не найдено.")
    selection = load_settings(db)["sync"]
    if payload.enabled and (
        selection["mode"] != "all" and str(department_id) not in selection["department_ids"]
    ):
        raise HTTPException(422, "Сначала включите синхронизацию этого заведения.")
    return current


def _verified_chat(config, chat_id):
    token = config.bot_token.get_secret_value()
    if not token:
        raise HTTPException(422, "Сначала подключите Telegram-бота.")
    identity = bot_id(token)
    bot = Telegram(
        token,
        transport=httpx.HTTPTransport(local_address=config.telegram_local_address)
        if config.telegram_local_address
        else None,
    )
    try:
        chat = bot.call("getChat", chat_id=int(chat_id))
        member = bot.call("getChatMember", chat_id=int(chat_id), user_id=int(identity))
        if (
            not isinstance(chat, dict)
            or chat.get("id") != int(chat_id)
            or chat.get("type") not in {"group", "supergroup", "channel"}
            or not isinstance(chat.get("title"), str)
            or not chat["title"]
            or not isinstance(member, dict)
            or member.get("status") not in {"member", "administrator", "creator", "restricted"}
            or (
                chat.get("type") == "channel"
                and not (
                    member.get("status") == "administrator"
                    and member.get("can_post_messages") is True
                )
            )
            or (
                member.get("status") == "restricted"
                and not (member.get("is_member") and member.get("can_send_documents"))
            )
        ):
            raise HTTPException(
                422, "Бот должен иметь право отправлять файлы в группу или публиковать в канал."
            )
        if (
            member["status"] == "member"
            and chat.get("permissions", {}).get("can_send_documents") is False
        ):
            raise HTTPException(422, "Разрешите боту отправку файлов в группе.")
        return identity, chat["title"]
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            502, "Не удалось проверить чат. Проверьте ID и добавление бота."
        ) from None
    finally:
        bot.close()


def save_destination(administration, actor, department_id: UUID, payload: DestinationWrite):
    # Check the administrator and revision before contacting Telegram.
    with administration.write(actor) as db:
        current = _check_destination(db, department_id, payload)
    unchanged_disabled = (
        current and not payload.enabled and str(current["chat_id"]) == payload.chat_id
    )
    identity, title = (
        (current["bot_id"], current["chat_title"])
        if unchanged_disabled
        else _verified_chat(DocumentSettings(), payload.chat_id)
    )
    with administration.write(actor) as db:
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (settings_schema(db) + ":operation-settings",),
        )
        _check_destination(db, department_id, payload)
        db.execute(
            sql.SQL(
                "INSERT INTO {} "
                "(department_id,chat_id,chat_title,bot_id,enabled,revision,updated_by) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(department_id) DO UPDATE SET "
                "chat_id=excluded.chat_id,chat_title=excluded.chat_title,bot_id=excluded.bot_id,"
                "enabled=excluded.enabled,revision=excluded.revision,updated_by=excluded.updated_by,"
                "updated_at=now()"
            ).format(table(db, "low_markup_destinations")),
            (
                department_id,
                int(payload.chat_id),
                title,
                identity,
                payload.enabled,
                payload.revision + 1,
                getattr(actor, "auth_user_id", actor),
            ),
        )
        administration.audit(db, actor, "low_markup.destination.update", department_id)
    return read_settings(administration)
