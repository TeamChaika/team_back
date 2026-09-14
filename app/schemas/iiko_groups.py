"""Основные поля номенклатурных групп и диагностика их иерархии."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.schemas.iiko_catalog import IikoCatalogSnapshot


class IikoDepartmentFilter(BaseModel):
    departments: list[UUID]
    excluding: StrictBool


class IikoGroup(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    num: str = Field(description="Артикул группы, сохраняется строкой.")
    code: str
    deleted: StrictBool
    description: str | None = None
    parent_id: UUID | None = Field(validation_alias="parent")
    category_id: UUID | None = Field(default=None, validation_alias="category")
    accounting_category_id: UUID | None = Field(default=None, validation_alias="accountingCategory")
    tax_category_id: UUID | None = Field(default=None, validation_alias="taxCategory")
    front_image_id: UUID | None = Field(default=None, validation_alias="frontImageId")
    position: int | None = None
    visibility_filter: IikoDepartmentFilter | None = Field(
        default=None, validation_alias="visibilityFilter"
    )


class IikoGroupsSnapshot(IikoCatalogSnapshot):
    root_groups: int = Field(ge=0, description="Количество групп с parent=null.")
    missing_parent_ids: list[UUID] = Field(description="Родительские ID, которых нет в снимке.")
    cycle_group_ids: list[UUID] = Field(description="Группы, непосредственно образующие циклы.")


class IikoGroupsPage(BaseModel):
    snapshot: IikoGroupsSnapshot
    offset: int
    limit: int
    items: list[IikoGroup]
