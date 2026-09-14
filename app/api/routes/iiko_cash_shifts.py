"""Один метод чтения списка кассовых смен."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import IikoCashShiftsDependency
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_cash_shifts import CashShiftsQuery, CashShiftsResponse

router = APIRouter(
    prefix="/iiko/cash-shifts",
    tags=["iiko — кассовые смены"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.get(
    "",
    response_model=CashShiftsResponse,
    summary="Получить список кассовых смен",
    description=(
        "Каждый вызов читает GET /v2/cashshifts/list. "
        "open_date_from и open_date_to задают дни ОТКРЫТИЯ смен, обе даты включаются. "
        "Максимум 7 дней; рекомендуется один день. Закрытие может быть на следующий день. "
        "department_id и group_id можно повторять. status по умолчанию ANY; "
        "это фильтр iiko, а session_status — значение из ответа без переименования статусов. "
        "revision_from=-1 — обычная выгрузка, автоматического курсора нет. "
        "Денежные поля возвращаются точными десятичными строками со знаком, null сохраняется. "
        "sum_writeoff_orders — заказы за счёт заведения. Общая выручка не рассчитывается. "
        "Полный JSON и параметры сохраняются локально. "
        "После проверки выполните POST /api/v1/iiko/logout для освобождения лицензии."
    ),
)
async def get_cash_shifts(
    service: IikoCashShiftsDependency,
    query: Annotated[CashShiftsQuery, Query()],
) -> CashShiftsResponse:
    return await service.get(query)
