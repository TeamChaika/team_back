"""Daily venue reports with durable, at-most-once uncertain Telegram delivery."""

import re

import httpx
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.documents.config import DocumentSettings
from app.documents.telegram import Telegram
from app.documents.telegram_cleanup import TelegramError
from app.low_markup_report import export_xlsx, prepare_report
from app.operation_settings import load_settings, settings_schema
from app.sync_references import SyncError

MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def connect(settings):
    return psycopg.connect(
        settings.database_url.get_secret_value(),
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=10,
    )


def bot_id(token):
    # Stable across token rotation, matching the tenant bot namespace contract.
    prefix, separator, secret = token.partition(":")
    if not separator or not secret or not re.fullmatch(r"[1-9][0-9]{0,19}", prefix):
        raise SyncError("low_markup_bot_not_configured")
    return prefix


def table(db, name):
    return sql.Identifier(settings_schema(db), name)


def destinations(db):
    selection = load_settings(db)["sync"]
    rows = db.execute(
        sql.SQL(
            "SELECT d.*,n.name FROM {} d JOIN {} n ON n.id=d.department_id "
            "AND n.source_id='primary' WHERE d.enabled ORDER BY d.department_id"
        ).format(table(db, "low_markup_destinations"), table(db, "corporate_nodes"))
    ).fetchall()
    return [
        row
        for row in rows
        if selection["mode"] == "all" or str(row["department_id"]) in selection["department_ids"]
    ]


def caption(report):
    text = f"{report.title} · {report.business_date:%d.%m.%Y}\nНаценка менее 150%."
    if not report.rows:
        text += "\nБлюд с наценкой менее 150% не найдено."
    if report.excluded_cost_count:
        text += f"\nБез достоверной себестоимости: {report.excluded_cost_count}."
    return text[:1024]


def _run(db, settings, business_date, stop):
    targets = destinations(db)
    if not targets:
        return
    document_settings = DocumentSettings()
    token = document_settings.bot_token.get_secret_value()
    identity = bot_id(token)
    failures = any(row["bot_id"] != identity for row in targets)
    targets = [row for row in targets if row["bot_id"] == identity]
    if not targets:
        raise SyncError("low_markup_bot_mismatch")
    ledger = table(db, "low_markup_deliveries")
    db.execute(
        sql.SQL(
            "UPDATE {} SET state='unknown',error_code='interrupted',"
            "updated_at=now() WHERE state='sending'"
        ).format(ledger)
    )
    previous = {
        str(row["department_id"]): row
        for row in db.execute(
            sql.SQL(
                "SELECT *,next_attempt_at IS NULL OR next_attempt_at<=now() AS due "
                "FROM {} WHERE business_date=%s"
            ).format(ledger),
            (business_date,),
        ).fetchall()
    }
    pending = [
        row
        for row in targets
        if str(row["department_id"]) not in previous
        or (
            previous[str(row["department_id"])]["state"] == "failed"
            and previous[str(row["department_id"])]["due"]
        )
    ]
    failures |= any(
        previous.get(str(row["department_id"]), {}).get("state") in {"unknown", "failed"}
        for row in targets
        if row not in pending
    )
    if not pending:
        if failures:
            raise SyncError("low_markup_delivery_incomplete")
        return
    reports = prepare_report(
        settings, business_date, {str(row["department_id"]): row["name"] for row in pending}
    )
    bot = Telegram(
        token,
        transport=httpx.HTTPTransport(local_address=document_settings.telegram_local_address)
        if document_settings.telegram_local_address
        else None,
    )
    try:
        for target in pending:
            if stop.is_set():
                raise SyncError("low_markup_interrupted")
            identifier = str(target["department_id"])
            # Build before reserving a delivery: export failures are safe to retry.
            try:
                report = reports[identifier]
                document = (f"low-markup-{business_date}.xlsx", export_xlsx(report), MIME)
            except Exception:
                failures = True
                continue
            with db.transaction():
                db.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (settings_schema(db) + ":operation-settings",),
                )
                db.execute(
                    sql.SQL("SELECT 1 FROM {} WHERE department_id=%s FOR UPDATE").format(
                        table(db, "low_markup_destinations")
                    ),
                    (identifier,),
                )
                current = next(
                    (row for row in destinations(db) if str(row["department_id"]) == identifier),
                    None,
                )
                if (
                    not current
                    or current["chat_id"] != target["chat_id"]
                    or current["bot_id"] != identity
                ):
                    continue
                db.execute(
                    sql.SQL(
                        "INSERT INTO {} "
                        "(business_date,department_id,chat_id,bot_id,department_name,"
                        "state,attempts) VALUES(%s,%s,%s,%s,%s,'sending',1) "
                        "ON CONFLICT(business_date,department_id) DO UPDATE SET state='sending',"
                        "chat_id=excluded.chat_id,bot_id=excluded.bot_id,"
                        "department_name=excluded.department_name,attempts={}.attempts+1,"
                        "error_code=NULL,next_attempt_at=NULL,updated_at=now()"
                    ).format(ledger, ledger),
                    (business_date, identifier, target["chat_id"], identity, target["name"]),
                )
            state, error, delay, message_id = "sent", None, None, None
            try:
                result = bot.call(
                    "sendDocument",
                    chat_id=target["chat_id"],
                    caption=caption(report),
                    document=document,
                )
                if (
                    not isinstance(result, dict)
                    or type(result.get("message_id")) is not int
                    or result["message_id"] <= 0
                ):
                    raise ValueError("Missing message identity")
                message_id = result["message_id"]
            except TelegramError as exc:
                state = "failed" if exc.code == 429 else "unknown"
                error = "telegram_rate_limited" if exc.code == 429 else "telegram_uncertain"
                delay = exc.retry_after if exc.code == 429 else None
            except Exception:
                state, error = "unknown", "telegram_uncertain"
            db.execute(
                sql.SQL(
                    "UPDATE {} SET state=%s,message_id=%s,error_code=%s,"
                    "next_attempt_at=now()+%s*interval '1 second',updated_at=now() "
                    "WHERE business_date=%s AND department_id=%s"
                ).format(ledger),
                (state, message_id, error, delay, business_date, identifier),
            )
            failures |= state != "sent"
    finally:
        bot.close()
    if failures:
        raise SyncError("low_markup_delivery_incomplete")


def run_reports(settings, business_date, stop):
    with connect(settings) as db:
        lock = settings_schema(db) + ":low-markup-reports"
        acquired = db.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired", (lock,)
        ).fetchone()["acquired"]
        if not acquired:
            raise SyncError("low_markup_busy")
        try:
            _run(db, settings, business_date, stop)
        finally:
            db.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (lock,))
