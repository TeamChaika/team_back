"""Counterparty command journal and explicit grants, separate from invoice permissions."""

from uuid import uuid4

from psycopg.types.json import Jsonb

from app.commercial_invoices.counterparty_models import (
    candidates,
    code_for,
    fingerprint,
    identity_key,
    normalize,
)
from app.commercial_invoices.counterparty_transport import source_key
from app.commercial_invoices.policy import actor, stores_for
from app.documents.policy import fail, identifier, invalid, profile

CATALOG_LOCK = 7623011102053
ERRORS = {
    "duplicate": "Такой контрагент уже есть. Выберите существующую карточку.",
    "code_conflict": "Код новой карточки уже занят в iiko. Создание остановлено.",
    "uuid_conflict": "UUID новой карточки уже существует в iiko. Создание остановлено.",
    "rejected": "iiko отклонил создание. Проверьте поля и права интеграции.",
    "access_revoked": "Право создания отозвано. Контрагент не отправлен.",
    "source_changed": "Подключение iiko изменилось. Операция остановлена.",
    "unknown": "Результат ещё не подтверждён. Выполняется проверка в iiko.",
}


def permitted(db, user_id, kind):
    row = db.execute(
        "SELECT can_create FROM commercial_counterparty_grants WHERE user_id=%s",
        (user_id,),
    ).fetchone()
    return bool(
        row
        and row["can_create"]
        and set(stores_for(db, user_id, kind, "create"))
        & set(stores_for(db, user_id, kind, "view"))
    )


def require_creator(db, portal_id, kind):
    user = actor(db, portal_id, kind=kind)
    if not permitted(db, user["id"], kind):
        fail(403, "Нет отдельного права создавать контрагентов или доступного склада.")
    return user


def serialize(db, row):
    result = {"id": str(row["id"]), "state": row["state"]}
    if row["state"] == "confirmed":
        party = db.execute(
            "SELECT data FROM commercial_counterparties WHERE id=%s", (row["iiko_id"],)
        ).fetchone()
        result["counterparty"] = party["data"] if party else None
    if row["error_code"]:
        result["error"] = ERRORS.get(row["error_code"], ERRORS["unknown"])
        result["error_code"] = row["error_code"]
    if row.get("candidates"):
        result["candidates"] = row["candidates"]
    return {"operation": result}


def command(service, portal_id, kind, body):
    if not service.counterparty_enabled:
        fail(503, "Создание контрагентов ещё не включено.")
    payload = normalize(body)
    operation_id = identifier(body["request_id"])
    digest = fingerprint({"kind": kind, "payload": payload})
    with service.database.connection(readonly=True) as db:
        user = require_creator(db, portal_id, kind)
        old = db.execute(
            "SELECT * FROM commercial_counterparty_operations WHERE id=%s", (operation_id,)
        ).fetchone()
        if old:
            if (
                old["actor_id"] != user["id"]
                or str(old["portal_id"]) != str(portal_id)
                or old["fingerprint"] != digest
            ):
                fail(409, "Этот request_id уже использован другой командой.")
            return serialize(db, old)
    # A fresh registry is required, including when the daily cached list is stale.
    try:
        with service.counterparty_provider.session() as (client, token):
            registry = service.counterparty_provider.registry(client, token)
    except Exception:
        fail(503, "Не удалось проверить актуальный справочник iiko. Попробуйте позже.")
    duplicates = candidates(payload, registry)
    if duplicates:
        fail(
            409,
            {
                "code": "counterparty_duplicate",
                "message": ERRORS["duplicate"],
                "candidates": duplicates,
            },
        )
    origin = source_key(service.counterparty_provider)
    identity = identity_key(payload)
    with service.database.connection() as db:
        user = require_creator(db, portal_id, kind)
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("counterparty:" + str(operation_id),),
        )
        old = db.execute(
            "SELECT * FROM commercial_counterparty_operations WHERE id=%s", (operation_id,)
        ).fetchone()
        if old:
            if (
                old["actor_id"] != user["id"]
                or str(old["portal_id"]) != str(portal_id)
                or old["fingerprint"] != digest
            ):
                fail(409, "Этот request_id уже использован другой командой.")
            return serialize(db, old)
        # Contact matches overlap: two different phones can share an email or address.
        # Serialize admission by source, then use the same matching rule as iiko preflight.
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("counterparty-source:" + origin,),
        )
        existing = db.execute(
            "SELECT * FROM commercial_counterparty_operations WHERE source_key=%s "
            "AND state<>'rejected' ORDER BY created_at",
            (origin,),
        )
        pending = next(
            (
                row
                for row in existing
                if row["identity_key"] == identity
                or candidates(payload, [{**row["payload"], "id": str(row["iiko_id"])}])
            ),
            None,
        )
        if pending:
            if pending["state"] == "confirmed":
                party = serialize(db, pending)["operation"]["counterparty"]
                fail(
                    409,
                    {
                        "code": "counterparty_duplicate",
                        "message": ERRORS["duplicate"],
                        "candidates": [party] if party else [],
                    },
                )
            fail(
                409,
                "Создание контрагента с такими реквизитами уже проверяется. Дождитесь результата.",
            )
        remote_id = uuid4()
        row = db.execute(
            "INSERT INTO commercial_counterparty_operations "
            "(id,actor_id,portal_id,kind,fingerprint,source_key,iiko_id,code,payload,identity_key) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                operation_id,
                user["id"],
                portal_id,
                kind,
                digest,
                origin,
                remote_id,
                code_for(remote_id),
                Jsonb(payload),
                identity,
            ),
        ).fetchone()
        db.execute(
            "INSERT INTO commercial_counterparty_events(operation_id,state) VALUES(%s,'queued')",
            (operation_id,),
        )
        return serialize(db, row)


