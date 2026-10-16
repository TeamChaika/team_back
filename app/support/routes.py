"""Authenticated support routes intentionally independent of business entitlements."""

import os
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .capture import configure_capture
from .models import Context, EventCreate, Status, StatusWrite, TicketCreate, screen_for_path
from .store import SupportStore


def client_router(prefix, store, context_dependency):
    router = APIRouter(prefix=prefix)
    dependency = Depends(context_dependency)

    @router.get("/tickets")
    def listing(
        context=dependency,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0, le=100000),
    ):
        return store.listing(context, limit=limit, offset=offset)

    @router.get("/tickets/{ticket_id}")
    def detail(ticket_id: UUID, context=dependency):
        return store.detail(ticket_id, context)

    @router.post("/tickets")
    def create(body: TicketCreate, context=dependency):
        return store.create_ticket(context, body)

    @router.post("/events")
    def event(body: EventCreate, context=dependency):
        return store.create_event(context, body)

    return router


def mount_primary(app, repo, company_name="Чайка", store=None):
    from app.web.auth import ACCESS_COOKIE

    company_id = os.environ.get("SUPPORT_COMPANY_ID", "primary-dashboard")
    company_name = os.environ.get("SUPPORT_COMPANY_NAME", company_name)
    store = store or SupportStore(os.environ.get("SUPPORT_DATABASE_URL", ""))
    app.state.support_store = store
    capture = configure_capture(store, Context(company_id, company_name, "system"))
    app.state.support_capture = capture

    async def context(request: Request):
        user_id = await request.app.state.auth.user(request.cookies.get(ACCESS_COOKIE))
        identity = await run_in_threadpool(repo.support_identity, user_id)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            request.app.state.auth.check_origin(request)
        value = Context(
            company_id,
            company_name,
            str(identity["id"]),
            identity.get("display_name", "Сотрудник"),
        )
        request.state.support_context = value
        return value

    app.include_router(client_router("/api/support", store, context))

    @app.middleware("http")
    async def capture_errors(request, call_next):
        request_id = uuid4()
        request.state.support_request_id = request_id
        captured = False
        try:
            response = await call_next(request)
        except Exception as exc:
            if request.url.path.startswith("/api/") and not request.url.path.startswith(
                "/api/support"
            ):
                capture.submit(
                    getattr(
                        request.state,
                        "support_context",
                        Context(company_id, company_name, "system"),
                    ),
                    code="unhandled_exception",
                    screen=screen_for_path(request.url.path),
                    status_code=500,
                    request_id=request_id,
                    exception=exc,
                )
            captured = True
            response = JSONResponse({"detail": "Внутренняя ошибка сервера"}, status_code=500)
        response.headers["X-Request-ID"] = str(request_id)
        if (
            not captured
            and response.status_code >= 500
            and request.url.path.startswith("/api/")
            and not request.url.path.startswith("/api/support")
        ):
            capture.submit(
                getattr(
                    request.state, "support_context", Context(company_id, company_name, "system")
                ),
                code="http_5xx",
                screen=screen_for_path(request.url.path),
                status_code=response.status_code,
                request_id=request_id,
            )
        return response


def mount_central(app, repo, owner, store=None):
    from app.saas_admin.auth_validation import csrf_matches
    from app.saas_admin.repository import Problem

    store = store or SupportStore(os.environ.get("SUPPORT_DATABASE_URL", getattr(repo, "dsn", "")))
    app.state.support_store = store
    app.state.support_capture = configure_capture(store)

    def tenant_context(slug: str, request: Request):
        # tenant_session checks exact slug and active membership, accepts valid SSO.
        value = repo.tenant_session(request.cookies.get("saas_tenant_session", ""), slug)
        bound = getattr(request.state, "saas_company", None)
        if bound and str(bound["id"]) != str(value["company"]["id"]):
            raise Problem(403, "tenant_boundary", "Другая компания")
        if request.method not in {"GET", "HEAD"} and not csrf_matches(
            request.headers.get("x-csrf-token", ""), value["csrf_token"]
        ):
            raise Problem(403, "csrf_failed", "Обновите страницу")
        context = Context(
            str(value["company"]["id"]),
            value["company"]["name"],
            str(value["actor"]["auth_user_id"]),
            value["actor"].get("display_name", value["user"].get("display_name", "Сотрудник")),
        )
        request.state.support_context = context
        return context

    app.include_router(client_router("/api/saas-tenant/{slug}/support", store, tenant_context))
    router = APIRouter(prefix="/api/saas-admin/support")
    owner_dependency = Depends(owner)

    @router.get("/tickets")
    def tickets(
        actor=owner_dependency,
        company_id: str | None = Query(None, max_length=100),
        status: Status | None = None,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0, le=100000),
    ):
        return store.listing(company_id=company_id, status=status, limit=limit, offset=offset)

    @router.get("/tickets/{ticket_id}")
    def ticket(ticket_id: UUID, actor=owner_dependency):
        return store.detail(ticket_id)

    @router.post("/tickets/{ticket_id}/status")
    def transition(ticket_id: UUID, body: StatusWrite, actor=owner_dependency):
        return store.transition(
            ticket_id,
            body,
            str(actor["user"]["id"]),
            actor["user"].get("display_name", "Владелец платформы"),
        )

    @router.get("/events")
    def events(
        actor=owner_dependency,
        company_id: str | None = Query(None, max_length=100),
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0, le=100000),
    ):
        return store.listing(table="events", company_id=company_id, limit=limit, offset=offset)

    @router.get("/events/{event_id}")
    def event(event_id: UUID, actor=owner_dependency):
        return store.detail(event_id, table="events")

    app.include_router(router)
