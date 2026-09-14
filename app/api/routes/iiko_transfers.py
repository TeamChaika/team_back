"""Один метод чтения внутренних перемещений за ограниченный период."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_transfers import TransfersQuery, TransfersResponse
from app.services.iiko_transfers import IikoTransfersService


def get_service(request: Request) -> IikoTransfersService:
    return request.app.state.iiko_transfers


IikoTransfersDependency = Annotated[IikoTransfersService, Depends(get_service)]

router = APIRouter(
    prefix="/iiko/transfers",
    tags=["iiko — внутренние перемещения"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.get(
    "",
    response_model=TransfersResponse,
    summary="Выгрузить внутренние перемещения за период",
    description=(
        "Каждый вызов читает GET /v2/documents/internalTransfer. "
        "date_from и date_to задают учётные дни; максимум 7 дней, рекомендуется один день. "
        "Без status возвращаются все статусы; доступны NEW, PROCESSED, DELETED. "
        "revision_from=-1 — обычная выгрузка. Возвращаемая revision относится ко всей выгрузке, "
        "не к отдельному документу. Для сохранения в Supabase используйте "
        "POST /api/v1/sync/transfers. "
        "amount, amount_factor и cost сохраняются точными десятичными строками. "
        "cost — себестоимость всего количества строки, null сохраняется. "
        "measure_unit_id — единица на момент перемещения, без подмены текущей единицей товара. "
        "Полный JSON и параметры сохраняются локально. "
        "После проверки выполните POST /api/v1/iiko/logout для освобождения лицензии."
    ),
)
async def get_transfers(
    service: IikoTransfersDependency,
    query: Annotated[TransfersQuery, Query()],
) -> TransfersResponse:
    return await service.get(query)
