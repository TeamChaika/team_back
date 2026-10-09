"""Tenant collectors run in fresh processes with immutable runtime configuration."""

from tests.test_tenant_runtime_wiring import run_startup, tenant_environment


def test_sync_read_write_templates_and_locks_are_process_scoped():
    run_startup(
        """
from unittest.mock import MagicMock
from app.tenancy.sql import ANALYTICS_SCHEMA
from app.tenancy.config import load_runtime
from app.sync_references import register_sources, Source, LOCK_ID
from app.scheduler import LEADER_LOCK
from app.manual_sync import job_lock
from app.sync_accounts import missing_accounts
from app.services.iiko_events import EVENT_ZONE
from app.sync_sales_history import DIRECTORY, REPORTS, ZONE
assert LOCK_ID != 7623011091001 and LEADER_LOCK != 7623011091051
assert str(EVENT_ZONE) == str(ZONE) == 'Asia/Tokyo'
assert DIRECTORY.is_relative_to(load_runtime().runtime_directory)
assert REPORTS.is_relative_to(load_runtime().runtime_directory)
db = MagicMock()
db.execute.return_value.fetchone.return_value = None
register_sources(db, [Source('primary','Own Chain','https://tenant.example/resto/api','a'*64)])
queries = [call.args[0] for call in db.execute.call_args_list]
assert all(ANALYTICS_SCHEMA in query for query in queries)
assert all('chaika.' not in query for query in queries)
job_lock(db, 'references')
assert db.execute.call_args.args[1][0].startswith(load_runtime().key + ':')
""".replace("from app.sync_accounts import missing_accounts\n", "")
    )


def test_collector_client_supports_own_port_or_socket_and_rejects_foreign():
    code = (
        '\n'
        'from unittest.mock import patch\n'
        'from app.tenancy.io import collector_client, collector_url\n'
        'from app.sync_references import check_local_api, SyncError\n'
        'from app.tenancy.config import load_runtime\n'
        "with patch('app.tenancy.io.httpx.Client') as client, "
        "patch('app.tenancy.io.httpx.HTTPTransport') as transport:\n"
        '    collector_client(base_url=check_local_api(collector_url()), timeout=5)\n'
        "    assert client.call_args.kwargs['base_url'] == collector_url()\n"
        "    assert client.call_args.kwargs['trust_env'] is False\n"
        '    if load_runtime().collector_port is None:\n'
        "        assert transport.call_args.kwargs['uds'] == "
        'str(load_runtime().collector_socket)\n'
        "for url in ('http://127.0.0.1:18011', 'http://evil.example'):\n"
        '    try:\n'
        '        check_local_api(url)\n'
        '    except SyncError:\n'
        '        pass\n'
        '    else:\n'
        "        raise AssertionError('foreign collector accepted')\n"
    )
    run_startup(code)
    env = tenant_environment()
    env["RESTCONTROL_TENANT_COLLECTOR_PORT"] = "18010"
    run_startup(code, env)


def test_main_capture_services_ignore_foreign_directory_overrides():
    run_startup("""
from app.main import create_app
from app.tenancy.config import load_runtime
from pathlib import Path
from unittest.mock import patch
import asyncio
seen = []
class Auth:
    def __init__(self,*args,**kwargs): pass
    async def aclose(self): pass
class Capture:
    def __init__(self,*args,**kwargs):
        path = kwargs.get('directory') or (args[-1] if isinstance(args[-1],Path) else None)
        if path is not None: seen.append(path)
    async def aclose(self): pass
names=['IikoConnectionsService','IikoTopologyService','IikoProductsService','IikoGroupsService','IikoAssemblyService','IikoInvoicesService','IikoStoresService','IikoBalancesService','IikoStoreBalancesService','IikoEmployeesService','IikoEmployeeRolesService','IikoWriteoffsService','IikoTransfersService','IikoOutgoingInvoicesService','IikoCashShiftsService','IikoDictionariesService','IikoOlapColumnsService','IikoDailySalesService']
from contextlib import ExitStack
with ExitStack() as stack:
    stack.enter_context(patch('app.main.IikoAuthService', Auth))
    stack.enter_context(patch('app.main.tenant_connect'))
    for name in names: stack.enter_context(patch('app.main.'+name,Capture))
    app=create_app(products_directory=Path('/tmp/foreign-products'))
    async def check():
        async with app.router.lifespan_context(app): pass
    asyncio.run(check())
assert len(seen) == 18
assert all(path.is_relative_to(load_runtime().runtime_directory) for path in seen)
""")
