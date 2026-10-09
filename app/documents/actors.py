"""Verified principals and durable audit snapshots; snapshots never confer access."""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.documents.context import runtime_of
from app.tenancy.actor import ActorContext


@dataclass(frozen=True)
class StoredActor:
    local_id: int | None
    actor: dict | None


def owner_actor(db, ref):
    if isinstance(ref, (dict, StoredActor)):
        raise HTTPException(403, "Требуется проверенная учётная запись.")
    if not isinstance(ref, ActorContext):
        return None
    runtime = runtime_of(db)
    if runtime.mode != "tenant" or ref.company_id != runtime.company_id:
        raise HTTPException(403, "Участник не принадлежит этой компании.")
    return ref if ref.kind == "platform_owner" else None


def principal_uuid(ref):
    return ref.auth_user_id if isinstance(ref, ActorContext) else ref


def local_id(ref):
    if isinstance(ref, StoredActor):
        return ref.local_id
    return None if isinstance(ref, ActorContext) else ref


def snapshot(ref):
    if isinstance(ref, StoredActor):
        return ref.actor
    return ref.as_dict() if isinstance(ref, ActorContext) else None


def stored_actor(row):
    return StoredActor(row.get("actor_id"), row.get("actor"))


def same_actor(row, ref):
    value = snapshot(ref)
    if value:
        saved = row.get("actor") or {}
        return row.get("actor_id") is None and all(
            saved.get(k) == value[k] for k in ("company_id", "auth_user_id", "kind")
        )
    return row.get("actor_id") == local_id(ref) and not row.get("actor")


def actor_user(value):
    return {
        "id": value,
        "supabase_id": value.auth_user_id,
        "first_name": value.display_name,
        "last_name": "",
        "username": value.display_name,
        "is_active": True,
        "actor": value.as_dict(),
    }


def display_user(value):
    return {"first_name": value["display_name"], "last_name": "", "username": value["display_name"]}


def audit(db, ref, action, object_kind, object_id=None, data=None):
    value = snapshot(ref)
    if not value:
        return
    db.execute(
        "INSERT INTO native_actor_audit "
        "(company_id,actor_uuid,actor_kind,actor_display_name,action,object_kind,object_id,data) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            UUID(value["company_id"]),
            UUID(value["auth_user_id"]),
            value["kind"],
            value["display_name"],
            action,
            object_kind,
            str(object_id) if object_id is not None else None,
            Jsonb(data or {}),
        ),
    )
