"""Чтение XML сотрудников и локальный снимок со связями по UUID и кодам."""

from pathlib import Path
from uuid import UUID
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

from app.core.config import BACKEND_DIR, Settings
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_employees import IikoEmployee, IikoEmployeesPage, IikoEmployeesSnapshot
from app.services.iiko_auth import IikoAuthService
from app.services.local_catalog import LocalCatalog

XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
LIST_FIELDS = {"rolesIds", "roleCodes", "departmentCodes", "responsibilityDepartmentCodes"}
BOOLEAN_FIELDS = {"deleted", "employee", "supplier", "client"}


def _value(field: Element) -> str | bool | None:
    if len(field):
        raise ValueError("Nested scalar field")
    nil = field.get(XSI_NIL)
    if nil is not None and nil not in {"true", "false", "1", "0"}:
        raise ValueError("Invalid xsi:nil")
    if nil in {"true", "1"}:
        if field.text and field.text.strip():
            raise ValueError("Nil field has content")
        return None
    value = field.text if field.text is not None else ""
    if field.tag in BOOLEAN_FIELDS:
        if value not in {"true", "false", "1", "0"}:
            raise ValueError("Invalid XML boolean")
        return value in {"true", "1"}
    if field.tag == "mainRoleId" and not value:
        return None
    return value


def read_employees(path: Path, max_bytes: int) -> list[IikoEmployee]:
    if path.stat().st_size > max_bytes:
        raise IikoError("iiko_employees_too_large", "Файл сотрудников превышает лимит размера.")
    names = {field.validation_alias or name for name, field in IikoEmployee.model_fields.items()}
    try:
        root = ElementTree.parse(
            path, forbid_dtd=True, forbid_entities=True, forbid_external=True
        ).getroot()
        if root.tag != "employees":
            raise ValueError("Unexpected XML root")
        employees: list[IikoEmployee] = []
        ids: set[UUID] = set()
        for element in root:
            if element.tag != "employee":
                raise ValueError("Unexpected employee wrapper")
            values = {}
            for field in element:
                if field.tag not in names:
                    continue  # Остальные поля сохраняются только в исходном локальном XML.
                value = _value(field)
                if field.tag in LIST_FIELDS:
                    values.setdefault(field.tag, []).append(value)
                elif field.tag in values:
                    raise ValueError("Repeated scalar field")
                else:
                    values[field.tag] = value
            employee = IikoEmployee.model_validate(values)
            if employee.id in ids or employee.deleted is True:
                raise ValueError("Duplicate UUID or deleted record in active-only request")
            ids.add(employee.id)
            employees.append(employee)
        return employees
    except (ParseError, DefusedXmlException, ValidationError, ValueError, OverflowError):
        raise IikoError(
            "iiko_employees_invalid_response",
            "Ответ сотрудников повреждён или не соответствует ожидаемой структуре XML.",
        ) from None


def summarize_employees(employees: list[IikoEmployee]) -> dict:
    role_ids = {role for e in employees for role in (e.role_ids or []) if role is not None}
    role_ids.update(e.main_role_id for e in employees if e.main_role_id is not None)
    role_codes = {role for e in employees for role in (e.role_codes or []) if role}
    role_codes.update(e.main_role_code for e in employees if e.main_role_code)
    return {
        "without_code": sum(e.code == "" for e in employees),
        "employee_flag_count": sum(e.employee is True for e in employees),
        "unique_role_ids": len(role_ids),
        "unique_role_codes": len(role_codes),
        "with_main_role_id": sum(e.main_role_id is not None for e in employees),
        "department_lists_omitted": sum(e.department_codes is None for e in employees),
    }


class IikoEmployeesService:
    def __init__(
        self, settings: Settings, auth: IikoAuthService, directory: Path | None = None
    ) -> None:
        self._catalog = LocalCatalog(
            settings,
            name="employees",
            directory=directory if directory is not None else BACKEND_DIR / ".local/employees",
            max_bytes=settings.iiko_employees_max_response_bytes,
            download=auth.download_employees,
            read=read_employees,
            snapshot_model=IikoEmployeesSnapshot,
            summarize=summarize_employees,
            raw_format="xml",
        )

    async def load(self) -> IikoEmployeesSnapshot:
        return await self._catalog.load()

    async def page(
        self, offset: int, limit: int, snapshot_id: UUID | None = None
    ) -> IikoEmployeesPage:
        snapshot, items = await self._catalog.page(offset, limit, snapshot_id)
        return IikoEmployeesPage(snapshot=snapshot, offset=offset, limit=limit, items=items)
