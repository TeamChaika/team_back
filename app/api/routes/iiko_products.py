"""Загрузка номенклатуры и просмотр сохранённого результата."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.dependencies import IikoProductsDependency
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_products import IikoProductsPage, IikoProductsSnapshot

router = APIRouter(
    prefix="/iiko/products",
    tags=["iiko — номенклатура"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.post(
    "/load",
    response_model=IikoProductsSnapshot,
    summary="Загрузить номенклатуру из iiko",
    description=(
        "Выполняет GET /v2/entities/products/list с includeDeleted=false. "
        "При необходимости авторизуется; после HTTP 401 повторяет вход и чтение один раз. "
        "Сохраняет полный ответ локально и возвращает сводку. Повторный вызов обновляет снимок. "
        "Для освобождения лицензии после загрузки используйте POST /api/v1/iiko/logout."
    ),
)
async def load_products(service: IikoProductsDependency) -> IikoProductsSnapshot:
    return await service.load()


@router.get(
    "",
    response_model=IikoProductsPage,
    summary="Посмотреть страницу загруженной номенклатуры",
    description=(
        "Читает локальный снимок без запросов к iiko. "
        "В items показаны основные поля; полный ответ хранится в локальном JSON. "
        "Денежные значения и веса возвращаются десятичными строками. "
        "Передайте snapshot_id из первой страницы, чтобы заметить обновление между страницами."
    ),
)
async def get_products(
    service: IikoProductsDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    snapshot_id: UUID | None = None,
) -> IikoProductsPage:
    return await service.page(offset, limit, snapshot_id)
