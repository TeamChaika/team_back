"""Owner-only encrypted company configuration, with write-only credentials."""

import json
from typing import Literal
from uuid import UUID

from pydantic import (
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from .auth_validation import csrf_matches
from .models import Model
from .repository import Problem, stamp


class Seller(Model):
    name: str = Field(min_length=1, max_length=300)
    inn: str = Field(pattern=r"^(?:[0-9]{10}|[0-9]{12})$")
    kpp: str = Field(default="", pattern=r"^(?:[0-9]{9})?$")
    address: str = Field(default="", max_length=1000)
    phone: str = Field(default="", max_length=100)
    bank_name: str = Field(min_length=1, max_length=300)
    bic: str = Field(pattern=r"^[0-9]{9}$")
    account: str = Field(pattern=r"^[0-9]{20}$")
    correspondent_account: str = Field(pattern=r"^[0-9]{20}$")


class Telegram(Model):
    username: str = Field(default="", max_length=32, pattern=r"^(?:[A-Za-z][A-Za-z0-9_]{4,31})?$")
    token: SecretStr | None = None
    clear_token: StrictBool = False

    @field_validator("token")
    @classmethod
    def token_valid(cls, value):
        import re

        if (
            value
            and value.get_secret_value()
            and not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}", value.get_secret_value())
        ):
            raise ValueError("Проверьте формат токена Telegram")
        return value

    @model_validator(mode="after")
    def clear_valid(self):
        if self.clear_token and self.token and self.token.get_secret_value():
            raise ValueError("Нельзя одновременно заменить и удалить токен")
        return self


class Assistant(Model):
    provider: Literal["openai", "openrouter", "timeweb"] = "openai"
    model: str = Field(default="gpt-5.4-mini", min_length=1, max_length=100)
    agent_id: UUID | None = None
    key: SecretStr | None = None
    clear_key: StrictBool = False

    @field_validator("key")
    @classmethod
    def key_valid(cls, value):
        if value and (
            len(value.get_secret_value()) > 4096
            or any(c.isspace() for c in value.get_secret_value())
        ):
            raise ValueError("Проверьте формат ключа ИИ")
        return value

    @model_validator(mode="after")
    def clear_valid(self):
        if self.clear_key and self.key and self.key.get_secret_value():
            raise ValueError("Нельзя одновременно заменить и удалить ключ")
        return self


class ModuleSettingsWrite(Model):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)
    expected_version: StrictInt = Field(ge=1)
    expected_revision: StrictInt | None = Field(default=None, ge=1)
    seller: Seller | None = None
    telegram: Telegram | None = None
    assistant: Assistant | None = None

    @model_validator(mode="after")
    def groups_valid(self):
        if not (self.model_fields_set - {"expected_version", "expected_revision"}):
            raise ValueError("Укажите настройки для сохранения")
        if any(
            name in self.model_fields_set and getattr(self, name) is None
            for name in ("telegram", "assistant")
        ):
            raise ValueError("Укажите настройки модуля")
        return self


class IntegrationsWrite(Model):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)
    expected_version: StrictInt = Field(ge=1)
    expected_revision: StrictInt = Field(ge=1)
    telegram: Telegram | None = None
    assistant: Assistant | None = None

    @model_validator(mode="after")
    def groups_valid(self):
        changed = self.model_fields_set - {"expected_version", "expected_revision"}
        if not changed or any(getattr(self, name) is None for name in changed):
            raise ValueError("Укажите настройки модуля")
        return self


def defaults():
    return {
        "seller": None,
        "telegram": {"username": "", "token": ""},
        "assistant": {"provider": "openai", "model": "gpt-5.4-mini", "agent_id": None, "key": ""},
    }


