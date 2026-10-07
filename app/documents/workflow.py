"""Reserve a version in PostgreSQL before making exactly one external write."""

import hashlib
import json
import math
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from app.documents.catalog import products
from app.documents.costs import (
    displayed_estimate,
    estimate,
    improved_estimate,
    needs_current_estimate,
)
from app.documents.policy import (
    actor,
    fail,
    identifier,
    invalid,
    require,
    stores_for,
    table,
    warehouse_grant_guard,
)
from app.documents.reads import summary
from app.documents.telegram_cleanup import approved


def has_receipt_approver(db, store_id):
    """Discrepancies require a sender who still has effective edit authority."""
    return bool(
        db.execute(
            "SELECT 1 FROM portal_documents_grant g "
            "JOIN authentication_user u ON u.id=g.user_id "
            "LEFT JOIN portal_documents_userlink l ON l.user_id=u.id "
            "LEFT JOIN portal_access p ON p.id=l.supabase_id "
            "WHERE g.kind='waybill' AND g.store_id=%s AND g.actions @> '[\"edit\"]'::jsonb "
            "AND u.is_active AND ((l.supabase_id IS NULL AND u.telegram_id IS NOT NULL) "
            "OR (p.active AND p.sections @> '[\"transfers\"]'::jsonb))"
            + warehouse_grant_guard("portal_documents_grant", alias="g")
            + " LIMIT 1",
            (store_id,),
        ).fetchone()
    )


def validate(db, kind, body):
    comment = body.get("comment", "")
    if not isinstance(comment, str) or len(comment) > 1000:
        invalid("Комментарий должен содержать не более 1000 символов.")
    store = identifier(body.get("store_id"))
    if not db.execute("SELECT 1 FROM stores WHERE id=%s", (store,)).fetchone():
        invalid("Выберите существующий склад.")
    fields = {"store_id": store, "comment": comment.strip()}
    if kind == "waybill":
        target = identifier(body.get("counteragent_id"))
        if (
            target == store
            or not db.execute("SELECT 1 FROM stores WHERE id=%s", (target,)).fetchone()
        ):
            invalid("Выберите другой склад получателя.")
        fields["counteragent_id"] = target
    else:
        reason, reason_id = body.get("reason"), body.get("reason_id")
        if not isinstance(reason, str) or type(reason_id) is not int or not 0 < reason_id < 2**63:
            invalid("Выберите действующую причину списания.")
        if not db.execute(
            "SELECT 1 FROM writeoffs_reasons WHERE id=%s AND name=%s AND account_id IS NOT NULL",
            (reason_id, reason),
        ).fetchone():
            invalid("Выберите действующую причину списания.")
        fields["reason_id"] = reason
    return fields, validate_items(db, body.get("items"))


def validate_items(db, rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
        invalid("Добавьте от 1 до 200 позиций.")
    names = products(db)
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"product_id", "amount"}:
            invalid()
        product = identifier(row["product_id"])
        amount = row["amount"]
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            invalid("Количество должно быть положительным числом.")
        if not 0 < amount <= 1e9 or not math.isfinite(amount):
            invalid("Количество должно быть положительным конечным числом, не больше 1 млрд.")
        if str(product) not in names or product in seen:
            invalid("Проверьте товары: неизвестная или повторная позиция.")
        seen.add(product)
        result.append({"product_id": product, "amount": amount})
    return result


def event(db, kind, doc, user_id, action, data=None):
    db.execute(
        "INSERT INTO portal_documents_event "
        "(kind,document_id,version,actor_id,action,data,created_at) VALUES "
        "(%s,%s,%s,%s,%s,%s,now())",
        (kind, doc["id"], doc["version"], user_id, action, Jsonb(data or {})),
    )


def enqueue(db, kind, doc):
    sender = kind == "waybill" and doc.get("receipt_state") == "pending_sender"
    permission = "edit" if sender else "approve"
    store = doc["counteragent_id"] if kind == "waybill" and not sender else doc["store_id"]
    db.execute(
        "INSERT INTO portal_documents_notification "
        "(kind,document_id,version,recipient_id,state,attempts,created_at,updated_at) "
        "SELECT %s,%s,%s,u.id,'pending',0,now(),now() FROM portal_documents_grant g "
        "JOIN authentication_user u ON u.id=g.user_id WHERE g.kind=%s AND g.store_id=%s "
        "AND g.actions @> %s AND u.is_active ON CONFLICT DO NOTHING",
        (kind, doc["id"], doc["version"], kind, store, Jsonb([permission])),
    )


