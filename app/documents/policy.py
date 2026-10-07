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


def warehouse_grant_guard(grant_table, *, alias=None):
    """An unlinked legacy worker retains its grants; linked users obey portal ACL.

    grant_table is a fixed internal identifier, never supplied by a request.
    The read-only view excludes inactive/missing portal identities, so they cannot
    regain access through Telegram, exports, or a service call bypassing HTTP.
    """
    if grant_table not in {"portal_documents_grant", "commercial_invoice_grants"}:
        raise ValueError("Unknown grant table")
    if alias not in {None, "g"}:
        raise ValueError("Unknown grant alias")
    reference = alias or grant_table
    return (
        " AND (NOT EXISTS(SELECT 1 FROM portal_documents_userlink wl "
        f"WHERE wl.user_id={reference}.user_id) OR EXISTS("
        "SELECT 1 FROM portal_documents_userlink wl "
        "JOIN chaika.portal_warehouse_access wa ON wa.user_id=wl.supabase_id "
        f"WHERE wl.user_id={reference}.user_id AND (wa.warehouse_scope_mode='all' OR "
        f"(wa.source_id='primary' AND wa.store_id={reference}.store_id))))"
    )


def stores_for(db, user_id, kind, action="view"):
    if action not in ACTIONS[kind]:
        return []
    return [
        r["store_id"]
        for r in db.execute(
            "SELECT store_id FROM portal_documents_grant "
            "WHERE user_id=%s AND kind=%s AND actions @> %s"
            + warehouse_grant_guard("portal_documents_grant"),
            (user_id, kind, Jsonb([action])),
        ).fetchall()
    ]


def require(db, user_id, kind, action, store_id):
    if store_id not in stores_for(db, user_id, kind, action):
        fail(403, "Нет права на это действие для выбранного склада.")


def full_name(row):
    return (row.get("first_name", "") + " " + row.get("last_name", "")).strip() or row["username"]
