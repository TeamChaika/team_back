"""Общая учётная дата-время для готовых отчётов iiko."""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, NaiveDatetime, field_validator


class AccountingReportQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: NaiveDatetime = Field(
        description="Учётное время iiko: YYYY-MM-DDTHH:MM:SS, без часового пояса и долей секунды",
        examples=["2026-09-09T23:59:59"],
    )

    @field_validator("timestamp", mode="before")
    @classmethod
    def require_accounting_datetime(cls, value: object) -> object:
        if isinstance(value, str):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value):
                raise ValueError("Укажите дату-время YYYY-MM-DDTHH:MM:SS без часового пояса")
        elif not isinstance(value, datetime):
            raise ValueError("Требуется учётная дата-время, не Unix timestamp")
        return value

    @field_validator("timestamp")
    @classmethod
    def require_whole_seconds(cls, value: datetime) -> datetime:
        if value.microsecond:
            raise ValueError("Доли секунды не поддерживаются")
        return value
