"""Один учётный день продаж по подразделениям; показатели берутся из iiko."""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from app.schemas.iiko_catalog import IikoSnapshot


class DailySalesQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_date: date = Field(description="Один учётный день iiko, YYYY-MM-DD")

    @field_validator("business_date", mode="before")
    @classmethod
    def require_date(cls, value: object) -> object:
        if isinstance(value, str):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("Укажите дату YYYY-MM-DD")
        elif not isinstance(value, date) or isinstance(value, datetime):
            raise ValueError("Требуется дата, не время или Unix timestamp")
        return value

    @field_validator("business_date")
    @classmethod
    def require_next_day(cls, value: date) -> date:
        if value == date.max:
            raise ValueError("Дата должна допускать границу следующего дня")
        return value

    def iiko_body(self) -> dict:
        return {
            "reportType": "SALES",
            "buildSummary": False,
            "groupByRowFields": ["OpenDate.Typed", "Department.Id", "Department"],
            "groupByColFields": [],
            "aggregateFields": [
                "DishDiscountSumInt",
                "ProductCostBase.ProductCost",
                "UniqOrderId",
                "GuestNum",
            ],
            "filters": {
                "OpenDate.Typed": {
                    "filterType": "DateRange",
                    "periodType": "CUSTOM",
                    "from": f"{self.business_date.isoformat()}T00:00:00.000",
                    "to": f"{(self.business_date + timedelta(days=1)).isoformat()}T00:00:00.000",
                    "includeLow": True,
                    "includeHigh": False,
                },
                "DeletedWithWriteoff": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
                "OrderDeleted": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
            },
        }


class DailySalesRow(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    business_date: date = Field(validation_alias="OpenDate.Typed")
    department_id: UUID = Field(validation_alias="Department.Id")
    department_name: str | None = Field(validation_alias="Department")
    revenue: Decimal | None = Field(
        validation_alias="DishDiscountSumInt", description="Сумма со скидкой, по расчёту iiko"
    )
    cost: Decimal | None = Field(validation_alias="ProductCostBase.ProductCost")
    checks: StrictInt | None = Field(validation_alias="UniqOrderId")
    guests: Decimal | None = Field(validation_alias="GuestNum")

    @field_validator("business_date", mode="before")
    @classmethod
    def require_accounting_day(cls, value: object) -> object:
        return DailySalesQuery.require_date(value)


class DailySalesResponse(IikoSnapshot):
    connection_id: Literal["primary"] = "primary"
    request: DailySalesQuery
    items: list[DailySalesRow]
