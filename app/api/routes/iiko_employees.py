"""Загрузка сотрудников и просмотр страниц локального справочника."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.dependencies import IikoEmployeesDependency
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_employees import IikoEmployeesPage, IikoEmployeesSnapshot

router = APIRouter(
    prefix="/iiko/employees",
    tags=["iiko — сотрудники"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.post(
    "/load",
    response_model=IikoEmployeesSnapshot,
    summary="Загрузить активных сотрудников из iiko",
    description=(
        "GET /employees?includeDeleted=false&revisionFrom=-1 — полный список неудалённых записей, "
        "включая системные учётные записи. Это не список работающих сегодня сотрудников. "
        "Авторизуется при необходимости, проверяет и сохраняет исходный XML локально. "
        "После проверки выполните POST /api/v1/iiko/logout для освобождения лицензии."
    ),
)
async def load_employees(service: IikoEmployeesDependency) -> IikoEmployeesSnapshot:
    return await service.load()


@router.get(
    "",
    response_model=IikoEmployeesPage,
    summary="Посмотреть страницу загруженных сотрудников",
    description=(
        "Читает локальный снимок без обращения к iiko, в том числе после перезапуска. "
        "UUID — идентификатор записи; табельный номер может быть пустым или повторяться. "
        "main_role_id и role_ids связываются с должностями; коды сохранены отдельно. "
        "Отсутствующие списки и null не превращаются в пустой набор подразделений. "
        "Ответ содержит имена, связи и признаки; остальные сведения остаются в локальном XML. "
        "snapshot_id выявляет обновление между страницами."
    ),
)
async def get_employees(
    service: IikoEmployeesDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    snapshot_id: UUID | None = None,
) -> IikoEmployeesPage:
    return await service.page(offset, limit, snapshot_id)
