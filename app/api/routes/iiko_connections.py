"""Локальный список подключений и определение типа одного сервера."""

from fastapi import APIRouter

from app.api.dependencies import IikoConnectionsDependency
from app.schemas.iiko import IikoErrorResponse, IikoSessionStatus
from app.schemas.iiko_connections import IikoConnectionsResponse, IikoServerTypeResponse

router = APIRouter(
    prefix="/iiko/connections",
    tags=["iiko — подключения"],
    responses={code: {"model": IikoErrorResponse} for code in (404, 409, 500, 502, 503, 504)},
)


@router.get("", response_model=IikoConnectionsResponse, summary="Настроенные подключения iiko")
async def list_connections(service: IikoConnectionsDependency) -> IikoConnectionsResponse:
    """Локальные сведения без обращения к iiko. Тип появляется после успешной проверки."""
    return service.list()


@router.get(
    "/{connection_id}/server-type",
    response_model=IikoServerTypeResponse,
    summary="Проверить тип выбранного сервера",
    description=(
        "Выберите connection_id из списка подключений. primary — существующее подключение. "
        "Каждый вызов обращается к GET /replication/serverType выбранного сервера; "
        "тип не определяется по URL. CHAIN, REPLICATED_RMS или STANDALONE_RMS. "
        "Авторизация выполняется автоматически, сессия отдельная для каждого сервера. "
        "После проверки выполните logout для того же connection_id."
    ),
)
async def get_server_type(
    connection_id: str,
    service: IikoConnectionsDependency,
) -> IikoServerTypeResponse:
    return await service.get_server_type(connection_id)


@router.post(
    "/{connection_id}/logout",
    response_model=IikoSessionStatus,
    summary="Освободить лицензию выбранного подключения",
)
async def logout_connection(
    connection_id: str,
    service: IikoConnectionsDependency,
) -> IikoSessionStatus:
    return await service.logout(connection_id)
