"""Именованные подключения задаются локально, отдельно от HTTP-запросов."""

import json
from pathlib import Path
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)

from app.core.config import BACKEND_DIR, Settings
from app.integrations.iiko.errors import IikoError


class ConnectionDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,49}$")
    label: str = Field(min_length=1, max_length=100)
    base_url: HttpUrl
    use_primary_credentials: bool = False
    login: str = Field(default="", repr=False)
    password: SecretStr = Field(default=SecretStr(""), repr=False)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: HttpUrl) -> HttpUrl:
        Settings.validate_iiko_url(value)
        return value

    @model_validator(mode="after")
    def validate_credentials(self) -> Self:
        if self.use_primary_credentials and (self.login or self.password.get_secret_value()):
            raise ValueError("Choose either primary or individual credentials")
        return self


class ConnectionsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    connections: list[ConnectionDefinition] = Field(max_length=100)


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate configuration field")
        result[key] = value
    return result


def read_connections(settings: Settings) -> list[tuple[ConnectionDefinition, Settings]]:
    inline = settings.iiko_connections_json.get_secret_value()
    if settings.iiko_connections_file is None and not inline:
        return []
    try:
        if inline:
            raw = inline.encode()
        else:
            path = Path(settings.iiko_connections_file)
            if not path.is_absolute():
                path = BACKEND_DIR / path
            with path.open("rb") as stream:
                raw = stream.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("Connections configuration too large")
        config = ConnectionsConfig.model_validate(json.loads(raw, object_pairs_hook=_unique_fields))
        ids = {"primary"}
        urls = {str(settings.iiko_base_url).rstrip("/")} if settings.iiko_base_url else set()
        connections = []
        for definition in config.connections:
            url = str(definition.base_url).rstrip("/")
            if definition.id in ids or url in urls:
                raise ValueError("Connection id or server URL already registered")
            ids.add(definition.id)
            urls.add(url)
            values = settings.model_dump()
            values.update(
                iiko_base_url=definition.base_url,
                iiko_connections_file=None,
                iiko_connections_json=SecretStr(""),
            )
            if not definition.use_primary_credentials:
                values.update(iiko_login=definition.login, iiko_password=definition.password)
            connections.append((definition, Settings(_env_file=None, **values)))
        return connections
    except (OSError, ValueError, RecursionError):
        raise IikoError(
            "iiko_connections_config_error",
            "Проверьте локальный файл подключений: формат, HTTPS-адреса и уникальность записей.",
            status_code=500,
        ) from None
