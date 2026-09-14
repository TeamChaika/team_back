"""Public refresh-job contract; no process IDs, paths or credentials."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, NaiveDatetime, field_validator, model_validator

from app.schemas.iiko_cash_shifts import CashShiftsQuery


class CashShiftSyncQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    open_date_from: date = Field(description="Первый день открытия смен")
    open_date_to: date = Field(description="Последний день включительно; не более 7 дней")

    @field_validator("open_date_from", "open_date_to", mode="before")
    @classmethod
    def require_date(cls, value):
        return CashShiftsQuery.require_date(value)

    @model_validator(mode="after")
    def validate_window(self):
        CashShiftsQuery(open_date_from=self.open_date_from, open_date_to=self.open_date_to)
        return self


class CashShiftDayResult(BaseModel):
    day: date
    snapshot_id: UUID
    shifts: int
    matched: int
    unmapped: int
    open: int
    closed: int
    accepted: int


class CashShiftSyncStatus(BaseModel):
    run_id: UUID
    status: Literal["succeeded"]
    source_id: str
    finished_at: datetime
    days: list[CashShiftDayResult]
    counts: dict[str, int | bool | str]


class RefreshStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID = Field(
        default_factory=uuid4,
        description="Сохраняйте UUID при повторе запроса: один UUID создаёт одно задание.",
    )


class ResourceProgress(BaseModel):
    resource: str
    title: str
    status: str
    run_id: UUID | None = None
    completed_days: int
    total_days: int
    percent: float
    documents: int
    items: int
    completed_through: date | None = None
    next_date: date | None = None
    updated_at: datetime | None = None
    error_code: str | None = None


class RefreshStatus(BaseModel):
    job_id: UUID
    status: Literal["accepted", "running", "succeeded", "failed", "interrupted"]
    date_from: date
    date_to: date
    days: int = 60
    timezone: str = "Europe/Simferopol"
    created_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None
    resources: list[ResourceProgress]


class EmployeeSyncStatus(BaseModel):
    run_id: UUID
    status: Literal["succeeded"]
    source_id: str
    snapshot_id: UUID
    observed_at: datetime
    finished_at: datetime
    counts: dict[str, int | bool]


class AccountSyncStatus(EmployeeSyncStatus):
    """Committed account catalogue and reconciliation counters."""


class StoreBalanceSyncStatus(BaseModel):
    run_id: UUID
    status: Literal["succeeded"]
    source_id: str
    snapshot_id: UUID
    timestamp: NaiveDatetime
    observed_at: datetime
    finished_at: datetime
    counts: dict[str, int | bool]


class DictionarySyncStatus(BaseModel):
    run_id: UUID
    status: Literal["succeeded"]
    source_id: str
    finished_at: datetime
    snapshots: dict[str, UUID]
    counts: dict[str, int | bool]
    links: dict[str, int]


class CounteragentBalanceSyncStatus(BaseModel):
    run_id: UUID
    status: Literal["succeeded"]
    source_id: str
    snapshot_id: UUID
    timestamp: NaiveDatetime
    observed_at: datetime
    finished_at: datetime
    counts: dict[str, int | bool]
