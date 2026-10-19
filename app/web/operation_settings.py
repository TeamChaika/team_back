"""Administrative operation settings: validated selection, revision and audit."""

from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.operation_settings import load_settings, settings_schema, settings_table
from app.sync_references import LOCK_ID
from app.web.repository import serial


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["all", "selected"]


class DepartmentsSelection(Selection):
    department_ids: list[UUID] = Field(max_length=1000)

    @model_validator(mode="after")
    def check_selection(self):
        _validate_selection(self.mode, self.department_ids)
        return self


class StoresSelection(Selection):
    store_ids: list[UUID] = Field(max_length=5000)

    @model_validator(mode="after")
    def check_selection(self):
        _validate_selection(self.mode, self.store_ids)
        return self


def _validate_selection(mode, ids):
    if len(ids) != len(set(ids)):
        raise ValueError("Repeated identifier")
    if mode == "all" and ids:
        raise ValueError("All mode must not include selected identifiers")


class VenueSettingsWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    archived_department_ids: list[UUID] = Field(max_length=1000)

    @model_validator(mode="after")
    def check_selection(self):
        _validate_selection("selected", self.archived_department_ids)
        return self


class OperationSettingsWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    sync: DepartmentsSelection
    writeoff: StoresSelection
    waybill: StoresSelection


def _catalog(db):
    schema = sql.Identifier(settings_schema(db))
    departments = db.execute(
        sql.SQL(
            "SELECT id,name FROM {}.corporate_nodes WHERE source_id='primary' "
            "AND type IN ('DEPARTMENT','CENTRALSTORE','MANUFACTURE') ORDER BY name,id"
        ).format(schema)
    ).fetchall()
    nodes = db.execute(
        sql.SQL("SELECT id,parent_id FROM {}.corporate_nodes WHERE source_id='primary'").format(
            schema
        )
    ).fetchall()
    stores = db.execute(
        sql.SQL(
            "SELECT id,parent_id,name FROM {}.stores WHERE source_id='primary' ORDER BY name,id"
        ).format(schema)
    ).fetchall()
    names = {row["id"]: row["name"] for row in departments}
    parents = {row["id"]: row["parent_id"] for row in nodes}
    parents.update({row["id"]: row["parent_id"] for row in stores})
    for row in stores:
        current, seen = row["parent_id"], set()
        while current is not None and current not in names and current not in seen:
            seen.add(current)
            current = parents.get(current)
        row["department_id"] = current if current in names else None
        row["department_name"] = names.get(current)
    return {"departments": departments, "stores": stores}


def _response(db):
    result = {**load_settings(db), **_catalog(db)}
    return serial(result)


def read_operation_settings(administration):
    with administration.repo.connection(repeatable=True) as db:
        return _response(db)


def read_venue_settings(administration):
    with administration.repo.connection(repeatable=True) as db:
        return _venue_response(db)


def _venue_response(db):
    saved = load_settings(db)
    return serial(
        {
            "revision": saved["revision"],
            "archived_department_ids": saved["analytics"]["archived_department_ids"],
            "departments": _catalog(db)["departments"],
            "updated_at": saved["updated_at"],
        }
    )


def save_operation_settings(administration, actor, payload):
    with administration.write(actor) as db:
        # Namespaced lock also serializes the first insert, when no row exists yet.
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (settings_schema(db) + ":operation-settings",),
        )
        before = load_settings(db)
        if before["revision"] != payload.revision:
            raise HTTPException(409, "Настройки изменены другим администратором. Обновите их.")
        venue_only = isinstance(payload, VenueSettingsWrite)
        if not venue_only and before["sync"] != payload.sync.model_dump(mode="json"):
            locked = db.execute(
                "SELECT pg_try_advisory_xact_lock(%s) AS acquired", (LOCK_ID,)
            ).fetchone()
            if not locked["acquired"]:
                raise HTTPException(
                    423, "Синхронизация выполняется. Повторите сохранение после её завершения."
                )
        catalog = _catalog(db)
        departments = {row["id"] for row in catalog["departments"]}
        stores = {row["id"] for row in catalog["stores"]}
        values = {key: before[key] for key in ("sync", "writeoff", "waybill", "analytics")}
        if venue_only:
            if set(payload.archived_department_ids) - departments:
                raise HTTPException(422, "Неизвестное заведение. Обновите список.")
            values["analytics"] = payload.model_dump(mode="json", exclude={"revision"})
        else:
            if set(payload.sync.department_ids) - departments:
                raise HTTPException(422, "Неизвестное заведение. Обновите список.")
            if (set(payload.writeoff.store_ids) | set(payload.waybill.store_ids)) - stores:
                raise HTTPException(422, "Неизвестный склад. Обновите список.")
            values.update(payload.model_dump(mode="json", exclude={"revision"}))
        actor_id = getattr(actor, "auth_user_id", actor)
        after_revision = before["revision"] + 1
        db.execute(
            sql.SQL(
                "INSERT INTO {} (singleton,revision,settings,updated_at,updated_by) "
                "VALUES(true,%s,%s,now(),%s) ON CONFLICT(singleton) DO UPDATE SET "
                "revision=excluded.revision,settings=excluded.settings,"
                "updated_at=excluded.updated_at,updated_by=excluded.updated_by"
            ).format(settings_table(db)),
            (after_revision, Jsonb(values), actor_id),
        )
        db.execute(
            sql.SQL(
                "INSERT INTO {} (revision,actor_id,before_settings,after_settings) "
                "VALUES(%s,%s,%s,%s)"
            ).format(settings_table(db, "operation_settings_audit")),
            (
                after_revision,
                actor_id,
                Jsonb({k: before[k] for k in ("sync", "writeoff", "waybill", "analytics")}),
                Jsonb(values),
            ),
        )
        return _venue_response(db) if venue_only else _response(db)
