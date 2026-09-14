"""Protected RMS event capture, synchronization and stored order topology."""

from datetime import date
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Query, Response

from app.api.dependencies import IikoConnectionsDependency, SettingsDependency
from app.api.routes.sync_jobs import authorize
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_events import (
    EventsCapture,
    EventsDayQuery,
    EventsSyncQuery,
    EventsSyncResult,
    OrderTopology,
)
from app.services.iiko_events import capture_events
from app.services.order_topology import read_topology
from app.services.sync_jobs import SyncJobError
from app.sync_events import synchronize_events
from app.sync_references import SyncError

router = APIRouter(tags=["RMS — события и топология"], dependencies=[Depends(authorize)])


@router.get(
    "/iiko/connections/{source_id}/events",
    response_model=EventsCapture,
    summary="Получить события одного дня с RMS",
    description=(
        "Сохраняет приватный локальный снимок и метаданные. Возвращает только счётчики. "
        "Сессия iiko закрывается автоматически."
    ),
)
async def get_events(
    source_id: str,
    query: Annotated[EventsDayQuery, Query()],
    connections: IikoConnectionsDependency,
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    if source_id == "primary":
        raise IikoError("events_rms_required", "Для событий выберите RMS.", status_code=422)
    connection = connections.get_connection(source_id)
    try:
        return await capture_events(connections, source_id, query.date)
    finally:
        await connection.auth.logout()


@router.post(
    "/sync/events",
    response_model=EventsSyncResult,
    summary="Синхронизировать один день событий RMS с БД",
    description=(
        "Полная выгрузка одного дня, версии событий, сопоставление переносов и отметка покрытия "
        "сохраняются атомарно. Общий замок загрузчиков; при занятости 409. "
        "Повтор не дублирует события."
    ),
)
def sync_events(payload: EventsSyncQuery, settings: SettingsDependency, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return synchronize_events(settings, payload)
    except SyncError as error:
        if str(error) == "sync_already_running":
            raise SyncJobError(
                "sync_already_running", "Синхронизация уже выполняется.", 409
            ) from None
        raise SyncJobError(
            "events_sync_failed", "События не синхронизированы. Проверьте sync_runs."
        ) from None
    except (psycopg.Error, OSError, ValueError, KeyError, TypeError):
        raise SyncJobError("events_sync_unavailable", "Синхронизация событий недоступна.") from None


@router.get(
    "/events/orders/topology",
    response_model=OrderTopology,
    summary="Топология заказа из БД: события, участники и переносы",
    description=(
        "Без запросов к iiko. Поиск по RMS, дате и номеру; связанные заказы включаются по UUID, "
        "в том числе из других загруженных дней. До 50 заказов и 2000 событий; "
        "превышение возвращает 413."
    ),
)
def get_topology(
    settings: SettingsDependency,
    response: Response,
    source_id: Annotated[str, Query(pattern=r"^[a-z][a-z0-9-]{0,49}$")],
    date: date,
    order_number: Annotated[int, Query(ge=1)],
):
    response.headers["Cache-Control"] = "no-store"
    try:
        return read_topology(settings, source_id, date, order_number)
    except (psycopg.Error, OSError, ValueError, KeyError, TypeError):
        raise SyncJobError(
            "topology_unavailable", "Не удалось прочитать топологию из БД."
        ) from None