def finish(db, key, result, state="done"):
    db.execute(
        "UPDATE portal_documents_operation SET "
        "document_id=%s,result=%s,state=%s,completed_at=%s WHERE id=%s",
        (
            result["id"],
            Jsonb(result),
            state,
            None if state in {"pending", "queued"} else datetime.now(UTC),
            key,
        ),
    )


def mutate(database, provider, identity, kind, action, body, document_id=None, *, telegram=False):
    parent, child = table(kind)
    if action not in {
        "create",
        "edit",
        "copy",
        "cancel",
        "confirm",
        "deny",
        "receive",
        "confirm_receipt",
        "reject_receipt",
    }:
        fail(404, "Действие не найдено.")
    if kind == "writeoff" and action in {
        "edit",
        "copy",
        "cancel",
        "receive",
        "confirm_receipt",
        "reject_receipt",
    }:
        fail(403, "Это действие недоступно для списаний.")
    allowed = {"request_id", "version"}
    if action == "receive":
        allowed.add("items")
    if action in {"create", "edit", "copy"}:
        allowed |= {"store_id", "counteragent_id", "reason", "reason_id", "comment", "items"}
    if not isinstance(body, dict) or set(body) - allowed:
        invalid()
    key = identifier(body.get("request_id"))
    try:
        fingerprint = hashlib.sha256(
            json.dumps([kind, action, document_id, body], sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
    except (ValueError, TypeError):
        invalid()
    with database.connection() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(key),))
        user = actor(
            db,
            None if telegram else identity,
            telegram_id=identity if telegram else None,
            kind=kind,
            lock=True,
        )
        previous = db.execute(
            "SELECT * FROM portal_documents_operation WHERE id=%s", (key,)
        ).fetchone()
        if previous:
            if previous["actor_id"] != user["id"] or previous["fingerprint"] != fingerprint:
                fail(409, "Этот идентификатор уже использован другим запросом.")
            visible = stores_for(db, user["id"], kind)
            doc = db.execute(
                f"SELECT * FROM {parent} WHERE id=%s", (previous["document_id"],)
            ).fetchone()
            if not doc or not ({doc["store_id"], doc.get("counteragent_id")} & set(visible)):
                fail(403, "Доступ к документу отозван.")
            return previous["result"]
        doc = None
        if action != "create":
            doc = db.execute(
                f"SELECT * FROM {parent} WHERE id=%s FOR UPDATE", (document_id,)
            ).fetchone()
            if not doc:
                fail(404, "Документ не найден.")
            permission = (
                "approve"
                if action in {"confirm", "deny", "receive"}
                else "edit"
                if action in {"confirm_receipt", "reject_receipt"}
                else action
            )
            store = (
                doc["counteragent_id"]
                if kind == "waybill" and permission == "approve"
                else doc["store_id"]
            )
            require(db, user["id"], kind, permission, store)
            if type(body.get("version")) is not int or body["version"] != doc["version"]:
                fail(409, "Документ изменён. Откройте актуальную версию.")
            if doc["submission_state"] in {"queued", "sending"}:
                fail(409, "Документ уже согласован и ожидает завершения отправки в iiko.")
            if doc["submission_state"] == "unknown":
                fail(409, "Результат отправки требует проверки. Повтор запрещён.")
            if action != "copy" and doc["status"] != "Created":
                fail(409, "Документ уже обработан.")
        receipt_state = doc.get("receipt_state", "none") if doc else "none"
        if action in {"confirm_receipt", "reject_receipt"}:
            if receipt_state != "pending_sender":
                fail(409, "Нет расхождений, ожидающих согласования отправителя.")
        elif receipt_state == "pending_sender" and action != "copy":
            fail(409, "Сначала отправитель должен согласовать или отклонить расхождения.")
        receipt_rows = None
        discrepancy = False
        if action == "receive":
            original = db.execute(
                f"SELECT product_id,amount FROM {child} WHERE waybill_id=%s ORDER BY id",
                (doc["id"],),
            ).fetchall()
            receipt_rows = validate_receipt(body.get("items"), original)
            discrepancy = any(
                r["amount"] != o["amount"] for r, o in zip(receipt_rows, original, strict=True)
            )
            if discrepancy and not has_receipt_approver(db, doc["store_id"]):
                fail(
                    409,
                    "У отправителя нет активного согласующего с правом изменения накладных. "
                    "Обратитесь к администратору.",
                )
        queue = action in {"confirm", "confirm_receipt"} or (
            action == "receive" and not discrepancy
        )
        if action in {"create", "edit", "copy"}:
            fields, rows = validate(db, kind, body)
            require(
                db, user["id"], kind, "edit" if action == "edit" else "create", fields["store_id"]
            )
            if action == "edit" and any(
                fields[k] != doc[k] for k in ("store_id", "counteragent_id")
            ):
                invalid("Склады созданной накладной нельзя менять.")
        db.execute(
            "INSERT INTO portal_documents_operation "
            "(id,actor_id,kind,action,document_id,fingerprint,state,result,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'pending','{}',now())",
            (key, user["id"], kind, action, document_id, fingerprint),
        )
        if action in {"create", "copy"}:
            if kind == "writeoff":
                fields["cost_estimate"] = Jsonb(estimate(db, fields["store_id"], rows))
            # Field names come only from validate(), not the request.
            columns = ",".join(fields)
            placeholders = ",".join(["%s"] * len(fields))
            doc = db.execute(
                f"INSERT INTO {parent} "
                f"({columns},created_by_id,status,version,submission_state,created_at) "
                f"VALUES ({placeholders},%s,'Created',1,'idle',now()) RETURNING *",
                (*fields.values(), user["id"]),
            ).fetchone()
        elif action == "edit":
            previous_items = db.execute(
                f"SELECT product_id,amount,received_amount FROM {child} "
                "WHERE waybill_id=%s ORDER BY id",
                (doc["id"],),
            ).fetchall()
            event(
                db,
                kind,
                doc,
                user["id"],
                "edit_previous",
                {"items": [{**r, "product_id": str(r["product_id"])} for r in previous_items]},
            )
            doc = db.execute(
                f"UPDATE {parent} SET comment=%s,version=version+1,"
                "submission_state='idle',receipt_state='none' "
                "WHERE id=%s RETURNING *",
                (fields["comment"], doc["id"]),
            ).fetchone()
            db.execute(f"DELETE FROM {child} WHERE {kind}_id=%s", (doc["id"],))
        if action in {"create", "edit", "copy"}:
            with db.cursor() as cursor:
                cursor.executemany(
                    f"INSERT INTO {child} ({kind}_id,product_id,amount) VALUES (%s,%s,%s)",
                    [(doc["id"], r["product_id"], r["amount"]) for r in rows],
                )
            enqueue(db, kind, doc)
            event(
                db,
                kind,
                doc,
                user["id"],
                action,
                {
                    "items": [{**r, "product_id": str(r["product_id"])} for r in rows],
                    "comment": doc["comment"],
                },
            )
        elif action in {"receive", "confirm_receipt", "reject_receipt"}:
            if receipt_rows is not None:
                with db.cursor() as cursor:
                    cursor.executemany(
                        f"UPDATE {child} SET received_amount=%s "
                        "WHERE waybill_id=%s AND product_id=%s",
                        [(r["amount"], doc["id"], r["product_id"]) for r in receipt_rows],
                    )
            state = (
                "rejected"
                if action == "reject_receipt"
                else "pending_sender"
                if discrepancy
                else "accepted"
            )
            doc = db.execute(
                f"UPDATE {parent} SET version=version+1,receipt_state=%s,submission_state=%s,"
                "processed_by_id=%s WHERE id=%s RETURNING *",
                (
                    state,
                    "queued" if queue else "idle",
                    user["id"] if queue else doc["processed_by_id"],
                    doc["id"],
                ),
            ).fetchone()
            snapshot = db.execute(
                f"SELECT product_id,amount,received_amount FROM {child} "
                "WHERE waybill_id=%s ORDER BY id",
                (doc["id"],),
            ).fetchall()
            event(
                db,
                kind,
                doc,
                user["id"],
                action,
                {"items": [{**r, "product_id": str(r["product_id"])} for r in snapshot]},
            )
            if not queue:
                enqueue(db, kind, doc)
        else:
            approval_quote = None
            if kind == "writeoff" and action == "confirm" and needs_current_estimate(doc):
                quote_rows = db.execute(
                    "SELECT product_id,amount FROM writeoffs_items "
                    "WHERE writeoff_id=%s ORDER BY id",
                    (doc["id"],),
                ).fetchall()
                approval_quote = {
                    **improved_estimate(db, doc, quote_rows),
                    "frozen_at_approval": True,
                }
                db.execute(
                    "UPDATE writeoffs SET cost_estimate=%s WHERE id=%s",
                    (
                        Jsonb({**doc["cost_estimate"], "approval_estimate": approval_quote}),
                        doc["id"],
                    ),
                )
            if kind == "waybill" and action == "confirm" and receipt_state == "rejected":
                db.execute(
                    f"UPDATE {child} SET received_amount=NULL WHERE waybill_id=%s", (doc["id"],)
                )
                db.execute(f"UPDATE {parent} SET receipt_state='none' WHERE id=%s", (doc["id"],))
            status = (
                doc["status"]
                if action == "confirm"
                else "Denied"
                if action == "deny"
                else "Cancelled"
            )
            doc = db.execute(
                f"UPDATE {parent} SET processed_by_id=%s,version=version+1,status=%s, "
                "submission_state=%s,processed_at=%s WHERE id=%s RETURNING *",
                (
                    user["id"],
                    status,
                    "queued" if action == "confirm" else doc["submission_state"],
                    doc["processed_at"] if action == "confirm" else datetime.now(UTC),
                    doc["id"],
                ),
            ).fetchone()
            event(
                db,
                kind,
                doc,
                user["id"],
                action,
                {"approval_estimate": approval_quote} if approval_quote is not None else None,
            )
        result = summary(kind, doc)
        if kind == "writeoff":
            result["cost_estimate"] = displayed_estimate(doc.get("cost_estimate"))
        finish(db, key, result, "queued" if queue else "done")
        if queue:
            approved(db, kind, doc)
            rows = db.execute(
                f"SELECT product_id,amount FROM {child} WHERE {kind}_id=%s ORDER BY id",
                (doc["id"],),
            ).fetchall()
            if kind == "waybill" and doc.get("receipt_state") == "accepted":
                rows = db.execute(
                    f"SELECT product_id,received_amount AS amount FROM {child} "
                    "WHERE waybill_id=%s AND received_amount>0 ORDER BY id",
                    (doc["id"],),
                ).fetchall()
            # Capture all submitted values inside the reservation transaction.
            source = db.execute(
                "SELECT name FROM stores WHERE id=%s", (doc["store_id"],)
            ).fetchone()["name"]
            creator = db.execute(
                "SELECT first_name,last_name,username FROM authentication_user WHERE id=%s",
                (doc["created_by_id"],),
            ).fetchone()
            target = (
                db.execute(
                    "SELECT name FROM stores WHERE id=%s", (doc["counteragent_id"],)
                ).fetchone()["name"]
                if kind == "waybill"
                else None
            )
            reason = (
                db.execute(
                    "SELECT account_id FROM writeoffs_reasons WHERE name=%s", (doc["reason_id"],)
                ).fetchone()
                if kind == "writeoff"
                else None
            )
            receiver = user
            if kind == "waybill" and doc.get("receipt_state") == "accepted":
                receiver = db.execute(
                    "SELECT u.* FROM portal_documents_event e "
                    "JOIN authentication_user u ON u.id=e.actor_id "
                    "WHERE e.kind='waybill' AND e.document_id=%s AND e.action='receive' "
                    "ORDER BY e.id DESC LIMIT 1",
                    (doc["id"],),
                ).fetchone()
                if receiver is None:
                    fail(409, "Не найдена история приёмки. Требуется проверка документа.")
            payload = provider.payload(kind, doc, rows, creator, receiver, source, target, reason)
            db.execute(
                "INSERT INTO native_dispatch (operation_id,kind,document_id,version,payload) "
                "VALUES (%s,%s,%s,%s,%s)",
                (
                    key,
                    kind,
                    doc["id"],
                    doc["version"],
                    Jsonb(
                        {"xml": payload.decode()}
                        if isinstance(payload, bytes)
                        else {"json": payload}
                    ),
                ),
            )
    return result


def validate_receipt(rows, original):
    """Accept exactly the existing products, including explicit zero shortages."""
    if not isinstance(rows, list) or len(rows) != len(original):
        invalid("Укажите фактическое количество каждой позиции накладной.")
    values = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"product_id", "amount"}:
            invalid()
        product, amount = identifier(row["product_id"]), row["amount"]
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not 0 <= amount <= 1e9
            or not math.isfinite(amount)
            or product in values
        ):
            invalid("Фактическое количество должно быть конечным числом от 0 до 1 млрд.")
        values[product] = amount
    if set(values) != {r["product_id"] for r in original}:
        invalid("Состав товаров накладной нельзя менять при приёмке.")
    if not any(values.values()):
        invalid("Нельзя принять пустую накладную. Отклоните документ, если ничего не получено.")
    return [{"product_id": r["product_id"], "amount": values[r["product_id"]]} for r in original]
