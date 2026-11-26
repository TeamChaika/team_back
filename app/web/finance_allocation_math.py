"""Validated allocation commands and exact, deterministic kopeck distribution."""

from calendar import monthrange
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field, model_validator

from app.web.coverage import ZONE
from app.web.finance_ledger import ExpenseCancel, ExpenseFields, Input

Method = Literal["equal", "weights", "revenue", "pending"]
Money = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)]
Weight = Annotated[Decimal, Field(gt=0, max_digits=24, decimal_places=12)]


class AllocationPart(Input):
    department_id: UUID
    weight: Weight | None = None
    amount: Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)] | None = None


class Distribution(Input):
    method: Method
    parts: list[AllocationPart] = Field(min_length=2, max_length=100)

    @model_validator(mode="after")
    def valid_distribution(self):
        if len({p.department_id for p in self.parts}) != len(self.parts):
            raise ValueError("Выберите разные заведения.")
        if self.method == "weights" and any(p.weight is None for p in self.parts):
            raise ValueError("Укажите положительный вес каждого заведения.")
        amounts = [p.amount is not None for p in self.parts]
        if any(amounts) and not all(amounts):
            raise ValueError("Подтвердите суммы для всех заведений.")
        if self.method == "pending" and any(
            p.amount is not None or p.weight is not None for p in self.parts
        ):
            raise ValueError("Отложенный расход не содержит рассчитанных долей.")
        return self


class AllocationPreview(Distribution):
    expense_date: date
    amount: Money


class AllocationFields(ExpenseFields, Distribution):
    pass


class AllocationCreate(AllocationFields):
    request_id: UUID


class AllocationEdit(AllocationFields):
    revision: int = Field(ge=1)


AllocationCancel = ExpenseCancel

UNAVAILABLE = (
    "Для распределения по выручке нужен законченный месяц с полными подтверждёнными "
    "данными каждого заведения. Задайте доли вручную или повторите позднее."
)


def _revenue_weights(scope, repo, payload, *, db=None):
    start = payload.expense_date.replace(day=1)
    end = start.replace(day=monthrange(start.year, start.month)[1])
    if end >= datetime.now(ZONE).date():
        raise HTTPException(422, UNAVAILABLE)
    ids = tuple(part.department_id for part in payload.parts)
    # One saved report for the entire selection; no live-sales request or live fallback.
    options = {"with_scope_coverage": True}
    if db is not None:
        options["db"] = db
    sales = repo.sales(replace(scope, selected=ids), "daily", start, end, **options)
    if (
        not sales["complete"]
        or sales["partial_days"]
        or sales["incomplete_scope_dates"]
        or any(i["field"] == "DishDiscountSumInt" for i in sales["reconciliation_issues"])
    ):
        raise HTTPException(422, UNAVAILABLE)
    weights = {str(venue): Decimal(0) for venue in ids}
    with localcontext() as ctx:
        ctx.prec = 70
        for row in sales["rows"]:
            key = str(row["department_id"])
            if key not in weights:
                continue
            if row["revenue"] is None:
                raise HTTPException(422, UNAVAILABLE)
            revenue = Decimal(row["revenue"])
            if not revenue.is_finite():
                raise HTTPException(422, UNAVAILABLE)
            weights[key] += revenue
    if any(value < 0 for value in weights.values()) or not any(weights.values()):
        raise HTTPException(422, UNAVAILABLE)
    return [weights[str(venue)] for venue in ids]


def calculate_parts(scope, repo, payload, *, db=None):
    """Use fractions for rounding, disclose only relative revenue shares."""
    if not {p.department_id for p in payload.parts}.issubset(set(scope.ids)):
        raise HTTPException(403, "Нет доступа ко всем заведениям распределения.")
    if payload.method == "pending":
        raise HTTPException(422, "Отложенный расход сохраняется без расчёта долей.")
    if payload.method == "revenue":
        weights = _revenue_weights(scope, repo, payload, db=db)
    elif payload.method == "weights":
        weights = [part.weight for part in payload.parts]
    else:
        weights = [Decimal(1)] * len(payload.parts)
    exact_weights = [Fraction(w) for w in weights]
    total = sum(exact_weights)
    cents = int(payload.amount * 100)
    shares = [cents * weight / total for weight in exact_weights]
    rounded = [share.numerator // share.denominator for share in shares]
    order = sorted(range(len(shares)),
                   key=lambda i: (-(shares[i] - rounded[i]), str(payload.parts[i].department_id)))
    for index in order[:cents - sum(rounded)]:
        rounded[index] += 1
    if any(value == 0 and weight > 0
           for value, weight in zip(rounded, weights, strict=True)):
        raise HTTPException(
            422, "Слишком малая доля: каждому заведению нужна хотя бы одна копейка.",
        )
    with localcontext() as ctx:
        ctx.prec = 70
        denominator = sum(weights)
        parts = [{
            "department_id": part.department_id,
            "weight": ((weight / denominator).quantize(Decimal("0.000000000001"))
                       if payload.method == "revenue" else weight),
            "amount": Decimal(rounded[index]) / 100,
        } for index, (part, weight) in enumerate(zip(payload.parts, weights, strict=True))]
    if any(part.amount is not None and part.amount != result["amount"]
           for part, result in zip(payload.parts, parts, strict=True)):
        raise HTTPException(409, "Распределение изменилось. Повторите предварительный расчёт.")
    return parts


def public_preview(method, parts):
    return {"method": method, "parts": [{
        "department_id": str(part["department_id"]),
        "weight": str(part["weight"]),
        "amount": format(part["amount"], ".2f"),
    } for part in parts]}
