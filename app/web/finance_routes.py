"""Restaurant expense journal and owner's operating result in the primary portal."""

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.web.coverage import ZONE
from app.web.finance_allocation_math import (
    AllocationCancel,
    AllocationCreate,
    AllocationEdit,
    AllocationPreview,
    calculate_parts,
    public_preview,
)
from app.web.finance_ledger import ExpenseCancel, ExpenseCreate, ExpenseEdit, FinanceLedger
from app.web.permissions import finance_level
from app.web.repository import Scope


def _finance_scope(scope):
    if not finance_level(scope.user):
        raise HTTPException(403, "Нет доступа к финансовому разделу.")
    venues = tuple(d for d in scope.departments if d["type"] == "DEPARTMENT")
    allowed = {d["id"] for d in venues}
    if not allowed or not set(scope.selection_ids).issubset(allowed):
        raise HTTPException(403, "Нет доступа к выбранному заведению.")
    return replace(scope, departments=venues)


def _check_period(start, end):
    if end < start or (end - start).days > 365:
        raise HTTPException(422, "Выберите период от одного дня до года.")


def _report(scope, repo, ledger, live_sales, start, end):
    if finance_level(scope.user) != "owner":
        raise HTTPException(403, "Отчёт о прибыли доступен владельцу.")
    _check_period(start, end)
    today = datetime.now(ZONE).date()
    if end > today:
        raise HTTPException(422, "Период отчёта должен заканчиваться не позднее сегодняшнего дня.")
    expenses = ledger.totals(scope, start, end)
    bundle = None
    if live_sales is not None and start <= today <= end:
        try:
            bundle, _ = live_sales.get(scope)
        except HTTPException as error:
            if error.status_code not in {423, 503}:
                raise
            # The saved snapshot has its own timestamps and coverage checks.
    sales = repo.sales(scope, "daily", start, end, live_bundle=bundle, with_scope_coverage=True)
    covered = (
        sales["complete"]
        and not sales["incomplete_scope_dates"]
        and not any(
            issue["field"] in {"DishDiscountSumInt", "ProductCostBase.ProductCost"}
            for issue in sales["reconciliation_issues"]
        )
        and all(str(day["date"]) == today.isoformat() for day in sales["partial_days"])
    )
    revenue = sales["totals"]["revenue"] if covered else None
    cost = sales["totals"]["cost"] if covered else None
    if covered and not sales["rows"]:
        revenue = cost = "0.00"
    complete = revenue is not None and cost is not None
    with localcontext() as ctx:
        ctx.prec = 70
        gross = Decimal(revenue) - Decimal(cost) if complete else None
        profit = gross - Decimal(expenses["expenses"]) if complete else None
        before_allocation = (
            gross - Decimal(expenses.get("direct_expenses", expenses["expenses"]))
            if complete else None
        )
    return {
        **expenses,
        "revenue": revenue,
        "cost": cost,
        "gross_profit": str(gross) if gross is not None else None,
        "operating_profit": str(profit) if profit is not None else None,
        "operating_profit_before_allocation": (
            str(before_allocation) if before_allocation is not None else None
        ),
        "complete": complete,
        "provisional": complete and end == today,
    }


def create_finance_router(access, repo, live_sales, *, ledger=None, allocations=None):
    from app.web.finance_allocations import FinanceAllocations

    router = APIRouter(prefix="/api/profit-loss")
    ledger = ledger if ledger is not None else FinanceLedger(repo)
    allocations = allocations if allocations is not None else FinanceAllocations(repo)

    def resolve(scope: Annotated[Scope, Depends(access)]):
        return _finance_scope(scope)

    Access = Annotated[Scope, Depends(resolve)]

    def resolve_allocations(scope: Access):
        if finance_level(scope.user) not in {"accountant", "owner"}:
            raise HTTPException(403, "Распределение доступно бухгалтерии и владельцу.")
        # Repository._read_scope keeps all granted departments in this inventory;
        # the journal's selected venue is a display filter, not a grant boundary.
        return replace(scope, selected=None)

    AllocationAccess = Annotated[Scope, Depends(resolve_allocations)]

    @router.post("/allocations/preview")
    def preview(scope: AllocationAccess, payload: AllocationPreview, request: Request):
        request.app.state.auth.check_origin(request)
        return public_preview(payload.method, calculate_parts(scope, repo, payload))

    @router.post("/allocations")
    def allocation_create(scope: AllocationAccess, payload: AllocationCreate, request: Request):
        request.app.state.auth.check_origin(request)
        return allocations.create(
            scope, payload, lambda value, db=None: calculate_parts(scope, repo, value, db=db),
        )

    @router.get("/allocations/pending")
    def allocation_pending(
        scope: AllocationAccess,
        start: date,
        end: date,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        _check_period(start, end)
        return allocations.pending(scope, start, end, limit=limit, offset=offset)

    @router.get("/allocations/{allocation_id}")
    def allocation_get(scope: AllocationAccess, allocation_id: UUID):
        return allocations.get(scope, allocation_id)

    @router.get("/allocations/{allocation_id}/history")
    def allocation_history(scope: AllocationAccess, allocation_id: UUID):
        return allocations.history(scope, allocation_id)

    @router.patch("/allocations/{allocation_id}")
    def allocation_update(
        scope: AllocationAccess, allocation_id: UUID, payload: AllocationEdit, request: Request,
    ):
        request.app.state.auth.check_origin(request)
        return allocations.update(
            scope, allocation_id, payload,
            lambda value, db=None: calculate_parts(scope, repo, value, db=db),
        )

    @router.post("/allocations/{allocation_id}/cancel")
    def allocation_cancel(
        scope: AllocationAccess, allocation_id: UUID, payload: AllocationCancel, request: Request,
    ):
        request.app.state.auth.check_origin(request)
        return allocations.cancel(scope, allocation_id, payload)

    @router.get("/expenses")
    def expenses(
        scope: Access,
        start: date,
        end: date,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        _check_period(start, end)
        return ledger.list(scope, start, end, limit=limit, offset=offset)

    @router.post("/expenses")
    def create(scope: Access, payload: ExpenseCreate, request: Request):
        request.app.state.auth.check_origin(request)
        return ledger.create(scope, payload)

    @router.patch("/expenses/{expense_id}")
    def update(scope: Access, expense_id: UUID, payload: ExpenseEdit, request: Request):
        request.app.state.auth.check_origin(request)
        return ledger.update(scope, expense_id, payload)

    @router.post("/expenses/{expense_id}/cancel")
    def cancel(scope: Access, expense_id: UUID, payload: ExpenseCancel, request: Request):
        request.app.state.auth.check_origin(request)
        return ledger.cancel(scope, expense_id, payload)

    @router.get("/expenses/{expense_id}/history")
    def history(scope: Access, expense_id: UUID):
        return ledger.history(scope, expense_id)

    @router.get("/report")
    def report(scope: Access, start: date, end: date):
        return _report(scope, repo, ledger, live_sales, start, end)

    return router
