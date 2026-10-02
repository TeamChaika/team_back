"""Reserve a version in PostgreSQL before making exactly one external write."""

import hashlib
import json
import math
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from app.documents.catalog import products
from app.documents.policy import actor, fail, identifier, invalid, require, stores_for, table
from app.documents.reads import summary


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
    rows = body.get("items")
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
    return fields, result


def event(db, kind, doc, user_id, action, data=None):
    db.execute(
        "INSERT INTO portal_documents_event "
        "(kind,document_id,version,actor_id,action,data,created_at) VALUES "
        "(%s,%s,%s,%s,%s,%s,now())",
        (kind, doc["id"], doc["version"], user_id, action, Jsonb(data or {})),
    )


def enqueue(db, kind, doc):
    store = doc["counteragent_id"] if kind == "waybill" else doc["store_id"]
    db.execute(
        "INSERT INTO portal_documents_notification "
        "(kind,document_id,version,recipient_id,state,attempts,created_at,updated_at) "
        "SELECT %s,%s,%s,u.id,'pending',0,now(),now() FROM portal_documents_grant g "
        "JOIN authentication_user u ON u.id=g.user_id WHERE g.kind=%s AND g.store_id=%s "
        "AND g.actions @> '[\"approve\"]'::jsonb AND u.is_active ON CONFLICT DO NOTHING",
        (kind, doc["id"], doc["version"], kind, store),
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
    if action not in {"create", "edit", "copy", "cancel", "confirm", "deny"}:
        fail(404, "Действие не найдено.")
    if kind == "writeoff" and action in {"edit", "copy", "cancel"}:
        fail(403, "Это действие недоступно для списаний.")
    allowed = {"request_id", "version"}
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
            permission = "approve" if action in {"confirm", "deny"} else action
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
            doc = db.execute(
                f"UPDATE {parent} SET comment=%s,version=version+1,submission_state='idle' "
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
        else:
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
            event(db, kind, doc, user["id"], action)
        result = summary(kind, doc)
        finish(db, key, result, "queued" if action == "confirm" else "done")
        if action == "confirm":
            rows = db.execute(
                f"SELECT product_id,amount FROM {child} WHERE {kind}_id=%s ORDER BY id",
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
            payload = provider.payload(kind, doc, rows, creator, user, source, target, reason)
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
