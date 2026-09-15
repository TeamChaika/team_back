"""Идентификаторы сотрудников и связи с должностями и подразделениями."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.iiko_catalog import IikoCatalogSnapshot


class IikoEmployee(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    code: str = Field(description="Табельный номер; может быть пустым или повторяться")
    name: str
    first_name: str | None = Field(default=None, validation_alias="firstName")
    middle_name: str | None = Field(default=None, validation_alias="middleName")
    last_name: str | None = Field(default=None, validation_alias="lastName")
    phone: str | None = None
    cell_phone: str | None = Field(default=None, validation_alias="cellPhone")
    email: str | None = None
    main_role_id: UUID | None = Field(default=None, validation_alias="mainRoleId")
    role_ids: list[UUID | None] | None = Field(default=None, validation_alias="rolesIds")
    main_role_code: str | None = Field(default=None, validation_alias="mainRoleCode")
    role_codes: list[str | None] | None = Field(default=None, validation_alias="roleCodes")
    preferred_department_code: str | None = Field(
        default=None, validation_alias="preferredDepartmentCode"
    )
    department_codes: list[str | None] | None = Field(
        default=None, validation_alias="departmentCodes"
    )
    department_codes_state: str | None = Field(
        default=None, validation_alias="departmentCodesState"
    )
    responsibility_department_codes: list[str | None] | None = Field(
        default=None, validation_alias="responsibilityDepartmentCodes"
    )
    responsibility_department_codes_state: str | None = Field(
        default=None, validation_alias="responsibilityDepartmentCodesState"
    )
    deleted: bool | None = None
    employee: bool | None = None
    supplier: bool | None = None
    client: bool | None = None


class IikoEmployeesSnapshot(IikoCatalogSnapshot):
    revision_from: Literal[-1] = -1
    without_code: int = Field(ge=0)
    employee_flag_count: int = Field(ge=0)
    unique_role_ids: int = Field(ge=0)
    unique_role_codes: int = Field(ge=0)
    with_main_role_id: int = Field(ge=0)
    department_lists_omitted: int = Field(ge=0)


class IikoEmployeesPage(BaseModel):
    snapshot: IikoEmployeesSnapshot
    offset: int
    limit: int
    items: list[IikoEmployee]
