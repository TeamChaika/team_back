"""Авторизация, выход и чтение номенклатуры из iikoServer."""

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx

from app.core.config import Settings
from app.core.logging import configure_http_logging
from app.integrations.iiko.dictionaries import DICTIONARIES, DictionaryKind
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_cash_shifts import CashShiftFilter
from app.schemas.iiko_olap_sales import DailySalesQuery
from app.schemas.iiko_transfers import TransferStatus
from app.schemas.iiko_writeoffs import WriteoffStatus

MAX_RESPONSE_BYTES = 4096
type CatalogEndpoint = Literal["v2/entities/products/list", "v2/entities/products/group/list"]
type DataEndpoint = (
    CatalogEndpoint
    | Literal[
        "v2/assemblyCharts/getAssembled",
        "v2/assemblyCharts/getAll",
        "documents/export/incomingInvoice",
        "documents/export/outgoingInvoice",
        "corporation/stores",
        "corporation/departments",
        "corporation/groups",
        "v2/reports/balance/counteragents",
        "v2/reports/balance/stores",
        "employees",
        "employees/roles",
        "suppliers",
        "v2/entities/list",
        "v2/entities/products/category/list",
        "v2/documents/writeoff",
        "v2/documents/internalTransfer",
        "v2/cashshifts/list",
        "replication/serverType",
        "replication/statuses",
        "v2/reports/olap/columns",
        "v2/reports/olap",
        "events",
        "events/metadata",
    ]
)


@dataclass(frozen=True)
class CatalogDownload:
    size_bytes: int
    sha256: str


class IikoClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        configure_http_logging()
        self._http = httpx.AsyncClient(
            timeout=settings.iiko_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            transport=transport,
        )

    @asynccontextmanager
    async def _stream_response(
        self,
        endpoint: Literal["auth", "logout"] | DataEndpoint,
        *,
        params: dict[str, str] | list[tuple[str, str]] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict | None = None,
    ) -> AsyncIterator[httpx.Response]:
        url = f"{str(self._settings.iiko_base_url).rstrip('/')}/{endpoint}"
        request_timeout = (
            self._settings.iiko_products_timeout_seconds
            if endpoint == "v2/entities/products/list"
            else self._settings.iiko_timeout_seconds
        )
        if endpoint == "v2/entities/products/group/list":
            request_timeout = self._settings.iiko_groups_timeout_seconds
        if endpoint == "v2/assemblyCharts/getAssembled":
            request_timeout = self._settings.iiko_assembly_timeout_seconds
        if endpoint == "v2/assemblyCharts/getAll":
            request_timeout = self._settings.iiko_all_assembly_timeout_seconds
        if endpoint == "documents/export/incomingInvoice":
            request_timeout = self._settings.iiko_invoices_timeout_seconds
        if endpoint == "documents/export/outgoingInvoice":
            request_timeout = self._settings.iiko_outgoing_timeout_seconds
        if endpoint == "corporation/stores":
            request_timeout = self._settings.iiko_stores_timeout_seconds
        if endpoint == "v2/reports/balance/counteragents":
            request_timeout = self._settings.iiko_balances_timeout_seconds
        if endpoint == "v2/reports/balance/stores":
            request_timeout = self._settings.iiko_store_balances_timeout_seconds
        if endpoint in {"employees", "employees/roles"}:
            request_timeout = self._settings.iiko_employees_timeout_seconds
        if endpoint in {spec.endpoint for spec in DICTIONARIES.values()}:
            request_timeout = self._settings.iiko_dictionaries_timeout_seconds
        if endpoint == "v2/documents/internalTransfer":
            request_timeout = self._settings.iiko_transfers_timeout_seconds
        if endpoint == "v2/documents/writeoff":
            request_timeout = self._settings.iiko_writeoffs_timeout_seconds
        if endpoint == "v2/cashshifts/list":
            request_timeout = self._settings.iiko_cash_shifts_timeout_seconds
        if endpoint == "v2/reports/olap/columns":
            request_timeout = self._settings.iiko_olap_columns_timeout_seconds
        if endpoint == "v2/reports/olap":
            request_timeout = self._settings.iiko_olap_sales_timeout_seconds
        try:
            async with (
                asyncio.timeout(request_timeout),
                self._http.stream(
                    "POST" if endpoint == "v2/reports/olap" else "GET",
                    url,
                    params=params,
                    headers=headers,
                    timeout=request_timeout,
                    json=json_body,
                ) as response,
            ):
                if response.status_code != 200:
                    raise IikoError(
                        "iiko_request_rejected",
                        f"iiko отклонил запрос (HTTP {response.status_code}). "
                        "Проверьте доступ, учётные данные и свободную лицензию.",
                        outcome_unknown=response.status_code >= 500,
                        upstream_status_code=response.status_code,
                    )
                if "text/html" in response.headers.get("content-type", "").lower():
                    raise IikoError(
                        "iiko_invalid_response",
                        "iiko вернул HTML вместо ожидаемых данных.",
                        outcome_unknown=True,
                    )
                yield response
        except (httpx.TimeoutException, TimeoutError):
            raise IikoError(
                "iiko_timeout",
                "iiko не ответил вовремя. Результат операции неизвестен.",
                status_code=504,
                outcome_unknown=True,
            ) from None

        except httpx.RequestError:
            raise IikoError(
                "iiko_unavailable",
                "Ошибка соединения с iiko. Проверьте адрес, сеть и TLS.",
                outcome_unknown=True,
            ) from None

    async def _request(
        self,
        endpoint: Literal["auth", "logout"],
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        body = bytearray()
        async with self._stream_response(endpoint, params=params, headers=headers) as response:
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise IikoError(
                        "iiko_invalid_response",
                        "Ответ iiko слишком большой.",
                        outcome_unknown=True,
                    )
        return body.decode("utf-8", errors="replace").strip()

    async def authenticate(self) -> str:
        # SHA1 требуется протоколом iikoServer; это не хранение паролей нашей системы.
        password_hash = hashlib.sha1(
            self._settings.iiko_password.get_secret_value().encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()
        token = await self._request(
            "auth", params={"login": self._settings.iiko_login, "pass": password_hash}
        )
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,512}", token):
            raise IikoError(
                "iiko_invalid_response", "iiko вернул некорректный токен.", outcome_unknown=True
            )
        return token

    async def logout(self, token: str) -> None:
        confirmation = await self._request("logout", headers={"Cookie": f"key={token}"})
        # Документация описывает эхо токена; наша установка возвращает это сообщение.
        if confirmation not in {token, f"Connection released: {token}"}:
            raise IikoError(
                "iiko_invalid_response",
                "iiko не подтвердил выход из текущей сессии.",
                outcome_unknown=True,
            )
        self._http.cookies.clear()

    async def download_products(self, token: str, destination: Path) -> CatalogDownload:
        return await self._download_file(
            "v2/entities/products/list",
            token,
            destination,
            params={"includeDeleted": "false"},
            max_bytes=self._settings.iiko_products_max_response_bytes,
            error_code="iiko_products_too_large",
        )

    async def download_groups(self, token: str, destination: Path) -> CatalogDownload:
        return await self._download_file(
            "v2/entities/products/group/list",
            token,
            destination,
            params={"includeDeleted": "false"},
            max_bytes=self._settings.iiko_groups_max_response_bytes,
            error_code="iiko_groups_too_large",
        )

    async def download_stores(self, token: str, destination: Path) -> CatalogDownload:
        return await self._download_file(
            "corporation/stores",
            token,
            destination,
            params={"revisionFrom": "-1"},
            accept="application/xml",
            max_bytes=self._settings.iiko_stores_max_response_bytes,
            error_code="iiko_stores_too_large",
        )

    async def download_employees(self, token: str, destination: Path) -> CatalogDownload:
        return await self._download_file(
            "employees",
            token,
            destination,
            params={"includeDeleted": "false", "revisionFrom": "-1"},
            accept="application/xml",
            max_bytes=self._settings.iiko_employees_max_response_bytes,
            error_code="iiko_employees_too_large",
        )

    async def download_employee_roles(self, token: str, destination: Path) -> CatalogDownload:
        return await self._download_file(
            "employees/roles",
            token,
            destination,
            params={"revisionFrom": "-1"},
            accept="application/xml",
            max_bytes=self._settings.iiko_employees_max_response_bytes,
            error_code="iiko_employee_roles_too_large",
        )

    async def download_dictionary(
        self, token: str, destination: Path, *, kind: DictionaryKind
    ) -> CatalogDownload:
        spec = DICTIONARIES[kind]
        return await self._download_file(
            spec.endpoint,
            token,
            destination,
            params=spec.params,
            accept="application/xml" if spec.format == "xml" else "application/json",
            max_bytes=self._settings.iiko_dictionaries_max_response_bytes,
            error_code="iiko_dictionary_too_large",
        )

    async def download_departments(self, token: str, destination: Path) -> CatalogDownload:
        return await self._download_file(
            "corporation/departments",
            token,
            destination,
            params={"revisionFrom": "-1"},
            accept="application/xml",
            max_bytes=4 * 1024 * 1024,
            error_code="iiko_departments_too_large",
        )

    async def download_corporate_groups(self, token: str, destination: Path) -> CatalogDownload:
        return await self._download_file(
            "corporation/groups",
            token,
            destination,
            params={"revisionFrom": "-1"},
            accept="application/xml",
            max_bytes=4 * 1024 * 1024,
            error_code="iiko_corporate_groups_too_large",
        )

    async def download_all_assembly(
        self, token: str, destination: Path, *, business_date: date
    ) -> CatalogDownload:
        return await self._download_file(
            "v2/assemblyCharts/getAll",
            token,
            destination,
            params={
                "dateFrom": business_date.isoformat(),
                "dateTo": (business_date + timedelta(days=1)).isoformat(),
                "includeDeletedProducts": "true",
                "includePreparedCharts": "false",
            },
            max_bytes=self._settings.iiko_all_assembly_max_response_bytes,
            error_code="iiko_all_assembly_too_large",
        )

    async def download_assembled(
        self,
        token: str,
        destination: Path,
        *,
        product_id: UUID,
        business_date: date,
        department_id: UUID | None = None,
    ) -> CatalogDownload:
        params = {"productId": str(product_id), "date": business_date.isoformat()}
        if department_id is not None:
            params["departmentId"] = str(department_id)
        return await self._download_file(
            "v2/assemblyCharts/getAssembled",
            token,
            destination,
            params=params,
            max_bytes=self._settings.iiko_assembly_max_response_bytes,
            error_code="iiko_assembly_too_large",
        )

    async def download_incoming_invoices(
        self,
        token: str,
        destination: Path,
        *,
        date_from: date,
        date_to: date,
        supplier_ids: list[UUID],
        revision_from: int,
    ) -> CatalogDownload:
        params = [
            ("from", date_from.isoformat()),
            ("to", date_to.isoformat()),
            ("revisionFrom", str(revision_from)),
        ]
        params.extend(("supplierId", str(supplier_id)) for supplier_id in supplier_ids)
        return await self._download_file(
            "documents/export/incomingInvoice",
            token,
            destination,
            params=params,
            accept="application/xml",
            max_bytes=self._settings.iiko_invoices_max_response_bytes,
            error_code="iiko_invoices_too_large",
        )

    async def download_outgoing_invoices(
        self, token: str, destination: Path, *, date_from: date, date_to: date
    ) -> CatalogDownload:
        return await self._download_file(
            "documents/export/outgoingInvoice",
            token,
            destination,
            params={"from": date_from.isoformat(), "to": date_to.isoformat()},
            accept="application/xml",
            max_bytes=self._settings.iiko_outgoing_max_response_bytes,
            error_code="iiko_outgoing_too_large",
        )

    async def download_counteragent_balances(
        self,
        token: str,
        destination: Path,
        *,
        timestamp: datetime,
        account_ids: list[UUID],
        counteragent_ids: list[UUID],
        department_ids: list[UUID],
    ) -> CatalogDownload:
        params = [("timestamp", timestamp.isoformat(timespec="seconds"))]
        for name, ids in [
            ("account", account_ids),
            ("counteragent", counteragent_ids),
            ("department", department_ids),
        ]:
            params.extend((name, str(item_id)) for item_id in ids)
        return await self._download_file(
            "v2/reports/balance/counteragents",
            token,
            destination,
            params=params,
            max_bytes=self._settings.iiko_balances_max_response_bytes,
            error_code="iiko_balances_too_large",
        )

    async def download_store_balances(
        self,
        token: str,
        destination: Path,
        *,
        timestamp: datetime,
        department_ids: list[UUID],
        store_ids: list[UUID],
        product_ids: list[UUID],
    ) -> CatalogDownload:
        params = [("timestamp", timestamp.isoformat(timespec="seconds"))]
        for name, ids in [
            ("department", department_ids),
            ("store", store_ids),
            ("product", product_ids),
        ]:
            params.extend((name, str(item_id)) for item_id in ids)
        return await self._download_file(
            "v2/reports/balance/stores",
            token,
            destination,
            params=params,
            max_bytes=self._settings.iiko_store_balances_max_response_bytes,
            error_code="iiko_store_balances_too_large",
        )

    async def download_writeoffs(
        self,
        token: str,
        destination: Path,
        *,
        date_from: date,
        date_to: date,
        status: WriteoffStatus | None,
        revision_from: int,
    ) -> CatalogDownload:
        params = {
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "revisionFrom": str(revision_from),
        }
        if status is not None:
            params["status"] = status
        return await self._download_file(
            "v2/documents/writeoff",
            token,
            destination,
            params=params,
            max_bytes=self._settings.iiko_writeoffs_max_response_bytes,
            error_code="iiko_writeoffs_too_large",
        )

    async def download_transfers(
        self,
        token: str,
        destination: Path,
        *,
        date_from: date,
        date_to: date,
        status: TransferStatus | None,
        revision_from: int,
    ) -> CatalogDownload:
        params = {
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "revisionFrom": str(revision_from),
        }
        if status is not None:
            params["status"] = status
        return await self._download_file(
            "v2/documents/internalTransfer",
            token,
            destination,
            params=params,
            max_bytes=self._settings.iiko_transfers_max_response_bytes,
            error_code="iiko_transfers_too_large",
        )

    async def download_cash_shifts(
        self,
        token: str,
        destination: Path,
        *,
        open_date_from: date,
        open_date_to: date,
        department_ids: list[UUID],
        group_ids: list[UUID],
        status: CashShiftFilter,
        revision_from: int,
    ) -> CatalogDownload:
        params = [
            ("openDateFrom", open_date_from.isoformat()),
            ("openDateTo", open_date_to.isoformat()),
            ("status", status),
            ("revisionFrom", str(revision_from)),
        ]
        for name, ids in [("departmentId", department_ids), ("groupId", group_ids)]:
            params.extend((name, str(item_id)) for item_id in ids)
        return await self._download_file(
            "v2/cashshifts/list",
            token,
            destination,
            params=params,
            max_bytes=self._settings.iiko_cash_shifts_max_response_bytes,
            error_code="iiko_cash_shifts_too_large",
        )

    async def download_server_type(self, token: str, destination: Path) -> CatalogDownload:
        return await self._download_file(
            "replication/serverType",
            token,
            destination,
            params={},
            max_bytes=MAX_RESPONSE_BYTES,
            error_code="iiko_server_type_too_large",
        )

    async def download_replication_statuses(self, token: str, destination: Path) -> CatalogDownload:
        return await self._download_file(
            "replication/statuses",
            token,
            destination,
            params={},
            accept="application/xml",
            max_bytes=4 * 1024 * 1024,
            error_code="iiko_replication_too_large",
        )

    async def download_olap_sales_columns(self, token: str, destination: Path) -> CatalogDownload:
        return await self._download_file(
            "v2/reports/olap/columns",
            token,
            destination,
            params={"reportType": "SALES"},
            max_bytes=self._settings.iiko_olap_columns_max_response_bytes,
            error_code="iiko_olap_columns_too_large",
        )

    async def download_events(self, token: str, destination: Path, *, day: date) -> CatalogDownload:
        return await self._download_file(
            "events",
            token,
            destination,
            params={
                "from_time": f"{day.isoformat()}T00:00:00.000",
                "to_time": f"{(day + timedelta(days=1)).isoformat()}T00:00:00.000",
            },
            accept="application/xml",
            max_bytes=32 * 1024 * 1024,
            error_code="iiko_events_too_large",
        )

    async def download_event_types(self, token: str, destination: Path) -> CatalogDownload:
        return await self._download_file(
            "events/metadata",
            token,
            destination,
            params={},
            accept="application/xml",
            max_bytes=32 * 1024 * 1024,
            error_code="iiko_event_types_too_large",
        )

    async def download_daily_sales(
        self, token: str, destination: Path, *, query: DailySalesQuery
    ) -> CatalogDownload:
        return await self._download_file(
            "v2/reports/olap",
            token,
            destination,
            params={},
            json_body=query.iiko_body(),
            max_bytes=self._settings.iiko_olap_sales_max_response_bytes,
            error_code="iiko_olap_sales_too_large",
        )

    async def _download_file(
        self,
        endpoint: DataEndpoint,
        token: str,
        destination: Path,
        *,
        params: dict[str, str] | list[tuple[str, str]],
        max_bytes: int,
        error_code: str,
        accept: str = "application/json",
        json_body: dict | None = None,
    ) -> CatalogDownload:
        size = 0
        digest = hashlib.sha256()
        async with self._stream_response(
            endpoint,
            params=params,
            headers={"Cookie": f"key={token}", "Accept": accept},
            json_body=json_body,
        ) as response:
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise IikoError(
                            error_code,
                            "Ответ iiko превысил допустимый размер загрузки.",
                        )
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
        return CatalogDownload(size_bytes=size, sha256=digest.hexdigest())

    def forget_session(self) -> None:
        self._http.cookies.clear()

    async def aclose(self) -> None:
        await self._http.aclose()
