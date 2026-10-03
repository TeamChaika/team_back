"""Single-use Telegram ownership proof; never grants document permissions."""

import hashlib
import re
import secrets
import time
from uuid import uuid4

import httpx
from psycopg import IntegrityError

from app.documents.policy import fail, identifier, profile

TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}\Z")
INVALID_LINK = "Ссылка недействительна или истекла. Создайте новую в «Мой профиль»."


def _username(service):
    configured = service.settings.bot_username.strip().lstrip("@")
    if configured and USERNAME.fullmatch(configured):
        return configured
    cached = getattr(service, "_telegram_username_cache", None)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    from app.documents.telegram import Telegram

    bot = Telegram(
        service.settings.bot_token.get_secret_value(),
        transport=httpx.HTTPTransport(local_address=service.settings.telegram_local_address)
        if service.settings.telegram_local_address
        else None,
    )
    try:
        result = bot.call("getMe")
        username = result.get("username", "") if isinstance(result, dict) else ""
        if not USERNAME.fullmatch(username):
            raise ValueError("Invalid bot username")
    except Exception:
        fail(503, "Telegram временно недоступен. Попробуйте позже.")
    finally:
        bot.close()
    service._telegram_username_cache = (username, time.monotonic() + 3600)
    return username


def _available(service):
    return bool(service.settings.native_enabled and service.settings.bot_token.get_secret_value())


def _identity(db, portal_id, *, lock=False, create=False):
    if lock:
        # Same namespace as administration.save_access; serializes profile creation/relinking.
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("link:" + str(portal_id),)
        )
    profile(db, portal_id)
    row = db.execute(
        "SELECT u.id,u.is_active,u.telegram_id,l.revision FROM authentication_user u "
        "JOIN portal_documents_userlink l ON l.user_id=u.id WHERE l.supabase_id=%s"
        + (" FOR UPDATE OF u,l" if lock else ""),
        (portal_id,),
    ).fetchone()
    if row and not row["is_active"]:
        fail(403, "Рабочий профиль отключён. Обратитесь к администратору.")
    if not row and create:
        user_id = db.execute(
            "INSERT INTO authentication_user "
            "(password,is_superuser,username,first_name,last_name,email,"
            "is_staff,is_active,date_joined) "
            "VALUES (%s,false,%s,'','','',false,true,now()) RETURNING id",
            ("!" + uuid4().hex, "portal_" + portal_id.hex),
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO portal_documents_userlink (user_id,supabase_id,revision) VALUES (%s,%s,1)",
            (user_id, portal_id),
        )
        row = {"id": user_id, "is_active": True, "telegram_id": None, "revision": 1}
    return row


def status(service, portal_id):
    with service.database.connection(readonly=True) as db:
        row = _identity(db, identifier(portal_id))
    telegram_id = row["telegram_id"] if row else None
    return {
        "available": _available(service),
        "linked": telegram_id is not None,
        "telegram_id": str(telegram_id) if telegram_id is not None else None,
    }


def issue(service, portal_id):
    if not _available(service):
        fail(503, "Подключение Telegram сейчас недоступно.")
    portal_id = identifier(portal_id)
    # Validate identity before contacting Telegram; validate again in mutation transaction.
    with service.database.connection(readonly=True) as db:
        _identity(db, portal_id)
    username = _username(service)
    token = secrets.token_urlsafe(32)
    try:
        with service.database.connection() as db:
            row = _identity(db, portal_id, lock=True, create=True)
            if row["telegram_id"] is not None:
                fail(409, "Telegram уже подключён. Сначала отключите текущую привязку.")
            db.execute("DELETE FROM native_telegram_link WHERE portal_id=%s", (portal_id,))
            result = db.execute(
                "INSERT INTO native_telegram_link "
                "(token_hash,portal_id,user_id,revision,expires_at) "
                "VALUES (%s,%s,%s,%s,clock_timestamp()+interval '10 minutes') RETURNING expires_at",
                (hashlib.sha256(token.encode()).hexdigest(), portal_id, row["id"], row["revision"]),
            ).fetchone()
    except IntegrityError:
        fail(409, "Рабочий профиль изменился. Обновите страницу.")
    return {
        "url": f"https://t.me/{username}?start=link_{token}",
        "expires_at": result["expires_at"].isoformat(),
    }


def unlink(service, portal_id):
    portal_id = identifier(portal_id)
    with service.database.connection() as db:
        row = _identity(db, portal_id, lock=True)
        db.execute("DELETE FROM native_telegram_link WHERE portal_id=%s", (portal_id,))
        if row:
            db.execute("UPDATE authentication_user SET telegram_id=NULL WHERE id=%s", (row["id"],))
            db.execute(
                "UPDATE portal_documents_userlink SET revision=revision+1 WHERE user_id=%s",
                (row["id"],),
            )
    return {"linked": False}


def consume(service, token, telegram_id):
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        fail(400, INVALID_LINK)
    return consume_digest(service, hashlib.sha256(token.encode()).hexdigest(), telegram_id)


def consume_digest(service, digest, telegram_id):
    """Internal worker entry point; never expose a digest-accepting HTTP endpoint."""
    if type(telegram_id) is not int or not 0 < telegram_id < 2**63:
        fail(400, INVALID_LINK)
    try:
        with service.database.connection() as db:
            token = db.execute(
                "SELECT * FROM native_telegram_link WHERE token_hash=%s", (digest,)
            ).fetchone()
            if not token:
                fail(400, INVALID_LINK)
            row = _identity(db, token["portal_id"], lock=True)
            token = db.execute(
                "SELECT * FROM native_telegram_link WHERE token_hash=%s "
                "AND expires_at>clock_timestamp() FOR UPDATE",
                (digest,),
            ).fetchone()
            if (
                not token
                or not row
                or row["id"] != token["user_id"]
                or row["revision"] != token["revision"]
            ):
                fail(400, INVALID_LINK)
            if row["telegram_id"] is not None:
                fail(409, "Telegram уже подключён. Сначала отключите текущую привязку.")
            db.execute(
                "UPDATE authentication_user SET telegram_id=%s WHERE id=%s",
                (telegram_id, row["id"]),
            )
            db.execute(
                "UPDATE portal_documents_userlink SET revision=revision+1 WHERE user_id=%s",
                (row["id"],),
            )
            db.execute("DELETE FROM native_telegram_link WHERE portal_id=%s", (token["portal_id"],))
    except IntegrityError:
        fail(409, "Этот Telegram уже подключён к другому профилю. Сначала отключите его там.")
    return {"linked": True}


def _redact_recovery(value):
    if isinstance(value, str):
        return re.sub(r"(/reset-password[#?]token=)[A-Za-z0-9_-]+", r"\1[redacted]", value)
    if isinstance(value, dict):
        return {key: _redact_recovery(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_recovery(item) for item in value]
    return value


def queued_update(update):
    """Discard raw deep-link credentials before writing Telegram's durable inbox."""
    update = _redact_recovery(dict(update))
    update.pop("_telegram_link_digest", None)
    message = update.get("message") or {}
    parts = (message.get("text") or "").split()
    if len(parts) >= 2 and parts[0].split("@")[0] == "/start" and parts[1].startswith("link_"):
        token = parts[1][5:]
        # Keep only fields needed by the handler, not quoted/forwarded raw text.
        update = {
            "update_id": update.get("update_id"),
            "message": {
                "text": "/start link_",
                "chat": message.get("chat", {}),
                "from": message.get("from", {}),
            },
            "_telegram_link_digest": hashlib.sha256(token.encode()).hexdigest()
            if TOKEN.fullmatch(token)
            else "invalid",
        }
    return update
