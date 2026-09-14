"""Загрузка справочника складов и локальный просмотр без обращения к iiko."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.dependencies import IikoStoresDependency
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_stores import IikoStoresPage, IikoStoresSnapshot

router = APIRouter(
    prefix="/iiko/stores",
    tags=["iiko — склады"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.post(
    "/load",
    response_model=IikoStoresSnapshot,
    summary="Загрузить склады из iiko",
    description=(
        "GET /corporation/stores?revisionFrom=-1 — полная выгрузка складов. "
        "Авторизуется при необходимости, проверяет и сохраняет исходный XML локально. "
        "Метод не сообщает признак удаления: состояние активности не выводится из наличия записи. "
        "После проверки выполните POST /api/v1/iiko/logout для освобождения лицензии."
    ),
)
async def load_stores(service: IikoStoresDependency) -> IikoStoresSnapshot:
    return await service.load()


@router.get(
    "",
    response_model=IikoStoresPage,
    summary="Посмотреть страницу загруженных складов",
    description=(
        "Читает локальный снимок без запросов к iiko, в том числе после перезапуска. "
        "id связывается с default_store_id накладной и store_id её строк. "
        "parent_id — ссылка на родительское подразделение, его справочник пока не загружен. "
        "Код склада может быть пустым или повторяться. "
        "snapshot_id выявляет обновление между страницами."
    ),
)
async def get_stores(
    service: IikoStoresDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    snapshot_id: UUID | None = None,
) -> IikoStoresPage:
    return await service.page(offset, limit, snapshot_id)
