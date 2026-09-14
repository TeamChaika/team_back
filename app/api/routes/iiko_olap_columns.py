"""Первый шаг OLAP: доступные поля продаж на основном подключении."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import IikoOlapColumnsDependency
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_olap_columns import OlapColumnsQuery, OlapColumnsResponse

router = APIRouter(
    prefix="/iiko/olap",
    tags=["iiko — поля OLAP"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.get(
    "/columns",
    response_model=OlapColumnsResponse,
    summary="Получить доступные поля OLAP-продаж",
    description=(
        "Читает GET /v2/reports/olap/columns?reportType=SALES с подключения primary. "
        "Каждый вызов обращается к iiko и сохраняет исходный JSON. "
        "Ключи columns — точные имена полей для запроса; name — подпись в iikoOffice. "
        "Сохраняются типы, категории tags и отдельные признаки aggregation_allowed, "
        "grouping_allowed, filtering_allowed. Это описание доступных полей; "
        "сумм и строк продаж в ответе нет. На этом шаге поддержан только SALES. "
        "После проверки выполните POST /api/v1/iiko/logout для освобождения лицензии."
    ),
)
async def get_olap_columns(
    service: IikoOlapColumnsDependency,
    query: Annotated[OlapColumnsQuery, Query()],
) -> OlapColumnsResponse:
    return await service.get(query)
