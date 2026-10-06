"""Durable, idempotent removal of delivered approval previews."""

import logging

from app.documents.policy import table

log = logging.getLogger(__name__)


class TelegramError(ValueError):
    """Provider error with safe structured details (never request URLs/tokens)."""

    def __init__(self, code, description="", retry_after=None):
        super().__init__("Telegram request rejected")
        self.code = code if type(code) is int and 100 <= code <= 599 else 0
        self.description = description.lower() if isinstance(description, str) else ""
        self.retry_after = min(86400, max(1, retry_after)) if type(retry_after) is int else 60


def approved(db, kind, doc):
    """Called under the workflow's document lock in its approval transaction."""
    db.execute(
        "INSERT INTO native_telegram_cleanup(kind,document_id,approved_version) "
        "VALUES (%s,%s,%s) ON CONFLICT(kind,document_id) DO UPDATE SET "
        "approved_version=greatest(native_telegram_cleanup.approved_version,excluded.approved_version)",
        (kind, doc["id"], doc["version"]),
    )
    db.execute(
        "UPDATE native_telegram_messages SET state='pending',"
        "next_attempt_at=now(),updated_at=now() "
        "WHERE kind=%s AND document_id=%s AND version<=%s AND state='active'",
        (kind, doc["id"], doc["version"]),
    )
    db.execute(
        "UPDATE portal_documents_notification SET state='obsolete',updated_at=now() "
        "WHERE kind=%s AND document_id=%s AND version<=%s AND state='pending'",
        (kind, doc["id"], doc["version"]),
    )


def record(service, kind, doc, chat_id, message_id):
    # Serialize with approval: either approval sees this row, or this row sees its tombstone.
    with service.database.connection() as db:
        parent, _ = table(kind)
        current = db.execute(
            f"SELECT id,version FROM {parent} WHERE id=%s FOR UPDATE", (doc["id"],)
        ).fetchone()
        if not current or doc["version"] > current["version"]:
            return False
        db.execute(
            "INSERT INTO native_telegram_messages"
            "(chat_id,message_id,kind,document_id,version,state) "
            "VALUES (%s,%s,%s,%s,%s,CASE WHEN EXISTS (SELECT 1 FROM native_telegram_cleanup "
            "WHERE kind=%s AND document_id=%s AND approved_version>=%s) "
            "THEN 'pending' ELSE 'active' END) "
            "ON CONFLICT(chat_id,message_id) DO NOTHING",
            (chat_id, message_id, kind, doc["id"], doc["version"], kind, doc["id"], doc["version"]),
        )
    return True


def record_sent(service, bot, kind, doc, chat_id, message_id):
    """Keep known delivery IDs after transient DB failures; compensate if persistence fails."""
    for _ in range(3):
        try:
            if record(service, kind, doc, chat_id, message_id):
                return
        except Exception:
            pass
    compensated = False
    payload = {"chat_id": chat_id, "message_id": message_id}
    try:
        bot.call("deleteMessage", **payload)
        compensated = True
    except TelegramError as error:
        if error.code == 400 and "message to delete not found" in error.description:
            compensated = True
    except Exception:
        pass
    if not compensated:
        try:
            bot.call("editMessageReplyMarkup", **payload, reply_markup={"inline_keyboard": []})
            compensated = True
        except Exception:
            pass
    # Include only identifiers for repair; never log provider descriptions or exception URLs.
    log.error(
        "Telegram ledger unavailable: kind=%s document=%s chat=%s message=%s compensated=%s",
        kind,
        doc["id"],
        chat_id,
        message_id,
        compensated,
    )
    raise RuntimeError("Telegram delivery ledger unavailable")


def record_callback(service, kind, doc, data, original):
    """A legacy callback can register only its own recognizable document keyboard."""
    if not isinstance(original, dict):
        return
    chat = original.get("chat")
    markup = original.get("reply_markup")
    if not isinstance(chat, dict) or not isinstance(markup, dict):
        return
    chat_id, message_id = chat.get("id"), original.get("message_id")
    if (
        type(chat_id) is not int
        or not -(2**63) < chat_id < 2**63
        or chat_id == 0
        or type(message_id) is not int
        or not 0 < message_id < 2**63
    ):
        return
    keyboard = markup.get("inline_keyboard")
    if not isinstance(keyboard, list) or not any(
        isinstance(row, list)
        and any(isinstance(button, dict) and button.get("callback_data") == data for button in row)
        for row in keyboard
    ):
        return
    record(service, kind, doc, chat_id, message_id)


def eligible(service, kind, doc):
    with service.database.connection(readonly=True) as db:
        parent, _ = table(kind)
        row = db.execute(
            f"SELECT version,status,submission_state FROM {parent} WHERE id=%s "
            "AND NOT EXISTS (SELECT 1 FROM native_telegram_cleanup WHERE kind=%s "
            "AND document_id=%s AND approved_version>=%s)",
            (doc["id"], kind, doc["id"], doc["version"]),
        ).fetchone()
        return bool(
            row
            and row["version"] == doc["version"]
            and row["status"] == "Created"
            and row["submission_state"] in {"idle", "failed"}
        )


def deliver_cleanup(service, bot):
    # Leader ensures a single network caller; holding the row lock also excludes parallel callers.
    with service.database.connection() as db:
        db.execute("SET LOCAL idle_in_transaction_session_timeout='0'")
        row = db.execute(
            "SELECT * FROM native_telegram_messages WHERE state='pending' "
            "AND next_attempt_at<=now() "
            "ORDER BY next_attempt_at,chat_id,message_id FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()
        if not row:
            return False
        state, remove_buttons, error, delay = "pending", row["remove_buttons"], None, 0
        payload = {"chat_id": row["chat_id"], "message_id": row["message_id"]}
        try:
            if remove_buttons:
                bot.call("editMessageReplyMarkup", **payload, reply_markup={"inline_keyboard": []})
                state = "buttons_removed"
            else:
                bot.call("deleteMessage", **payload)
                state = "deleted"
        except TelegramError as exc:
            error = f"telegram_{exc.code}"
            if exc.code == 400 and "message to delete not found" in exc.description:
                state = "deleted"
            elif (
                remove_buttons and exc.code == 400 and "message is not modified" in exc.description
            ):
                state = "buttons_removed"
            elif (
                not remove_buttons
                and exc.code == 400
                and (
                    "can't be deleted" in exc.description or "cannot be deleted" in exc.description
                )
            ):
                remove_buttons = True
            elif exc.code in {400, 403, 404}:
                state = "unavailable"
            elif exc.code == 429:
                delay = exc.retry_after
                # A batch must not hammer other previews in the same throttled chat.
                db.execute(
                    "UPDATE native_telegram_messages SET next_attempt_at=greatest("
                    "next_attempt_at,now()+%s*interval '1 second') "
                    "WHERE chat_id=%s AND state='pending'",
                    (delay, row["chat_id"]),
                )
        except Exception:
            error = "transport"
        if state == "pending" and not delay and error and remove_buttons == row["remove_buttons"]:
            delay = min(3600, 5 * 2 ** min(row["attempts"], 10))
        db.execute(
            "UPDATE native_telegram_messages SET state=%s,remove_buttons=%s,attempts=attempts+1,"
            "last_error=%s,next_attempt_at=now()+%s*interval '1 second',updated_at=now() "
            "WHERE chat_id=%s AND message_id=%s",
            (state, remove_buttons, error, delay, row["chat_id"], row["message_id"]),
        )
    return True
