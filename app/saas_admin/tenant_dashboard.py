"""The existing dashboard's read contracts behind the tenant BFF boundary."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, Query, Request

from .dashboard_service import DashboardService
from .repository import Problem
from .tenant_routes import TENANT_BASE, TENANT_COOKIE


def mount_dashboard_routes(app, repo):
    service = DashboardService()
    app.state.tenant_dashboard = service
    base = TENANT_BASE + "/{slug}/dashboard"

    def source(slug: str, request: Request):
        return repo.tenant_dashboard_source(request.cookies.get(TENANT_COOKIE, ""), slug)

    def checked(request, slug, selected_source, value):
        # Revocation/config changes during a long upstream call also take effect.
        current = source(slug, request)
        if current.cache_key != selected_source.cache_key:
            raise Problem(409, "source_changed", "Настройки изменились. Обновите данные")
        return value

    dependency = Depends(source)

    @app.get(base + "/me")
    def me(slug: str, request: Request, selected_source=dependency):
        return checked(request, slug, selected_source, service.meta(selected_source))

    @app.get(base + "/overview")
    def overview(
        slug: str,
        request: Request,
        start: date,
        end: date,
        department_id: Annotated[list[UUID] | None, Query(max_length=100)] = None,
        granularity: Literal["day", "week", "month"] = "day",
        selected_source=dependency,
    ):
        value = service.overview(
            selected_source, start, end, [str(key) for key in department_id or []], granularity
        )
        return checked(request, slug, selected_source, value)

    @app.get(base + "/sales/{kind}")
    def sales(
        slug: str,
        request: Request,
        kind: Literal["daily", "dishes"],
        start: date,
        end: date,
        department_id: Annotated[list[UUID] | None, Query(max_length=100)] = None,
        dish_id: UUID | None = None,
        dish_name: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        selected_source=dependency,
    ):
        if (dish_id is not None or dish_name is not None) and (
            kind != "dishes" or (dish_id is not None and dish_name is not None)
        ):
            raise Problem(422, "invalid_filter", "Выберите один фильтр в отчёте по блюдам")
        value = service.sales(
            selected_source,
            start,
            end,
            [str(key) for key in department_id or []],
            kind,
            str(dish_id) if dish_id else None,
            dish_name,
        )
        return checked(request, slug, selected_source, value)
