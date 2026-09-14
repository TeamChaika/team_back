"""Authenticated start and status routes for document refresh jobs."""

import secrets
import subprocess
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

import httpx
import psycopg
from fastapi import APIRouter, Depends, Request, Response, Security
from fastapi.security import APIKeyHeader

from app.api.dependencies import SettingsDependency
from app.schemas.iiko import IikoErrorResponse
from app.schemas.iiko_outgoing import OutgoingInvoicesQuery, OutgoingSyncResult
from app.schemas.iiko_reports import AccountingReportQuery
from app.schemas.iiko_transfers import TransfersSyncQuery, TransferSyncResult
from app.schemas.sales_drilldown import DiscountDetailsQuery
from app.schemas.sync_jobs import (
    AccountSyncStatus,
    CashShiftSyncQuery,
    CashShiftSyncStatus,
    CounteragentBalanceSyncStatus,
    DictionarySyncStatus,
    EmployeeSyncStatus,
    RefreshStart,
    RefreshStatus,
    StoreBalanceSyncStatus,
)
from app.services.sales_drilldown import discount_details
from app.services.sync_jobs import SyncJobError, SyncJobsService
from app.sync_accounts import synchronize_accounts
from app.sync_cash_shifts import synchronize_cash_shifts
from app.sync_counteragent_balances import synchronize_counteragent_balances
from app.sync_dictionaries import synchronize_dictionaries
from app.sync_employees import synchronize_employees
from app.sync_outgoing import synchronize_outgoing
from app.sync_references import SyncError
from app.sync_store_balances import synchronize_store_balances
from app.sync_transfers import synchronize_transfers

key_header = APIKeyHeader(name="X-Sync-Key", scheme_name="SyncKey", auto_error=False)


def authorize(settings: SettingsDependency, key: Annotated[str | None, Security(key_header)]):
    expected = settings.sync_api_key.get_secret_value()
    if not expected:
        raise SyncJobError("sync_key_not_configured", "Ключ запуска синхронизации не настроен.")
    if not key or not secrets.compare_digest(key.encode(), expected.encode()):
        raise SyncJobError("sync_unauthorized", "Неверный ключ синхронизации.", 401)


def get_service(request: Request) -> SyncJobsService:
    return request.app.state.sync_jobs


Service = Annotated[SyncJobsService, Depends(get_service)]


@contextmanager
def storage_errors():
    try:
        yield
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        raise SyncJobError(
            "sync_state_unavailable", "Не удалось прочитать или сохранить задание."
        ) from None


router = APIRouter(
    prefix="/sync",
    tags=["Синхронизация"],
    dependencies=[Depends(authorize)],
    responses={code: {"model": IikoErrorResponse} for code in (401, 404, 409, 503)},
)


@router.post(
    "/discount-details",
    summary="Заказы и позиции по сохранённой строке скидки",
    description=(
        "Требует X-Sync-Key. Без order_id возвращает заказы скидки, с order_id — "
        "полный состав одного из этих заказов. По 100 строк, offset для продолжения. "
        "Источник — одна сохранённая строка OLAP; дата, ресторан и скидка берутся из БД. "
        "Новые ответы сохраняются в Supabase после проверки и logout. "
        "Во время другой синхронизации новая загрузка получает 409; сохранённые данные доступны."
    ),
)
def discount_drilldown(
    payload: DiscountDetailsQuery, settings: SettingsDependency, response: Response
):
    response.headers["Cache-Control"] = "no-store"
    return discount_details(settings, payload, None)


