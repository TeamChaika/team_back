"""Один метод iiko: выгрузка приходных накладных за ограниченный период."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import IikoInvoicesDependency
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_invoices import IncomingInvoicesQuery, IncomingInvoicesResponse

router = APIRouter(
    prefix="/iiko/incoming-invoices",
    tags=["iiko — приходные накладные"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.get(
    "",
    response_model=IncomingInvoicesResponse,
    response_model_by_alias=False,
    summary="Выгрузить приходные накладные за период",
    description=(
        "Каждый вызов читает iiko через documents/export/incomingInvoice. "
        "Обе даты включаются в период; на этом шаге максимум 7 дней, рекомендуется один день. "
        "Без supplier_id — все поставщики; несколько UUID передаются повторением параметра. "
        "revision_from=-1 — обычная выгрузка. Это фильтр, автоматической синхронизации пока нет. "
        "Все полученные статусы сохраняются, включая NEW и DELETED. "
        "Цены и количества возвращаются точными десятичными строками. "
        "date_incoming и incoming_date сохраняются раздельно, без назначения часового пояса. "
        "Полный XML сохраняется локально; пустой результат содержит documents=[]. "
        "После проверки выполните POST /api/v1/iiko/logout для освобождения лицензии."
    ),
)
async def get_incoming_invoices(
    service: IikoInvoicesDependency,
    query: Annotated[IncomingInvoicesQuery, Query()],
) -> IncomingInvoicesResponse:
    return await service.get_incoming_invoices(query)
