"""Исходная техкарта одного элемента номенклатуры на учётный день."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.dependencies import IikoAssemblyDependency
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_assembly import AllAssemblyResponse, AssembledChartResponse

router = APIRouter(
    prefix="/iiko/assembly-charts",
    tags=["iiko — техкарты"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.get(
    "/all",
    response_model=AllAssemblyResponse,
    summary="Выгрузить все исходные техкарты на один день",
    description=(
        "getAll за [date, date+1), includeDeletedProducts=true, "
        "includePreparedCharts=false. Полные составы сохраняются в RAW; "
        "ответ содержит счётчики и ревизию. После чтения выполните logout."
    ),
)
async def get_all_assembly(
    service: IikoAssemblyDependency, business_date: Annotated[date, Query(alias="date")]
) -> AllAssemblyResponse:
    return await service.get_all(business_date)


@router.get(
    "/assembled",
    response_model=AssembledChartResponse,
    summary="Получить исходную техкарту блюда на дату",
    description=(
        "Каждый вызов обращается к iiko через getAssembled. Только первый уровень состава. "
        "product_id возьмите из номенклатуры, date — учётный день. "
        "Без department_id сохраняются строки для разных подразделений: "
        "учитывайте store_specification. "
        "Количество указано в основных единицах ингредиента, Decimal возвращается строкой. "
        "chart=null означает отсутствие карты на эту дату. Исходный ответ сохраняется локально. "
        "После проверки выполните POST /api/v1/iiko/logout для освобождения лицензии."
    ),
)
async def get_assembled(
    service: IikoAssemblyDependency,
    product_id: UUID,
    business_date: Annotated[date, Query(alias="date")],
    department_id: UUID | None = None,
) -> AssembledChartResponse:
    return await service.get_assembled(product_id, business_date, department_id)
