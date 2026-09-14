"""Один дневной отчёт SALES по подразделениям primary."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import IikoDailySalesDependency
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_olap_sales import DailySalesQuery, DailySalesResponse

router = APIRouter(
    prefix="/iiko/olap/sales",
    tags=["iiko — OLAP-продажи"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.get(
    "/daily",
    response_model=DailySalesResponse,
    summary="Продажи за один день по ресторанам",
    description=(
        "Читает POST /v2/reports/olap на primary. Один учётный день, семь полей, "
        "buildSummary=false. Исключены удалённые блюда и заказы (NOT_DELETED); "
        "фильтров возвратов и типов оплат нет, суммы сохраняются со знаком как в iiko. "
        "Сумма со скидкой, себестоимость, чеки и гости сгруппированы по подразделению. "
        "UUID подразделения ещё не сопоставлен с RMS. Отсутствующий ресторан не означает "
        "нулевые продажи. Null не заменяется нулём, денежные значения — точные строки. "
        "RAW и параметры сохраняются локально; итог пока не сверён с iikoOffice. "
        "После проверки выполните POST /api/v1/iiko/logout."
    ),
)
async def get_daily_sales(
    service: IikoDailySalesDependency,
    query: Annotated[DailySalesQuery, Query()],
) -> DailySalesResponse:
    return await service.get(query)
