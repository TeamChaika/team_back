"""Telegram notifications and existing versioned approval buttons without Django."""

import json
from uuid import NAMESPACE_URL, uuid5

import httpx
from fastapi import HTTPException

from app.documents import reads
from app.documents.messages import document_messages
from app.documents.policy import actor, require


class Telegram:
    def __init__(self, token, transport=None):
        if not token:
            raise ValueError("Document bot token is required")
        self.client = httpx.Client(
            base_url=f"https://api.telegram.org/bot{token}/",
            timeout=25,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def call(self, method, *, photo=None, **payload):
        if photo is None:
            response = self.client.post(method, json=payload)
        else:
            fields = {
                key: json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list, bool))
                else str(value)
                for key, value in payload.items()
            }
            response = self.client.post(
                method, data=fields, files={"photo": ("document.png", photo, "image/png")}
            )
        if response.status_code != 200:
            raise ValueError("Telegram request unavailable")
        data = response.json()
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise ValueError("Telegram request rejected")
        return data.get("result")

    def close(self):
        self.client.close()


def preview(bot, settings, chat_id, kind, doc):
    prefix = "Waybill" if kind == "waybill" else "writeoff"
    section = "transfers" if kind == "waybill" else "writeoffs"
    buttons = {
        "inline_keyboard": [
            [
                {
                    "text": "Открыть накладную ↗" if kind == "waybill" else "Открыть списание ↗",
                    "url": f"{settings.dashboard_url.rstrip('/')}/{section}/documents/{doc['id']}",
                }
            ],
            [
                {
                    "text": "✓ Согласовать",
                    "callback_data": f"confirm{prefix}:{doc['id']}:{doc['version']}",
                },
                {
                    "text": "✕ Отклонить",
                    "callback_data": f"deny{prefix}:{doc['id']}:{doc['version']}",
                },
            ],
        ]
    }
    if kind == "waybill" and doc.get("receipt_state") == "pending_sender":
        buttons["inline_keyboard"][1] = [
            {
                "text": "✓ Подтвердить факт",
                "callback_data": f"confirmReceipt:{doc['id']}:{doc['version']}",
            },
            {
                "text": "✕ Вернуть получателю",
                "callback_data": f"rejectReceipt:{doc['id']}:{doc['version']}",
            },
        ]
    result = None
    for text, page, total in document_messages(kind, doc):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if page == total:
            payload["reply_markup"] = buttons
        result = bot.call("sendMessage", **payload)
    return result["message_id"]


