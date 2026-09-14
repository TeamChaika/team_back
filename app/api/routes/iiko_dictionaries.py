"""Load and page allowlisted iiko reference dictionaries."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.integrations.iiko.dictionaries import DictionaryKind
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_dictionaries import DictionaryPage, DictionarySnapshot
from app.services.iiko_dictionaries import IikoDictionariesService


def get_service(request: Request) -> IikoDictionariesService:
    return request.app.state.iiko_dictionaries


Service = Annotated[IikoDictionariesService, Depends(get_service)]
router = APIRouter(
    prefix="/iiko/dictionaries",
    tags=["iiko — справочники"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.post(
    "/{kind}/load",
    response_model=DictionarySnapshot,
    summary="Загрузить справочник из iiko",
    description=(
        "counteragents — список поставщиков /suppliers; measure-units — MeasureUnit; "
        "categories — пользовательские категории; accounts — счета с иерархией и типом. "
        "Полная выгрузка без курсора. Для JSON-справочников includeDeleted=true, "
        "для поставщиков этот параметр не передаётся. "
        "Сохраняет RAW локально. После проверки выполните logout. Для записи в БД используйте "
        "POST /api/v1/sync/dictionaries, для счетов — POST /api/v1/sync/accounts."
    ),
)
async def load_dictionary(kind: DictionaryKind, service: Service) -> DictionarySnapshot:
    return await service.load(kind)


@router.get("/{kind}", response_model=DictionaryPage, summary="Посмотреть загруженный справочник")
async def get_dictionary(
    kind: DictionaryKind,
    service: Service,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    snapshot_id: UUID | None = None,
) -> DictionaryPage:
    return await service.page(kind, offset, limit, snapshot_id)
