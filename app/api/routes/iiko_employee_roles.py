"""Protected role catalogue and one-shot database synchronization."""

from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Query, Response

from app.api.dependencies import IikoEmployeeRolesDependency, SettingsDependency
from app.api.routes.sync_jobs import authorize
from app.schemas.iiko_employee_roles import (
    EmployeeRolesPage,
    EmployeeRolesSnapshot,
    EmployeeRoleSyncResult,
)
from app.services.sync_jobs import SyncJobError
from app.sync_employee_roles import synchronize_employee_roles
from app.sync_references import SyncError

router = APIRouter(tags=["iiko — должности сотрудников"], dependencies=[Depends(authorize)])


@router.post(
    "/iiko/employee-roles/load",
    response_model=EmployeeRolesSnapshot,
    summary="Загрузить справочник должностей из iiko",
)
async def load(service: IikoEmployeeRolesDependency, response: Response):
    """GET /employees/roles?revisionFrom=-1. После проверки выполните logout."""
    response.headers["Cache-Control"] = "no-store"
    return await service.load()


@router.get(
    "/iiko/employee-roles",
    response_model=EmployeeRolesPage,
    summary="Страница загруженных должностей без запроса к iiko",
)
async def page(
    service: IikoEmployeeRolesDependency,
    response: Response,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    snapshot_id: UUID | None = None,
):
    response.headers["Cache-Control"] = "no-store"
    return await service.page(offset, limit, snapshot_id)


@router.post(
    "/sync/employee-roles",
    response_model=EmployeeRoleSyncResult,
    summary="Синхронизировать должности с Supabase и проверить связи сотрудников",
)
def synchronize(settings: SettingsDependency, response: Response):
    """Полный справочник Chain, общий замок, атомарная публикация и logout. Даты не нужны."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return synchronize_employee_roles(settings)
    except SyncError as error:
        if str(error) == "sync_already_running":
            raise SyncJobError(
                "sync_already_running", "Синхронизация уже выполняется.", 409
            ) from None
        raise SyncJobError(
            "employee_roles_sync_failed",
            "Должности не синхронизированы. Проверьте sync_runs.",
        ) from None
    except (psycopg.Error, OSError, ValueError, KeyError, TypeError):
        raise SyncJobError(
            "employee_roles_sync_unavailable",
            "Синхронизация должностей недоступна.",
        ) from None
