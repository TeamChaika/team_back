"""Создание приложения и подключение маршрутов."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.api.router import api_router
from app.core.config import Settings
from app.integrations.iiko.client import IikoClient
from app.integrations.iiko.errors import IikoError
from app.services.iiko_assembly import IikoAssemblyService
from app.services.iiko_auth import IikoAuthService
from app.services.iiko_balances import IikoBalancesService
from app.services.iiko_cash_shifts import IikoCashShiftsService
from app.services.iiko_connections import IikoConnectionsService
from app.services.iiko_dictionaries import IikoDictionariesService
from app.services.iiko_employee_roles import IikoEmployeeRolesService
from app.services.iiko_employees import IikoEmployeesService
from app.services.iiko_groups import IikoGroupsService
from app.services.iiko_invoices import IikoInvoicesService
from app.services.iiko_olap_columns import IikoOlapColumnsService
from app.services.iiko_olap_sales import IikoDailySalesService
from app.services.iiko_outgoing import IikoOutgoingInvoicesService
from app.services.iiko_products import IikoProductsService
from app.services.iiko_store_balances import IikoStoreBalancesService
from app.services.iiko_stores import IikoStoresService
from app.services.iiko_topology import IikoTopologyService
from app.services.iiko_transfers import IikoTransfersService
from app.services.iiko_writeoffs import IikoWriteoffsService
from app.services.sync_jobs import SyncJobError, SyncJobsService


def create_app(
    settings: Settings | None = None,
    *,
    iiko_transport: httpx.AsyncBaseTransport | None = None,
    dictionaries_directory: Path | None = None,
    products_directory: Path | None = None,
    groups_directory: Path | None = None,
    assembly_directory: Path | None = None,
    invoices_directory: Path | None = None,
    outgoing_directory: Path | None = None,
    stores_directory: Path | None = None,
    balances_directory: Path | None = None,
    store_balances_directory: Path | None = None,
    employees_directory: Path | None = None,
    employee_roles_directory: Path | None = None,
    writeoffs_directory: Path | None = None,
    transfers_directory: Path | None = None,
    cash_shifts_directory: Path | None = None,
    connections_directory: Path | None = None,
    olap_columns_directory: Path | None = None,
    olap_sales_directory: Path | None = None,
    topology_directory: Path | None = None,
    sync_directory: Path | None = None,
) -> FastAPI:
    settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        service = IikoAuthService(settings, IikoClient(settings, transport=iiko_transport))
        application.state.iiko_auth = service
        try:
            connections = IikoConnectionsService(
                settings, service, transport=iiko_transport, directory=connections_directory
            )
        except Exception:
            await service.aclose()
            raise
        application.state.iiko_connections = connections
        application.state.iiko_topology = IikoTopologyService(connections, topology_directory)
        application.state.iiko_products = IikoProductsService(settings, service, products_directory)
        application.state.iiko_groups = IikoGroupsService(settings, service, groups_directory)
        application.state.iiko_assembly = IikoAssemblyService(settings, service, assembly_directory)
        application.state.iiko_invoices = IikoInvoicesService(settings, service, invoices_directory)
        application.state.iiko_outgoing = IikoOutgoingInvoicesService(
            settings, service, outgoing_directory
        )
        application.state.iiko_stores = IikoStoresService(settings, service, stores_directory)
        application.state.iiko_balances = IikoBalancesService(settings, service, balances_directory)
        application.state.iiko_employees = IikoEmployeesService(
            settings, service, employees_directory
        )
        application.state.iiko_employee_roles = IikoEmployeeRolesService(
            settings, service, employee_roles_directory
        )
        application.state.iiko_dictionaries = IikoDictionariesService(
            settings, service, dictionaries_directory
        )
        application.state.iiko_store_balances = IikoStoreBalancesService(
            settings, service, store_balances_directory
        )
        application.state.iiko_transfers = IikoTransfersService(
            settings, service, transfers_directory
        )
        application.state.iiko_writeoffs = IikoWriteoffsService(
            settings, service, writeoffs_directory
        )
        application.state.iiko_cash_shifts = IikoCashShiftsService(
            settings, service, cash_shifts_directory
        )
        application.state.iiko_olap_columns = IikoOlapColumnsService(
            settings, service, olap_columns_directory
        )
        application.state.iiko_daily_sales = IikoDailySalesService(
            settings, service, olap_sales_directory
        )
        try:
            yield
        finally:
            try:
                await connections.aclose()
            finally:
                await service.aclose()

    application = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Chaika Team: iikoServer — номенклатура, техкарты, приходные накладные, "
            "склады, денежные балансы, складские остатки, сотрудники, "
            "акты списания, кассовые смены и OLAP-продажи."
        ),
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.sync_jobs = (
        SyncJobsService(settings)
        if sync_directory is None
        else SyncJobsService(settings, sync_directory)
    )

    @application.exception_handler(SyncJobError)
    async def handle_sync_job_error(request: Request, exc: SyncJobError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @application.exception_handler(IikoError)
    async def handle_iiko_error(request: Request, exc: IikoError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    application.include_router(api_router)
    return application


app = create_app()
