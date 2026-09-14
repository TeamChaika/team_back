"""Сессия на каждый настроенный сервер и один метод replication/serverType."""

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import TypeAdapter

from app.core.config import BACKEND_DIR, Settings
from app.core.iiko_connections import read_connections
from app.integrations.iiko.client import IikoClient
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko import IikoSessionStatus
from app.schemas.iiko_connections import (
    IikoConnectionsResponse,
    IikoConnectionStatus,
    IikoServerType,
    IikoServerTypeResponse,
)
from app.services.iiko_auth import IikoAuthService

logger = logging.getLogger(__name__)
_server_type_adapter = TypeAdapter(IikoServerType)


def read_server_type(path: Path) -> IikoServerType:
    try:
        text = path.read_text(encoding="utf-8").strip()
        value = json.loads(text) if text.startswith('"') else text
        return _server_type_adapter.validate_python(value)
    except (ValueError, RecursionError):
        raise IikoError(
            "iiko_server_type_invalid_response", "iiko вернул неизвестный формат или тип сервера."
        ) from None


@dataclass
class Connection:
    label: str
    settings: Settings
    auth: IikoAuthService
    last_result: IikoServerTypeResponse | None = None


class IikoConnectionsService:
    def __init__(
        self,
        settings: Settings,
        primary: IikoAuthService,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        directory: Path | None = None,
    ) -> None:
        definitions = read_connections(settings)
        self._connections = {"primary": Connection("Текущее подключение", settings, primary)}
        for definition, connection_settings in definitions:
            self._connections[definition.id] = Connection(
                definition.label,
                connection_settings,
                IikoAuthService(connection_settings, IikoClient(connection_settings, transport)),
            )
        self._directory = directory if directory is not None else BACKEND_DIR / ".local/connections"

    def _get(self, connection_id: str) -> Connection:
        connection = self._connections.get(connection_id)
        if connection is None:
            raise IikoError("iiko_connection_not_found", "Подключение не найдено.", status_code=404)
        return connection

    def get_connection(self, connection_id: str) -> Connection:
        """Внутренняя зависимость сервисов; реквизиты не возвращаются HTTP-клиенту."""
        return self._get(connection_id)

    def connection_ids(self) -> list[str]:
        return list(self._connections)

    def list(self) -> IikoConnectionsResponse:
        return IikoConnectionsResponse(
            items=[
                IikoConnectionStatus(
                    **connection.auth.status().model_dump(),
                    connection_id=connection_id,
                    label=connection.label,
                    base_url=str(connection.settings.iiko_base_url).rstrip("/")
                    if connection.settings.iiko_base_url
                    else None,
                    last_verified_type=connection.last_result.server_type
                    if connection.last_result
                    else None,
                    type_verified_at=connection.last_result.checked_at
                    if connection.last_result
                    else None,
                )
                for connection_id, connection in self._connections.items()
            ]
        )

    async def get_server_type(self, connection_id: str) -> IikoServerTypeResponse:
        connection = self._get(connection_id)
        snapshot_id = uuid4()
        directory = self._directory / connection_id
        part_file = directory / f"{snapshot_id}.txt.part"
        raw_file = directory / f"{snapshot_id}.txt"
        meta_file = directory / f"{snapshot_id}.meta.json"
        saved = False
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._directory.chmod(0o700)
            directory.mkdir(exist_ok=True, mode=0o700)
            directory.chmod(0o700)
            download = await connection.auth.download_server_type(part_file)
            server_type = read_server_type(part_file)
            result = IikoServerTypeResponse(
                connection_id=connection_id,
                server_type=server_type,
                checked_at=datetime.now(UTC),
                snapshot_id=snapshot_id,
                source_bytes=download.size_bytes,
                sha256=download.sha256,
            )
            metadata = result.model_dump(mode="json")
            identity = (
                f"{str(connection.settings.iiko_base_url).rstrip('/')}\n"
                f"{connection.settings.iiko_login}"
            )
            metadata["source_fingerprint"] = hashlib.sha256(identity.encode()).hexdigest()
            metadata["source_endpoint"] = "replication/serverType"
            descriptor = os.open(meta_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(metadata, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            part_file.replace(raw_file)
            saved = True
            connection.last_result = result
            return result
        except OSError:
            raise IikoError(
                "iiko_server_type_storage_error",
                "Не удалось сохранить результат проверки типа сервера.",
                status_code=500,
            ) from None
        finally:
            for path in [part_file] if saved else [part_file, meta_file]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Не удалось удалить незавершённую проверку типа сервера.")

    async def logout(self, connection_id: str) -> IikoSessionStatus:
        return await self._get(connection_id).auth.logout()

    async def aclose(self) -> None:
        for connection_id, connection in self._connections.items():
            if connection_id != "primary":
                await connection.auth.aclose()
