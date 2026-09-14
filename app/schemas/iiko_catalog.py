"""Общие метаданные локальных снимков справочников."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class IikoSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: UUID
    received_at: datetime
    total: int = Field(ge=0)
    source_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class IikoCatalogSnapshot(IikoSnapshot):
    include_deleted: Literal[False] = False
