"""Company-owned writeoff reasons and the persisted iiko account catalogue."""

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.documents.policy import fail, identifier, invalid, profile


def row(db, reason_id):
    return db.execute(
        "SELECT r.id,r.name,r.account_id,a.name AS account_name,r.version "
        "FROM writeoffs_reasons r LEFT JOIN chaika.accounts a "
        "ON a.source_id='primary' AND a.id=r.account_id WHERE r.id=%s",
        (reason_id,),
    ).fetchone()


def listing(db, portal_id):
    profile(db, portal_id, admin=True)
    reasons = db.execute(
        "SELECT r.id,r.name,r.account_id,a.name AS account_name,r.version "
        "FROM writeoffs_reasons r LEFT JOIN chaika.accounts a "
        "ON a.source_id='primary' AND a.id=r.account_id ORDER BY r.name,r.id"
    ).fetchall()
    accounts = db.execute(
        "SELECT id,name,code FROM chaika.accounts "
        "WHERE source_id='primary' AND NOT deleted AND present_in_latest ORDER BY name,id"
    ).fetchall()
    return {"reasons": reasons, "accounts": accounts}


def save(database, portal_id, payload, reason_id=None):
    fields = {"name", "account_id"} | ({"version"} if reason_id is not None else set())
    if not isinstance(payload, dict) or set(payload) != fields:
        invalid("Проверьте поля причины списания.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 128:
        invalid("Название причины должно содержать от 1 до 128 символов.")
    name = name.strip()
    account_id = identifier(payload["account_id"])
    if reason_id is not None and (type(payload["version"]) is not int or payload["version"] < 1):
        invalid("Некорректная версия причины списания.")
    try:
        with database.connection() as db:
            profile(db, portal_id, admin=True)
            if not db.execute(
                "SELECT id FROM chaika.accounts "
                "WHERE source_id='primary' AND id=%s AND NOT deleted "
                "AND present_in_latest",
                (account_id,),
            ).fetchone():
                invalid("Выберите действующий счёт iiko своей компании.")
            before = None
            if reason_id is None:
                saved = db.execute(
                    "INSERT INTO writeoffs_reasons(name,account_id) VALUES (%s,%s) RETURNING id",
                    (name, account_id),
                ).fetchone()
                reason_id = saved["id"]
            else:
                old = db.execute(
                    "SELECT name,account_id,version FROM writeoffs_reasons WHERE id=%s FOR UPDATE",
                    (reason_id,),
                ).fetchone()
                if not old:
                    fail(404, "Причина списания не найдена.")
                before = {
                    "name": old["name"],
                    "account_id": str(old["account_id"]) if old["account_id"] else None,
                }
                if old["version"] != payload["version"]:
                    fail(409, "Причина списания изменена. Обновите список.")
                db.execute(
                    "UPDATE writeoffs_reasons SET name=%s,account_id=%s,version=version+1 "
                    "WHERE id=%s",
                    (name, account_id, reason_id),
                )
            reason = row(db, reason_id)
            changes = {"before": before, "after": {"name": name, "account_id": str(account_id)}}
            db.execute(
                "INSERT INTO writeoffs_reason_events(reason_id,actor_supabase_id,version,data) "
                "VALUES (%s,%s,%s,%s)",
                (reason_id, portal_id, reason["version"], Jsonb(changes)),
            )
            return {"reason": reason}
    except UniqueViolation:
        fail(409, "Причина списания с таким названием уже существует.")
