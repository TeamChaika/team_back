"""Strict metadata and write-only credential contracts."""

import ipaddress
import re
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    field_validator,
    model_validator,
)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def hostname(value: str) -> str:
    value = value.rstrip(".").lower().encode("idna").decode("ascii")
    if len(value) > 253 or "." not in value:
        raise ValueError("Укажите публичное доменное имя")
    if any(
        value == x or value.endswith("." + x)
        for x in ("localhost", "local", "internal", "test", "invalid", "example")
    ):
        raise ValueError("Локальный домен недопустим")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("IP-адрес недопустим")
    if (
        any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in value.split("."))
        or value.split(".")[-1].isdigit()
    ):
        raise ValueError("Некорректное доменное имя")
    return value


def metadata_url(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > 1000 or any(ord(c) < 33 for c in value) or "\\" in value:
        raise ValueError("Некорректный URL")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Нужен HTTP(S) URL")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("URL не должен содержать пароль, параметры или фрагмент")
    host = hostname(parsed.hostname)
    port = parsed.port
    if port is not None and port == 0:
        raise ValueError("Порт должен быть от 1 до 65535")
    return f"{parsed.scheme}://{host}" + (f":{port}" if port is not None else "") + parsed.path


class Contact(Model):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(max_length=254)
    phone: str = Field(max_length=50)

    @field_validator("email")
    @classmethod
    def email_valid(cls, value):
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Некорректный email")
        return value


class RMS(Model):
    id: UUID
    label: str = Field(min_length=1, max_length=200)
    url: str
    enabled: StrictBool
    _url = field_validator("url")(metadata_url)


class Modules(Model):
    analytics: StrictBool = False
    documents: StrictBool = False
    commercial_invoices: StrictBool = False
    finance: StrictBool = False
    deposits: StrictBool = False


class FeatureOverride(Model):
    mode: Literal["allow", "deny", "inherit"] = "inherit"
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def aware_expiration(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Срок исключения должен включать часовой пояс")
        return value


class Subscription(Model):
    plan: str = Field(default="", max_length=120)
    policy: Literal["legacy", "plans_v1"] = "legacy"
    plan_id: str | None = None
    status: Literal["active", "suspended", "cancelled"] = "active"
    timezone: str = "Europe/Simferopol"
    overrides: dict[str, FeatureOverride] = Field(default_factory=dict, max_length=100)

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, KeyError):
            raise ValueError("Неизвестный часовой пояс") from None
        return value

    @field_validator("overrides")
    @classmethod
    def features_valid(cls, value):
        from .entitlements import FEATURES

        if set(value) - set(FEATURES):
            raise ValueError("Неизвестная возможность")
        return value

    start_date: date | None = None
    end_date: date | None = None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def date_format(cls, value):
        if value is not None and (
            not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
        ):
            raise ValueError("Дата должна быть YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def dates(self):
        from .entitlements import PLANS

        if self.policy == "plans_v1" and self.plan_id not in PLANS:
            raise ValueError("Выберите существующий план")
        if self.policy == "legacy" and (self.plan_id or self.overrides):
            raise ValueError("Исключения доступны после выбора нового плана")
        if self.end_date and not self.start_date:
            raise ValueError("Укажите дату начала")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("Конец раньше начала")
        return self


class ConnectionCredential(Model):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    login: str = Field(min_length=1, max_length=200)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=1024)

    @field_validator("login")
    @classmethod
    def login_trimmed(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Укажите логин подключения")
        return value


class CompanyWrite(Model):
    connection_credentials: dict[str, ConnectionCredential] | None = None
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")
    domain: str | None = None
    status: Literal["draft", "active", "suspended"] = "draft"
    primary_admin: Contact | None = None
    chain_url: str | None = None
    rms: list[RMS] = Field(default_factory=list, max_length=100)
    modules: Modules = Field(default_factory=Modules)
    subscription: Subscription = Field(default_factory=Subscription)
    notes: str = Field(default="", max_length=10000)
    _chain = field_validator("chain_url")(metadata_url)

    @field_validator("domain")
    @classmethod
    def domain_valid(cls, value):
        if value is None:
            return None
        if any(ord(c) < 33 for c in value) or "\\" in value:
            raise ValueError("Некорректный домен")
        p = urlsplit(value if "://" in value else "https://" + value)
        if (
            p.scheme != "https"
            or not p.hostname
            or p.username is not None
            or p.password is not None
            or p.port is not None
            or p.path not in ("", "/")
            or p.query
            or p.fragment
        ):
            raise ValueError("Укажите домен без пути, порта и параметров")
        return hostname(p.hostname)

    @field_validator("rms")
    @classmethod
    def distinct_rms(cls, value):
        if len({r.id for r in value}) != len(value):
            raise ValueError("Повторяющийся идентификатор RMS")
        return value


def derived(company: dict) -> dict:
    from .entitlements import PLANS, subscription_state

    sub = company["subscription"]
    sub.setdefault("policy", "legacy")
    if sub["policy"] == "plans_v1" and sub.get("plan_id") in PLANS:
        sub["plan"] = PLANS[sub["plan_id"]][0]
    company["subscription_state"] = subscription_state(sub)
    company["integration_state"] = (
        "not_checked" if company["chain_url"] or company["rms"] else "not_configured"
    )
    return company
