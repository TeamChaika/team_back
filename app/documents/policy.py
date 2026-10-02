from uuid import UUID

from fastapi import HTTPException
from psycopg.types.json import Jsonb

TABLES = {"waybill": ("waybills", "waybills_items"), "writeoff": ("writeoffs", "writeoffs_items")}
SECTIONS = {"waybill": "transfers", "writeoff": "writeoffs"}
ACTIONS = {
    "waybill": {"view", "create", "edit", "cancel", "approve", "copy"},
    "writeoff": {"view", "create", "approve"},
}


def fail(status, message):
    raise HTTPException(status, message)


def invalid(message="Проверьте поля документа."):
    fail(422, message)


def identifier(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        invalid()


def table(kind):
    if kind not in TABLES:
        fail(404, "Тип документа не найден.")
    return TABLES[kind]


def profile(db, portal_id, *, admin=False, kind=None):
    row = db.execute("SELECT * FROM portal_access WHERE id=%s", (portal_id,)).fetchone()
    if not row or not row["active"]:
        fail(403, "Учётная запись отключена.")
    if admin and not row["is_portal_admin"]:
        fail(403, "Управление доступно только администратору.")
    if kind is not None and SECTIONS[kind] not in row["sections"]:
        fail(403, "Нет доступа к разделу документов.")
    return row


def actor(db, portal_id=None, *, telegram_id=None, kind, lock=False):
    table(kind)
    suffix = " FOR UPDATE OF u" if lock else ""
    if telegram_id is None:
        profile(db, portal_id, kind=kind)
        row = db.execute(
            "SELECT u.*, l.supabase_id FROM authentication_user u "
            "JOIN portal_documents_userlink l ON l.user_id=u.id WHERE l.supabase_id=%s" + suffix,
            (portal_id,),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT u.*, l.supabase_id FROM authentication_user u "
            "LEFT JOIN portal_documents_userlink l ON l.user_id=u.id WHERE u.telegram_id=%s"
            + suffix,
            (telegram_id,),
        ).fetchone()
    if not row or not row["is_active"]:
        fail(403, "Администратор должен подключить рабочий профиль и склады.")
    if row["supabase_id"]:
        profile(db, row["supabase_id"], kind=kind)
    return row


def stores_for(db, user_id, kind, action="view"):
    if action not in ACTIONS[kind]:
        return []
    return [
        r["store_id"]
        for r in db.execute(
            "SELECT store_id FROM portal_documents_grant "
            "WHERE user_id=%s AND kind=%s AND actions @> %s",
            (user_id, kind, Jsonb([action])),
        ).fetchall()
    ]


def require(db, user_id, kind, action, store_id):
    if store_id not in stores_for(db, user_id, kind, action):
        fail(403, "Нет права на это действие для выбранного склада.")


def full_name(row):
    return (row.get("first_name", "") + " " + row.get("last_name", "")).strip() or row["username"]
