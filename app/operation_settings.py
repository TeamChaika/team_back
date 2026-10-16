"""Company-owned operation policy, independent of employee access grants."""

from copy import deepcopy

from psycopg import sql
from psycopg.rows import dict_row

DEFAULT_SCHEMA = "chaika"
DEFAULT_SETTINGS = {
    "revision": 0,
    "sync": {"mode": "all", "department_ids": []},
    "writeoff": {"mode": "all", "store_ids": []},
    "waybill": {"mode": "all", "store_ids": []},
    "updated_at": None,
    "updated_by": None,
}


def settings_schema(db):
    runtime = getattr(db, "tenant_runtime", None)
    return runtime.schema("analytics") if runtime is not None else DEFAULT_SCHEMA


def settings_table(db, name="operation_settings"):
    if name not in {"operation_settings", "operation_settings_audit"}:
        raise ValueError("Unknown settings table")
    return sql.Identifier(settings_schema(db), name)


def load_settings(db):
    # A missing migration must fail rather than silently enable disabled scopes.
    with db.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute(
            sql.SQL("SELECT revision,settings,updated_at,updated_by FROM {} WHERE singleton")
            .format(settings_table(db))
        ).fetchone()
    if row is None:
        return deepcopy(DEFAULT_SETTINGS)
    return {
        **deepcopy(row["settings"]),
        "revision": row["revision"],
        "updated_at": row["updated_at"],
        "updated_by": str(row["updated_by"]) if row["updated_by"] else None,
    }


def allowed_store(db, kind, store_id, *, settings=None):
    if kind not in {"waybill", "writeoff"}:
        raise ValueError("Unknown document operation")
    selection = (load_settings(db) if settings is None else settings)[kind]
    return selection["mode"] == "all" or str(store_id) in selection["store_ids"]


def filter_stores(db, kind, stores, *, settings=None):
    if kind not in {"waybill", "writeoff"}:
        raise ValueError("Unknown document operation")
    selection = (load_settings(db) if settings is None else settings)[kind]
    if selection["mode"] == "all":
        return list(stores)
    allowed = set(selection["store_ids"])
    return [row for row in stores if str(row["id"]) in allowed]
