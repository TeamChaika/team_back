"""Иерархия корпорации, наблюдения репликации и сопоставление по UUID."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.schemas.iiko_catalog import IikoSnapshot


class CorporateItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    parent_id: UUID | None = Field(default=None, validation_alias="parentId")
    code: str | None = None
    name: str | None = None
    type: str = Field(min_length=1)
    taxpayer_id_number: str | None = Field(default=None, validation_alias="taxpayerIdNumber")


class CorporateHierarchyResponse(IikoSnapshot):
    connection_id: str
    revision_from: Literal[-1] = -1
    items: list[CorporateItem]
    missing_parent_ids: list[UUID]


class CorporateGroupIdentity(BaseModel):
    """Только идентификаторы группы; кассы и отделения пока остаются в RAW."""

    id: UUID
    name: str
    department_id: UUID | None = Field(default=None, validation_alias="departmentId")
    group_service_mode: str = Field(validation_alias="groupServiceMode", min_length=1)


class CorporateGroupsResponse(IikoSnapshot):
    connection_id: str
    revision_from: Literal[-1] = -1
    items: list[CorporateGroupIdentity]


class ReplicationStatus(BaseModel):
    department_id: UUID = Field(validation_alias="departmentId")
    department_name: str | None = Field(default=None, validation_alias="departmentName")
    last_receive_date: AwareDatetime | None = Field(
        default=None, validation_alias="lastReceiveDate"
    )
    last_send_date: AwareDatetime | None = Field(default=None, validation_alias="lastSendDate")
    status: str = Field(
        min_length=1, description="Исходный статус iiko, без вывода о полноте данных"
    )

    @field_validator("last_receive_date", "last_send_date", mode="before")
    @classmethod
    def require_datetime(cls, value: object) -> object:
        if value is not None and not isinstance(value, str | datetime):
            raise ValueError("Expected ISO datetime, not Unix timestamp")
        if isinstance(value, str) and "T" not in value:
            raise ValueError("Expected ISO datetime with timezone")
        return value


class ReplicationResponse(IikoSnapshot):
    connection_id: Literal["primary"] = "primary"
    items: list[ReplicationStatus]
    status_counts: dict[str, int]


class DepartmentBinding(BaseModel):
    connection_id: str
    state: Literal[
        "matched",
        "not_loaded",
        "groups_not_loaded",
        "missing_group_department",
        "not_in_rms",
        "missing_department",
        "multiple_departments",
        "not_in_chain",
        "duplicate_binding",
    ]
    department_id: UUID | None = None
    department_name: str | None = None
    rms_department_name: str | None = None
    candidate_ids: list[UUID] = Field(default_factory=list)
    rms_snapshot_id: UUID | None = None
    rms_received_at: datetime | None = None
    groups_snapshot_id: UUID | None = None
    groups_received_at: datetime | None = None


class DepartmentMappingResponse(BaseModel):
    primary_snapshot_id: UUID
    primary_received_at: datetime
    bindings: list[DepartmentBinding]
    unmapped_chain_departments: list[CorporateItem]
