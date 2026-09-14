"""Общее место подключения маршрутов API версии 1."""

from fastapi import APIRouter

from app.api.routes.iiko import router as iiko_router
from app.api.routes.iiko_assembly import router as iiko_assembly_router
from app.api.routes.iiko_balances import router as iiko_balances_router
from app.api.routes.iiko_cash_shifts import router as iiko_cash_shifts_router
from app.api.routes.iiko_connections import router as iiko_connections_router
from app.api.routes.iiko_dictionaries import router as iiko_dictionaries_router
from app.api.routes.iiko_employee_roles import router as iiko_employee_roles_router
from app.api.routes.iiko_employees import router as iiko_employees_router
from app.api.routes.iiko_events import router as iiko_events_router
from app.api.routes.iiko_groups import router as iiko_groups_router
from app.api.routes.iiko_invoices import router as iiko_invoices_router
from app.api.routes.iiko_olap_columns import router as iiko_olap_columns_router
from app.api.routes.iiko_olap_sales import router as iiko_olap_sales_router
from app.api.routes.iiko_outgoing import router as iiko_outgoing_router
from app.api.routes.iiko_products import router as iiko_products_router
from app.api.routes.iiko_store_balances import router as iiko_store_balances_router
from app.api.routes.iiko_stores import router as iiko_stores_router
from app.api.routes.iiko_topology import router as iiko_topology_router
from app.api.routes.iiko_transfers import router as iiko_transfers_router
from app.api.routes.iiko_writeoffs import router as iiko_writeoffs_router
from app.api.routes.sync_jobs import router as sync_jobs_router
from app.api.routes.system import router as system_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(system_router)
api_router.include_router(iiko_router)
api_router.include_router(iiko_products_router)
api_router.include_router(iiko_groups_router)
api_router.include_router(iiko_assembly_router)
api_router.include_router(iiko_invoices_router)
api_router.include_router(iiko_outgoing_router)
api_router.include_router(iiko_stores_router)
api_router.include_router(iiko_balances_router)
api_router.include_router(iiko_store_balances_router)
api_router.include_router(iiko_employees_router)
api_router.include_router(iiko_employee_roles_router)
api_router.include_router(iiko_dictionaries_router)
api_router.include_router(iiko_writeoffs_router)
api_router.include_router(iiko_transfers_router)
api_router.include_router(iiko_cash_shifts_router)
api_router.include_router(iiko_connections_router)
api_router.include_router(iiko_olap_columns_router)
api_router.include_router(iiko_olap_sales_router)
api_router.include_router(iiko_topology_router)
api_router.include_router(sync_jobs_router)

api_router.include_router(iiko_events_router)