def deliver_notification(service, bot):
    with service.database.connection() as db:
        notice = db.execute(
            "SELECT * FROM portal_documents_notification WHERE state='pending' "
            "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()
        if not notice:
            return False
        kind = notice["kind"]
        doc = db.execute(reads.joined(kind) + " WHERE d.id=%s", (notice["document_id"],)).fetchone()
        state = "sending"
        recipient = db.execute(
            "SELECT telegram_id FROM authentication_user WHERE id=%s", (notice["recipient_id"],)
        ).fetchone()
        if (
            not doc
            or doc["version"] != notice["version"]
            or doc["status"] != "Created"
            or doc["submission_state"] in {"queued", "sending", "unknown"}
        ):
            state = "obsolete"
        elif not recipient or not recipient["telegram_id"]:
            state = "skipped"
        else:
            try:
                user = actor(db, telegram_id=recipient["telegram_id"], kind=kind)
                sender = kind == "waybill" and doc.get("receipt_state") == "pending_sender"
                store = (
                    doc["counteragent_id"] if kind == "waybill" and not sender else doc["store_id"]
                )
                require(db, user["id"], kind, "edit" if sender else "approve", store)
                data = {**reads.serialize(kind, doc), "items": reads.items(db, kind, doc["id"])}
            except HTTPException as error:
                if error.status_code >= 500:
                    raise  # Missing catalog is retryable before any Telegram message.
                state = "skipped"
        db.execute(
            "UPDATE portal_documents_notification SET "
            "state=%s,attempts=attempts+%s,updated_at=now() WHERE id=%s",
            (state, int(state == "sending"), notice["id"]),
        )
    if state != "sending":
        return True
    message_id = None
    try:
        message_id = preview(bot, service.settings, recipient["telegram_id"], kind, data)
        state = "sent"
    except Exception:
        state = "unknown"  # Includes partial multipart delivery; do not automatically resend.
    with service.database.connection() as db:
        db.execute(
            "UPDATE portal_documents_notification SET state=%s,message_id=%s,updated_at=now() "
            "WHERE id=%s AND state='sending'",
            (state, message_id, notice["id"]),
        )
    return True


def handle_update(service, bot, update):
    from app.documents.password_recovery import handle_update as recovery_update

    if recovery_update(service, bot, update):
        return
    callback = update.get("callback_query")
    if callback:
        data = callback.get("data", "")
        parts = data.split(":")
        text = "Кнопка старой версии. Выполните /pending и проверьте актуальный состав документа."
        prefixes = {
            "confirmReceipt": ("waybill", "confirm_receipt"),
            "rejectReceipt": ("waybill", "reject_receipt"),
            "confirmWaybill": ("waybill", "confirm"),
            "denyWaybill": ("waybill", "deny"),
            "confirmwriteoff": ("writeoff", "confirm"),
            "denywriteoff": ("writeoff", "deny"),
        }
        if (
            len(parts) == 3
            and parts[0] in prefixes
            and all(p.isdigit() and 0 < int(p) < 2**63 for p in parts[1:])
        ):
            kind, action = prefixes[parts[0]]
            doc_id, version = map(int, parts[1:])
            user_id = callback["from"]["id"]
            key = uuid5(
                NAMESPACE_URL,
                f"chaika:{user_id}:{callback['id']}:{kind}:{doc_id}:{version}:{action}",
            )
            try:
                result = service.bot_action(
                    user_id, kind, action, doc_id, {"request_id": str(key), "version": version}
                )
                if result["submission_state"] == "queued":
                    text = (
                        "Согласовано. Документ в очереди: "
                        "отправим в iiko автоматически при наличии связи."
                    )
                elif result.get("receipt_state") == "rejected":
                    text = "Расхождения отклонены. Накладная возвращена получателю на проверку."
                elif result["status"] == "Sent":
                    text = "Документ отправлен в iiko."
                elif result["status"] == "Denied":
                    text = "Заявка отклонена."
                else:
                    text = "Результат отправки требует проверки. Откройте карточку документа."
            except HTTPException as error:
                text = str(error.detail)
        bot.call(
            "answerCallbackQuery",
            callback_query_id=callback["id"],
            text=text[:190],
            show_alert=True,
        )
        return
    message = update.get("message") or {}
    parts = (message.get("text") or "").split()
    text = parts[0].split("@")[0] if parts else ""
    if text not in {"/start", "/pending"}:
        return
    # Do not disclose private document previews into a group chat.
    if message.get("chat", {}).get("type") != "private":
        return
    chat_id, user_id = message["chat"]["id"], message["from"]["id"]
    if text == "/start" and len(parts) >= 2 and parts[1].startswith("link_"):
        from app.documents.telegram_link import consume, consume_digest

        try:
            if "_telegram_link_digest" in update:
                consume_digest(service, update["_telegram_link_digest"], user_id)
            else:
                consume(service, parts[1][5:], user_id)
            result = "Telegram подключён. Вернитесь в «Мой профиль»."
        except HTTPException as error:
            result = str(error.detail)
        bot.call("sendMessage", chat_id=chat_id, text=result)
        return
    if text == "/start":
        bot.call(
            "sendMessage",
            chat_id=chat_id,
            text=f"Ваш Telegram ID: {user_id}. Передайте его администратору dashboard.\n"
            "/pending — документы на согласовании.",
        )
        return
    count, failures = 0, 0
    for kind in ("waybill", "writeoff"):
        try:
            rows = service.pending(user_id, kind)
        except HTTPException:
            failures += 1
            continue
        for doc in rows:
            preview(bot, service.settings, chat_id, kind, doc)
            count += 1
    if failures or not count:
        bot.call(
            "sendMessage",
            chat_id=chat_id,
            text="Часть разделов недоступна. Проверьте права."
            if failures
            else "Нет документов, доступных вам для согласования.",
        )