class CompanyModuleSettings:
    def __init__(self, repository):
        self.repo = repository

    def _read(self, db, company_id):
        row = db.execute(
            "SELECT ciphertext FROM company_module_settings WHERE company_id=%s", (company_id,)
        ).fetchone()
        return json.loads(self.repo.vault.decrypt(row["ciphertext"])) if row else defaults()

    @staticmethod
    def _present(company, values):
        telegram, assistant = values["telegram"], values["assistant"]
        return {
            "company_version": company["version"],
            "integrations_revision": values.get("integrations_revision", 1),
            "seller": values["seller"],
            "telegram": {
                "username": telegram["username"],
                "token_configured": bool(telegram["token"]),
            },
            "assistant": {
                **{k: assistant[k] for k in ("provider", "model", "agent_id")},
                "key_configured": bool(assistant["key"]),
            },
            "missing": {
                "seller": values["seller"] is None,
                "telegram": not (telegram["username"] and telegram["token"]),
                "assistant": not assistant["key"]
                or (assistant["provider"] == "timeweb" and not assistant["agent_id"]),
            },
        }

    def get(self, company_id, actor):
        with self.repo.connect() as db:
            self.repo._require_owner(db, actor)
            company = self.repo._get(db, company_id)
            return self._present(company, self._read(db, company["id"]))

    @staticmethod
    def _merge(values, body):
        changed = sorted(body.model_fields_set - {"expected_version", "expected_revision"})
        if "seller" in changed:
            values["seller"] = body.seller.model_dump() if body.seller else None
        for name, secret, clear in (
            ("telegram", "token", "clear_token"),
            ("assistant", "key", "clear_key"),
        ):
            item = getattr(body, name)
            if item is None:
                continue
            incoming = getattr(item, secret)
            raw = incoming.get_secret_value() if incoming else ""
            existing = values[name][secret]
            if name == "assistant" and item.provider != values[name]["provider"]:
                existing = ""
            values[name] = item.model_dump(mode="json", exclude={secret, clear})
            values[name][secret] = "" if getattr(item, clear) else raw or existing
        if {"telegram", "assistant"}.intersection(changed):
            values["integrations_revision"] = values.get("integrations_revision", 1) + 1
        return changed

    def _save(self, db, company_id, values):
        db.execute(
            "INSERT INTO company_module_settings(company_id,ciphertext) VALUES(%s,%s) "
            "ON CONFLICT(company_id) DO UPDATE SET "
            "ciphertext=excluded.ciphertext,updated_at=now()",
            (company_id, self.repo.vault.encrypt(json.dumps(values))),
        )

    def integrations_revision(self, company_id):
        with self.repo.connect() as db:
            return self._read(db, company_id).get("integrations_revision", 1)

    def _tenant(self, db, slug, token, csrf=None):
        row = self.repo._tenant_verified(db, token, slug)
        if not row:
            raise Problem(401, "unauthorized", "Требуется повторный вход")
        if row["must_change"]:
            raise Problem(403, "password_change_required", "Измените временный пароль")
        if not row.get("_platform_owner") and row.get("role") != "company_admin":
            raise Problem(403, "company_admin_required", "Требуется руководитель компании")
        if csrf is not None and not csrf_matches(csrf, row["csrf"]):
            raise Problem(403, "csrf_failed", "Обновите страницу")
        company = self.repo._get(db, str(row["company_id"]))
        if company["slug"] != slug or company["status"] != "active":
            raise Problem(403, "tenant_boundary", "Компания недоступна")
        return row, company

    def _integrations_present(self, company, values, db):
        result = self._present(company, values)
        result.pop("seller")
        result["missing"].pop("seller")
        result["integrations_revision"] = values.get("integrations_revision", 1)
        runtime = db.execute(
            "SELECT checks FROM runtime_provisioning WHERE company_id=%s "
            "AND configuration_version=%s",
            (company["id"], company["version"]),
        ).fetchone()
        marker = runtime["checks"].get("integrations", {}) if runtime else {}
        if marker.get("revision") == result["integrations_revision"]:
            result["apply_status"] = (
                marker["state"]
                if marker.get("state") in {"applied", "failed"}
                else "pending"
            )
        return result

    def tenant_get(self, slug, token):
        with self.repo.connect(True) as db:
            _, company = self._tenant(db, slug, token)
            return self._integrations_present(company, self._read(db, company["id"]), db)

    def tenant_update(self, slug, token, csrf, body):
        from uuid import uuid4

        with self.repo.connect(True) as db:
            target = db.execute("SELECT id FROM companies WHERE slug=%s", (slug,)).fetchone()
            if target:
                locked = db.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked",
                    ("provision:" + str(target["id"]),),
                ).fetchone()
                if not locked["locked"]:
                    raise Problem(409, "runtime_busy", "Подготовка ещё идёт. Повторите позже")
            row, company = self._tenant(db, slug, token, csrf)
            # Auth refresh commits its rotated credentials; reacquire any released
            # transaction lock before changing settings or readiness evidence.
            locked = db.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked",
                ("provision:" + str(company["id"]),),
            ).fetchone()
            if not locked["locked"]:
                raise Problem(409, "runtime_busy", "Подготовка ещё идёт. Повторите позже")
            values = self._read(db, company["id"])
            if (
                company["version"] != body.expected_version
                or values.get("integrations_revision", 1) != body.expected_revision
            ):
                raise Problem(409, "version_conflict", "Настройки изменены. Обновите страницу")
            changed = self._merge(values, body)
            self._save(db, company["id"], values)
            from .integration_rollout import invalidate_integrations

            invalidate_integrations(db, company, values["integrations_revision"], changed)
            if row.get("_platform_owner"):
                self.repo._audit(db, company["id"], row, "integrations_updated", changed)
            else:
                db.execute(
                    "INSERT INTO tenant_events(id,company_id,admin_id,action,created_at) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (
                        str(uuid4()),
                        company["id"],
                        row["id"],
                        "integrations_updated:" + ",".join(changed),
                        stamp(),
                    ),
                )
            return self._integrations_present(company, values, db)

    def update(self, company_id, body, actor):
        repo = self.repo
        with repo.connect(True) as db:
            locked = db.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked",
                ("provision:" + str(company_id),),
            ).fetchone()
            if not locked["locked"]:
                raise Problem(409, "runtime_busy", "Подготовка ещё идёт. Повторите позже")
            repo._require_owner(db, actor)
            company = repo._get(db, company_id)
            if company["version"] != body.expected_version:
                raise Problem(409, "version_conflict", "Запись изменена. Обновите карточку")
            values = self._read(db, company["id"])
            revision = values.get("integrations_revision", 1)
            if {"telegram", "assistant"}.intersection(body.model_fields_set) and (
                body.expected_revision != revision
                and not (body.expected_revision is None and revision == 1)
            ):
                raise Problem(409, "version_conflict", "Настройки изменены. Обновите карточку")
            changed = self._merge(values, body)
            self._save(db, company["id"], values)
            company["version"] += 1
            company["updated_at"] = stamp()
            repo._write_company(db, company)
            repo._audit(
                db,
                company["id"],
                actor,
                "module_settings_updated",
                ["module_settings." + name for name in changed],
            )
            return self._present(company, values)

    def runtime_settings(self, company_id, *, expected_version=None, expected_revision=None):
        """Private operator call only; never mount this result in an HTTP response."""
        with self.repo.connect() as db:
            company = self.repo._get(db, company_id)
            if expected_version is not None and company["version"] != expected_version:
                raise Problem(
                    409, "version_conflict", "Настройки компании изменены. Повторите запуск"
                )
            values = self._read(db, company["id"])
        if (
            expected_revision is not None
            and values.get("integrations_revision", 1) != expected_revision
        ):
            raise Problem(409, "version_conflict", "Настройки изменены. Повторите запуск")
        telegram, assistant = values["telegram"], values["assistant"]
        ai = {
            "provider": assistant["provider"],
            "model": assistant["model"],
            "api_key": assistant["key"],
        }
        if assistant["agent_id"]:
            ai["timeweb_agent_id"] = assistant["agent_id"]
        return {
            "document_settings": {
                "commercial_seller_json": values["seller"] or "",
                "bot_username": telegram["username"],
                "bot_token": telegram["token"],
            },
            "assistant_settings": ai,
        }


def mount_module_settings(app, repo, owner_dependency):
    service = CompanyModuleSettings(repo)

    @app.get("/api/saas-admin/companies/{company_id}/module-settings")
    def get(company_id: str, session=owner_dependency):
        return service.get(company_id, session["user"])

    @app.patch("/api/saas-admin/companies/{company_id}/module-settings")
    def patch(company_id: str, body: ModuleSettingsWrite, session=owner_dependency):
        return service.update(company_id, body, session["user"])


def mount_tenant_integrations(app, repo):
    from fastapi import Request

    service = CompanyModuleSettings(repo)
    path = "/api/saas-tenant/{slug}/integrations"

    @app.get(path)
    def integrations(slug: str, request: Request):
        return service.tenant_get(slug, request.cookies.get("saas_tenant_session", ""))

    @app.post(path)
    def save_integrations(slug: str, body: IntegrationsWrite, request: Request):
        return service.tenant_update(
            slug,
            request.cookies.get("saas_tenant_session", ""),
            request.headers.get("x-csrf-token", ""),
            body,
        )
