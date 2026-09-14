"""Репликация только на Chain, иерархии настроенных серверов и локальные связи."""

from fastapi import APIRouter

from app.api.dependencies import IikoTopologyDependency
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_topology import (
    CorporateGroupsResponse,
    CorporateHierarchyResponse,
    DepartmentMappingResponse,
    ReplicationResponse,
)

router = APIRouter(
    prefix="/iiko",
    tags=["iiko — подразделения и репликация"],
    responses={code: {"model": IikoErrorResponse} for code in (404, 409, 500, 502, 503, 504)},
)


@router.get(
    "/replication/statuses",
    response_model=ReplicationResponse,
    summary="Последние статусы репликации Chain",
    description=(
        "Читает replication/statuses только на primary, в формате XML. Даты получения "
        "и отправки сохраняются с часовым поясом. SUCCESS не доказывает полноту продаж "
        "или актуальность старого статуса. После проверки выполните logout primary."
    ),
)
async def get_replication_statuses(service: IikoTopologyDependency) -> ReplicationResponse:
    return await service.get_replication()


@router.get(
    "/connections/{connection_id}/departments",
    response_model=CorporateHierarchyResponse,
    summary="Иерархия подразделений выбранного сервера",
    description=(
        "Читает corporation/departments?revisionFrom=-1 по настроенному connection_id. "
        "Возвращает все типы сущностей; DEPARTMENT — торговое предприятие. "
        "Дополнительные юридические сведения сохраняются только в RAW. Каждый вызов "
        "обновляет локальное наблюдение. После проверки выполните logout того же подключения."
    ),
)
async def get_connection_departments(
    connection_id: str, service: IikoTopologyDependency
) -> CorporateHierarchyResponse:
    return await service.get_departments(connection_id)


@router.get(
    "/connections/department-mapping",
    response_model=DepartmentMappingResponse,
    summary="Сопоставление RMS с Chain по UUID",
    description=(
        "Локальный расчёт без запросов к iiko. Сначала загрузите иерархии primary и RMS "
        "и corporate-groups каждого RMS. Все группы RMS должны указывать на один "
        "DEPARTMENT, существующий в иерархиях RMS и Chain. Имена не используются как ключ. "
        "Дубли связей и неоднозначность "
        "отмечаются отдельно. Используются последние успешные снимки текущего процесса "
        "с указанием дат; после перезапуска их нужно загрузить заново."
    ),
)
async def get_department_mapping(service: IikoTopologyDependency) -> DepartmentMappingResponse:
    return service.mapping()


@router.get(
    "/connections/{connection_id}/corporate-groups",
    response_model=CorporateGroupsResponse,
    summary="Привязки групп касс к подразделению сервера",
    description=(
        "Читает corporation/groups?revisionFrom=-1 в XML. Возвращает идентификатор, имя, "
        "departmentId и режим обслуживания группы. Вложенные кассы и отделения пока "
        "сохраняются только в RAW. Группы нужны для идентификации RMS: иерархия может "
        "содержать все подразделения Chain. После проверки выполните logout подключения."
    ),
)
async def get_corporate_groups(
    connection_id: str, service: IikoTopologyDependency
) -> CorporateGroupsResponse:
    return await service.get_corporate_groups(connection_id)