@router.post(
    "/refresh",
    response_model=RefreshStatus,
    status_code=202,
    responses={
        200: {"model": RefreshStatus, "description": "Задание с этим request_id уже существует"}
    },
    summary="Обновить накладные и списания за последние 60 дней",
    description=(
        "Сегодня и 59 предыдущих дней по Europe/Simferopol. Возвращает job_id сразу; "
        "загрузка выполняется отдельным процессом, по одному дню, с чередованием ресурсов. "
        "Повтор с тем же request_id возвращает существующее задание. Другой запуск во время "
        "активной загрузки получает 409. Для нового обновления используйте новый request_id."
    ),
)
def refresh(payload: RefreshStart, service: Service, response: Response) -> RefreshStatus:
    with storage_errors():
        result, created = service.start(payload.request_id)
    response.status_code = 202 if created else 200
    response.headers["Location"] = f"/api/v1/sync/jobs/{result.job_id}"
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get(
    "/jobs/{job_id}", response_model=RefreshStatus, summary="Прогресс задания синхронизации"
)
def status(job_id: UUID, service: Service, response: Response) -> RefreshStatus:
    response.headers["Cache-Control"] = "no-store"
    with storage_errors():
        return service.get(job_id)


@router.post(
    "/employees",
    response_model=EmployeeSyncStatus,
    summary="Синхронизировать справочник сотрудников с БД",
    description=(
        "Полный текущий справочник Chain (includeDeleted=false, revisionFrom=-1). "
        "Запрос ждёт завершения загрузки и сохранения в Supabase. Повтор обновляет записи "
        "по UUID, сохраняя исходные признаки, связи и RAW. Даты не требуются. "
        "Использует общий замок синхронизации и освобождает сессию iiko. "
        "Названия должностей и удалённые сотрудники в этот этап не входят."
    ),
)
def employees(settings: SettingsDependency, response: Response) -> EmployeeSyncStatus:
    response.headers["Cache-Control"] = "no-store"
    try:
        return EmployeeSyncStatus.model_validate(synchronize_employees(settings))
    except SyncError as error:
        if str(error) == "sync_already_running":
            raise SyncJobError(
                "sync_already_running", "Синхронизация уже выполняется.", 409
            ) from None
        raise SyncJobError(
            "employee_sync_failed", "Сотрудники не синхронизированы. Проверьте журнал sync_runs."
        ) from None
    except (psycopg.Error, OSError, ValueError, KeyError, TypeError):
        raise SyncJobError(
            "employee_sync_unavailable", "Синхронизация сотрудников недоступна."
        ) from None


@router.post(
    "/store-balances",
    response_model=StoreBalanceSyncStatus,
    summary="Синхронизировать остатки всех складов с БД",
    description=(
        "Полный снимок Chain на timestamp: YYYY-MM-DDTHH:MM:SS без часового пояса. "
        "Фильтры складов, товаров и подразделений не принимаются. Запрос ждёт сохранения "
        "в Supabase. Повтор обновляет текущий снимок только на это учётное время; "
        "предыдущие наблюдения сохраняются. Нулевые и отрицательные значения не исправляются. "
        "Общий замок исключает параллельные загрузчики; сессия iiko освобождается."
    ),
)
def store_balances(
    payload: AccountingReportQuery, settings: SettingsDependency, response: Response
) -> StoreBalanceSyncStatus:
    response.headers["Cache-Control"] = "no-store"
    try:
        return StoreBalanceSyncStatus.model_validate(synchronize_store_balances(settings, payload))
    except SyncError as error:
        if str(error) == "sync_already_running":
            raise SyncJobError(
                "sync_already_running", "Синхронизация уже выполняется.", 409
            ) from None
        raise SyncJobError(
            "store_balance_sync_failed", "Остатки не синхронизированы. Проверьте журнал sync_runs."
        ) from None
    except (psycopg.Error, OSError, ValueError, KeyError, TypeError):
        raise SyncJobError(
            "store_balance_sync_unavailable", "Синхронизация остатков недоступна."
        ) from None


