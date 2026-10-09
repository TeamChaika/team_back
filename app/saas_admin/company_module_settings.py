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
    seller: Seller | None = None
    telegram: Telegram | None = None
    assistant: Assistant | None = None

    @model_validator(mode="after")
    def groups_valid(self):
        if not (self.model_fields_set - {"expected_version"}):
            raise ValueError("Укажите настройки для сохранения")
        if any(
            name in self.model_fields_set and getattr(self, name) is None
            for name in ("telegram", "assistant")
        ):
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

    def update(self, company_id, body, actor):
        repo = self.repo
        with repo.connect(True) as db:
            repo._require_owner(db, actor)
            company = repo._get(db, company_id)
            if company["version"] != body.expected_version:
                raise Problem(409, "version_conflict", "Запись изменена. Обновите карточку")
            values = self._read(db, company["id"])
            changed = sorted(body.model_fields_set - {"expected_version"})
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
                # A provider change must never silently send an old provider's key elsewhere.
                if name == "assistant" and item.provider != values[name]["provider"]:
                    existing = ""
                values[name] = item.model_dump(mode="json", exclude={secret, clear})
                values[name][secret] = "" if getattr(item, clear) else raw or existing
            db.execute(
                "INSERT INTO company_module_settings(company_id,ciphertext) VALUES(%s,%s) "
                "ON CONFLICT(company_id) DO UPDATE SET "
                "ciphertext=excluded.ciphertext,updated_at=now()",
                (company["id"], repo.vault.encrypt(json.dumps(values))),
            )
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

    def runtime_settings(self, company_id, *, expected_version=None):
        """Private operator call only; never mount this result in an HTTP response."""
        with self.repo.connect() as db:
            company = self.repo._get(db, company_id)
            if expected_version is not None and company["version"] != expected_version:
                raise Problem(
                    409, "version_conflict", "Настройки компании изменены. Повторите запуск"
                )
            values = self._read(db, company["id"])
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
