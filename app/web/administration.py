"""Account provisioning and deposit configuration, available only to portal admins."""

import hashlib
import json
import re
from contextlib import contextmanager
from typing import Annotated, Literal
from uuid import UUID

import httpx
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from app.tenancy.actor import ActorContext
from app.tenancy.sql import ANALYTICS_SCHEMA as DB
from app.tenancy.sql import PAYMENTS_SCHEMA as PAYMENTS
from app.web.permissions import ANALYTICS_SECTIONS, SECTIONS, require_admin
from app.web.repository import Scope, serial


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)


class DepositGrant(Input):
    venue: str = Field(min_length=1, max_length=100)
    can_create: bool = False


class Account(Input):
    display_name: str = Field(min_length=1, max_length=150)
    active: bool = True
    sections: list[str] = Field(max_length=16)
    warehouse_scope_mode: Literal["all", "selected"] | None = None
    warehouse_ids: list[UUID] | None = Field(default=None, max_length=500)
    all_departments: bool = False
    department_ids: list[UUID] = Field(default_factory=list, max_length=100)
    deposits_all: bool = False
    deposits_create: bool = False
    deposit_grants: list[DepositGrant] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def permissions(self):
        if len(set(self.sections)) != len(self.sections) or set(self.sections) - set(SECTIONS):
            raise ValueError("Unknown or duplicate section")
        if (self.warehouse_scope_mode is None) != (self.warehouse_ids is None):
            raise ValueError("Warehouse mode and grants must be supplied together")
        if self.warehouse_ids is not None and len(set(self.warehouse_ids)) != len(
            self.warehouse_ids
        ):
            raise ValueError("Duplicate warehouse")
        if self.warehouse_scope_mode == "all" and self.warehouse_ids:
            raise ValueError("Full warehouse mode must not include selected grants")
        if (
            (ANALYTICS_SECTIONS - {"invoices", "outgoing"}).intersection(self.sections)
            and self.warehouse_scope_mode == "all"
            and not (self.all_departments or self.department_ids)
        ):
            raise ValueError("Select iiko restaurants")
        if len({g.venue for g in self.deposit_grants}) != len(self.deposit_grants):
            raise ValueError("Duplicate venue")
        if "deposits" not in self.sections and (
            self.deposits_all or self.deposits_create or self.deposit_grants
        ):
            raise ValueError("Deposit section required")
        if self.deposits_create and not self.deposits_all:
            raise ValueError("Creation for all venues requires access to all venues")
        return self


class NewAccount(Account):
    request_id: UUID
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def initial_department_scope(self):
        if (ANALYTICS_SECTIONS - {"invoices", "outgoing"}).intersection(self.sections) and (
            self.warehouse_scope_mode != "selected"
            and not (self.all_departments or self.department_ids)
        ):
            raise ValueError("Select iiko restaurants")
        return self

    @field_validator("email")
    @classmethod
    def email_format(cls, value):
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Invalid email")
        return value.lower()


class EditAccount(Account):
    revision: int = Field(ge=1)


class Venue(Input):
    name: str = Field(min_length=1, max_length=100)
    active: bool = True
    revision: int | None = Field(default=None, ge=1)


class Terminal(Input):
    name: str = Field(min_length=1, max_length=100)
    qrt_uuid: UUID | None = None
    api_key: SecretStr | None = Field(default=None, min_length=8, max_length=512)
    active: bool = True
    make_default: bool = False
    revision: int | None = Field(default=None, ge=1)
    mode: Literal["sandbox", "live"] | None = None
    merchant_id: str | None = Field(default=None, max_length=200)

    @field_validator("api_key")
    @classmethod
    def key_format(cls, value):
        if value and any(c.isspace() for c in value.get_secret_value()):
            raise ValueError("Invalid key")
        return value

    @model_validator(mode="after")
    def default_active(self):
        if self.make_default and not self.active:
            raise ValueError("Default terminal must be active")
        return self


