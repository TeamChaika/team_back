"""Один метод чтения актов списания за ограниченный период."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import IikoWriteoffsDependency
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_writeoffs import WriteoffsQuery, WriteoffsResponse

router = APIRouter(
    prefix="/iiko/writeoffs",
    tags=["iiko — акты списания"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.get(
    "",
    response_model=WriteoffsResponse,
    summary="Выгрузить акты списания за период",
    description=(
        "Каждый вызов читает GET /v2/documents/writeoff. "
        "date_from и date_to задают учётные дни; максимум 7 дней, рекомендуется один день. "
        "Без status возвращаются все статусы; доступны NEW, PROCESSED, DELETED. "
        "revision_from=-1 — обычная выгрузка. Возвращаемая revision относится ко всей выгрузке, "
        "не к отдельному документу; автоматической синхронизации пока нет. "
        "amount, amount_factor и cost сохраняются точными десятичными строками. "
        "cost — себестоимость всего количества строки, null сохраняется. "
        "measure_unit_id — единица на момент списания, без подмены текущей единицей товара. "
        "Полный JSON и параметры сохраняются локально. "
        "После проверки выполните POST /api/v1/iiko/logout для освобождения лицензии."
    ),
)
async def get_writeoffs(
    service: IikoWriteoffsDependency,
    query: Annotated[WriteoffsQuery, Query()],
) -> WriteoffsResponse:
    return await service.get(query)
