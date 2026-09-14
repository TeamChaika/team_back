"""Одна сессия и последовательные запросы в пределах одного процесса API."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from functools import partial
from pathlib import Path
from uuid import UUID

from app.core.config import Settings
from app.integrations.iiko.client import CatalogDownload, IikoClient
from app.integrations.iiko.dictionaries import DictionaryKind
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko import IikoAuthResponse, IikoSessionStatus
from app.schemas.iiko_cash_shifts import CashShiftFilter
from app.schemas.iiko_olap_sales import DailySalesQuery
from app.schemas.iiko_transfers import TransferStatus
from app.schemas.iiko_writeoffs import WriteoffStatus

logger = logging.getLogger(__name__)


class IikoAuthService:
    def __init__(self, settings: Settings, client: IikoClient) -> None:
        self._configured = settings.iiko_configured
        self._client = client
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._unknown = False

    def status(self) -> IikoSessionStatus:
        state = "unknown" if self._unknown else "token_cached" if self._token else "logged_out"
        return IikoSessionStatus(configured=self._configured, state=state)

    async def authenticate(self) -> IikoAuthResponse:
        async with self._lock:
            return await self._authenticate()

    async def _authenticate(self) -> IikoAuthResponse:
        if not self._configured:
            raise IikoError(
                "iiko_not_configured",
                "Заполните CHAIKA_IIKO_BASE_URL, CHAIKA_IIKO_LOGIN и CHAIKA_IIKO_PASSWORD.",
                status_code=503,
            )
        if self._unknown:
            raise IikoError(
                "iiko_session_unknown",
                "Результат предыдущей операции неизвестен. Новый токен не запрашивается. "
                "Выполните logout; если токен не был получен, "
                "проверьте сессию у администратора iiko.",
                status_code=409,
            )
        reused = self._token is not None
        if not reused:
            try:
                self._token = await self._client.authenticate()
            except IikoError as exc:
                self._unknown = exc.outcome_unknown
                raise
            except asyncio.CancelledError:
                self._unknown = True
                raise
        return IikoAuthResponse(**self.status().model_dump(), reused=reused)

    async def download_products(self, destination: Path) -> CatalogDownload:
        return await self._download_catalog(self._client.download_products, destination)

    async def download_groups(self, destination: Path) -> CatalogDownload:
        return await self._download_catalog(self._client.download_groups, destination)

    async def download_stores(self, destination: Path) -> CatalogDownload:
        return await self._download_catalog(self._client.download_stores, destination)

    async def download_employees(self, destination: Path) -> CatalogDownload:
        return await self._download_catalog(self._client.download_employees, destination)

    async def download_employee_roles(self, destination: Path) -> CatalogDownload:
        return await self._download_catalog(self._client.download_employee_roles, destination)

    async def download_dictionary(
        self, destination: Path, *, kind: DictionaryKind
    ) -> CatalogDownload:
        return await self._download_catalog(
            partial(self._client.download_dictionary, kind=kind), destination
        )

    async def download_incoming_invoices(
        self,
        destination: Path,
        *,
        date_from: date,
        date_to: date,
        supplier_ids: list[UUID],
        revision_from: int,
    ) -> CatalogDownload:
        download = partial(
            self._client.download_incoming_invoices,
            date_from=date_from,
            date_to=date_to,
            supplier_ids=supplier_ids,
            revision_from=revision_from,
        )
        return await self._download_catalog(download, destination)

    async def download_outgoing_invoices(
        self, destination: Path, *, date_from: date, date_to: date
    ) -> CatalogDownload:
        return await self._download_catalog(
            partial(self._client.download_outgoing_invoices, date_from=date_from, date_to=date_to),
            destination,
        )

    async def download_all_assembly(
        self, destination: Path, *, business_date: date
    ) -> CatalogDownload:
        download = partial(self._client.download_all_assembly, business_date=business_date)
        return await self._download_catalog(download, destination)

    async def download_assembled(
        self,
        destination: Path,
        *,
        product_id: UUID,
        business_date: date,
        department_id: UUID | None = None,
    ) -> CatalogDownload:
        download = partial(
            self._client.download_assembled,
            product_id=product_id,
            business_date=business_date,
            department_id=department_id,
        )
        return await self._download_catalog(download, destination)

    async def download_counteragent_balances(
        self,
        destination: Path,
        *,
        timestamp: datetime,
        account_ids: list[UUID],
        counteragent_ids: list[UUID],
        department_ids: list[UUID],
    ) -> CatalogDownload:
        download = partial(
            self._client.download_counteragent_balances,
            timestamp=timestamp,
            account_ids=account_ids,
            counteragent_ids=counteragent_ids,
            department_ids=department_ids,
        )
        return await self._download_catalog(download, destination)

    async def download_store_balances(
        self,
        destination: Path,
        *,
        timestamp: datetime,
        department_ids: list[UUID],
        store_ids: list[UUID],
        product_ids: list[UUID],
    ) -> CatalogDownload:
        download = partial(
            self._client.download_store_balances,
            timestamp=timestamp,
            department_ids=department_ids,
            store_ids=store_ids,
            product_ids=product_ids,
        )
        return await self._download_catalog(download, destination)

    async def download_writeoffs(
        self,
        destination: Path,
        *,
        date_from: date,
        date_to: date,
        status: WriteoffStatus | None,
        revision_from: int,
    ) -> CatalogDownload:
        download = partial(
            self._client.download_writeoffs,
            date_from=date_from,
            date_to=date_to,
            status=status,
            revision_from=revision_from,
        )
        return await self._download_catalog(download, destination)

    async def download_transfers(
        self,
        destination: Path,
        *,
        date_from: date,
        date_to: date,
        status: TransferStatus | None,
        revision_from: int,
    ) -> CatalogDownload:
        download = partial(
            self._client.download_transfers,
            date_from=date_from,
            date_to=date_to,
            status=status,
            revision_from=revision_from,
        )
        return await self._download_catalog(download, destination)

    async def download_cash_shifts(
        self,
        destination: Path,
        *,
        open_date_from: date,
        open_date_to: date,
        department_ids: list[UUID],
        group_ids: list[UUID],
        status: CashShiftFilter,
        revision_from: int,
    ) -> CatalogDownload:
        download = partial(
            self._client.download_cash_shifts,
            open_date_from=open_date_from,
            open_date_to=open_date_to,
            department_ids=department_ids,
            group_ids=group_ids,
            status=status,
            revision_from=revision_from,
        )
        return await self._download_catalog(download, destination)

    async def download_server_type(self, destination: Path) -> CatalogDownload:
        return await self._download_catalog(self._client.download_server_type, destination)

    async def download_replication_statuses(self, destination: Path) -> CatalogDownload:
        return await self._download_catalog(self._client.download_replication_statuses, destination)

    async def download_departments(self, destination: Path) -> CatalogDownload:
        return await self._download_catalog(self._client.download_departments, destination)

    async def download_corporate_groups(self, destination: Path) -> CatalogDownload:
        return await self._download_catalog(self._client.download_corporate_groups, destination)

    async def download_olap_sales_columns(self, destination: Path) -> CatalogDownload:
        return await self._download_catalog(self._client.download_olap_sales_columns, destination)

    async def download_events(self, destination: Path, *, day: date) -> CatalogDownload:
        return await self._download_catalog(
            partial(self._client.download_events, day=day), destination
        )

    async def download_event_types(self, destination: Path) -> CatalogDownload:
        return await self._download_catalog(self._client.download_event_types, destination)

    async def download_daily_sales(
        self, destination: Path, *, query: DailySalesQuery
    ) -> CatalogDownload:
        download = partial(self._client.download_daily_sales, query=query)
        return await self._download_catalog(download, destination)

    async def _download_catalog(
        self, download: Callable[[str, Path], Awaitable[CatalogDownload]], destination: Path
    ) -> CatalogDownload:
        async with self._lock:
            await self._authenticate()
            for attempt in range(2):
                assert self._token is not None
                try:
                    return await download(self._token, destination)
                except IikoError as exc:
                    if exc.upstream_status_code != 401:
                        raise
                    # 401 документирован как недействительная сессия; 403 сюда не относится.
                    self._token = None
                    self._client.forget_session()
                    if attempt == 1:
                        raise
                    await self._authenticate()
            raise RuntimeError("Unreachable catalog request state")

    async def _logout(self) -> IikoSessionStatus:
        if self._token is None:
            if self._unknown:
                raise IikoError(
                    "iiko_session_unknown",
                    "Токен неизвестен: подтвердить освобождение лицензии невозможно. "
                    "Проверьте сессию у администратора iiko перед перезапуском приложения.",
                    status_code=409,
                )
            return self.status()
        try:
            await self._client.logout(self._token)
        except (IikoError, asyncio.CancelledError):
            self._unknown = True
            raise
        self._token = None
        self._unknown = False
        return self.status()

    async def logout(self) -> IikoSessionStatus:
        async with self._lock:
            return await self._logout()

    async def aclose(self) -> None:
        async with self._lock:
            try:
                await self._logout()
            except IikoError as exc:
                logger.warning("Выход из iiko при остановке не подтверждён: %s", exc.code)
            finally:
                await self._client.aclose()
