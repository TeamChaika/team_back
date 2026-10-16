"""Saved establishment selection, independent of physical iiko connection configuration.

Whole documents are atomic: mixed or unresolved warehouse ownership is frozen.
Company-wide immutable RAW is retained for audit, never used to bypass this boundary.
"""

from dataclasses import dataclass
from uuid import UUID

from psycopg.rows import tuple_row

from app.operation_settings import load_settings
from app.tenancy.sql import ANALYTICS_SCHEMA as SCHEMA


def fetch_rows(db, query, params=None):
    with db.cursor(row_factory=tuple_row) as cursor:
        return cursor.execute(query, params).fetchall()


def identifier(value):
    return str(UUID(str(value))) if value is not None else None


@dataclass(frozen=True)
class SyncScope:
    departments: frozenset[str] | None
    stores: frozenset[str] = frozenset()
    nodes: frozenset[str] = frozenset()
    revision: int = 0

    @property
    def empty(self):
        return self.departments == frozenset()

    def department(self, value):
        return self.departments is None or identifier(value) in self.departments

    def store(self, value):
        return self.departments is None or identifier(value) in self.stores

    def metadata(self):
        return {
            "sync_scope_revision": self.revision,
            "sync_scope_mode": "all" if self.departments is None else "selected",
            "sync_department_ids": sorted(self.departments or []),
            "partial_scope": self.departments is not None,
        }


def load_scope(db):
    settings = load_settings(db)
    selection = settings["sync"]
    if selection["mode"] == "all":
        return SyncScope(None, revision=settings["revision"])
    if selection["mode"] != "selected":
        raise ValueError("invalid_sync_scope")
    departments = frozenset(identifier(value) for value in selection["department_ids"])
    rows = fetch_rows(
        db, f"SELECT id,parent_id FROM {SCHEMA}.corporate_nodes WHERE source_id='primary'"
    )
    stores = fetch_rows(db, f"SELECT id,parent_id FROM {SCHEMA}.stores WHERE source_id='primary'")
    parents = {identifier(key): identifier(parent) for key, parent in rows + stores}

    def included(key):
        seen = set()
        key = identifier(key)
        while key is not None and key not in seen:
            if key in departments:
                return True
            seen.add(key)
            key = parents.get(key)
        return False

    return SyncScope(
        departments,
        frozenset(identifier(key) for key, _ in stores if included(key)),
        frozenset(identifier(key) for key, _ in rows if included(key)),
        settings["revision"],
    )


def skipped(scope):
    return {
        "status": "succeeded",
        "skipped": True,
        "reason": "sync_scope_disabled",
        "counts": scope.metadata(),
    }


def source_enabled(db, source_id, scope=None, *, include_blocked=False):
    scope = scope or load_scope(db)
    rows = fetch_rows(
        db,
        f"SELECT department_id,sync_blocked FROM {SCHEMA}.rms_bindings "
        "WHERE source_id=%s AND state='matched'",
        (source_id,),
    )
    if rows and rows[0][1] and not include_blocked:
        return False
    if scope.departments is None:
        return True
    return bool(rows and scope.department(rows[0][0]))


def active_sources(db, sources, *, keep_primary=False, include_blocked=False):
    scope = load_scope(db)
    return [
        source
        for source in sources
        if (keep_primary and source.id == "primary")
        or source_enabled(db, source.id, scope, include_blocked=include_blocked)
    ]


def document_stores(row, resource):
    if resource == "transfers":
        return {identifier(row.get("store_from_id")), identifier(row.get("store_to_id"))}
    if resource == "writeoffs":
        return {identifier(row.get("store_id"))}
    default = row.get("default_store_id")
    result = {identifier(item.get("store_id") or default) for item in row.get("items", [])}
    if default is not None or not result:
        result.add(identifier(default))
    return result


