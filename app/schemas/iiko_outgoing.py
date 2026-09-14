"""Outgoing-invoice XML fields, distinct from the incoming-invoice contract."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.iiko_invoices import InvoiceFields


class OutgoingInvoicesQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date_from: date
    date_to: date = Field(description="Конечная дата включительно; максимум 7 дней")

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if not 0 <= (self.date_to - self.date_from).days < 7:
            raise ValueError("Период должен составлять от 1 до 7 дней включительно")
        return self


class OutgoingInvoiceItem(InvoiceFields):
    product_id: UUID | None = Field(default=None, validation_alias="productId")
    product_article: str | None = Field(default=None, validation_alias="productArticle")
    store_id: UUID | None = Field(default=None, validation_alias="storeId")
    store_code: str | None = Field(default=None, validation_alias="storeCode")
    container_id: UUID | None = Field(default=None, validation_alias="containerId")
    price: Decimal | None = None
    price_without_vat: Decimal | None = Field(default=None, validation_alias="priceWithoutVat")
    amount: Decimal | None = None
    sum: Decimal
    discount_sum: Decimal | None = Field(default=None, validation_alias="discountSum")
    vat_percent: Decimal | None = Field(default=None, validation_alias="vatPercent")
    vat_sum: Decimal | None = Field(default=None, validation_alias="vatSum")


class OutgoingInvoice(InvoiceFields):
    id: UUID
    document_number: str | None = Field(default=None, validation_alias="documentNumber")
    date_incoming: date | datetime = Field(validation_alias="dateIncoming")
    status: Literal["NEW", "PROCESSED", "DELETED"]
    use_default_document_time: bool | None = Field(
        default=None, validation_alias="useDefaultDocumentTime"
    )
    account_to_id: UUID | None = Field(default=None, validation_alias="accountToId")
    account_to_code: str | None = Field(default=None, validation_alias="accountToCode")
    revenue_account_id: UUID | None = Field(default=None, validation_alias="revenueAccountId")
    revenue_account_code: str | None = Field(default=None, validation_alias="revenueAccountCode")
    default_store_id: UUID | None = Field(default=None, validation_alias="defaultStoreId")
    default_store_code: str | None = Field(default=None, validation_alias="defaultStoreCode")
    counteragent_id: UUID | None = Field(default=None, validation_alias="counteragentId")
    counteragent_code: str | None = Field(default=None, validation_alias="counteragentCode")
    conception_id: UUID | None = Field(default=None, validation_alias="conceptionId")
    conception_code: str | None = Field(default=None, validation_alias="conceptionCode")
    comment: str | None = None
    linked_incoming_invoice_id: UUID | None = Field(
        default=None, validation_alias="linkedIncomingInvoiceId"
    )
    # Some published XSD versions list this separately; never infer its direction.
    linked_outgoing_invoice_id: UUID | None = Field(
        default=None, validation_alias="linkedOutgoingInvoiceId"
    )
    items: list[OutgoingInvoiceItem] = Field(default_factory=list)

    @field_validator("date_incoming", mode="before")
    @classmethod
    def preserve_date_kind(cls, value: object) -> object:
        if isinstance(value, str):
            return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
        return value


class OutgoingInvoicesResponse(BaseModel):
    snapshot_id: UUID
    received_at: datetime
    request: OutgoingInvoicesQuery
    total: int
    items_count: int
    documents: list[OutgoingInvoice]
    source_bytes: int
    sha256: str


class OutgoingSyncResult(BaseModel):
    run_id: UUID
    status: Literal["succeeded"]
    request: OutgoingInvoicesQuery
    completed_days: int
    documents: int
    items: int
    last_day_checks: dict[str, int]
    logout_ok: bool