class Administration:
    def __init__(self, repo, payments=None):
        self.repo = repo
        self.payments = payments

    @contextmanager
    def write(self, actor):
        if isinstance(actor, ActorContext):
            if (
                self.repo.runtime.mode != "tenant"
                or actor.company_id != self.repo.runtime.company_id
            ):
                raise HTTPException(403, "Другая компания.")
            actor_id = actor.auth_user_id
        else:
            if self.repo.runtime.mode == "tenant":
                raise HTTPException(403, "Проверенный субъект компании обязателен.")
            actor_id = actor
        self.repo._pool.open()
        try:
            with self.repo._pool.connection() as db, db.transaction():
                db.execute("SET LOCAL statement_timeout='15000ms'")
                if not (isinstance(actor, ActorContext) and actor.kind == "platform_owner"):
                    row = db.execute(
                        f"SELECT is_portal_admin FROM {DB}.web_users "
                        "WHERE id=%s AND active FOR SHARE",
                        (actor_id,),
                    ).fetchone()
                    if not row or not row["is_portal_admin"]:
                        raise HTTPException(403, "Управление доступно только администратору.")
                yield db
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "Такая запись уже существует. Обновите список.") from None

    def audit(self, db, actor, action, target):
        # Deliberately excludes passwords, provider keys and submitted payloads.
        db.execute(
            f"INSERT INTO {DB}.portal_admin_audit(actor_id,action,target_id) VALUES(%s,%s,%s)",
            (actor.auth_user_id if isinstance(actor, ActorContext) else actor, action, target),
        )

    def identity(self, email):
        with self.repo.connection() as db:
            return db.execute(
                f"SELECT id,provision_id FROM {DB}.portal_identities WHERE lower(email)=%s",
                (email,),
            ).fetchone()

    def catalog(self, actor=None):
        with self.repo.connection() as db:
            users = db.execute(
                f"SELECT w.*,i.email FROM {DB}.web_users w JOIN {DB}.portal_identities "
                "i USING(id) ORDER BY w.display_name LIMIT 5001"
            ).fetchall()
            if len(users) > 5000:
                raise HTTPException(413, "Слишком много сотрудников для этого списка.")
            departments = db.execute(
                f"SELECT id,name FROM {DB}.corporate_nodes WHERE source_id='primary' AND "
                "type IN ('DEPARTMENT','CENTRALSTORE','MANUFACTURE') ORDER BY name"
            ).fetchall()
            grants = db.execute(
                f"SELECT user_id,department_id FROM {DB}.web_department_access WHERE "
                "source_id='primary'"
            ).fetchall()
            warehouses = db.execute(
                f"SELECT id,parent_id,name FROM {DB}.stores WHERE source_id='primary' "
                "ORDER BY name,id"
            ).fetchall()
            warehouse_grants = db.execute(
                f"SELECT user_id,store_id FROM {DB}.web_warehouse_access WHERE source_id='primary'"
            ).fetchall()
            if self.payments is None:
                deposits = db.execute(
                    f"SELECT user_id,venue,is_all,can_create FROM {PAYMENTS}.user_venues"
                ).fetchall()
                venues = db.execute(
                    f"SELECT name FROM {PAYMENTS}.qr_enterprises UNION SELECT DISTINCT "
                    f"venue AS name FROM {PAYMENTS}.deposits WHERE venue IS NOT NULL ORDER BY name"
                ).fetchall()
        if self.payments is not None:
            deposits, venues = self.payments.catalog(actor)
        for user in users:
            user["warehouse_ids"] = [
                g["store_id"] for g in warehouse_grants if g["user_id"] == user["id"]
            ]
            user["department_ids"] = [
                g["department_id"] for g in grants if g["user_id"] == user["id"]
            ]
            own = [
                g
                for g in deposits
                if str(g["user_id"]) == str(user["id"])
                and (self.payments is None or g["profile_revision"] == user["revision"])
            ]
            user["deposits_all"] = any(g["is_all"] for g in own)
            user["deposits_create"] = any(g["is_all"] and g["can_create"] for g in own)
            user["deposit_grants"] = [
                {"venue": g["venue"], "can_create": g["can_create"]} for g in own if not g["is_all"]
            ]
        return serial(
            {
                "users": users,
                "departments": departments,
                "warehouses": warehouses,
                "venues": [v["name"] for v in venues],
                "sections": [{"id": k, "title": v} for k, v in SECTIONS.items()],
            }
        )

    def save_account(
        self,
        actor,
        user_id,
        payload,
        *,
        creating=False,
        sync_payments=True,
        initialization_request_id=None,
    ):
        with self.write(actor) as db:
            current = db.execute(
                f"SELECT * FROM {DB}.web_users WHERE id=%s FOR UPDATE", (user_id,)
            ).fetchone()
            if creating and current:
                # A successful provisioning request can be replayed without resetting rights.
                return {"id": str(user_id), "already_created": True}
            if not creating:
                if not current:
                    raise HTTPException(404, "Сотрудник не найден.")
                if current["revision"] != payload.revision:
                    raise HTTPException(
                        409, "Права уже изменены. Обновите список и откройте сотрудника заново."
                    )
                if current["is_portal_admin"] and not payload.active:
                    raise HTTPException(422, "Нельзя отключить администратора через эту форму.")
            if self.payments is None:
                self.validate_scope(db, payload)
            else:
                self.validate_scope(db, payload, actor=actor)
            role = current["role"] if current else "manager"
            warehouse_mode = payload.warehouse_scope_mode or (
                current.get("warehouse_scope_mode", "all") if current else "all"
            )
            if (ANALYTICS_SECTIONS - {"invoices", "outgoing"}).intersection(payload.sections) and (
                warehouse_mode != "selected"
                and not (payload.all_departments or payload.department_ids)
            ):
                raise HTTPException(422, "Выберите заведения iiko.")
            db.execute(
                f"INSERT INTO {DB}.web_users(id,display_name,role,active,sections,"
                "all_departments,warehouse_scope_mode) VALUES(%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET "
                "display_name=EXCLUDED.display_name,active=EXCLUDED.active,"
                "sections=EXCLUDED.sections,all_departments=EXCLUDED.all_departments,"
                "warehouse_scope_mode=EXCLUDED.warehouse_scope_mode,"
                f"revision={DB}.web_users.revision+1",
                (
                    user_id,
                    payload.display_name,
                    role,
                    payload.active,
                    payload.sections,
                    payload.all_departments,
                    warehouse_mode,
                ),
            )
            if payload.warehouse_ids is not None:
                db.execute(f"DELETE FROM {DB}.web_warehouse_access WHERE user_id=%s", (user_id,))
                for warehouse_id in payload.warehouse_ids:
                    db.execute(
                        f"INSERT INTO {DB}.web_warehouse_access(user_id,source_id,store_id) "
                        "VALUES(%s,'primary',%s)",
                        (user_id, warehouse_id),
                    )
            db.execute(f"DELETE FROM {DB}.web_department_access WHERE user_id=%s", (user_id,))
            for department in set(payload.department_ids):
                db.execute(
                    f"INSERT INTO {DB}.web_department_access(user_id,source_id,"
                    "department_id) VALUES(%s,'primary',%s)",
                    (user_id, department),
                )
            if self.payments is None:
                db.execute(f"DELETE FROM {PAYMENTS}.user_venues WHERE user_id=%s", (user_id,))
            if self.payments is None and "deposits" in payload.sections:
                if payload.deposits_all:
                    db.execute(
                        f"INSERT INTO {PAYMENTS}.user_venues(user_id,venue,is_all,"
                        "can_create) VALUES(%s,'*',true,%s)",
                        (user_id, payload.deposits_create),
                    )
                else:
                    for grant in payload.deposit_grants:
                        db.execute(
                            f"INSERT INTO {PAYMENTS}.user_venues(user_id,venue,is_all,"
                            "can_create) VALUES(%s,%s,false,%s)",
                            (user_id, grant.venue, grant.can_create),
                        )
            if initialization_request_id is not None:
                marked = db.execute(
                    f"UPDATE {DB}.portal_account_initializations SET profile_applied=true "
                    "WHERE request_id=%s AND user_id=%s AND actor_id=%s "
                    "AND state='pending' RETURNING request_id",
                    (initialization_request_id, user_id, actor.auth_user_id),
                ).fetchone()
                if not marked:
                    raise HTTPException(409, "Не подтверждён запрос создания сотрудника.")
            self.audit(db, actor, "account.create" if creating else "account.update", user_id)
        if self.payments is not None and sync_payments:
            self.payments.sync_account(actor, user_id, payload)
        return {"id": str(user_id)}

    def save_identity_account(self, actor, user_id, payload):
        """Resume initial profile/payment setup, preserving all later admin edits."""
        values = payload.model_dump(mode="json", exclude={"password"})
        fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        with self.write(actor) as db:
            row = db.execute(
                f"SELECT w.revision,i.provision_id FROM {DB}.web_users w "
                f"JOIN {DB}.portal_identity_metadata i USING(id) WHERE w.id=%s FOR UPDATE OF w",
                (user_id,),
            ).fetchone()
            if not row or row["provision_id"] != str(payload.request_id):
                raise HTTPException(409, "Не подтверждён запрос создания сотрудника.")
            receipt = db.execute(
                f"SELECT * FROM {DB}.portal_account_initializations WHERE request_id=%s",
                (payload.request_id,),
            ).fetchone()
            if receipt:
                if (
                    receipt["fingerprint"] != fingerprint
                    or receipt["actor_id"] != actor.auth_user_id
                ):
                    raise HTTPException(409, "Этот запрос уже использован.")
                if receipt["state"] != "pending":
                    return {"id": str(user_id), "already_created": True}
            else:
                cursor = (
                    self.payments.account_sync_cursor(actor, user_id) if self.payments else None
                )
                db.execute(
                    f"INSERT INTO {DB}.portal_account_initializations "
                    "(request_id,user_id,actor_id,fingerprint,payment_cursor,state) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        payload.request_id,
                        user_id,
                        actor.auth_user_id,
                        fingerprint,
                        cursor,
                        "pending" if row["revision"] == 1 else "superseded",
                    ),
                )
                if row["revision"] > 1:
                    return {"id": str(user_id), "already_created": True}
        if row["revision"] == 1:
            edited = EditAccount(
                **payload.model_dump(exclude={"request_id", "email", "password"}), revision=1
            )
            try:
                self.save_account(
                    actor,
                    user_id,
                    edited,
                    sync_payments=False,
                    initialization_request_id=payload.request_id,
                )
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                # A competing valid initializer/admin may have advanced revision.
        with self.write(actor) as db:
            current = db.execute(
                f"SELECT revision FROM {DB}.web_users WHERE id=%s FOR UPDATE", (user_id,)
            ).fetchone()
            receipt = db.execute(
                f"SELECT * FROM {DB}.portal_account_initializations WHERE request_id=%s FOR UPDATE",
                (payload.request_id,),
            ).fetchone()
            if current["revision"] == 1:
                raise HTTPException(409, "Не удалось сохранить права сотрудника.")
            state = (
                "done"
                if receipt["profile_applied"]
                and current["revision"] == receipt["expected_revision"]
                else "superseded"
            )
            if receipt["state"] == "pending" and state == "done" and self.payments:
                self.payments.sync_account(
                    actor, user_id, payload, if_unchanged_since=receipt["payment_cursor"]
                )
            db.execute(
                f"UPDATE {DB}.portal_account_initializations SET state=%s WHERE request_id=%s",
                (state, payload.request_id),
            )
        return {"id": str(user_id)}

    def validate_scope(self, db, payload, actor=None):
        if payload.warehouse_ids is not None:
            stores = db.execute(
                f"SELECT id FROM {DB}.stores WHERE source_id='primary' AND id=ANY(%s::uuid[])",
                (payload.warehouse_ids,),
            ).fetchall()
            if {row["id"] for row in stores} != set(payload.warehouse_ids):
                raise HTTPException(422, "Выбран неизвестный склад iiko.")
        departments = db.execute(
            f"SELECT id FROM {DB}.corporate_nodes WHERE source_id='primary' AND type IN "
            "('DEPARTMENT','CENTRALSTORE','MANUFACTURE') AND id=ANY(%s::uuid[])",
            (payload.department_ids,),
        ).fetchall()
        if {r["id"] for r in departments} != set(payload.department_ids):
            raise HTTPException(422, "Выбрано неизвестное заведение iiko.")
        if self.payments is not None:
            self.payments.validate_grants(actor, payload)
        else:
            venues = db.execute(
                f"SELECT name FROM {PAYMENTS}.qr_enterprises UNION SELECT DISTINCT venue "
                f"AS name FROM {PAYMENTS}.deposits WHERE venue IS NOT NULL"
            ).fetchall()
            if {g.venue for g in payload.deposit_grants} - {v["name"] for v in venues}:
                raise HTTPException(422, "Выбрано неизвестное заведение депозитов.")

    def configuration(self, actor=None):
        if self.payments is not None:
            return self.payments.configuration(actor)
        with self.repo.connection() as db:
            venues = db.execute(
                "SELECT id,name,active,revision,default_terminal_id FROM "
                f"{PAYMENTS}.qr_enterprises ORDER BY name"
            ).fetchall()
            terminals = db.execute(
                "SELECT id,venue_id,name,qrt_uuid,active,revision,(length(api_key)>0) AS "
                f"key_configured FROM {PAYMENTS}.qr_terminals ORDER BY name"
            ).fetchall()
        return serial({"venues": venues, "terminals": terminals})

    def save_venue(self, actor, venue_id, payload):
        if self.payments is not None:
            return self.payments.save_venue(actor, venue_id, payload)
        with self.write(actor) as db:
            old = db.execute(
                f"SELECT name,revision FROM {PAYMENTS}.qr_enterprises WHERE id=%s FOR UPDATE",
                (venue_id,),
            ).fetchone()
            if old:
                if payload.revision != old["revision"]:
                    raise HTTPException(409, "Заведение уже изменено. Обновите список.")
                if old["name"] != payload.name:
                    conflict = db.execute(
                        f"SELECT 1 FROM {PAYMENTS}.qr_enterprises WHERE id<>%s AND "
                        "(name=%s OR %s=ANY(aliases))",
                        (venue_id, payload.name, payload.name),
                    ).fetchone()
                    if conflict:
                        raise HTTPException(409, "Название уже используется другим заведением.")
                    db.execute(
                        f"UPDATE {PAYMENTS}.deposits SET venue=%s WHERE venue=%s",
                        (payload.name, old["name"]),
                    )
                    db.execute(
                        f"UPDATE {PAYMENTS}.user_venues SET venue=%s WHERE venue=%s",
                        (payload.name, old["name"]),
                    )
                db.execute(
                    f"UPDATE {PAYMENTS}.qr_enterprises SET aliases=CASE WHEN name<>%s "
                    "THEN array_append(aliases,name) ELSE aliases END,name=%s,active=%s,"
                    "revision=revision+1 WHERE id=%s",
                    (payload.name, payload.name, payload.active, venue_id),
                )
            else:
                if payload.revision is not None:
                    raise HTTPException(404, "Заведение не найдено.")
                if db.execute(
                    f"SELECT 1 FROM {PAYMENTS}.qr_enterprises WHERE name=%s OR %s=ANY(aliases)",
                    (payload.name, payload.name),
                ).fetchone():
                    raise HTTPException(409, "Название уже используется.")
                db.execute(
                    f"INSERT INTO {PAYMENTS}.qr_enterprises(id,name,api_key,active) "
                    "VALUES(%s,%s,'',%s)",
                    (venue_id, payload.name, payload.active),
                )
            self.audit(db, actor, "venue.update" if old else "venue.create", venue_id)
        return {"id": str(venue_id)}

    def save_terminal(self, actor, venue_id, terminal_id, payload):
        if self.payments is not None:
            return self.payments.save_terminal(actor, venue_id, terminal_id, payload)
        with self.write(actor) as db:
            venue = db.execute(
                f"SELECT default_terminal_id FROM {PAYMENTS}.qr_enterprises WHERE id=%s FOR UPDATE",
                (venue_id,),
            ).fetchone()
            if not venue:
                raise HTTPException(404, "Заведение не найдено.")
            old = db.execute(
                f"SELECT id,venue_id,revision FROM {PAYMENTS}.qr_terminals WHERE id=%s FOR UPDATE",
                (terminal_id,),
            ).fetchone()
            if old and (old["venue_id"] != venue_id or old["revision"] != payload.revision):
                raise HTTPException(409, "Терминал уже изменён. Обновите список.")
            if not old and payload.revision is not None:
                raise HTTPException(404, "Терминал не найден.")
            if not old and payload.api_key is None:
                raise HTTPException(422, "Для нового терминала нужен API-ключ.")
            if venue["default_terminal_id"] == terminal_id and not payload.active:
                raise HTTPException(422, "Сначала выберите другой терминал для приёма оплаты.")
            key = payload.api_key.get_secret_value() if payload.api_key else None
            db.execute(
                f"INSERT INTO {PAYMENTS}.qr_terminals(id,venue_id,name,api_key,"
                "qrt_uuid,active) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET "
                "name=EXCLUDED.name,api_key=COALESCE(%s,"
                f"{PAYMENTS}.qr_terminals.api_key),qrt_uuid=EXCLUDED.qrt_uuid,"
                f"active=EXCLUDED.active,revision={PAYMENTS}.qr_terminals.revision+1",
                (
                    terminal_id,
                    venue_id,
                    payload.name,
                    key or "",
                    str(payload.qrt_uuid) if payload.qrt_uuid else None,
                    payload.active,
                    key,
                ),
            )
            if payload.make_default or venue["default_terminal_id"] == terminal_id:
                db.execute(
                    f"UPDATE {PAYMENTS}.qr_enterprises v SET default_terminal_id=t.id,"
                    "api_key=t.api_key,qrt_uuid=t.qrt_uuid,revision=v.revision+1 FROM "
                    f"{PAYMENTS}.qr_terminals t WHERE v.id=%s AND t.id=%s",
                    (venue_id, terminal_id),
                )
            self.audit(db, actor, "terminal.update" if old else "terminal.create", terminal_id)
        return {"id": str(terminal_id)}


