"""Период открытия кассовых смен и поля ответа iikoServer."""

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    NaiveDatetime,
    computed_field,
    field_validator,
    model_validator,
)

type CashShiftFilter = Literal["ANY", "OPEN", "CLOSED", "ACCEPTED", "UNACCEPTED", "HASWARNINGS"]


class CashShiftsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open_date_from: date = Field(description="Первый день открытия смен", examples=["2026-09-09"])
    open_date_to: date = Field(
        description="Последний день открытия смен, включительно; максимум 7 дней",
        examples=["2026-09-09"],
    )
    department_id: list[UUID] = Field(default_factory=list, max_length=100)
    group_id: list[UUID] = Field(default_factory=list, max_length=100)
    status: CashShiftFilter = "ANY"
    revision_from: int = Field(default=-1, ge=-1)

    @field_validator("open_date_from", "open_date_to", mode="before")
    @classmethod
    def require_date(cls, value: object) -> object:
        if isinstance(value, str):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("Укажите день открытия YYYY-MM-DD")
        elif type(value) is not date:
            raise ValueError("Требуется дата, не Unix timestamp или дата-время")
        return value

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if not 0 <= (self.open_date_to - self.open_date_from).days < 7:
            raise ValueError("Период должен составлять от 1 до 7 дней включительно")
        return self


class CashShift(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    id: UUID
    session_number: int = Field(validation_alias="sessionNumber", strict=True)
    fiscal_number: int | None = Field(validation_alias="fiscalNumber", strict=True)
    cash_reg_number: int = Field(validation_alias="cashRegNumber", strict=True)
    cash_reg_serial: str | None = Field(validation_alias="cashRegSerial")
    open_date: NaiveDatetime = Field(validation_alias="openDate")
    close_date: NaiveDatetime | None = Field(validation_alias="closeDate")
    accept_date: NaiveDatetime | None = Field(validation_alias="acceptDate")
    manager_id: UUID | None = Field(validation_alias="managerId")
    responsible_user_id: UUID | None = Field(
        validation_alias=AliasChoices("responsibleUserId", "responsibleUser")
    )
    session_start_cash: Decimal | None = Field(validation_alias="sessionStartCash", examples=["0"])
    pay_orders: Decimal | None = Field(validation_alias="payOrders", examples=["123.45"])
    sum_writeoff_orders: Decimal | None = Field(
        validation_alias="sumWriteoffOrders",
        description="Заказы за счёт заведения; это не складские акты списания",
        examples=["0"],
    )
    sales_cash: Decimal | None = Field(validation_alias="salesCash", examples=["123.45"])
    sales_credit: Decimal | None = Field(validation_alias="salesCredit", examples=["0"])
    sales_card: Decimal | None = Field(validation_alias="salesCard", examples=["0"])
    pay_in: Decimal | None = Field(validation_alias="payIn", examples=["0"])
    pay_out: Decimal | None = Field(validation_alias="payOut", examples=["0"])
    pay_income: Decimal | None = Field(validation_alias="payIncome", examples=["-123.45"])
    cash_remain: Decimal | None = Field(validation_alias="cashRemain", examples=["0"])
    cash_diff: Decimal | None = Field(validation_alias="cashDiff", examples=["-123.45"])
    session_status: str = Field(validation_alias="sessionStatus", min_length=1)
    conception_id: UUID | None = Field(validation_alias=AliasChoices("conceptionId", "conception"))
    point_of_sale_id: UUID | None = Field(
        validation_alias=AliasChoices("pointOfSaleId", "pointOfSale")
    )

    @model_validator(mode="before")
    @classmethod
    def reject_conflicting_references(cls, value: object) -> object:
        if isinstance(value, dict):
            for current, documented in [
                ("responsibleUserId", "responsibleUser"),
                ("conceptionId", "conception"),
                ("pointOfSaleId", "pointOfSale"),
            ]:
                if current in value and documented in value and value[current] != value[documented]:
                    raise ValueError("Conflicting reference aliases")
        return value

    @field_validator("open_date", "close_date", "accept_date", mode="before")
    @classmethod
    def require_source_datetime(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, str):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?", value):
                raise ValueError("Ожидалась дата-время iiko без часового пояса")
        elif not isinstance(value, datetime):
            raise ValueError("Ожидалась дата-время iiko")
        return value


class CashShiftsResponse(BaseModel):
    snapshot_id: UUID
    received_at: datetime
    request: CashShiftsQuery
    total: int
    items: list[CashShift]
    source_bytes: int
    sha256: str

    @computed_field
    @property
    def opening_dates_outside_request(self) -> int:
        """iiko can return another calendar date; retain source time and request separately."""
        return sum(
            not self.request.open_date_from <= shift.open_date.date() <= self.request.open_date_to
            for shift in self.items
        )