def operation(service, portal_id, kind, operation_id):
    with service.database.connection(readonly=True) as db:
        user = actor(db, portal_id, kind=kind)
        row = db.execute(
            "SELECT * FROM commercial_counterparty_operations "
            "WHERE id=%s AND actor_id=%s AND portal_id=%s AND kind=%s",
            (identifier(operation_id), user["id"], portal_id, kind),
        ).fetchone()
        if not row:
            fail(404, "Операция не найдена.")
        return serialize(db, row)


def grants(database, portal_id, *, user_id=None, body=None):
    with database.connection() as db:
        profile(db, portal_id, admin=True)
        if body is not None:
            if (
                not isinstance(body, dict)
                or set(body) != {"request_id", "version", "can_create"}
                or type(body["version"]) is not int
                or body["version"] < 0
                or type(body["can_create"]) is not bool
            ):
                invalid()
            request_id = identifier(body["request_id"])
            digest = fingerprint({"user_id": user_id, "body": body})
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("counterparty-grant:" + str(request_id),),
            )
            previous = db.execute(
                "SELECT * FROM commercial_counterparty_grant_operations WHERE request_id=%s",
                (request_id,),
            ).fetchone()
            if previous:
                if (
                    str(previous["portal_id"]) != str(portal_id)
                    or previous["fingerprint"] != digest
                ):
                    fail(409, "Этот request_id уже использован другой командой.")
            else:
                user = db.execute(
                    "SELECT id FROM authentication_user WHERE id=%s FOR UPDATE", (user_id,)
                ).fetchone()
                if not user:
                    fail(404, "Рабочий профиль не найден.")
                current = db.execute(
                    "SELECT revision FROM commercial_counterparty_grants WHERE user_id=%s",
                    (user_id,),
                ).fetchone()
                if (current["revision"] if current else 0) != body["version"]:
                    fail(409, "Права изменены. Обновите список.")
                db.execute(
                    "INSERT INTO commercial_counterparty_grants(user_id,can_create) VALUES(%s,%s) "
                    "ON CONFLICT(user_id) DO UPDATE SET can_create=EXCLUDED.can_create, "
                    "revision=commercial_counterparty_grants.revision+1",
                    (user_id, body["can_create"]),
                )
                db.execute(
                    "INSERT INTO commercial_counterparty_grant_operations "
                    "(request_id,portal_id,fingerprint,user_id) VALUES(%s,%s,%s,%s)",
                    (request_id, portal_id, digest, user_id),
                )
        return {
            "users": db.execute(
                "SELECT u.id,coalesce(g.can_create,false) AS can_create,"
                "coalesce(g.revision,0) AS version FROM authentication_user u "
                "LEFT JOIN commercial_counterparty_grants g ON g.user_id=u.id ORDER BY u.id"
            ).fetchall()
        }
