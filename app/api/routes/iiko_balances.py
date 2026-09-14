"""Только отчёт балансов по счетам, контрагентам и подразделениям."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import IikoBalancesDependency
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_balances import CounteragentBalancesQuery, CounteragentBalancesResponse

router = APIRouter(
    prefix="/iiko/reports",
    tags=["iiko — балансы"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.get(
    "/counteragent-balances",
    response_model=CounteragentBalancesResponse,
    summary="Получить денежные балансы на учётную дату-время",
    description=(
        "Каждый вызов читает /v2/reports/balance/counteragents. "
        "timestamp задаётся в учётном времени iiko: YYYY-MM-DDTHH:MM:SS, без часового пояса. "
        "account_id, counteragent_id и department_id — необязательные списки UUID; "
        "для нескольких значений повторите параметр. Без фильтров запрашиваются все балансы. "
        "Для первой проверки рекомендуется один контрагент. "
        "Суммы возвращаются точными десятичными строками со знаком, без расчёта общего итога. "
        "null в контрагенте или подразделении сохраняется. Смысл знака зависит от счёта. "
        "Исходный JSON и параметры сохраняются локально. "
        "После проверки выполните POST /api/v1/iiko/logout для освобождения лицензии."
    ),
)
async def get_counteragent_balances(
    service: IikoBalancesDependency,
    query: Annotated[CounteragentBalancesQuery, Query()],
) -> CounteragentBalancesResponse:
    return await service.get(query)
