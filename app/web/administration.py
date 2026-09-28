"""Account provisioning and deposit configuration, available only to portal admins."""

import re
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

import httpx
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

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
    all_departments: bool = False
    department_ids: list[UUID] = Field(default_factory=list, max_length=100)
    deposits_all: bool = False
    deposits_create: bool = False
    deposit_grants: list[DepositGrant] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def permissions(self):
        if len(set(self.sections)) != len(self.sections) or set(self.sections) - set(SECTIONS):
            raise ValueError("Unknown or duplicate section")
        if ANALYTICS_SECTIONS.intersection(self.sections) and not (
            self.all_departments or self.department_ids
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
    password: SecretStr = Field(min_length=12, max_length=128)

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
    def __init__(self, repo):
        self.repo = repo

    @contextmanager
    def write(self, actor):
        self.repo._pool.open()
        try:
            with self.repo._pool.connection() as db, db.transaction():
                db.execute("SET LOCAL statement_timeout='15000ms'")
                row = db.execute(
                    "SELECT is_portal_admin FROM chaika.web_users WHERE id=%s AND active FOR SHARE",
                    (actor,),
                ).fetchone()
                if not row or not row["is_portal_admin"]:
                    raise HTTPException(403, "Управление доступно только администратору.")
                yield db
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "Такая запись уже существует. Обновите список.") from None

    def audit(self, db, actor, action, target):
        # Deliberately excludes passwords, provider keys and submitted payloads.
        db.execute(
            "INSERT INTO chaika.portal_admin_audit(actor_id,action,target_id) VALUES(%s,%s,%s)",
            (actor, action, target),
        )

    def identity(self, email):
        with self.repo.connection() as db:
            return db.execute(
                "SELECT id,provision_id FROM chaika.portal_identities WHERE lower(email)=%s",
                (email,),
            ).fetchone()

    def catalog(self):
        with self.repo.connection() as db:
            users = db.execute(
                "SELECT w.*,i.email FROM chaika.web_users w JOIN chaika.portal_identities "
                "i USING(id) ORDER BY w.display_name LIMIT 5001"
            ).fetchall()
            if len(users) > 5000:
                raise HTTPException(413, "Слишком много сотрудников для этого списка.")
            departments = db.execute(
                "SELECT id,name FROM chaika.corporate_nodes WHERE source_id='primary' AND "
                "type IN ('DEPARTMENT','CENTRALSTORE','MANUFACTURE') ORDER BY name"
            ).fetchall()
            grants = db.execute(
                "SELECT user_id,department_id FROM chaika.web_department_access WHERE "
                "source_id='primary'"
            ).fetchall()
            deposits = db.execute(
                "SELECT user_id,venue,is_all,can_create FROM chaika_deposits.user_venues"
            ).fetchall()
            venues = db.execute(
                "SELECT name FROM chaika_deposits.qr_enterprises UNION SELECT DISTINCT "
                "venue AS name FROM chaika_deposits.deposits WHERE venue IS NOT NULL ORDER "
                "BY name"
            ).fetchall()
        for user in users:
            user["department_ids"] = [
                g["department_id"] for g in grants if g["user_id"] == user["id"]
            ]
            own = [g for g in deposits if g["user_id"] == user["id"]]
            user["deposits_all"] = any(g["is_all"] for g in own)
            user["deposits_create"] = any(g["is_all"] and g["can_create"] for g in own)
            user["deposit_grants"] = [
                {"venue": g["venue"], "can_create": g["can_create"]} for g in own if not g["is_all"]
            ]
        return serial(
            {
                "users": users,
                "departments": departments,
                "venues": [v["name"] for v in venues],
                "sections": [{"id": k, "title": v} for k, v in SECTIONS.items()],
            }
        )

    def save_account(self, actor, user_id, payload, *, creating=False):
        with self.write(actor) as db:
            current = db.execute(
                "SELECT * FROM chaika.web_users WHERE id=%s FOR UPDATE", (user_id,)
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
            self.validate_scope(db, payload)
            role = current["role"] if current else "manager"
            db.execute(
                "INSERT INTO chaika.web_users(id,display_name,role,active,sections,"
                "all_departments) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET "
                "display_name=EXCLUDED.display_name,active=EXCLUDED.active,"
                "sections=EXCLUDED.sections,all_departments=EXCLUDED.all_departments,"
                "revision=chaika.web_users.revision+1",
                (
                    user_id,
                    payload.display_name,
                    role,
                    payload.active,
                    payload.sections,
                    payload.all_departments,
                ),
            )
            db.execute("DELETE FROM chaika.web_department_access WHERE user_id=%s", (user_id,))
            for department in set(payload.department_ids):
                db.execute(
                    "INSERT INTO chaika.web_department_access(user_id,source_id,"
                    "department_id) VALUES(%s,'primary',%s)",
                    (user_id, department),
                )
            db.execute("DELETE FROM chaika_deposits.user_venues WHERE user_id=%s", (user_id,))
            if "deposits" in payload.sections:
                if payload.deposits_all:
                    db.execute(
                        "INSERT INTO chaika_deposits.user_venues(user_id,venue,is_all,"
                        "can_create) VALUES(%s,'*',true,%s)",
                        (user_id, payload.deposits_create),
                    )
                else:
                    for grant in payload.deposit_grants:
                        db.execute(
                            "INSERT INTO chaika_deposits.user_venues(user_id,venue,is_all,"
                            "can_create) VALUES(%s,%s,false,%s)",
                            (user_id, grant.venue, grant.can_create),
                        )
            self.audit(db, actor, "account.create" if creating else "account.update", user_id)
        return {"id": str(user_id)}

    def validate_scope(self, db, payload):
        departments = db.execute(
            "SELECT id FROM chaika.corporate_nodes WHERE source_id='primary' AND type IN "
            "('DEPARTMENT','CENTRALSTORE','MANUFACTURE') AND id=ANY(%s::uuid[])",
            (payload.department_ids,),
        ).fetchall()
        if {r["id"] for r in departments} != set(payload.department_ids):
            raise HTTPException(422, "Выбрано неизвестное заведение iiko.")
        venues = db.execute(
            "SELECT name FROM chaika_deposits.qr_enterprises UNION SELECT DISTINCT venue "
            "AS name FROM chaika_deposits.deposits WHERE venue IS NOT NULL"
        ).fetchall()
        if {g.venue for g in payload.deposit_grants} - {v["name"] for v in venues}:
            raise HTTPException(422, "Выбрано неизвестное заведение депозитов.")

    def configuration(self):
        with self.repo.connection() as db:
            venues = db.execute(
                "SELECT id,name,active,revision,default_terminal_id FROM "
                "chaika_deposits.qr_enterprises ORDER BY name"
            ).fetchall()
            terminals = db.execute(
                "SELECT id,venue_id,name,qrt_uuid,active,revision,(length(api_key)>0) AS "
                "key_configured FROM chaika_deposits.qr_terminals ORDER BY name"
            ).fetchall()
        return serial({"venues": venues, "terminals": terminals})

    def save_venue(self, actor, venue_id, payload):
        with self.write(actor) as db:
            old = db.execute(
                "SELECT name,revision FROM chaika_deposits.qr_enterprises WHERE id=%s FOR UPDATE",
                (venue_id,),
            ).fetchone()
            if old:
                if payload.revision != old["revision"]:
                    raise HTTPException(409, "Заведение уже изменено. Обновите список.")
                if old["name"] != payload.name:
                    conflict = db.execute(
                        "SELECT 1 FROM chaika_deposits.qr_enterprises WHERE id<>%s AND "
                        "(name=%s OR %s=ANY(aliases))",
                        (venue_id, payload.name, payload.name),
                    ).fetchone()
                    if conflict:
                        raise HTTPException(409, "Название уже используется другим заведением.")
                    db.execute(
                        "UPDATE chaika_deposits.deposits SET venue=%s WHERE venue=%s",
                        (payload.name, old["name"]),
                    )
                    db.execute(
                        "UPDATE chaika_deposits.user_venues SET venue=%s WHERE venue=%s",
                        (payload.name, old["name"]),
                    )
                db.execute(
                    "UPDATE chaika_deposits.qr_enterprises SET aliases=CASE WHEN name<>%s "
                    "THEN array_append(aliases,name) ELSE aliases END,name=%s,active=%s,"
                    "revision=revision+1 WHERE id=%s",
                    (payload.name, payload.name, payload.active, venue_id),
                )
            else:
                if payload.revision is not None:
                    raise HTTPException(404, "Заведение не найдено.")
                if db.execute(
                    "SELECT 1 FROM chaika_deposits.qr_enterprises WHERE name=%s OR %s=ANY(aliases)",
                    (payload.name, payload.name),
                ).fetchone():
                    raise HTTPException(409, "Название уже используется.")
                db.execute(
                    "INSERT INTO chaika_deposits.qr_enterprises(id,name,api_key,active) "
                    "VALUES(%s,%s,'',%s)",
                    (venue_id, payload.name, payload.active),
                )
            self.audit(db, actor, "venue.update" if old else "venue.create", venue_id)
        return {"id": str(venue_id)}

    def save_terminal(self, actor, venue_id, terminal_id, payload):
        with self.write(actor) as db:
            venue = db.execute(
                "SELECT default_terminal_id FROM chaika_deposits.qr_enterprises WHERE "
                "id=%s FOR UPDATE",
                (venue_id,),
            ).fetchone()
            if not venue:
                raise HTTPException(404, "Заведение не найдено.")
            old = db.execute(
                "SELECT id,venue_id,revision FROM chaika_deposits.qr_terminals WHERE id=%s "
                "FOR UPDATE",
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
                "INSERT INTO chaika_deposits.qr_terminals(id,venue_id,name,api_key,"
                "qrt_uuid,active) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET "
                "name=EXCLUDED.name,api_key=COALESCE(%s,"
                "chaika_deposits.qr_terminals.api_key),qrt_uuid=EXCLUDED.qrt_uuid,"
                "active=EXCLUDED.active,revision=chaika_deposits.qr_terminals.revision+1",
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
                    "UPDATE chaika_deposits.qr_enterprises v SET default_terminal_id=t.id,"
                    "api_key=t.api_key,qrt_uuid=t.qrt_uuid,revision=v.revision+1 FROM "
                    "chaika_deposits.qr_terminals t WHERE v.id=%s AND t.id=%s",
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
        return scope.user["id"]

    Admin = Annotated[UUID, Depends(admin)]

    @router.get("/accounts")
    def accounts(actor: Admin):
        return store.catalog()

    @router.post("/accounts", status_code=201)
    async def create_account(payload: NewAccount, request: Request, actor: Admin):
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
        return store.configuration()

    @router.post("/venues/{venue_id}")
    def save_venue(venue_id: UUID, payload: Venue, actor: Admin):
        return store.save_venue(actor, venue_id, payload)

    @router.post("/venues/{venue_id}/terminals/{terminal_id}")
    def save_terminal(venue_id: UUID, terminal_id: UUID, payload: Terminal, actor: Admin):
        return store.save_terminal(actor, venue_id, terminal_id, payload)

    return router