def filter_documents(db, snapshot):
    scope = load_scope(db)
    rows = snapshot["payload"]["items"]
    if scope.departments is None:
        return rows
    resource = snapshot["resource"]
    allowed = []
    # Preserve a disabled document even if a later export moves its warehouse.
    table = {
        "transfers": "internal_transfers",
        "incoming_invoices": "incoming_invoices",
        "outgoing_invoices": "outgoing_invoices",
        "writeoffs": "writeoffs",
    }[resource]
    existing = {
        identifier(key): details
        for key, details in db.execute(
            f"SELECT id,details FROM {SCHEMA}.{table} WHERE source_id=%s AND id=ANY(%s::uuid[])",
            (snapshot["source_id"], [row["id"] for row in rows]),
        )
    }
    if resource in {"incoming_invoices", "outgoing_invoices"}:
        item_table = (
            "incoming_invoice_items"
            if resource == "incoming_invoices"
            else "outgoing_invoice_items"
        )
        for key, store in db.execute(
            f"SELECT document_id,store_id FROM {SCHEMA}.{item_table} "
            "WHERE source_id=%s AND document_id=ANY(%s::uuid[]) AND present_in_latest",
            (snapshot["source_id"], [row["id"] for row in rows]),
        ):
            if identifier(key) in existing:
                existing[identifier(key)].setdefault("items", []).append({"store_id": store})
        counterparts = {
            identifier(key): identifier(store)
            for key, store in db.execute(
                f"SELECT id,represented_store_id FROM {SCHEMA}.counteragents "
                "WHERE source_id=%s AND represents_store",
                (snapshot["source_id"],),
            )
        }
    else:
        counterparts = {}
    for row in rows:
        owners = document_stores(row, resource)
        previous = existing.get(identifier(row["id"]))
        if previous is not None:
            owners |= document_stores(previous, resource)
        party_field = "supplier_id" if resource == "incoming_invoices" else "counteragent_id"
        for document in [row] + ([previous] if previous is not None else []):
            party = identifier(document.get(party_field))
            if party in counterparts:
                owners.add(counterparts[party])
        if not owners or None in owners or not owners.issubset(scope.stores):
            continue
        allowed.append(row)
    return allowed


def carry_balance_rows(db, snapshot, items, kind, scope):
    """Carry only the same accounting timestamp; retain original RAW provenance."""
    if scope.departments is None:
        return 0
    if kind == "store":
        fields = ["store_id", "product_id", "amount", "sum"]
        predicate, ids = "store_id", list(scope.stores)
    else:
        fields = ["account_id", "counteragent_id", "department_id", "sum"]
        predicate, ids = "department_id", list(scope.departments)
    names = ",".join(fields)
    result = db.execute(
        f"INSERT INTO {SCHEMA}.{kind}_balance_items "
        f"(snapshot_id,line_num,{names},origin_snapshot_id) "
        f"SELECT %s,%s+row_number() OVER(ORDER BY old.line_num),"
        + ",".join("old." + field for field in fields)
        + f",coalesce(old.origin_snapshot_id,old.snapshot_id) FROM "
        f"{SCHEMA}.{kind}_balance_items old "
        f"JOIN {SCHEMA}.{kind}_balance_reports previous ON "
        f"previous.last_snapshot_id=old.snapshot_id "
        f"WHERE previous.source_id=%s AND previous.accounting_timestamp=%s "
        f"AND (old.{predicate} IS NULL OR NOT(old.{predicate}=ANY(%s::uuid[])))",
        (
            snapshot["id"],
            len(items),
            snapshot["source_id"],
            snapshot["payload"]["request"]["timestamp"],
            ids,
        ),
    )
    return result.rowcount


def reference_ids(db, scope, nodes, stores):
    """Require old and incoming venue ownership, including every parent in the path."""
    if scope.departments is None:
        return frozenset(), frozenset()
    previous_nodes = fetch_rows(
        db, f"SELECT id,parent_id FROM {SCHEMA}.corporate_nodes WHERE source_id='primary'"
    )
    previous_stores = fetch_rows(
        db, f"SELECT id,parent_id FROM {SCHEMA}.stores WHERE source_id='primary'"
    )
    previous = {
        identifier(key): identifier(parent) for key, parent in previous_nodes + previous_stores
    }
    allowed = scope.nodes | scope.stores | scope.departments
    proposed = {
        **previous,
        **{identifier(row["id"]): identifier(row.get("parent_id")) for row in nodes + stores},
    }

    def included(key):
        key, seen = identifier(key), set()
        while key is not None and key not in seen:
            if key in previous and key not in allowed:
                return False
            if key in scope.departments:
                return True
            seen.add(key)
            key = proposed.get(key)
        return False

    node_ids = {identifier(row["id"]) for row in nodes} | {
        identifier(row[0]) for row in previous_nodes
    }
    store_ids = {identifier(row["id"]) for row in stores} | {
        identifier(row[0]) for row in previous_stores
    }
    return (
        frozenset(key for key in node_ids if included(key)),
        frozenset(key for key in store_ids if included(key)),
    )