def create_admin_router(access, repo, web, store=None):
    router = APIRouter(prefix="/api/management", tags=["management"])
    store = store or Administration(repo)

    async def admin(request: Request, scope: Annotated[Scope, Depends(access)]):
        require_admin(scope.user)
        if request.method != "GET":
            request.app.state.auth.check_origin(request)
        return scope.actor if repo.runtime.mode == "tenant" else scope.user["id"]

    Admin = Annotated[UUID | ActorContext, Depends(admin)]

    @router.get("/accounts")
    def accounts(actor: Admin):
        return store.catalog(actor) if repo.runtime.mode == "tenant" else store.catalog()

    @router.post("/accounts", status_code=201)
    async def create_account(payload: NewAccount, request: Request, actor: Admin):
        if repo.runtime.mode == "tenant":
            from app.saas_admin.repository import Problem

            verifier = request.app.state.saas_auth_repository
            values = payload.model_dump(mode="json", exclude={"password"})
            body = {
                "request_id": str(payload.request_id),
                "email": payload.email,
                "display_name": payload.display_name,
                "password": payload.password.get_secret_value(),
                "profile_fingerprint": hashlib.sha256(
                    json.dumps(values, sort_keys=True).encode()
                ).hexdigest(),
            }
            try:
                identity = await run_in_threadpool(
                    verifier.create_company_account,
                    str(repo.runtime.company_id),
                    request.cookies.get("saas_tenant_session", ""),
                    request.headers.get("x-csrf-token", ""),
                    body,
                )
            except Problem as exc:
                raise HTTPException(exc.status, exc.message) from None
            return await run_in_threadpool(
                store.save_identity_account, actor, UUID(identity["id"]), payload
            )
        key = web.auth_admin_key.get_secret_value()
        if not key:
            raise HTTPException(503, "Создание учётных записей ещё не настроено.")
        identity = await run_in_threadpool(store.identity, payload.email)
        if identity and identity["provision_id"] != str(payload.request_id):
            raise HTTPException(409, "Учётная запись с этой почтой уже существует.")
        if not identity:
            try:
                # No automatic retries: a timeout can mean the Auth account was created.
                async with httpx.AsyncClient(
                    timeout=20, follow_redirects=False, trust_env=False
                ) as client:
                    response = await client.post(
                        web.supabase_url.rstrip("/") + "/auth/v1/admin/users",
                        headers={"apikey": key, "Authorization": "Bearer " + key},
                        json={
                            "email": payload.email,
                            "password": payload.password.get_secret_value(),
                            "email_confirm": True,
                            "app_metadata": {"chaika_portal_request_id": str(payload.request_id)},
                        },
                    )
                if response.status_code not in (200, 201):
                    raise HTTPException(
                        409 if response.status_code in (400, 422) else 503,
                        "Не удалось создать учётную запись. Проверьте список и повторите запрос.",
                    )
            except httpx.HTTPError:
                raise HTTPException(
                    503,
                    "Ответ сервиса входа не получен. Повторите тот же запрос для проверки "
                    "результата.",
                ) from None
            identity = await run_in_threadpool(store.identity, payload.email)
            if not identity or identity["provision_id"] != str(payload.request_id):
                raise HTTPException(
                    503, "Учётная запись ещё не подтверждена. Повторите тот же запрос."
                )
        return await run_in_threadpool(
            store.save_account, actor, identity["id"], payload, creating=True
        )

    @router.post("/accounts/{user_id}")
    def update_account(user_id: UUID, payload: EditAccount, actor: Admin):
        return store.save_account(actor, user_id, payload)

    @router.get("/venues")
    def venues(actor: Admin):
        return (
            store.configuration(actor) if repo.runtime.mode == "tenant" else store.configuration()
        )

    @router.post("/venues/{venue_id}")
    def save_venue(venue_id: UUID, payload: Venue, actor: Admin):
        return store.save_venue(actor, venue_id, payload)

    @router.post("/venues/{venue_id}/terminals/{terminal_id}")
    def save_terminal(venue_id: UUID, terminal_id: UUID, payload: Terminal, actor: Admin):
        return store.save_terminal(actor, venue_id, terminal_id, payload)

    return router
