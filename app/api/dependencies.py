"""Зависимости, доступные обработчикам HTTP-запросов."""

from typing import Annotated, cast

from fastapi import Depends, Request

from app.core.config import Settings
from app.services.iiko_assembly import IikoAssemblyService
from app.services.iiko_auth import IikoAuthService
from app.services.iiko_balances import IikoBalancesService
from app.services.iiko_cash_shifts import IikoCashShiftsService
from app.services.iiko_connections import IikoConnectionsService
from app.services.iiko_employee_roles import IikoEmployeeRolesService
from app.services.iiko_employees import IikoEmployeesService
from app.services.iiko_groups import IikoGroupsService
from app.services.iiko_invoices import IikoInvoicesService
from app.services.iiko_olap_columns import IikoOlapColumnsService
from app.services.iiko_olap_sales import IikoDailySalesService
from app.services.iiko_products import IikoProductsService
from app.services.iiko_store_balances import IikoStoreBalancesService
from app.services.iiko_stores import IikoStoresService
from app.services.iiko_topology import IikoTopologyService
from app.services.iiko_writeoffs import IikoWriteoffsService


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


SettingsDependency = Annotated[Settings, Depends(get_settings)]


def get_iiko_auth(request: Request) -> IikoAuthService:
    return cast(IikoAuthService, request.app.state.iiko_auth)


IikoAuthDependency = Annotated[IikoAuthService, Depends(get_iiko_auth)]


def get_iiko_products(request: Request) -> IikoProductsService:
    return cast(IikoProductsService, request.app.state.iiko_products)


IikoProductsDependency = Annotated[IikoProductsService, Depends(get_iiko_products)]


def get_iiko_groups(request: Request) -> IikoGroupsService:
    return cast(IikoGroupsService, request.app.state.iiko_groups)


IikoGroupsDependency = Annotated[IikoGroupsService, Depends(get_iiko_groups)]


def get_iiko_assembly(request: Request) -> IikoAssemblyService:
    return cast(IikoAssemblyService, request.app.state.iiko_assembly)


IikoAssemblyDependency = Annotated[IikoAssemblyService, Depends(get_iiko_assembly)]


def get_iiko_invoices(request: Request) -> IikoInvoicesService:
    return cast(IikoInvoicesService, request.app.state.iiko_invoices)


IikoInvoicesDependency = Annotated[IikoInvoicesService, Depends(get_iiko_invoices)]


def get_iiko_stores(request: Request) -> IikoStoresService:
    return cast(IikoStoresService, request.app.state.iiko_stores)


IikoStoresDependency = Annotated[IikoStoresService, Depends(get_iiko_stores)]


def get_iiko_balances(request: Request) -> IikoBalancesService:
    return cast(IikoBalancesService, request.app.state.iiko_balances)


IikoBalancesDependency = Annotated[IikoBalancesService, Depends(get_iiko_balances)]


def get_iiko_store_balances(request: Request) -> IikoStoreBalancesService:
    return cast(IikoStoreBalancesService, request.app.state.iiko_store_balances)


IikoStoreBalancesDependency = Annotated[IikoStoreBalancesService, Depends(get_iiko_store_balances)]


def get_iiko_employees(request: Request) -> IikoEmployeesService:
    return cast(IikoEmployeesService, request.app.state.iiko_employees)


IikoEmployeesDependency = Annotated[IikoEmployeesService, Depends(get_iiko_employees)]


def get_iiko_employee_roles(request: Request) -> IikoEmployeeRolesService:
    return cast(IikoEmployeeRolesService, request.app.state.iiko_employee_roles)


IikoEmployeeRolesDependency = Annotated[IikoEmployeeRolesService, Depends(get_iiko_employee_roles)]


def get_iiko_writeoffs(request: Request) -> IikoWriteoffsService:
    return cast(IikoWriteoffsService, request.app.state.iiko_writeoffs)


IikoWriteoffsDependency = Annotated[IikoWriteoffsService, Depends(get_iiko_writeoffs)]


def get_iiko_cash_shifts(request: Request) -> IikoCashShiftsService:
    return cast(IikoCashShiftsService, request.app.state.iiko_cash_shifts)


IikoCashShiftsDependency = Annotated[IikoCashShiftsService, Depends(get_iiko_cash_shifts)]


def get_iiko_connections(request: Request) -> IikoConnectionsService:
    return cast(IikoConnectionsService, request.app.state.iiko_connections)


IikoConnectionsDependency = Annotated[IikoConnectionsService, Depends(get_iiko_connections)]


def get_iiko_olap_columns(request: Request) -> IikoOlapColumnsService:
    return cast(IikoOlapColumnsService, request.app.state.iiko_olap_columns)


IikoOlapColumnsDependency = Annotated[IikoOlapColumnsService, Depends(get_iiko_olap_columns)]


def get_iiko_daily_sales(request: Request) -> IikoDailySalesService:
    return cast(IikoDailySalesService, request.app.state.iiko_daily_sales)


IikoDailySalesDependency = Annotated[IikoDailySalesService, Depends(get_iiko_daily_sales)]


def get_iiko_topology(request: Request) -> IikoTopologyService:
    return cast(IikoTopologyService, request.app.state.iiko_topology)


IikoTopologyDependency = Annotated[IikoTopologyService, Depends(get_iiko_topology)]
