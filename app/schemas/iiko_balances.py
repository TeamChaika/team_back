"""Денежные балансы iiko на учётную дату-время и точные фильтры запроса."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.iiko_reports import AccountingReportQuery


class CounteragentBalancesQuery(AccountingReportQuery):
    account_id: list[UUID] = Field(default_factory=list, max_length=20)
    counteragent_id: list[UUID] = Field(default_factory=list, max_length=20)
    department_id: list[UUID] = Field(default_factory=list, max_length=20)


class CounteragentBalance(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    account_id: UUID = Field(validation_alias="account")
    counteragent_id: UUID | None = Field(validation_alias="counteragent")
    department_id: UUID | None = Field(validation_alias="department")
    sum: Annotated[Decimal, Field(examples=["-1234.560000000"])]


class CounteragentBalancesResponse(BaseModel):
    snapshot_id: UUID
    received_at: datetime
    request: CounteragentBalancesQuery
    total: int
    items: list[CounteragentBalance]
    source_bytes: int
    sha256: str
