"""Склад iiko: UUID, код, название и ссылка на родительское подразделение."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.iiko_catalog import IikoSnapshot


class IikoStore(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    parent_id: UUID | None = Field(default=None, validation_alias="parentId")
    code: str | None = Field(default=None, description="Может быть пустым; не уникальный ключ.")
    name: str | None = None
    type: Literal["STORE"]
    taxpayer_id_number: str | None = Field(default=None, validation_alias="taxpayerIdNumber")


class IikoStoresSnapshot(IikoSnapshot):
    revision_from: Literal[-1] = -1
    parent_ids: list[UUID] = Field(
        description="Ссылки на родителей; их названия запрашиваются отдельно."
    )
    stores_without_code: int = Field(ge=0)
    stores_without_parent: int = Field(ge=0)


class IikoStoresPage(BaseModel):
    snapshot: IikoStoresSnapshot
    offset: int
    limit: int
    items: list[IikoStore]
