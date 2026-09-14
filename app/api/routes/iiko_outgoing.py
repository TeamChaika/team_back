"""Read outgoing invoices without writing documents to iiko."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_outgoing import OutgoingInvoicesQuery, OutgoingInvoicesResponse
from app.services.iiko_outgoing import IikoOutgoingInvoicesService


def get_service(request: Request) -> IikoOutgoingInvoicesService:
    return request.app.state.iiko_outgoing


Service = Annotated[IikoOutgoingInvoicesService, Depends(get_service)]
router = APIRouter(
    prefix="/iiko/outgoing-invoices",
    tags=["iiko — расходные накладные"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 500, 502, 503, 504)},
)


@router.get(
    "",
    response_model=OutgoingInvoicesResponse,
    summary="Выгрузить расходные накладные за период",
    description=(
        "Читает documents/export/outgoingInvoice за 1–7 дней включительно. "
        "Полная выгрузка без фильтра контрагента и без revisionFrom. "
        "Сохраняет NEW/PROCESSED/DELETED, точные суммы, количества и порядок строк. "
        "linked_incoming_invoice_id — исходная ссылка на приходную. "
        "Цена и количество могут относиться к разным единицам; сумму не пересчитываем. "
        "RAW XML остаётся локально. После проверки выполните logout; "
        "для сохранения в Supabase используйте POST /api/v1/sync/outgoing-invoices."
    ),
)
async def get_outgoing_invoices(
    service: Service, query: Annotated[OutgoingInvoicesQuery, Query()]
) -> OutgoingInvoicesResponse:
    return await service.get_outgoing_invoices(query)
