"""Ручная загрузка и локальный просмотр групп номенклатуры."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.dependencies import IikoGroupsDependency
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_groups import IikoGroupsPage, IikoGroupsSnapshot

router = APIRouter(
    prefix="/iiko/groups",
    tags=["iiko — группы номенклатуры"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.post(
    "/load",
    response_model=IikoGroupsSnapshot,
    summary="Загрузить группы номенклатуры из iiko",
    description=(
        "GET /v2/entities/products/group/list с includeDeleted=false. "
        "Авторизуется при необходимости, сохраняет ответ локально. "
        "В сводке указаны корневые группы, отсутствующие родители и циклы. "
        "Для освобождения лицензии используйте POST /api/v1/iiko/logout."
    ),
)
async def load_groups(service: IikoGroupsDependency) -> IikoGroupsSnapshot:
    return await service.load()


@router.get(
    "",
    response_model=IikoGroupsPage,
    summary="Посмотреть страницу загруженных групп",
    description=(
        "Читает локальный снимок без запросов к iiko. "
        "parent_id связывает группы между собой; parent_id продукта ссылается на id группы. "
        "parent_id=null означает корень. Категория — отдельная связь category_id. "
        "snapshot_id позволяет заметить обновление между страницами."
    ),
)
async def get_groups(
    service: IikoGroupsDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    snapshot_id: UUID | None = None,
) -> IikoGroupsPage:
    return await service.page(offset, limit, snapshot_id)
