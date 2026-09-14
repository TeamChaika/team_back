"""A saved discount row is the scope boundary for order drilldown."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DiscountDetailsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: UUID
    ordinal: int = Field(ge=0, le=20000)
    order_id: UUID | None = None
    offset: int = Field(default=0, ge=0, le=100000)
