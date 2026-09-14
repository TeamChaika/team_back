"""Поля номенклатуры, проверенные на ответе iiko; остальное остаётся в исходном JSON."""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.schemas.iiko_catalog import IikoCatalogSnapshot


class IikoContainer(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    id: UUID
    name: str
    num: str
    count: Decimal
    deleted: StrictBool


class IikoBarcode(BaseModel):
    model_config = ConfigDict(frozen=True)

    barcode: str
    container_id: UUID | None = Field(default=None, validation_alias="containerId")


class IikoProduct(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    id: UUID
    name: str
    num: str = Field(description="Артикул: строка, не обязательно уникальная.")
    code: str = Field(description="Код быстрого поиска; не штрихкод.")
    type: str = Field(min_length=1, description="Тип iiko: GOODS, DISH, PREPARED и другие.")
    deleted: StrictBool
    description: str | None = None
    parent_id: UUID | None = Field(default=None, validation_alias="parent")
    main_unit_id: UUID = Field(validation_alias="mainUnit")
    category_id: UUID | None = Field(default=None, validation_alias="category")
    accounting_category_id: UUID | None = Field(default=None, validation_alias="accountingCategory")
    default_sale_price: Decimal = Field(
        validation_alias="defaultSalePrice",
        description="Цена продажи по умолчанию. Не закупочная цена и не себестоимость.",
    )
    unit_weight: Decimal = Field(validation_alias="unitWeight", description="Кг на единицу.")
    unit_capacity: Decimal = Field(validation_alias="unitCapacity", description="Л на единицу.")
    containers: list[IikoContainer] = Field(default_factory=list)
    barcodes: list[IikoBarcode] | None = None


class IikoProductsSnapshot(IikoCatalogSnapshot):
    type_counts: dict[str, int]


class IikoProductsPage(BaseModel):
    snapshot: IikoProductsSnapshot
    offset: int
    limit: int
    items: list[IikoProduct]
