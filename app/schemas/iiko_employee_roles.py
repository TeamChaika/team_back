"""Source role identifiers and exact payroll settings; protected service access only."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.iiko_catalog import IikoSnapshot
from app.schemas.sync_jobs import EmployeeSyncStatus


class IikoEmployeeRole(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    id: UUID
    code: str
    name: str
    payment_per_hour: Decimal | None = Field(default=None, validation_alias="paymentPerHour")
    steady_salary: Decimal | None = Field(default=None, validation_alias="steadySalary")
    schedule_type: str | None = Field(default=None, validation_alias="scheduleType")
    deleted: bool | None = None


class EmployeeRolesSnapshot(IikoSnapshot):
    revision_from: Literal[-1] = -1
    without_code: int = Field(ge=0)
    deleted_count: int = Field(ge=0)
    duplicate_nonempty_codes: int = Field(ge=0)


class EmployeeRolesPage(BaseModel):
    snapshot: EmployeeRolesSnapshot
    offset: int
    limit: int
    items: list[IikoEmployeeRole]


class EmployeeRoleSyncResult(EmployeeSyncStatus):
    """Committed role dictionary and employee UUID reconciliation counters."""
