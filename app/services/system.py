"""Служебные функции без HTTP-запросов и внешних подключений."""

from app import __version__
from app.core.config import Settings
from app.schemas.system import HealthResponse, ServiceInfoResponse


def get_health() -> HealthResponse:
    return HealthResponse()


def get_service_info(settings: Settings) -> ServiceInfoResponse:
    return ServiceInfoResponse(name=settings.app_name, version=__version__)
