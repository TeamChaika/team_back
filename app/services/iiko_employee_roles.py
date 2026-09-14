"""Validated employee-role XML and a source-bound local catalogue."""

from collections import Counter
from pathlib import Path
from uuid import UUID
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

from app.core.config import BACKEND_DIR, Settings
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_employee_roles import (
    EmployeeRolesPage,
    EmployeeRolesSnapshot,
    IikoEmployeeRole,
)
from app.services.iiko_auth import IikoAuthService
from app.services.local_catalog import LocalCatalog

XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


def read_employee_roles(path: Path, max_bytes: int) -> list[IikoEmployeeRole]:
    if path.stat().st_size > max_bytes:
        raise IikoError("iiko_employee_roles_too_large", "Справочник должностей превышает лимит.")
    names = {f.validation_alias or n for n, f in IikoEmployeeRole.model_fields.items()}
    try:
        root = ElementTree.parse(
            path,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        ).getroot()
        if root.tag != "employeeRoles":
            raise ValueError("invalid_root")
        rows, ids = [], set()
        for element in root:
            if element.tag != "role":
                raise ValueError("invalid_wrapper")
            values = {}
            for field in element:
                if field.tag not in names:
                    continue  # Unknown fields remain in the immutable source XML.
                if len(field) or field.tag in values:
                    raise ValueError("invalid_scalar")
                nil = field.get(XSI_NIL, "false")
                if nil not in {"true", "false", "1", "0"}:
                    raise ValueError("invalid_nil")
                value = field.text if field.text is not None else ""
                if nil in {"true", "1"}:
                    if value.strip():
                        raise ValueError("nil_with_content")
                    value = None
                elif field.tag == "deleted":
                    if value not in {"true", "false", "1", "0"}:
                        raise ValueError("invalid_boolean")
                    value = value in {"true", "1"}
                values[field.tag] = value
            role = IikoEmployeeRole.model_validate(values)
            if role.id in ids:
                raise ValueError("duplicate_id")
            ids.add(role.id)
            rows.append(role)
        return rows
    except (ParseError, DefusedXmlException, ValidationError, ValueError, OverflowError):
        raise IikoError(
            "iiko_employee_roles_invalid_response",
            "Ответ должностей повреждён или имеет неверный XML.",
        ) from None


def summarize_employee_roles(rows: list[IikoEmployeeRole]) -> dict:
    codes = Counter(row.code for row in rows if row.code)
    return {
        "without_code": sum(row.code == "" for row in rows),
        "deleted_count": sum(row.deleted is True for row in rows),
        "duplicate_nonempty_codes": sum(n - 1 for n in codes.values()),
    }


class IikoEmployeeRolesService:
    def __init__(self, settings: Settings, auth: IikoAuthService, directory: Path | None = None):
        self._catalog = LocalCatalog(
            settings,
            name="employee_roles",
            directory=directory if directory is not None else BACKEND_DIR / ".local/employee-roles",
            max_bytes=settings.iiko_employees_max_response_bytes,
            download=auth.download_employee_roles,
            read=read_employee_roles,
            snapshot_model=EmployeeRolesSnapshot,
            summarize=summarize_employee_roles,
            raw_format="xml",
        )

    async def load(self) -> EmployeeRolesSnapshot:
        return await self._catalog.load()

    async def page(self, offset: int, limit: int, snapshot_id: UUID | None = None):
        snapshot, items = await self._catalog.page(offset, limit, snapshot_id)
        return EmployeeRolesPage(snapshot=snapshot, offset=offset, limit=limit, items=items)
