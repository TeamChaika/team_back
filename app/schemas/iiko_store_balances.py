"""Количественные и денежные остатки товаров на складах."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.iiko_reports import AccountingReportQuery


class StoreBalancesQuery(AccountingReportQuery):
    department_id: list[UUID] = Field(default_factory=list, max_length=20)
    store_id: list[UUID] = Field(default_factory=list, max_length=20)
    product_id: list[UUID] = Field(default_factory=list, max_length=20)


class StoreBalance(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    store_id: UUID = Field(validation_alias="store")
    product_id: UUID = Field(validation_alias="product")
    amount: Decimal = Field(
        description="Количество со знаком, без пересчёта единиц", examples=["12.345"]
    )
    sum: Decimal = Field(
        description="Денежный остаток со знаком, без округления", examples=["678.90"]
    )


class StoreBalancesResponse(BaseModel):
    snapshot_id: UUID
    received_at: datetime
    request: StoreBalancesQuery
    total: int
    items: list[StoreBalance]
    source_bytes: int
    sha256: str
