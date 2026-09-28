"""Fixed deposit API routes using the existing HttpOnly session and per-user RLS."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from app.web.auth import ACCESS_COOKIE
from app.web.repository import Scope

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ERRORS = {
    400: "Проверьте параметры запроса депозитов.",
    401: "Войдите в систему.",
    403: "Недостаточно прав для управления доступом к депозитам.",
    404: "Депозит или назначение доступа не найдено.",
    409: "Такой доступ уже назначен.",
    422: "Проверьте поля запроса депозитов.",
    429: "Слишком много запросов. Повторите позже.",
}


class DepositFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)
    query: str | None = Field(default=None, max_length=200)
    status_filter: Literal["paid", "pending", "failed"] | None = None
    restaurant: str | None = Field(default=None, max_length=100)
    min_amount: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_amount: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    date_from: datetime | None = None
    date_to: datetime | None = None
    sort_by: Literal[
        "created_at",
        "paid_at",
        "customer_name",
        "phone",
        "amount",
        "restaurant",
        "status",
        "reservation_date",
    ] = "created_at"
    sort_dir: Literal["asc", "desc"] = "desc"

    @model_validator(mode="after")
    def ordered_ranges(self):
        if self.min_amount is not None and self.max_amount is not None:
            if self.min_amount > self.max_amount:
                raise ValueError("Invalid amount range")
        if self.date_from and self.date_to:
            if not self.date_from.tzinfo or not self.date_to.tzinfo:
                raise ValueError("Explicit timezone required")
            if self.date_from > self.date_to:
                raise ValueError("Invalid date range")
        return self


class VenueGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    user_id: UUID
    venue: str = Field(min_length=1, max_length=100)
    is_all: bool = False


class VenueRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    user_id: UUID
    venue: str = Field(min_length=1, max_length=100)


class DepositsClient:
    def __init__(self, url, transport=None):
        self.client = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/",
            timeout=httpx.Timeout(45, connect=5, pool=5),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def call(self, token: str | None, method: str, path: str, *, binary=False, **kwargs):
        if not token:
            raise HTTPException(401, "Войдите в систему.")
        try:
            async with self.client.stream(
                method, path, headers={"Authorization": "Bearer " + token}, **kwargs
            ) as response:
                if response.status_code in ERRORS:
                    raise HTTPException(response.status_code, ERRORS[response.status_code])
                if response.status_code not in (200, 201, 204):
                    raise HTTPException(503, "Сервис депозитов временно недоступен.")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 32 * 1024 * 1024:
                        raise HTTPException(503, "Слишком большой ответ сервиса депозитов.")
                if response.status_code == 204:
                    return None
                if binary:
                    if not response.headers.get("content-type", "").startswith(XLSX):
                        raise HTTPException(503, "Сервис депозитов не вернул файл Excel.")
                    return bytes(content)
                parsed = httpx.Response(200, content=bytes(content)).json()
                if not isinstance(parsed, (dict, list)):
                    raise ValueError("Invalid response")
                return parsed
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "Сервис депозитов временно недоступен.") from None


def create_deposits_router(access, repo):
    router = APIRouter(prefix="/api/deposits", tags=["deposits"])
    Access = Annotated[Scope, Depends(access)]

    async def call(request, method, path, **kwargs):
        return await request.app.state.deposits.call(
            request.cookies.get(ACCESS_COOKIE), method, path, **kwargs
        )

    @router.get("")
    async def listing(request: Request, scope: Access, filters: Annotated[DepositFilters, Query()]):
        return await call(
            request, "GET", "deposits/", params=filters.model_dump(mode="json", exclude_none=True)
        )

    @router.get("/export")
    async def export(request: Request, scope: Access, filters: Annotated[DepositFilters, Query()]):
        params = filters.model_dump(mode="json", exclude_none=True)
        params.update(page=1, page_size=1000)
        content = await call(request, "GET", "deposits/export", params=params, binary=True)
        return Response(
            content,
            media_type=XLSX,
            headers={
                "Content-Disposition": 'attachment; filename="deposits.xlsx"',
            },
        )

    @router.get("/permissions")
    async def permissions(request: Request, scope: Access):
        return await call(request, "GET", "deposits/permissions")

    @router.get("/venues")
    async def venues(request: Request, scope: Access):
        return await call(request, "GET", "deposits/enterprises")

    @router.get("/access")
    async def grants(request: Request, scope: Access):
        # The upstream validates the separate deposit administrator role.
        rows = await call(request, "GET", "deposits/user-venues")
        users = await run_in_threadpool(repo.deposit_users)
        return {"rows": rows, "users": users}

    @router.post("/access/grant")
    async def grant(request: Request, scope: Access, payload: VenueGrant):
        request.app.state.auth.check_origin(request)
        return await call(
            request, "POST", "deposits/user-venues", json=payload.model_dump(mode="json")
        )

    @router.post("/access/update")
    async def update(request: Request, scope: Access, payload: VenueGrant):
        request.app.state.auth.check_origin(request)
        return await call(
            request,
            "PATCH",
            "deposits/user-venues",
            params={
                "user_id": str(payload.user_id),
                "venue": payload.venue,
            },
            json={"is_all": payload.is_all},
        )

    @router.post("/access/revoke", status_code=204)
    async def revoke(request: Request, scope: Access, payload: VenueRevoke):
        request.app.state.auth.check_origin(request)
        await call(
            request, "DELETE", "deposits/user-venues", params=payload.model_dump(mode="json")
        )
        return Response(status_code=204)

    @router.get("/{deposit_id}")
    async def detail(deposit_id: UUID, request: Request, scope: Access):
        return await call(request, "GET", f"deposits/{deposit_id}")

    return router
