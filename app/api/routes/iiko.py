"""Ручное управление сессией iikoServer из локального Swagger."""

from fastapi import APIRouter

from app.api.dependencies import IikoAuthDependency
from app.schemas.iiko import IikoAuthResponse, IikoErrorResponse, IikoSessionStatus

router = APIRouter(
    prefix="/iiko",
    tags=["iiko — авторизация"],
    responses={code: {"model": IikoErrorResponse} for code in (409, 502, 503, 504)},
)


@router.get(
    "/status",
    response_model=IikoSessionStatus,
    summary="Состояние сессии в нашем приложении",
    description=(
        "Не обращается к iiko. token_cached означает наличие токена, а не проверку его срока."
    ),
)
async def session_status(service: IikoAuthDependency) -> IikoSessionStatus:
    return service.status()


@router.post(
    "/auth",
    response_model=IikoAuthResponse,
    summary="Авторизоваться в iiko",
    description=(
        "Использует настройки сервера из .env. Повторный вызов использует сохранённый токен."
    ),
)
async def authenticate(service: IikoAuthDependency) -> IikoAuthResponse:
    return await service.authenticate()


@router.post(
    "/logout",
    response_model=IikoSessionStatus,
    summary="Выйти из iiko и освободить лицензию",
)
async def logout(service: IikoAuthDependency) -> IikoSessionStatus:
    return await service.logout()
