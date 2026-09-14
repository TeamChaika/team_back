"""Исходная техкарта: сохраняем интервалы, единицы и фильтры iiko без расчётов."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class StoreSpecification(BaseModel):
    model_config = ConfigDict(frozen=True)

    departments: list[UUID]
    inverse: StrictBool = Field(description="true — все подразделения, кроме перечисленных.")


class AssemblyChartItem(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    id: UUID
    sort_weight: Decimal = Field(validation_alias="sortWeight")
    product_id: UUID = Field(validation_alias="productId")
    product_size_id: UUID | None = Field(validation_alias="productSizeSpecification")
    store_specification: StoreSpecification | None = Field(validation_alias="storeSpecification")
    amount_in: Decimal = Field(
        validation_alias="amountIn",
        description="Брутто, в основных единицах ингредиента.",
        examples=["0.25"],
    )
    amount_middle: Decimal = Field(
        validation_alias="amountMiddle",
        description="Нетто, в основных единицах ингредиента.",
        examples=["0.20"],
    )
    amount_out: Decimal = Field(
        validation_alias="amountOut",
        description="Выход, в основных единицах ингредиента.",
        examples=["0.18"],
    )
    amount_in1: Decimal | None = Field(default=None, validation_alias="amountIn1")
    amount_out1: Decimal | None = Field(default=None, validation_alias="amountOut1")
    amount_in2: Decimal | None = Field(default=None, validation_alias="amountIn2")
    amount_out2: Decimal | None = Field(default=None, validation_alias="amountOut2")
    amount_in3: Decimal | None = Field(default=None, validation_alias="amountIn3")
    amount_out3: Decimal | None = Field(default=None, validation_alias="amountOut3")
    package_count: Decimal | None = Field(default=None, validation_alias="packageCount")
    package_type_id: UUID | None = Field(default=None, validation_alias="packageTypeId")


class AssemblyChart(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    id: UUID
    assembled_product_id: UUID = Field(validation_alias="assembledProductId")
    date_from: date = Field(validation_alias="dateFrom")
    date_to: date | None = Field(
        validation_alias="dateTo", description="Исключающая граница; null — без даты окончания."
    )
    assembled_amount: Decimal = Field(validation_alias="assembledAmount", examples=["1"])
    effective_direct_writeoff_store_specification: StoreSpecification | None = Field(
        validation_alias="effectiveDirectWriteoffStoreSpecification"
    )
    product_size_assembly_strategy: Literal["COMMON", "SPECIFIC"] = Field(
        validation_alias="productSizeAssemblyStrategy"
    )
    product_writeoff_strategy: Literal["ASSEMBLE", "DIRECT"] | None = Field(
        default=None, validation_alias="productWriteoffStrategy"
    )
    items: list[AssemblyChartItem]
    technology_description: str | None = Field(
        default=None, validation_alias="technologyDescription"
    )
    description: str | None = None
    appearance: str | None = None
    organoleptic: str | None = None
    output_comment: str | None = Field(default=None, validation_alias="outputComment")


class AssembledSourceResponse(BaseModel):
    """Контракт только getAssembled; известная ревизия непригодна для getAllUpdate."""

    known_revision: Literal[-1] = Field(validation_alias="knownRevision")
    assembly_charts: list[AssemblyChart] = Field(validation_alias="assemblyCharts", max_length=1)
    prepared_charts: list[object] | None = Field(
        default=None, validation_alias="preparedCharts", max_length=0
    )
    deleted_assembly_chart_ids: None = Field(
        default=None, validation_alias="deletedAssemblyChartIds"
    )
    deleted_prepared_chart_ids: None = Field(
        default=None, validation_alias="deletedPreparedChartIds"
    )


class AssembledChartResponse(BaseModel):
    snapshot_id: UUID
    received_at: datetime
    product_id: UUID
    business_date: date
    department_id: UUID | None
    known_revision: Literal[-1] = -1
    chart: AssemblyChart | None
    source_bytes: int
    sha256: str


class AllAssemblySourceResponse(BaseModel):
    known_revision: int = Field(validation_alias="knownRevision", strict=True, ge=-1)
    assembly_charts: list[AssemblyChart] = Field(validation_alias="assemblyCharts")
    prepared_charts: list[object] | None = Field(
        default=None, validation_alias="preparedCharts", max_length=0
    )
    deleted_assembly_chart_ids: None = Field(
        default=None, validation_alias="deletedAssemblyChartIds"
    )
    deleted_prepared_chart_ids: None = Field(
        default=None, validation_alias="deletedPreparedChartIds"
    )


class AllAssemblyResponse(BaseModel):
    snapshot_id: UUID
    received_at: datetime
    business_date: date
    date_to_exclusive: date
    include_deleted_products: Literal[True] = True
    include_prepared_charts: Literal[False] = False
    known_revision: int
    total: int
    items_count: int
    source_bytes: int
    sha256: str
