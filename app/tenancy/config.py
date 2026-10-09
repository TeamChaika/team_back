"""Immutable, process-scoped tenant configuration; never inferred from a request host."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

Area = Literal["analytics", "documents", "payments"]
_AREAS = ("analytics", "documents", "payments")
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z")


class RuntimeConfigurationError(ValueError):
    """Configuration cannot safely identify a single runtime."""


def identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise RuntimeConfigurationError("Invalid PostgreSQL identifier")
    return value


def schema_names(company_id: UUID | str) -> dict[str, str]:
    company = UUID(str(company_id))
    if company.int == 0:
        raise RuntimeConfigurationError("A nonzero company UUID is required")
    return {area: f"c_{company.hex}_{area}" for area in _AREAS}


def _origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeConfigurationError("Invalid exact HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or "*" in value
        or parsed.hostname != parsed.hostname.lower()
        or value != value.strip()
        or any(char.isspace() for char in value)
        or "\\" in value
        or parsed.hostname == "chaika.team"
        or parsed.hostname.endswith(".chaika.team")
        or port == 0
    ):
        raise RuntimeConfigurationError("Tenant requires an exact non-Chaika HTTPS origin")
    # Canonical ASCII host spelling prevents visually equivalent origin variants.
    if parsed.hostname.encode("idna").decode() != parsed.hostname:
        raise RuntimeConfigurationError("Origin must use ASCII hostname")
    canonical_host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    expected = f"https://{canonical_host}" + (f":{port}" if port is not None else "")
    if value != expected:
        raise RuntimeConfigurationError("Origin must be canonical and exact")
    return value


@dataclass(frozen=True)
class TenantRuntime:
    mode: Literal["legacy", "tenant"] = "legacy"
    company_id: UUID | None = None
    analytics_schema: str = "chaika"
    documents_schema: str = "chaika_iiko_documents"
    payments_schema: str = "chaika_deposits"
    timezone: str = "Europe/Moscow"
    frontend_origin: str = ""
    api_origin: str = ""
    runtime_directory: Path = Path(".")
    configuration_version: int = 1
    database_role: str = ""
    collector_port: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("legacy", "tenant"):
            raise RuntimeConfigurationError("Runtime mode must be legacy or tenant")
        object.__setattr__(self, "runtime_directory", Path(self.runtime_directory))
        if self.mode == "legacy":
            if self.company_id is not None:
                raise RuntimeConfigurationError("Legacy runtime cannot contain a company UUID")
            if (self.analytics_schema, self.documents_schema, self.payments_schema) != (
                "chaika",
                "chaika_iiko_documents",
                "chaika_deposits",
            ):
                raise RuntimeConfigurationError("Legacy schema contract cannot be overridden")
            return
        if self.company_id is None:
            raise RuntimeConfigurationError("Tenant company UUID is required")
        company = UUID(str(self.company_id))
        object.__setattr__(self, "company_id", company)
        for area, expected in schema_names(company).items():
            if identifier(self.schema(area)) != expected:
                raise RuntimeConfigurationError(f"{area} schema must derive from company UUID")
        if identifier(self.database_role) != f"c_{company.hex}_runtime":
            raise RuntimeConfigurationError("Database role must derive from company UUID")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise RuntimeConfigurationError("Valid explicit timezone is required") from exc
        _origin(self.frontend_origin)
        _origin(self.api_origin)
        if self.frontend_origin == self.api_origin:
            raise RuntimeConfigurationError("Frontend and API origins must be distinct")
        if type(self.configuration_version) is not int or self.configuration_version < 1:
            raise RuntimeConfigurationError("Positive configuration version is required")
        directory = self.runtime_directory
        if not directory.is_absolute() or ".." in directory.parts:
            raise RuntimeConfigurationError("Runtime directory must be an absolute tenant path")
        if directory.name != f"c_{company.hex}":
            raise RuntimeConfigurationError("Runtime directory must end with company UUID key")
        if self.collector_port is not None and (
            type(self.collector_port) is not int or not 1024 <= self.collector_port <= 65535
        ):
            raise RuntimeConfigurationError("Collector port must be between 1024 and 65535")

    def schema(self, area: Area) -> str:
        if area not in _AREAS:
            raise RuntimeConfigurationError("Unknown schema area")
        return getattr(self, f"{area}_schema")

    @property
    def key(self) -> str:
        return f"c_{self.company_id.hex}" if self.company_id is not None else "legacy"

    def runtime_path(self, name: str) -> Path:
        if not _NAME.fullmatch(name) or name in (".", ".."):
            raise RuntimeConfigurationError("Runtime filename must be one safe path component")
        return self.runtime_directory / name

    @property
    def collector_socket(self) -> Path:
        return self.runtime_path("collector.sock")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "TenantRuntime":
        env = os.environ if environ is None else environ
        mode = env.get("RESTCONTROL_RUNTIME_MODE", "legacy")
        if mode == "legacy":
            if any(key.startswith("RESTCONTROL_TENANT_") for key in env):
                raise RuntimeConfigurationError(
                    "Tenant configuration requires explicit tenant mode"
                )
            return cls()
        if mode != "tenant":
            raise RuntimeConfigurationError("Runtime mode must be legacy or tenant")

        def required(name: str) -> str:
            value = env.get(f"RESTCONTROL_TENANT_{name}", "")
            if not value or value != value.strip():
                raise RuntimeConfigurationError(f"RESTCONTROL_TENANT_{name} is required")
            return value

        company = UUID(required("COMPANY_ID"))
        schemas = schema_names(company)
        return cls(
            mode="tenant",
            company_id=company,
            **{f"{area}_schema": schema for area, schema in schemas.items()},
            timezone=required("TIMEZONE"),
            frontend_origin=required("FRONTEND_ORIGIN"),
            api_origin=required("API_ORIGIN"),
            runtime_directory=Path(required("RUNTIME_DIRECTORY")),
            configuration_version=int(required("CONFIGURATION_VERSION")),
            database_role=required("DATABASE_ROLE"),
            collector_port=int(env["RESTCONTROL_TENANT_COLLECTOR_PORT"])
            if env.get("RESTCONTROL_TENANT_COLLECTOR_PORT")
            else None,
        )


@lru_cache(maxsize=1)
def load_runtime() -> TenantRuntime:
    """Resolve once at process startup; callers cannot select a tenant per request."""
    return TenantRuntime.from_env()
