"""Публичные подключения и результат проверки типа сервера; секретов здесь нет."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.schemas.iiko import IikoSessionStatus

type IikoServerType = Literal["CHAIN", "REPLICATED_RMS", "STANDALONE_RMS"]


class IikoConnectionStatus(IikoSessionStatus):
    connection_id: str
    label: str
    base_url: str | None
    last_verified_type: IikoServerType | None
    type_verified_at: datetime | None


class IikoConnectionsResponse(BaseModel):
    items: list[IikoConnectionStatus]


class IikoServerTypeResponse(BaseModel):
    connection_id: str
    server_type: IikoServerType
    checked_at: datetime
    snapshot_id: UUID
    source_bytes: int
    sha256: str
