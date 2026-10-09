"""Telegram proof of an existing binding, without creating identities or grants."""

import hashlib
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException

from app.documents.context import lock_resource, runtime_of
from app.documents.policy import fail
from app.documents.telegram_identity import bot_namespace, credential_digest
from app.documents.telegram_link import TOKEN, _available, _username

INVALID = "Ссылка недействительна или уже использована. Запросите новую в Telegram."
UNAVAILABLE = (
    "Восстановление недоступно. Проверьте привязку Telegram или обратитесь к администратору."
)


def digest(token):
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        fail(400, INVALID)
    return hashlib.sha256(token.encode()).hexdigest()


def identity(db, telegram_id):
    if type(telegram_id) is not int or not 0 < telegram_id < 2**63:
        fail(400, UNAVAILABLE)
    # Count ALL bindings, including inactive/orphaned ones: ambiguity must fail closed.
    rows = db.execute(
        "SELECT u.id,u.is_active,u.telegram_id,l.supabase_id,l.revision "
        "FROM authentication_user u LEFT JOIN portal_documents_userlink l ON l.user_id=u.id "
        "WHERE u.telegram_id=%s ORDER BY u.id",
        (telegram_id,),
    ).fetchall()
    if len(rows) != 1 or not rows[0]["is_active"] or not rows[0]["supabase_id"]:
        fail(400, UNAVAILABLE)
    row = rows[0]
    db.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (lock_resource(db, "link:" + str(row["supabase_id"])),),
    )
    locked = db.execute(
        "SELECT u.id,u.is_active,u.telegram_id,l.supabase_id,l.revision "
        "FROM authentication_user u JOIN portal_documents_userlink l ON l.user_id=u.id "
        "WHERE u.id=%s FOR UPDATE OF u,l",
        (row["id"],),
    ).fetchone()
    if locked != row:
        fail(400, UNAVAILABLE)
    profile = db.execute(
        "SELECT active FROM portal_access WHERE id=%s", (row["supabase_id"],)
    ).fetchone()
    if not profile or not profile["active"]:
        fail(400, UNAVAILABLE)
    return row


def start_url(service):
    if not _available(service):
        fail(503, UNAVAILABLE)
    return {"url": f"https://t.me/{_username(service)}?start=recover"}


def issue(service, telegram_id):
    if not _available(service):
        fail(503, UNAVAILABLE)
    origin = service.settings.dashboard_url.rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        fail(503, UNAVAILABLE)
    raw = secrets.token_urlsafe(32)
    with service.database.connection() as db:
        row = identity(db, telegram_id)
        previous = db.execute(
            "SELECT created_at>clock_timestamp()-interval '1 minute' AS limited, "
            "claimed_at IS NOT NULL AND expires_at>clock_timestamp() AS busy "
            "FROM native_password_recovery WHERE portal_id=%s",
            (row["supabase_id"],),
        ).fetchone()
        if previous and (previous["limited"] or previous["busy"]):
            fail(
                429,
                "Повторите запрос позже. Предыдущая ссылка ещё обрабатывается "
                "или была создана недавно.",
            )
        db.execute("DELETE FROM native_password_recovery WHERE portal_id=%s", (row["supabase_id"],))
        db.execute(
            "INSERT INTO native_password_recovery"
            "(token_hash,portal_id,user_id,telegram_id,revision,expires_at) "
            "VALUES (%s,%s,%s,%s,%s,clock_timestamp()+interval '10 minutes')",
            (
                credential_digest(raw, bot_namespace(service)),
                row["supabase_id"],
                row["id"],
                telegram_id,
                row["revision"],
            ),
        )
    return origin + "/reset-password#token=" + raw


def claim(service, raw):
    digest(raw)  # Validate the opaque token before constructing its bot-bound digest.
    hashed = credential_digest(raw, bot_namespace(service))
    with service.database.connection() as db:
        result = db.execute(
            "UPDATE native_password_recovery SET claimed_at=clock_timestamp() "
            "WHERE token_hash=%s AND claimed_at IS NULL "
            "AND expires_at>clock_timestamp() RETURNING *",
            (hashed,),
        ).fetchone()
        if not result:
            fail(400, INVALID)
    return result


def locked_identity(db, claim):
    row = identity(db, claim["telegram_id"])
    if (row["supabase_id"], row["id"], row["revision"]) != (
        claim["portal_id"],
        claim["user_id"],
        claim["revision"],
    ):
        fail(400, INVALID)
    return row


def handle_update(service, bot, update):
    callback = update.get("callback_query") or {}
    message = callback.get("message") if callback else update.get("message")
    message = message or {}
    parts = (message.get("text") or "").split()
    requested = (
        not callback
        and len(parts) == 2
        and parts[0].split("@")[0] == "/start"
        and parts[1] == "recover"
    )
    confirmed = callback.get("data") == "recovery_confirm"
    if not requested and not confirmed:
        return False
    sender = (callback.get("from") if callback else message.get("from")) or {}
    chat = message.get("chat") or {}
    if (
        chat.get("type") != "private"
        or type(sender.get("id")) is not int
        or chat.get("id") != sender["id"]
        or sender.get("is_bot")
    ):
        return True
    if requested:
        brand = " Chaika Team" if runtime_of(service.database).mode == "legacy" else ""
        bot.call(
            "sendMessage",
            chat_id=sender["id"],
            text=f"Восстановить пароль{brand} для учётной записи, "
            "к которой привязан этот Telegram?",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "Восстановить пароль", "callback_data": "recovery_confirm"}]
                ]
            },
        )
        return True
    try:
        url = issue(service, sender["id"])
        bot.call(
            "sendMessage",
            chat_id=sender["id"],
            text="Ссылка действует 10 минут и используется один раз. Не пересылайте её. "
            "Если вы не запрашивали смену пароля, не открывайте ссылку.",
            reply_markup={"inline_keyboard": [[{"text": "Задать новый пароль", "url": url}]]},
        )
        text = "Ссылка отправлена в этот чат."
    except HTTPException as error:
        text = str(error.detail)
    bot.call(
        "answerCallbackQuery", callback_query_id=callback["id"], text=text[:190], show_alert=True
    )
    return True
