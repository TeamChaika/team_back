"""Public website API; integration routes stay on the private collector."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from time import perf_counter
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware

from app.commercial_invoices.historical import historical_pdf, historical_pdf_allowed
from app.commercial_invoices.runtime import build_service as commercial_service
from app.core.config import Settings
from app.documents.config import DocumentSettings
from app.documents.service import DocumentService
from app.manual_sync import RunRequest
from app.schemas.sales_drilldown import DiscountDetailsQuery
from app.services.order_topology import read_topology
from app.services.sales_drilldown import discount_details
from app.services.sync_jobs import SyncJobError
from app.tenancy.config import load_runtime
from app.web.administration import create_admin_router
from app.web.assistant import AssistantSettings, Message, answer_question
from app.web.assistant_store import AssistantStore
from app.web.auth import ACCESS_COOKIE, REFRESH_COOKIE, Auth, Login, LoginLimiter, login_candidates
from app.web.commercial_invoices import create_commercial_invoices_router
from app.web.coverage import ZONE
from app.web.deposits import DepositsClient, create_deposits_router
from app.web.documents import DocumentsClient, create_documents_router
from app.web.employees import EmployeeCommand, EmployeeEditor, owner
from app.web.indicators import IndicatorQuery, IndicatorService, catalog
from app.web.live_sales import LiveSales
from app.web.password_policy import require_personal_password
from app.web.password_recovery import create_recovery_router
from app.web.permissions import (
    IIKO_SECTIONS,
    require_admin,
    require_section,
    require_warehouse_section,
    section_for_path,
    sections_for,
    warehouse_capabilities,
)
from app.web.profile import create_profile_router
from app.web.repository import Repository, Scope, serial
from app.web.settings import WebSettings


def create_portal(
    settings=None,
    web_settings=None,
    *,
    auth_transport=None,
    repository=None,
    assistant_store=None,
    assistant_settings=None,
    assistant_transport=None,
    employee_editor=None,
    indicator_service=None,
    deposits_transport=None,
    documents_transport=None,
    document_service=None,
    document_settings=None,
    saas_auth_repository=None,
    payment_service=None,
    administration_store=None,
):
    runtime = load_runtime()
    settings = settings or Settings()
    web = web_settings or WebSettings()
    repo = repository or Repository(settings)
    if runtime.mode == "tenant" and getattr(repo, "runtime", None) != runtime:
        raise ValueError("Tenant portal requires a repository bound to its startup runtime")
    limiter = LoginLimiter(web.max_login_attempts)
    chats = assistant_store or AssistantStore(repo)
    employees = employee_editor or EmployeeEditor(settings, repo)
    live_sales = LiveSales(settings) if settings.live_sales_enabled else None
    indicators = indicator_service or IndicatorService(settings)
    document_config = document_settings or DocumentSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.auth = Auth(web, transport=auth_transport)
        app.state.deposits = (
            payment_service
            if runtime.mode == "tenant"
            else DepositsClient(web.deposits_api_url, transport=deposits_transport)
        )
        app.state.documents = document_service or (
            DocumentService(document_config)
            if document_config.native_enabled and web.documents_enabled
            else DocumentsClient(
                web.documents_api_url, web.documents_enabled, transport=documents_transport
            )
        )
        app.state.commercial_invoices = commercial_service(app.state.documents, document_config)
        scheduler = None

        async def start_background():
            nonlocal scheduler
            # Warm the shared intraday cache before long-running background syncs start.
            if live_sales:
                try:
                    await run_in_threadpool(live_sales.get)
                except HTTPException:
                    pass  # LiveSales logs the fixed error code; requests can retry later.
            # Tenant fleet starts its scheduler only after initial sync is ready.
            # The portal must not create a second scheduler ahead of that check.
            if settings.sync_enabled and runtime.mode == "legacy":
                from app.scheduler import start_process

                scheduler = start_process()

        background = None
        document_worker = None
        payment_worker = None
        try:
            if repository is None or runtime.mode == "tenant":
                await run_in_threadpool(repo.open)
            background = asyncio.create_task(start_background())
            if (
                web.documents_enabled
                and document_config.native_enabled
                and document_config.worker_enabled
            ):
                from app.documents.runtime import supervise

                document_worker = asyncio.create_task(supervise())
            if payment_service is not None:

                async def reconcile_payments():
                    while True:
                        try:
                            await payment_service.reconcile_due()
                        except Exception:
                            logging.getLogger(__name__).warning(
                                "Tenant payment reconciliation unavailable"
                            )
                        await asyncio.sleep(30)

                payment_worker = asyncio.create_task(reconcile_payments())
            yield
        finally:
            if payment_worker is not None:
                payment_worker.cancel()
                await asyncio.gather(payment_worker, return_exceptions=True)
            if document_worker is not None:
                document_worker.cancel()
                await asyncio.gather(document_worker, return_exceptions=True)
            if background is not None:
                await background
            if scheduler is not None:
                from app.scheduler import stop_process

                await run_in_threadpool(stop_process, scheduler)
            await app.state.auth.client.aclose()
            if runtime.mode == "legacy":
                await app.state.deposits.client.aclose()
            if hasattr(app.state.documents, "dispatch"):
                await run_in_threadpool(app.state.documents.close)
            else:
                await app.state.documents.client.aclose()
            if hasattr(indicators, "close"):
                await run_in_threadpool(indicators.close)
            if repository is None:
                await run_in_threadpool(repo.close)

    app = FastAPI(
        title="RestControl" if runtime.mode == "tenant" else "Chaika Team",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.tenant_runtime = runtime
    app.state.saas_auth_repository = saas_auth_repository
    if runtime.mode == "tenant":
        from urllib.parse import urlsplit

        from starlette.middleware.trustedhost import TrustedHostMiddleware

        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=[urlsplit(runtime.api_origin).hostname]
        )
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=4)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[web.origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"]
        if runtime.mode == "tenant"
        else ["GET", "POST"],
        allow_headers=["Content-Type", "X-CSRF-Token"]
        if runtime.mode == "tenant"
        else ["Content-Type"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        started = perf_counter()
        payment_guest = bool(
            runtime.mode == "tenant"
            and runtime.payment_origin
            and request.headers.get("origin") == runtime.payment_origin
        )
        if payment_guest:
            from app.tenant_payments.guest_boundary import guest_method_allowed

            if not guest_method_allowed(request.url.path, request.method):
                return JSONResponse(status_code=403, content={"detail": "Этот раздел недоступен."})
            # Guest capability requests never carry staff authentication downstream.
            request.scope["headers"] = [
                (key, value)
                for key, value in request.scope["headers"]
                if key.lower() not in {b"cookie", b"authorization", b"x-csrf-token"}
            ]
        response = await call_next(request)
        if payment_guest:
            for header in ("access-control-allow-credentials", "set-cookie"):
                if header in response.headers:
                    del response.headers[header]
            response.headers["Access-Control-Allow-Origin"] = runtime.payment_origin
            response.headers.append("Vary", "Origin")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = (
            "no-referrer" if runtime.mode == "tenant" else "same-origin"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            timings = getattr(request.state, "timings", [])
            timings.append(("total", (perf_counter() - started) * 1000))
            response.headers["Server-Timing"] = ", ".join(
                f"{name};dur={duration:.1f}" for name, duration in timings
            )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default errors may contain submitted passwords/tokens.
        return JSONResponse(
            status_code=422, content={"detail": "Проверьте формат полей и выбранный период."}
        )

    @app.exception_handler(psycopg.Error)
    async def database_error(request, exc):
        return JSONResponse(
            status_code=503,
            content={"detail": "База данных временно недоступна. Повторите запрос позже."},
        )

    @app.exception_handler(SyncJobError)
    async def topology_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    async def access(
        request: Request,
        department_id: Annotated[list[UUID] | None, Query(max_length=100)] = None,
    ):
        started = perf_counter()
        from app.web.auth import tenant_actor_from_request

        actor = await tenant_actor_from_request(request, runtime)
        user_id = (
            actor.auth_user_id
            if actor
            else await request.app.state.auth.user(request.cookies.get(ACCESS_COOKIE))
        )
        authenticated = perf_counter()
        selection = tuple(sorted(set(department_id or []), key=str))
        selected = selection[0] if len(selection) == 1 else selection or None
        scope = (
            await run_in_threadpool(repo.actor_scope, actor, selected)
            if actor
            else await run_in_threadpool(repo.scope, user_id, selected)
        )
        require_personal_password(scope.user)
        require_section(scope.user, section_for_path(request.url.path))
        require_warehouse_section(scope, section_for_path(request.url.path))
        request.state.timings = [
            ("auth", (authenticated - started) * 1000),
            ("permissions", (perf_counter() - authenticated) * 1000),
        ]
        return scope

    Access = Annotated[Scope, Depends(access)]

    async def portal_access(request: Request):
        started = perf_counter()
        from app.web.auth import tenant_actor_from_request

        actor = await tenant_actor_from_request(request, runtime)
        user_id = (
            actor.auth_user_id
            if actor
            else await request.app.state.auth.user(request.cookies.get(ACCESS_COOKIE))
        )
        authenticated = perf_counter()
        scope = (
            await run_in_threadpool(repo.actor_scope, actor)
            if actor
            else await run_in_threadpool(repo.portal_scope, user_id)
        )
        if (request.method, request.url.path) not in {
            ("GET", "/api/me"),
            ("POST", "/api/profile/password"),
        }:
            require_personal_password(scope.user)
        request.state.timings = [
            ("auth", (authenticated - started) * 1000),
            ("permissions", (perf_counter() - authenticated) * 1000),
        ]
        return scope

    PortalAccess = Annotated[Scope, Depends(portal_access)]

    async def deposit_access(request: Request):
        scope = await portal_access(request)
        require_section(scope.user, "deposits")
        require_warehouse_section(scope, "deposits")
        return scope

    if runtime.mode == "tenant":
        if payment_service is None:
            raise ValueError("Full tenant portal requires its own payments service")
        from app.tenancy.payment_bootstrap import (
            TenantPaymentAdministration,
            principal_from_scope,
        )
        from app.tenant_payments.routes import create_router as create_tenant_payment_router
        from app.web.administration import Administration

        payment_administration = TenantPaymentAdministration(payment_service, repo)

        async def payment_principal(request: Request):
            return principal_from_scope(runtime, await portal_access(request))

        def payment_origin(request):
            request.app.state.auth.check_origin(request)

        app.include_router(
            create_tenant_payment_router(
                payment_service,
                payment_principal,
                payment_origin,
                company_users=repo.deposit_users,
                validate_user=payment_administration.validate_user,
            )
        )
        administration_store = administration_store or Administration(
            repo, payments=payment_administration
        )
    else:
        app.include_router(create_deposits_router(deposit_access, repo))
    app.include_router(create_documents_router(portal_access))
    app.include_router(create_commercial_invoices_router(portal_access))
    app.include_router(create_admin_router(portal_access, repo, web, store=administration_store))
    app.include_router(create_profile_router(portal_access, repo))
    app.include_router(create_recovery_router(repo, web, transport=auth_transport))

    def check_period(start, end):
        if start > end or (end - start).days > 30 or end > datetime.now(ZONE).date():
            raise HTTPException(422, "Выберите период от одного до 31 дня.")

    @app.get("/api/health")
    async def health():
        # Liveness only; this does not certify Supabase, data freshness or permissions.
        return {"status": "ok"}

    @app.post("/api/auth/login")
    async def login(payload: Login, request: Request, response: Response):
        auth = request.app.state.auth
        auth.check_origin(request)
        limiter.check(request.client.host if request.client else "unknown")
        candidates = login_candidates(payload.email)
        email = (
            await run_in_threadpool(repo.resolve_login_email, candidates)
            if len(candidates) > 1
            else candidates[0]
        )
        result = await auth.call(
            "POST",
            "token?grant_type=password",
            json={"email": email, "password": payload.password.get_secret_value()},
        )
        user_id = await auth.user(result["access_token"])
        scope = await run_in_threadpool(repo.portal_scope, user_id)
        auth.cookies(response, result)
        return {
            "name": scope.user["display_name"],
            "password_change_required": bool(scope.user.get("password_change_required")),
        }

    @app.post("/api/auth/refresh")
    async def refresh(request: Request, response: Response):
        auth = request.app.state.auth
        auth.check_origin(request)
        from app.web.auth import tenant_actor_from_request

        actor = await tenant_actor_from_request(request, runtime)
        if actor is not None:
            return {"status": "ok", "password_change_required": False}
        token = request.cookies.get(REFRESH_COOKIE)
        if not token or len(token) > 8192:
            raise HTTPException(401, "Войдите в систему.")
        result = await auth.call(
            "POST", "token?grant_type=refresh_token", json={"refresh_token": token}
        )
        user_id = await auth.user(result["access_token"])
        scope = await run_in_threadpool(repo.portal_scope, user_id)
        auth.cookies(response, result)
        return {
            "status": "ok",
            "password_change_required": bool(scope.user.get("password_change_required")),
        }

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        auth = request.app.state.auth
        auth.check_origin(request)
        from app.web.auth import tenant_actor_from_request

        actor = await tenant_actor_from_request(request, runtime)
        if actor is not None:
            await run_in_threadpool(
                request.app.state.saas_auth_repository.tenant_logout,
                request.cookies.get("saas_tenant_session", ""),
            )
            delegated = JSONResponse({"status": "logged_out"})
            delegated.delete_cookie(
                "saas_tenant_session", path="/", secure=True, httponly=True, samesite="strict"
            )
            return delegated
        token = request.cookies.get(ACCESS_COOKIE)
        response = JSONResponse({"status": "logged_out"})
        if token:
            try:
                await auth.call(
                    "POST", "logout?scope=local", headers={"Authorization": "Bearer " + token}
                )
            except HTTPException as exc:
                if exc.status_code != 401:
                    response = JSONResponse(
                        {
                            "detail": (
                                "Локальный выход выполнен; отзыв сессии в Supabase не подтверждён."
                            )
                        },
                        status_code=503,
                    )
        response.delete_cookie(ACCESS_COOKIE, path="/")
        response.delete_cookie(REFRESH_COOKIE, path="/")
        return response

    @app.get("/api/me")
    def me(scope: PortalAccess):
        if scope.user.get("password_change_required"):
            return {
                "user": {
                    "id": str(scope.user["id"]),
                    "display_name": scope.user["display_name"],
                    "role": scope.user["role"],
                    "password_change_required": True,
                },
                "departments": [],
                "sales_dates": [],
                "balance_dates": [],
                "sections": [],
                "modules": [],
                "can_manage": False,
                "documents_enabled": False,
                "live_sales_enabled": False,
                "today": datetime.now(ZONE).date().isoformat(),
            }
        return {
            **repo.metadata(scope),
            "warehouse_scope": {
                "mode": scope.user.get("warehouse_scope_mode", "all"),
                "warehouse_ids": [str(store) for store in scope.store_ids],
            },
            "warehouse_capabilities": warehouse_capabilities(scope),
            "today": datetime.now(ZONE).date().isoformat(),
            "live_sales_enabled": (
                bool(live_sales) or (scope.warehouse_restricted and bool(scope.store_ids))
            )
            and bool(IIKO_SECTIONS.intersection(sections_for(scope.user))),
            "sections": sections_for(scope.user),
            "can_manage": bool(scope.user.get("is_portal_admin")),
            "documents_enabled": web.documents_enabled,
            "modules": (["iiko"] if IIKO_SECTIONS.intersection(sections_for(scope.user)) else [])
            + (["deposits"] if "deposits" in sections_for(scope.user) else []),
        }

    @app.get("/api/sales/{kind}")
    def sales(
        kind: str,
        scope: Access,
        start: date,
        end: date,
        dish_id: Annotated[str | None, Query(max_length=200)] = None,
        dish_name: Annotated[str | None, Query(max_length=500)] = None,
    ):
        check_period(start, end)
        if kind not in {"daily", "dishes", "payments", "discounts", "returns", "waiters", "hours"}:
            raise HTTPException(404, "Отчёт не найден.")
        if (dish_id is not None or dish_name is not None) and (
            kind != "dishes" or (dish_id is not None and dish_name is not None)
        ):
            raise HTTPException(422, "Фильтр блюда доступен только в отчёте по блюдам.")
        if (
            live_sales
            and not scope.warehouse_restricted
            and start <= datetime.now(ZONE).date() <= end
        ):
            bundle, metadata = live_sales.get()
            return {
                **repo.sales(
                    scope,
                    kind,
                    start,
                    end,
                    dish_id=dish_id,
                    dish_name=dish_name,
                    live_bundle=bundle,
                ),
                "live": metadata,
            }
        if dish_id is not None or dish_name is not None:
            if kind != "dishes" or (dish_id is not None and dish_name is not None):
                raise HTTPException(422, "Фильтр блюда доступен только в отчёте по блюдам.")
            return repo.sales(scope, kind, start, end, dish_id=dish_id, dish_name=dish_name)
        return repo.sales(scope, kind, start, end)

    @app.get("/api/indicators/filters")
    def indicator_filters(scope: Access):
        return {"filters": catalog(), **repo.indicator_filters(scope)}

    @app.post("/api/indicators/query")
    def indicator_query(payload: IndicatorQuery, request: Request, scope: Access):
        request.app.state.auth.check_origin(request)
        return serial(indicators.get(scope, payload))

    @app.post("/api/indicators/metric/{metric}")
    def indicator_metric(metric: str, payload: IndicatorQuery, request: Request, scope: Access):
        request.app.state.auth.check_origin(request)
        result = serial(indicators.get(scope, payload, metric))
        return JSONResponse(result, status_code=202 if result["status"] == "loading" else 200)

    @app.post("/api/indicators/options/{field}")
    def indicator_options(field: str, payload: IndicatorQuery, request: Request, scope: Access):
        request.app.state.auth.check_origin(request)
        if scope.warehouse_restricted:
            return serial(indicators.warehouse_options(scope, payload, field))
        data = repo.indicator_filters(scope)
        if field not in data["options"]:
            raise HTTPException(422, "Неизвестный фильтр.")
        return {"values": data["options"][field], "sync": data["sync"].get(field)}

    @app.get("/api/overview")
    def overview(
        scope: Access,
        start: date,
        end: date,
        granularity: Literal["day", "week", "month"] = "day",
    ):
        check_period(start, end)
        if start.toordinal() <= (end - start).days + 1:
            raise HTTPException(422, "Для выбранной даты невозможно определить предыдущий период.")
        if (
            live_sales
            and not scope.warehouse_restricted
            and start <= datetime.now(ZONE).date() <= end
        ):
            bundle, metadata = live_sales.get()
            return {
                **repo.overview(scope, start, end, granularity, live_bundle=bundle),
                "live": metadata,
            }
        return repo.overview(scope, start, end, granularity)

    @app.get("/api/purchase-prices")
    def purchase_prices(
        scope: Access,
        kind: Literal["unlinked", "linked", "all"] = "unlinked",
        exclude_household: bool = True,
        recent_only: bool = True,
        include_impact: bool = False,
    ):
        return repo.purchase_prices(
            scope, kind, exclude_household, recent_only=recent_only, include_impact=include_impact
        )

    @app.get("/api/purchase-prices/history")
    def purchase_price_history(
        scope: Access,
        product_id: UUID,
        unit_id: UUID,
        linked: bool = False,
        store_id: UUID | None = None,
    ):
        report = repo.purchase_prices(
            scope, "all", False, (product_id, store_id, unit_id, linked), recent_only=False
        )
        if not report["rows"]:
            raise HTTPException(404, "История цены не найдена или недоступна.")
        return report["rows"][0]

    @app.get("/api/purchase-prices/impact")
    def purchase_price_impact(
        scope: Access,
        product_id: UUID,
        unit_id: UUID,
        store_id: UUID | None = None,
        linked: bool = False,
        analysis_department_id: UUID | None = None,
        all_departments: bool = False,
    ):
        return repo.purchase_impact(
            scope, (product_id, store_id, unit_id, linked), analysis_department_id, all_departments
        )

    @app.get("/api/assistant/status")
    def assistant_status(scope: Access):
        config = assistant_settings or AssistantSettings()
        return {
            "configured": config.configured,
            "provider": config.provider,
            "model": config.model_name,
            "requests_per_hour": config.requests_per_hour,
        }

    @app.get("/api/assistant/conversations")
    def assistant_conversations(scope: Access):
        return chats.list(scope)

    @app.get("/api/assistant/conversations/{conversation_id}")
    def assistant_history(conversation_id: UUID, scope: Access):
        return chats.history(scope, conversation_id)

    @app.post("/api/assistant/messages")
    async def assistant_message(payload: Message, request: Request, scope: Access):
        request.app.state.auth.check_origin(request)
        return await answer_question(
            repo,
            chats,
            scope,
            payload,
            assistant_settings or AssistantSettings(),
            transport=assistant_transport,
        )

    @app.post("/api/discount-details")
    async def discount_drilldown(payload: DiscountDetailsQuery, request: Request, scope: Access):
        request.app.state.auth.check_origin(request)
        if scope.warehouse_restricted:
            return serial(
                await run_in_threadpool(repo.warehouse_sales.discount_details, scope, payload)
            )
        return await run_in_threadpool(
            discount_details,
            settings,
            payload,
            scope.ids,
            allowed_store_ids=list(scope.store_ids) if scope.warehouse_restricted else None,
        )

    @app.get("/api/balance-products")
    def balance_products(
        scope: Access,
        q: str = Query(default="", max_length=150),
        store_id: UUID | None = None,
    ):
        return repo.balance_products(scope, q, store_id)

    @app.get("/api/employees/options")
    def employee_options(scope: Access):
        owner(scope)
        return employees.options(scope)

    @app.get("/api/employees/{employee_id}/edit")
    def employee_edit(employee_id: UUID, scope: Access):
        owner(scope)
        return employees.read(scope, employee_id)

    @app.get("/api/employees/pending")
    def employee_pending(scope: Access):
        owner(scope)
        return employees.pending(scope)

    @app.post("/api/employees/changes/{request_id}/refresh")
    def employee_reconcile(request_id: UUID, request: Request, scope: Access):
        request.app.state.auth.check_origin(request)
        owner(scope)
        return employees.reconcile(scope, request_id)

    @app.post("/api/employees")
    def employee_create(payload: EmployeeCommand, request: Request, scope: Access):
        request.app.state.auth.check_origin(request)
        owner(scope)
        return employees.save(scope, payload)

    @app.post("/api/employees/{employee_id}")
    def employee_update(
        employee_id: UUID, payload: EmployeeCommand, request: Request, scope: Access
    ):
        request.app.state.auth.check_origin(request)
        owner(scope)
        return employees.save(scope, payload, employee_id)

    @app.get("/api/resources/{resource}")
    def resources(
        resource: str,
        scope: Access,
        start: date | None = None,
        end: date | None = None,
        q: str = Query(default="", max_length=150),
        offset: int = Query(default=0, ge=0, le=100000),
        status: Literal["NEW", "PROCESSED", "DELETED"] | None = None,
        store_id: UUID | None = None,
        product_id: UUID | None = None,
        sort: Literal["amount", "sum"] = "sum",
        direction: Literal["asc", "desc"] = "desc",
    ):
        if bool(start) != bool(end):
            raise HTTPException(422, "Укажите обе даты.")
        if start:
            check_period(start, end)
        if resource == "balances":
            return repo.balances(scope, q, offset, store_id, product_id, sort, direction)
        if resource == "events":
            if not start:
                raise HTTPException(422, "Для событий нужен период.")
            return repo.events(scope, start, end, q, offset)
        return repo.resources(scope, resource, start, end, q, offset, status=status)

    @app.get("/api/resources/{resource}/{item_id}")
    def detail(resource: str, item_id: UUID, request: Request, scope: Access):
        result = repo.detail(scope, resource, item_id)
        if resource == "outgoing":
            result["header"]["can_invoice_pdf"] = historical_pdf_allowed(
                repo, request.app.state.commercial_invoices, scope, item_id
            )
        return result

    @app.get("/api/commercial-invoices/existing-outgoing/{item_id}/pdf")
    async def existing_invoice_pdf(item_id: UUID, request: Request, scope: PortalAccess):
        require_section(scope.user, "outgoing")
        analytical = (
            await run_in_threadpool(repo.actor_scope, scope.actor)
            if scope.actor
            else await run_in_threadpool(repo.scope, scope.user["id"], None)
        )
        result = await run_in_threadpool(
            historical_pdf, repo, request.app.state.commercial_invoices, analytical, item_id
        )
        return Response(
            result,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="invoice-{item_id}.pdf"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/topology")
    def topology(
        scope: Access,
        source_id: str,
        day: date,
        order_number: int = Query(ge=1),
        order_id: UUID | None = None,
    ):
        if source_id not in scope.rms_ids:
            raise HTTPException(403, "Нет доступа к этому ресторану.")
        return read_topology(settings, source_id, day, order_number, order_id=order_id)

    @app.post("/api/status/sync/{job}/run", status_code=202)
    def run_sync(job: str, body: RunRequest, request: Request, scope: Access):
        require_admin(scope.user)
        request.app.state.auth.check_origin(request)
        return repo.manual_sync(scope, job, body.request_id)

    @app.get("/api/status")
    def status(scope: Access):
        return repo.status(scope)

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Маршрут не найден.")
        root = web.frontend_dir.resolve()
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            raise HTTPException(404)
        target = candidate if candidate.is_file() else root / "index.html"
        if not target.is_file():
            if path == "":
                # A standalone API image has no React build. Platform probes still need HTTP.
                return JSONResponse({"service": "Chaika Team API", "health": "/api/health"})
            raise HTTPException(503, "Интерфейс ещё собирается.")
        return FileResponse(
            target, headers={"Cache-Control": "no-store"} if target.suffix == ".html" else None
        )

    return app


app = create_portal() if load_runtime().mode == "legacy" else None
