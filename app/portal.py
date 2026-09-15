"""Website API and built frontend, without iiko credentials or integration routes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
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

from app.core.config import Settings
from app.schemas.sales_drilldown import DiscountDetailsQuery
from app.services.order_topology import read_topology
from app.services.sales_drilldown import discount_details
from app.services.sync_jobs import SyncJobError
from app.web.assistant import AssistantSettings, Message, answer_question
from app.web.assistant_store import AssistantStore
from app.web.auth import ACCESS_COOKIE, REFRESH_COOKIE, Auth, Login, LoginLimiter
from app.web.repository import Repository, Scope
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
):
    settings = settings or Settings()
    web = web_settings or WebSettings()
    repo = repository or Repository(settings)
    limiter = LoginLimiter(web.max_login_attempts)
    chats = assistant_store or AssistantStore(repo)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.auth = Auth(web, transport=auth_transport)
        try:
            if repository is None:
                await run_in_threadpool(repo.open)
            yield
        finally:
            await app.state.auth.client.aclose()
            if repository is None:
                await run_in_threadpool(repo.close)

    app = FastAPI(
        title="Chaika Team", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=4)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[web.origin],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        started = perf_counter()
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
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

    async def access(request: Request, department_id: UUID | None = None):
        started = perf_counter()
        user_id = await request.app.state.auth.user(request.cookies.get(ACCESS_COOKIE))
        authenticated = perf_counter()
        scope = await run_in_threadpool(repo.scope, user_id, department_id)
        request.state.timings = [
            ("auth", (authenticated - started) * 1000),
            ("permissions", (perf_counter() - authenticated) * 1000),
        ]
        return scope

    Access = Annotated[Scope, Depends(access)]

    def check_period(start, end):
        if start > end or (end - start).days > 30:
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
        result = await auth.call(
            "POST",
            "token?grant_type=password",
            json={"email": payload.email, "password": payload.password.get_secret_value()},
        )
        user_id = await auth.user(result["access_token"])
        scope = await run_in_threadpool(repo.scope, user_id)
        auth.cookies(response, result)
        return {"name": scope.user["display_name"]}

    @app.post("/api/auth/refresh")
    async def refresh(request: Request, response: Response):
        auth = request.app.state.auth
        auth.check_origin(request)
        token = request.cookies.get(REFRESH_COOKIE)
        if not token or len(token) > 8192:
            raise HTTPException(401, "Войдите в систему.")
        result = await auth.call(
            "POST", "token?grant_type=refresh_token", json={"refresh_token": token}
        )
        user_id = await auth.user(result["access_token"])
        await run_in_threadpool(repo.scope, user_id)
        auth.cookies(response, result)
        return {"status": "ok"}

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        auth = request.app.state.auth
        auth.check_origin(request)
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
    def me(scope: Access):
        return repo.metadata(scope)

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
        if dish_id is not None or dish_name is not None:
            if kind != "dishes" or (dish_id is not None and dish_name is not None):
                raise HTTPException(422, "Фильтр блюда доступен только в отчёте по блюдам.")
            return repo.sales(scope, kind, start, end, dish_id=dish_id, dish_name=dish_name)
        return repo.sales(scope, kind, start, end)

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
        return await run_in_threadpool(discount_details, settings, payload, scope.ids)

    @app.get("/api/balance-products")
    def balance_products(
        scope: Access,
        q: str = Query(default="", max_length=150),
        store_id: UUID | None = None,
    ):
        return repo.balance_products(scope, q, store_id)

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
    ):
        if bool(start) != bool(end):
            raise HTTPException(422, "Укажите обе даты.")
        if start:
            check_period(start, end)
        if resource == "balances":
            return repo.balances(scope, q, offset, store_id, product_id)
        if resource == "events":
            if not start:
                raise HTTPException(422, "Для событий нужен период.")
            return repo.events(scope, start, end, q, offset)
        return repo.resources(scope, resource, start, end, q, offset, status=status)

    @app.get("/api/resources/{resource}/{item_id}")
    def detail(resource: str, item_id: UUID, scope: Access):
        return repo.detail(scope, resource, item_id)

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


app = create_portal()
