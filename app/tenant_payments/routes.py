"""Existing dashboard deposit contracts plus capability-protected guest routes."""

from datetime import datetime
from io import BytesIO
from typing import Annotated
from urllib.parse import parse_qs
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from openpyxl import Workbook
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.tenant_payments.models import DepositFilters, NewDeposit, TerminalInput, VenueInput
from app.web.deposits import XLSX, VenueGrant, VenueRevoke


class ShortGuestAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class GuestAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=128)
    request_id: UUID


class EditDeposit(NewDeposit):
    revision: int = Field(ge=1)


class Callback(BaseModel):
    model_config = ConfigDict(extra="ignore")
    operation_id: UUID


class PaymentRoute(APIRoute):
    """Validation diagnostics cannot echo a submitted terminal key/capability."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                response = await handler(request)
            except RequestValidationError as error:
                errors = [
                    {key: item[key] for key in ("loc", "msg", "type")} for item in error.errors()
                ]
                response = JSONResponse({"detail": errors}, status_code=422)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        return safe


def workbook_bytes(items):
    book = Workbook()
    sheet = book.active
    sheet.title = "Депозиты"
    sheet.append(
        [
            "ID",
            "Гость",
            "Телефон",
            "Заведение",
            "Сумма, ₽",
            "Статус",
            "Бронирование",
            "Создан",
            "Оплачен",
            "Комментарий",
        ]
    )
    for item in items:
        values = [
            item[key]
            for key in (
                "id",
                "customer_name",
                "phone",
                "restaurant",
                "amount",
                "status",
                "reservation_date",
                "created_at",
                "paid_at",
                "notes",
            )
        ]
        # Guest-supplied text must never become a spreadsheet formula.
        sheet.append(
            [
                "'" + value
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@"))
                else value
                for value in values
            ]
        )
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def create_router(
    service, principal_dependency, check_origin, *, company_users=None, validate_user=None
):
    """Dependencies come from the common portal; no cookie/JWT fallback to legacy pay.

    principal_dependency returns PaymentPrincipal from a fresh verified scope.
    check_origin is the common portal's Origin/CSRF gate. validate_user confirms a
    target is active in this company before any grant (required for grant routes).
    """
    router = APIRouter(tags=["tenant-payments"], route_class=PaymentRoute)
    Principal = Annotated[object, Depends(principal_dependency)]
    store = service.store

    @router.get("/api/deposits")
    async def listing(principal: Principal, filters: Annotated[DepositFilters, Query()]):
        return await run_in_threadpool(store.listing, principal, filters)

    @router.get("/api/deposits/export")
    async def export(principal: Principal, filters: Annotated[DepositFilters, Query()]):
        rows = await run_in_threadpool(store.listing, principal, filters, export=True)
        content = await run_in_threadpool(workbook_bytes, rows["items"])
        return Response(
            content,
            media_type=XLSX,
            headers={"Content-Disposition": 'attachment; filename="deposits.xlsx"'},
        )

    @router.get("/api/deposits/permissions")
    async def permissions(principal: Principal):
        store.require(principal)
        return {
            "can_manage_access": principal.can_manage or principal.actor.kind == "platform_owner",
            "timezone": store.runtime.timezone,
            "today": datetime.now(ZoneInfo(store.runtime.timezone)).date().isoformat(),
        }

    @router.get("/api/deposits/venues")
    async def venues(principal: Principal):
        return [venue["name"] for venue in await run_in_threadpool(store.venues, principal)]

    @router.get("/api/deposits/creation-venues")
    async def creation_venues(principal: Principal):
        return [
            venue["name"] for venue in await run_in_threadpool(store.venues, principal, create=True)
        ]

    @router.get("/api/deposits/venue-options")
    async def venue_options(principal: Principal):
        return await run_in_threadpool(store.venues, principal)

    @router.get("/api/deposits/access")
    async def grants(principal: Principal):
        rows = await run_in_threadpool(store.grants, principal)
        users = await run_in_threadpool(company_users) if company_users else []
        return {"rows": rows, "users": users}

    @router.post("/api/deposits/access/grant")
    @router.post("/api/deposits/access/update")
    async def grant(request: Request, principal: Principal, payload: VenueGrant):
        check_origin(request)
        store.require(principal, manage=True)
        target = await run_in_threadpool(validate_user, payload.user_id) if validate_user else None
        if not target:
            raise HTTPException(422, "Сотрудник не принадлежит этой компании или отключён.")
        return await run_in_threadpool(
            store.grant,
            principal,
            payload.user_id,
            payload.venue,
            is_all=payload.is_all,
            can_create=payload.can_create,
            profile_revision=target.get("revision") if isinstance(target, dict) else None,
        )

    @router.post("/api/deposits/access/revoke", status_code=204)
    async def revoke(request: Request, principal: Principal, payload: VenueRevoke):
        check_origin(request)
        await run_in_threadpool(
            store.revoke,
            principal,
            payload.user_id,
            payload.venue,
            is_all=payload.venue == "Все заведения",
        )
        return Response(status_code=204)

    @router.post("/api/deposits", status_code=201)
    async def create(request: Request, principal: Principal, payload: NewDeposit):
        check_origin(request)
        return await run_in_threadpool(store.create, principal, payload)

    @router.get("/api/deposits/{deposit_id}")
    async def detail(deposit_id: UUID, principal: Principal):
        return await run_in_threadpool(store.get, principal, deposit_id)

    @router.patch("/api/deposits/{deposit_id}")
    async def edit(deposit_id: UUID, request: Request, principal: Principal, payload: EditDeposit):
        check_origin(request)
        return await run_in_threadpool(store.edit, principal, deposit_id, payload, payload.revision)

    @router.get("/api/payment-settings")
    async def settings(principal: Principal):
        return await run_in_threadpool(store.management, principal)

    @router.post("/api/payment-settings/venues/{venue_id}")
    async def save_venue(
        venue_id: UUID, request: Request, principal: Principal, payload: VenueInput
    ):
        check_origin(request)
        return await run_in_threadpool(store.save_venue, principal, venue_id, payload)

    @router.post("/api/payment-settings/venues/{venue_id}/terminals/{terminal_id}")
    async def save_terminal(
        venue_id: UUID,
        terminal_id: UUID,
        request: Request,
        principal: Principal,
        payload: TerminalInput,
    ):
        check_origin(request)
        return await run_in_threadpool(
            store.save_terminal, principal, venue_id, terminal_id, payload
        )

    @router.post("/api/payment-settings/venues/{venue_id}/terminals/{terminal_id}/validate")
    async def validate_terminal(
        venue_id: UUID, terminal_id: UUID, request: Request, principal: Principal
    ):
        check_origin(request)
        return await service.validate_terminal(principal, venue_id, terminal_id)

    @router.get("/api/guest-deposits/{deposit_id}")
    async def guest(deposit_id: UUID, token: Annotated[str, Query(min_length=20, max_length=128)]):
        return await run_in_threadpool(store.guest, deposit_id, token)

    def guest_origin(request):
        origin = request.headers.get("origin")
        if not origin or origin not in {
            store.runtime.frontend_origin,
            store.runtime.payment_origin,
        }:
            raise HTTPException(403, "Недопустимый источник запроса.")

    @router.post("/api/guest-deposits/{deposit_id}/prepare")
    async def prepare(deposit_id: UUID, request: Request, payload: GuestAction):
        guest_origin(request)
        return await service.prepare(deposit_id, payload.token, payload.request_id)

    @router.post("/api/guest-deposits/{deposit_id}/reconcile")
    async def reconcile(deposit_id: UUID, request: Request, payload: GuestAction):
        guest_origin(request)
        return await service.refresh_guest(deposit_id, payload.token)

    @router.get("/api/guest-links/{code}")
    async def short_guest(code: str):
        deposit_id, token = await run_in_threadpool(store.resolve_guest_link, code)
        return await run_in_threadpool(store.guest, deposit_id, token)

    @router.post("/api/guest-links/{code}/prepare")
    async def short_prepare(code: str, request: Request, payload: ShortGuestAction):
        guest_origin(request)
        deposit_id, token = await run_in_threadpool(store.resolve_guest_link, code)
        return await service.prepare(deposit_id, token, payload.request_id)

    @router.post("/api/guest-links/{code}/reconcile")
    async def short_reconcile(code: str, request: Request, payload: ShortGuestAction):
        guest_origin(request)
        deposit_id, token = await run_in_threadpool(store.resolve_guest_link, code)
        return await service.refresh_guest(deposit_id, token)

    @router.post("/api/payment-callbacks/{attempt_id}")
    async def callback(
        attempt_id: UUID,
        request: Request,
        token: Annotated[str, Query(min_length=20, max_length=128)],
    ):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                raise HTTPException(413, "Слишком большое уведомление.")
        try:
            if request.headers.get("content-type", "").startswith(
                "application/x-www-form-urlencoded"
            ):
                parsed = parse_qs(raw.decode("utf-8"), max_num_fields=50)
                values = {key: value[-1] for key, value in parsed.items()}
                payload = Callback.model_validate(values)
            else:
                payload = Callback.model_validate_json(raw)
        except ValueError:
            raise HTTPException(422, "Некорректное уведомление.") from None
        return await service.callback(attempt_id, token, payload.operation_id)

    return router
