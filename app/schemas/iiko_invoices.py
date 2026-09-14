"""Параметры выгрузки и поля приходных накладных; денежные значения без float."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


class IncomingInvoicesQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date = Field(description="Начальная дата включительно")
    date_to: date = Field(description="Конечная дата включительно; максимум 7 дней за запрос")
    supplier_id: list[UUID] = Field(default_factory=list, max_length=20)
    revision_from: int = Field(default=-1, ge=-1, description="Ревизия объекта строго больше этой")

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if not 0 <= (self.date_to - self.date_from).days < 7:
            raise ValueError("Период должен составлять от 1 до 7 дней включительно")
        return self


class InvoiceFields(BaseModel):
    model_config = ConfigDict(populate_by_name=True, allow_inf_nan=False)


class IncomingInvoiceItem(InvoiceFields):
    num: int
    sum: Decimal
    product_id: UUID | None = Field(default=None, validation_alias="product")
    product_article: str | None = Field(default=None, validation_alias="productArticle")
    supplier_product_id: UUID | None = Field(default=None, validation_alias="supplierProduct")
    supplier_product_article: str | None = Field(
        default=None, validation_alias="supplierProductArticle"
    )
    amount: Decimal | None = None
    actual_amount: Decimal | None = Field(default=None, validation_alias="actualAmount")
    amount_unit_id: UUID | None = Field(default=None, validation_alias="amountUnit")
    container_id: UUID | None = Field(default=None, validation_alias="containerId")
    actual_unit_weight: Decimal | None = Field(default=None, validation_alias="actualUnitWeight")
    price: Decimal | None = None
    price_unit: str | None = Field(default=None, validation_alias="priceUnit")
    price_without_vat: Decimal | None = Field(default=None, validation_alias="priceWithoutVat")
    discount_sum: Decimal | None = Field(default=None, validation_alias="discountSum")
    vat_percent: Decimal | None = Field(default=None, validation_alias="vatPercent")
    vat_sum: Decimal | None = Field(default=None, validation_alias="vatSum")
    store_id: UUID | None = Field(default=None, validation_alias="store")
    is_additional_expense: bool | None = Field(default=None, validation_alias="isAdditionalExpense")
    producer: str | None = None
    code: str | None = None
    customs_declaration_number: str | None = Field(
        default=None, validation_alias="customsDeclarationNumber"
    )


class IncomingInvoice(InvoiceFields):
    id: UUID
    document_number: str | None = Field(default=None, validation_alias="documentNumber")
    date_incoming: date | datetime | None = Field(default=None, validation_alias="dateIncoming")
    incoming_date: date | datetime | None = Field(default=None, validation_alias="incomingDate")
    incoming_document_number: str | None = Field(
        default=None, validation_alias="incomingDocumentNumber"
    )
    due_date: date | datetime | None = Field(default=None, validation_alias="dueDate")
    status: Literal["NEW", "PROCESSED", "DELETED"] | None = None
    supplier_id: UUID | None = Field(default=None, validation_alias="supplier")
    default_store_id: UUID | None = Field(default=None, validation_alias="defaultStore")
    conception_id: UUID | None = Field(default=None, validation_alias="conception")
    conception_code: str | None = Field(default=None, validation_alias="conceptionCode")
    comment: str | None = None
    invoice: str | None = None
    use_default_document_time: bool | None = Field(
        default=None, validation_alias="useDefaultDocumentTime"
    )
    employee_pass_to_account_id: UUID | None = Field(
        default=None, validation_alias="employeePassToAccount"
    )
    transport_invoice_number: str | None = Field(
        default=None, validation_alias="transportInvoiceNumber"
    )
    linked_outgoing_invoice_id: UUID | None = Field(
        default=None, validation_alias="linkedOutgoingInvoiceId"
    )
    distribution_algorithm: (
        Literal["DISTRIBUTION_BY_SUM", "DISTRIBUTION_BY_AMOUNT", "DISTRIBUTION_NOT_SPECIFIED"]
        | None
    ) = Field(default=None, validation_alias="distributionAlgorithm")
    revision: int | None = None
    items: list[IncomingInvoiceItem] = Field(default_factory=list)

    @field_validator("date_incoming", "incoming_date", "due_date", mode="before")
    @classmethod
    def preserve_source_date_kind(cls, value: object, info: ValidationInfo) -> object:
        # Наша установка выгружает отсутствующий срок оплаты как <dueDate>null</dueDate>.
        if info.field_name == "due_date" and value == "null":
            return None
        # Union date | datetime иначе может превратить полночь в дату без времени.
        if isinstance(value, str):
            if len(value) == 10:
                return date.fromisoformat(value)
            return datetime.fromisoformat(value)
        return value


class IncomingInvoicesResponse(BaseModel):
    snapshot_id: UUID
    received_at: datetime
    request: IncomingInvoicesQuery
    total: int
    items_count: int
    documents: list[IncomingInvoice]
    source_bytes: int
    sha256: str
