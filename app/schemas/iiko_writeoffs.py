"""Учётные даты, статусы, количества и себестоимость актов списания."""

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, NaiveDatetime, field_validator, model_validator

type WriteoffStatus = Literal["NEW", "PROCESSED", "DELETED"]


class WriteoffsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date = Field(description="Начальный учётный день", examples=["2026-09-09"])
    date_to: date = Field(
        description="Конечный учётный день; максимум 7 дней", examples=["2026-09-09"]
    )
    status: WriteoffStatus | None = None
    revision_from: int = Field(default=-1, ge=-1)

    @field_validator("date_from", "date_to", mode="before")
    @classmethod
    def require_date(cls, value: object) -> object:
        if isinstance(value, str):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("Укажите учётный день YYYY-MM-DD")
        elif type(value) is not date:
            raise ValueError("Требуется дата, не Unix timestamp или дата-время")
        return value

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if not 0 <= (self.date_to - self.date_from).days < 7:
            raise ValueError("Период должен составлять от 1 до 7 дней включительно")
        return self


class WriteoffDocumentItem(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    num: int = Field(strict=True)
    product_id: UUID = Field(validation_alias="productId")
    product_size_id: UUID | None = Field(default=None, validation_alias="productSizeId")
    amount_factor: Decimal | None = Field(
        default=None, validation_alias="amountFactor", examples=["1"]
    )
    amount: Decimal = Field(examples=["2.125"])
    measure_unit_id: UUID | None = Field(default=None, validation_alias="measureUnitId")
    container_id: UUID | None = Field(default=None, validation_alias="containerId")
    cost: Decimal | None = Field(
        default=None, description="Себестоимость всего количества в строке", examples=["123.45"]
    )


class WriteoffDocument(BaseModel):
    id: UUID
    date_incoming: NaiveDatetime = Field(validation_alias="dateIncoming")
    document_number: str = Field(validation_alias="documentNumber")
    status: WriteoffStatus
    conception_id: UUID | None = Field(default=None, validation_alias="conceptionId")
    comment: str | None = None
    store_id: UUID = Field(validation_alias="storeId")
    account_id: UUID = Field(validation_alias="accountId")
    external_outgoing_invoice_id: UUID | None = Field(
        default=None, validation_alias="externalOutgoingInvoiceId"
    )
    external_production_document_id: UUID | None = Field(
        default=None, validation_alias="externalProductionDocumentId"
    )
    items: list[WriteoffDocumentItem]

    @field_validator("date_incoming", mode="before")
    @classmethod
    def require_accounting_datetime(cls, value: object) -> object:
        if isinstance(value, str):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?", value):
                raise ValueError("Ожидалась учётная дата-время без часового пояса")
        elif not isinstance(value, datetime):
            raise ValueError("Ожидалась учётная дата-время")
        return value


class WriteoffExport(BaseModel):
    response: list[WriteoffDocument]
    revision: int = Field(strict=True, ge=-1)


class WriteoffsResponse(BaseModel):
    snapshot_id: UUID
    received_at: datetime
    request: WriteoffsQuery
    revision: int = Field(
        description="Ревизия всей выгрузки на стороне iiko, не отдельного документа"
    )
    total: int
    items_count: int
    documents: list[WriteoffDocument]
    source_bytes: int
    sha256: str