@router.post(
    "/cash-shifts",
    response_model=CashShiftSyncStatus,
    summary="Сохранить кассовые смены за 1–7 дней открытия",
    description="Полная выгрузка Chain с ANY и revisionFrom=-1, по одному дню. "
    "RAW и строки дня сохраняются одной транзакцией. Связь ресторана — по точному UUID "
    "точки продаж из групп Chain на момент загрузки. Неоднозначные связи остаются "
    "неопределёнными. Повтор обновляет день и сохраняет историю. Общий замок и logout.",
)
def cash_shifts(
    query: CashShiftSyncQuery, settings: SettingsDependency, response: Response
) -> CashShiftSyncStatus:
    response.headers["Cache-Control"] = "no-store"
    try:
        return CashShiftSyncStatus.model_validate(synchronize_cash_shifts(settings, query))
    except SyncError as error:
        if str(error) == "sync_already_running":
            raise SyncJobError(
                "sync_already_running", "Синхронизация уже выполняется.", 409
            ) from None
        raise SyncJobError(
            "cash_shifts_sync_failed",
            "Загрузка смен не завершена. Сохранённые дни и ошибка указаны в sync_runs.",
        ) from None
    except (psycopg.Error, OSError, ValueError, KeyError, TypeError):
        raise SyncJobError(
            "cash_shifts_sync_unavailable", "Синхронизация смен недоступна."
        ) from None


@router.post(
    "/accounts",
    response_model=AccountSyncStatus,
    summary="Синхронизировать справочник счетов",
    description="Полный Account из Chain, включая удалённые счета и родительские UUID. "
    "Сохраняет RAW и справочник одной транзакцией, проверяет ссылки балансов и списаний. "
    "Общий замок синхронизации и logout. Даты не требуются.",
)
def accounts(settings: SettingsDependency, response: Response) -> AccountSyncStatus:
    response.headers["Cache-Control"] = "no-store"
    try:
        return AccountSyncStatus.model_validate(synchronize_accounts(settings))
    except SyncError as error:
        if str(error) == "sync_already_running":
            raise SyncJobError(
                "sync_already_running", "Синхронизация уже выполняется.", 409
            ) from None
        raise SyncJobError(
            "accounts_sync_failed", "Счета не синхронизированы. Проверьте sync_runs."
        ) from None
    except (psycopg.Error, OSError, ValueError, KeyError, TypeError):
        raise SyncJobError(
            "accounts_sync_unavailable", "Синхронизация счетов недоступна."
        ) from None


@router.post(
    "/dictionaries",
    response_model=DictionarySyncStatus,
    summary="Синхронизировать контрагентов, единицы и категории",
    description=(
        "Последовательно загружает три полных справочника из Chain и сохраняет их в Supabase "
        "одной транзакцией. Контрагенты — список поставщиков /suppliers. Единицы измерения "
        "и пользовательские категории включают удалённые записи. Даты не требуются. "
        "Возвращает количество записей, проверку связей и подтверждение logout."
    ),
)
def dictionaries(settings: SettingsDependency, response: Response) -> DictionarySyncStatus:
    response.headers["Cache-Control"] = "no-store"
    try:
        return DictionarySyncStatus.model_validate(synchronize_dictionaries(settings))
    except SyncError as error:
        if str(error) == "sync_already_running":
            raise SyncJobError(
                "sync_already_running", "Синхронизация уже выполняется.", 409
            ) from None
        raise SyncJobError(
            "dictionary_sync_failed", "Справочники не синхронизированы. Проверьте sync_runs."
        ) from None
    except (psycopg.Error, OSError, ValueError, KeyError, TypeError):
        raise SyncJobError(
            "dictionary_sync_unavailable", "Синхронизация справочников недоступна."
        ) from None


