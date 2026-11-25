"""Exercise reminder timing and authorization with a fake Telegram and isolated DB."""

# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest

from app.documents.reads import ZONE
from app.documents.telegram_cleanup import TelegramError
from tests.test_native_documents import (
    PRODUCT,
    RECEIVER,
    SENDER,
    SOURCE,
    TARGET,
    act,
    body,
    create,
    database,  # noqa: F401
    service,  # noqa: F401
)
from tests.test_receipt_discrepancies import receive
from tests.test_waybill_auto_receipt import age_waybill, enabled  # noqa: F401


class Bot:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def call(self, method, **payload):
        assert method == "sendMessage"
        self.calls.append(payload)
        if self.error:
            raise self.error
        return {"chat": {"id": payload["chat_id"]}, "message_id": len(self.calls)}


def test_only_receiving_approvers_get_one_reminder_in_the_twelve_hour_window(service, enabled):
    from app.documents.waybill_reminders import deliver_reminder

    for hours in [59, 72, 80]:
        age_waybill(service, create(service), timedelta(hours=hours))
    doc = create(service)
    created = age_waybill(service, doc, timedelta(hours=60))
    with service.database.connection() as db:
        db.execute(
            """INSERT INTO portal_documents_grant(user_id,kind,store_id,actions) VALUES
            (3,'waybill',%s,'["approve","view"]'),(4,'waybill',%s,'["approve","view"]')""",
            (TARGET, SOURCE),
        )
    bot = Bot()
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda _: deliver_reminder(service, bot), range(3)))
    while deliver_reminder(service, bot):
        pass
    assert sorted(p["chat_id"] for p in bot.calls) == [102, 103]
    when = (created + timedelta(hours=72)).astimezone(ZONE).strftime("%d.%m.%Y в %H:%M")
    for p in bot.calls:
        assert doc["number"] in p["text"] and when in p["text"]
        assert "Receiver" in p["text"]
        assert p["reply_markup"]["inline_keyboard"][0][0]["url"].endswith(
            f"/transfers/documents/{doc['id']}"
        )
    act(service, doc, "edit", SENDER, body(version=1, comment="Changed"))
    assert not deliver_reminder(service, bot)
    assert not service.provider.sends


@pytest.mark.parametrize(
    "changed", ["approved", "disputed", "cancelled", "content", "access", "warehouse"]
)
def test_revalidates_between_reservation_and_telegram(service, enabled, changed):
    from app.documents.waybill_reminders import claim_reminder, send_reminder

    doc = create(service)
    age_waybill(service, doc, timedelta(hours=65))
    job = claim_reminder(service)
    assert job is not None
    if changed == "approved":
        act(service, doc)
    elif changed == "disputed":
        receive(service, doc)
    elif changed == "cancelled":
        act(service, doc, "cancel", SENDER)
    elif changed == "content":
        act(
            service,
            doc,
            "edit",
            SENDER,
            body(version=1, items=[{"product_id": str(PRODUCT), "amount": 2}]),
        )
    else:
        with service.database.connection() as db:
            if changed == "access":
                db.execute("UPDATE portal_access SET active=false WHERE id=%s", (RECEIVER,))
            else:
                db.execute(
                    "DELETE FROM portal_documents_grant WHERE kind='waybill' AND store_id=%s",
                    (TARGET,),
                )
    bot = Bot()
    send_reminder(service, bot, job)
    assert not bot.calls


def test_ambiguous_delivery_survives_restart_without_resend(service, enabled):
    from app.documents.waybill_reminders import deliver_reminder

    age_waybill(service, create(service), timedelta(hours=65))
    bot = Bot(httpx.ReadTimeout("lost acknowledgement"))
    assert deliver_reminder(service, bot)
    assert not deliver_reminder(service, Bot())
    with service.database.connection(readonly=True) as db:
        assert (
            db.execute("SELECT state FROM waybill_auto_receipt_reminders").fetchone()["state"]
            == "unknown"
        )


def test_rate_limit_retries_only_a_definitely_rejected_send_before_deadline(service, enabled):
    from app.documents.waybill_reminders import deliver_reminder

    age_waybill(service, create(service), timedelta(hours=65))
    assert deliver_reminder(service, Bot(TelegramError(429, "limited", 60)))
    bot = Bot()
    assert not deliver_reminder(service, bot)
    with service.database.connection() as db:
        db.execute("UPDATE waybill_auto_receipt_reminders SET retry_at=now()")
    assert deliver_reminder(service, bot)
    assert not deliver_reminder(service, bot)
    assert len(bot.calls) == 1


def test_a_crash_before_sending_releases_the_reservation_without_duplicates(service, enabled):
    from app.documents.waybill_reminders import claim_reminder, send_reminder

    age_waybill(service, create(service), timedelta(hours=65))
    original = claim_reminder(service)
    assert claim_reminder(service) is None
    with service.database.connection() as db:
        db.execute("UPDATE waybill_auto_receipt_reminders SET retry_at=now()-interval '1 minute'")
    replacement = claim_reminder(service)
    assert replacement and replacement["claim_id"] != original["claim_id"]
    bot = Bot()
    send_reminder(service, bot, original)
    assert not bot.calls
    send_reminder(service, bot, replacement)
    assert len(bot.calls) == 1


def test_a_crash_during_send_marks_uncertain_without_replay(service, enabled):
    from app.documents.waybill_reminders import claim_reminder, deliver_reminder

    age_waybill(service, create(service), timedelta(hours=65))
    assert claim_reminder(service)
    with service.database.connection() as db:
        db.execute(
            "UPDATE waybill_auto_receipt_reminders SET state='sending', "
            "updated_at=now()-interval '10 minutes'"
        )
    assert not deliver_reminder(service, Bot())
    with service.database.connection(readonly=True) as db:
        row = db.execute("SELECT state FROM waybill_auto_receipt_reminders").fetchone()
        assert row["state"] == "unknown"


def test_legacy_unlinked_approver_can_act_in_the_bot(service, enabled):
    from app.documents.waybill_reminders import deliver_reminder

    age_waybill(service, create(service), timedelta(hours=65))
    with service.database.connection() as db:
        db.execute("DELETE FROM portal_documents_userlink WHERE user_id=2")
    bot = Bot()
    assert deliver_reminder(service, bot)
    assert "/pending" in bot.calls[0]["text"]
    assert "reply_markup" not in bot.calls[0]
