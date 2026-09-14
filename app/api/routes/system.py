"""Служебные маршруты: доступность приложения и информация о сервисе."""

from fastapi import APIRouter

from app.api.dependencies import SettingsDependency
from app.schemas.system import HealthResponse, ServiceInfoResponse
from app.services.system import get_health, get_service_info

router = APIRouter(tags=["Система"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Проверить работу API",
    description="Проверяет только ответ нашего приложения. Не проверяет iiko или базу данных.",
)
async def health() -> HealthResponse:
    return get_health()


@router.get("/info", response_model=ServiceInfoResponse, summary="Информация о сервисе")
async def service_info(settings: SettingsDependency) -> ServiceInfoResponse:
    return get_service_info(settings)