@router.post(
    "/counteragent-balances",
    response_model=CounteragentBalanceSyncStatus,
    summary="Синхронизировать денежные балансы",
    description=(
        "Сохраняет полный отчёт Chain на учётную дату-время без фильтров. "
        "RAW, строки с точными суммами и текущая версия снимка записываются одной транзакцией. "
        "Повторный запуск сохраняет историю. Null, исходный знак и повторные строки сохраняются. "
        "Справочник счетов пока не загружен, общий денежный итог не вычисляется. "
        "По завершении освобождает лицензию iiko."
    ),
)
def counteragent_balances(
    query: AccountingReportQuery, settings: SettingsDependency, response: Response
) -> CounteragentBalanceSyncStatus:
    response.headers["Cache-Control"] = "no-store"
    try:
        return CounteragentBalanceSyncStatus.model_validate(
            synchronize_counteragent_balances(settings, query)
        )
    except SyncError as error:
        if str(error) == "sync_already_running":
            raise SyncJobError(
                "sync_already_running", "Синхронизация уже выполняется.", 409
            ) from None
        raise SyncJobError(
            "counteragent_balance_sync_failed", "Балансы не синхронизированы. Проверьте sync_runs."
        ) from None
    except (psycopg.Error, OSError, ValueError, KeyError, TypeError):
        raise SyncJobError(
            "counteragent_balance_sync_unavailable", "Синхронизация денежных балансов недоступна."
        ) from None


@router.post(
    "/outgoing-invoices",
    response_model=OutgoingSyncResult,
    summary="Синхронизировать расходные накладные за 1–7 дней",
    description=(
        "Последовательно читает каждый день и сохраняет RAW, документы, строки и прогресс "
        "одной транзакцией на день. Для длинной истории используйте app.sync_outgoing. "
        "В случае ошибки ранее завершённые дни сохранены; повтор безопасен. "
        "Проверяет исходные UUID связей с приходными. Logout выполняется автоматически."
    ),
)
def outgoing_invoices(
    query: OutgoingInvoicesQuery, settings: SettingsDependency, response: Response
) -> OutgoingSyncResult:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = synchronize_outgoing(settings, query)
        counts = result["counts"]
        return OutgoingSyncResult(
            run_id=result["run_id"],
            status=result["status"],
            request=query,
            completed_days=counts["completed_days"],
            documents=counts["documents_read"],
            items=counts["items_read"],
            last_day_checks=counts["last_day_checks"],
            logout_ok=result["logout_ok"],
        )
    except SyncError as error:
        if str(error) == "sync_already_running":
            raise SyncJobError(
                "sync_already_running", "Синхронизация уже выполняется.", 409
            ) from None
        raise SyncJobError(
            "outgoing_sync_failed", "Расходные не синхронизированы. Проверьте sync_runs."
        ) from None
    except (psycopg.Error, OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        raise SyncJobError(
            "outgoing_sync_unavailable", "Синхронизация расходных недоступна."
        ) from None


@router.post(
    "/transfers",
    response_model=TransferSyncResult,
    summary="Синхронизировать внутренние перемещения за 1–7 дней",
    description=(
        "Последовательно читает каждый день и сохраняет RAW, документы, строки и прогресс "
        "одной транзакцией на день. Для длинной истории используйте app.sync_transfers. "
        "В случае ошибки ранее завершённые дни сохранены; повтор безопасен. "
        "Проверяет UUID обоих складов, товаров и единиц измерения. "
        "Logout выполняется автоматически."
    ),
)
def internal_transfers(
    query: TransfersSyncQuery, settings: SettingsDependency, response: Response
) -> TransferSyncResult:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = synchronize_transfers(settings, query)
        counts = result["counts"]
        return TransferSyncResult(
            run_id=result["run_id"],
            status=result["status"],
            request=query,
            completed_days=counts["completed_days"],
            documents=counts["documents_read"],
            items=counts["items_read"],
            last_day_checks=counts["last_day_checks"],
            logout_ok=result["logout_ok"],
        )
    except SyncError as error:
        if str(error) == "sync_already_running":
            raise SyncJobError(
                "sync_already_running", "Синхронизация уже выполняется.", 409
            ) from None
        raise SyncJobError(
            "transfers_sync_failed", "Перемещения не синхронизированы. Проверьте sync_runs."
        ) from None
    except (psycopg.Error, OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        raise SyncJobError(
            "transfers_sync_unavailable", "Синхронизация перемещений недоступна."
        ) from None
