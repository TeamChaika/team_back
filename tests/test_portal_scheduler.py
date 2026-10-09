"""Portal lifecycle respects the tenant fleet's exclusive scheduler ownership."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import portal, scheduler
from app.core.config import Settings
from app.documents.config import DocumentSettings
from app.tenancy.config import TenantRuntime
from app.web.settings import WebSettings
from tests.test_tenant_runtime import runtime as tenant_runtime


@pytest.mark.parametrize("mode", ["legacy", "tenant"])
@pytest.mark.parametrize("enabled", [False, True])
def test_portal_only_owns_legacy_scheduler(monkeypatch, mode, enabled):
    runtime = tenant_runtime() if mode == "tenant" else TenantRuntime()
    monkeypatch.setattr(portal, "load_runtime", lambda: runtime)
    repo = MagicMock(runtime=runtime)
    payments = MagicMock(reconcile_due=AsyncMock()) if mode == "tenant" else None
    documents = MagicMock(client=MagicMock(aclose=AsyncMock()))
    # Model the HTTP document client, which has no local native dispatch/pool.
    del documents.dispatch
    del documents.database
    process = object()
    start = MagicMock(return_value=process)
    stop = MagicMock()
    monkeypatch.setattr(scheduler, "start_process", start)
    monkeypatch.setattr(scheduler, "stop_process", stop)
    app = portal.create_portal(
        Settings(_env_file=None, sync_enabled=enabled, live_sales_enabled=False),
        WebSettings(_env_file=None, anon_key="test"),
        repository=repo,
        payment_service=payments,
        document_service=documents,
        document_settings=DocumentSettings(_env_file=None, native_enabled=False),
    )

    async def lifecycle():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)

    asyncio.run(lifecycle())
    if mode == "legacy" and enabled:
        start.assert_called_once_with()
        stop.assert_called_once_with(process)
    else:
        start.assert_not_called()
        stop.assert_not_called()
