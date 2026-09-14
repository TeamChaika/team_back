"""Остатки товаров на складах на заданную учётную дату-время."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import IikoStoreBalancesDependency
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_store_balances import StoreBalancesQuery, StoreBalancesResponse

router = APIRouter(
    prefix="/iiko/reports",
    tags=["iiko — остатки на складах"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.get(
    "/store-balances",
    response_model=StoreBalancesResponse,
    summary="Получить остатки товаров на складах на учётную дату-время",
    description=(
        "Каждый вызов читает /v2/reports/balance/stores. "
        "timestamp — учётное время iiko: YYYY-MM-DDTHH:MM:SS, без часового пояса. "
        "department_id, store_id, product_id — необязательные списки UUID; "
        "для нескольких значений повторите параметр. Без фильтров запрашиваются все остатки. "
        "Для первой проверки рекомендуется один склад из GET /api/v1/iiko/stores. "
        "amount и sum возвращаются точными десятичными строками, включая отрицательные значения. "
        "Единицы не пересчитываются, общие итоги не рассчитываются. "
        "Подразделение в строках источника отсутствует; оно используется только как фильтр. "
        "Исходный JSON и параметры сохраняются локально. "
        "После проверки выполните POST /api/v1/iiko/logout для освобождения лицензии."
    ),
)
async def get_store_balances(
    service: IikoStoreBalancesDependency,
    query: Annotated[StoreBalancesQuery, Query()],
) -> StoreBalancesResponse:
    return await service.get(query)
