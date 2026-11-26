"""Allocation rounding, source completeness, and API permission boundaries."""

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.web.coverage import ZONE
from app.web.finance_allocation_math import AllocationPreview, calculate_parts
from app.web.finance_routes import _report, create_finance_router
from app.web.repository import Repository, Scope

A, B, C = (UUID(int=i) for i in (21, 22, 23))
BASE = "/api/profit-loss/allocations"
ORIGIN = {"Origin": "https://dashboard.chaika.team"}


@pytest.fixture
def api():
    state = SimpleNamespace(
        scope=Scope(
            {"id": UUID(int=1), "role": "manager", "sections": ["profit-loss-accounting"]},
            tuple({"id": i, "type": "DEPARTMENT"} for i in (A, B, C)),
            None, (), (),
        ),
        calls=0,
        writes=0,
        sales={
            "complete": True, "partial_days": [], "incomplete_scope_dates": [],
            "reconciliation_issues": [],
            "rows": [{"department_id": str(A), "revenue": "123456.00"},
                     {"department_id": str(B), "revenue": "370368.00"}],
        },
    )

    class Repo:
        def sales(self, scope, kind, start, end, **kwargs):
            state.calls += 1
            assert set(scope.ids) == {A, B}
            assert (start, end) == (date(2026, 1, 1), date(2026, 1, 31))
            assert kwargs == {"with_scope_coverage": True}
            return state.sales

    class Allocations:
        def create(self, scope, payload, calculate):
            parts = ([{"department_id": p.department_id} for p in payload.parts]
                     if payload.method == "pending" else calculate(payload))
            state.writes += 1
            return {"parts": parts}

        def update(self, scope, allocation_id, payload, calculate):
            return self.create(scope, payload, calculate)

        def get(self, scope, allocation_id):
            return {"id": str(allocation_id)}

        def pending(self, scope, start, end, *, limit, offset):
            return {"rows": [], "total": 0}

        def history(self, scope, allocation_id):
            return {"rows": []}

        def cancel(self, scope, allocation_id, payload):
            state.writes += 1
            return {"revision": payload.revision + 1}

    app = FastAPI()

    def guard(request):
        if request.headers.get("origin") != ORIGIN["Origin"]:
            raise HTTPException(403, "Origin")

    app.state.auth = SimpleNamespace(check_origin=guard)
    app.include_router(create_finance_router(
        lambda: state.scope, Repo(), None, allocations=Allocations(),
    ))
    with TestClient(app) as client:
        yield state, client


def payload(method="equal", amount="10.00", ids=(A, B)):
    return {"expense_date": "2026-01-15", "amount": amount, "method": method,
            "parts": [{"department_id": str(i)} for i in ids]}


def full_payload(**kwargs):
    return {**payload(**kwargs), "category": "rent", "payment_method": "bank",
            "request_id": str(UUID(int=9))}


def test_equal_split_preserves_cents_and_ties_are_independent_of_input_order(api):
    _, client = api
    for ids in ((C, A, B), (B, C, A)):
        result = client.post(BASE + "/preview", json=payload(ids=ids), headers=ORIGIN)
        assert result.status_code == 200, result.text
        amounts = {p["department_id"]: p["amount"] for p in result.json()["parts"]}
        assert amounts == {str(A): "3.34", str(B): "3.33", str(C): "3.33"}


def test_decimal_weights_and_tiny_positive_parts(api):
    _, client = api
    data = payload("weights", "0.03")
    data["parts"][0]["weight"], data["parts"][1]["weight"] = "0.1", "0.2"
    response = client.post(BASE + "/preview", json=data, headers=ORIGIN)
    assert [p["amount"] for p in response.json()["parts"]] == ["0.01", "0.02"]
    data["amount"] = "0.01"
    assert client.post(BASE + "/preview", json=data, headers=ORIGIN).status_code == 422


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(parts=p["parts"][:1]),
    lambda p: p["parts"][1].update(department_id=str(A)),
    lambda p: p.update(method="weights"),
    lambda p: p["parts"][0].update(weight="NaN"),
    lambda p: p["parts"][0].update(weight="0"),
    lambda p: p["parts"][0].update(weight="-1"),
])
def test_invalid_distribution_rejected(api, mutation):
    _, client = api
    data = payload()
    mutation(data)
    assert client.post(BASE + "/preview", json=data, headers=ORIGIN).status_code == 422


def test_revenue_preview_only_discloses_relative_weights(api):
    state, client = api
    response = client.post(BASE + "/preview", json=payload("revenue"), headers=ORIGIN)
    assert response.status_code == 200, response.text
    parts = response.json()["parts"]
    assert [p["amount"] for p in parts] == ["2.50", "7.50"]
    assert [Decimal(p["weight"]) for p in parts] == [Decimal("0.25"), Decimal("0.75")]
    assert "123456" not in response.text and "370368" not in response.text
    assert state.calls == 1


@pytest.mark.parametrize("field,value", [
    ("complete", False), ("partial_days", [{"date": "2026-01-31"}]),
    ("incomplete_scope_dates", ["2026-01-10"]),
    ("reconciliation_issues", [{"field": "DishDiscountSumInt"}]),
    ("rows", [{"department_id": str(A), "revenue": None}]),
])
def test_revenue_requires_complete_closed_saved_month(api, field, value):
    state, client = api
    state.sales[field] = value
    result = client.post(BASE + "/preview", json=payload("revenue"), headers=ORIGIN)
    assert result.status_code == 422
    assert "вручную" in result.json()["detail"]


def test_current_month_and_forbidden_venues_never_query_sales(api):
    state, client = api
    data = payload("revenue")
    data["expense_date"] = date.today().isoformat()
    assert client.post(BASE + "/preview", json=data, headers=ORIGIN).status_code == 422
    data = payload("revenue", ids=(A, UUID(int=99)))
    assert client.post(BASE + "/preview", json=data, headers=ORIGIN).status_code == 403
    state.scope = replace(state.scope, selected=A, departments=(state.scope.departments[0],))
    response = client.post(BASE + "/preview", json=payload("revenue"), headers=ORIGIN)
    assert response.status_code == 403
    assert state.calls == 0


def test_journal_selection_does_not_remove_actual_group_grants(api):
    state, client = api
    state.scope = replace(state.scope, selected=A)
    response = client.post(BASE + "/preview", json=payload("revenue"), headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert state.calls == 1


def test_allocation_endpoints_require_accountant_and_origin(api):
    state, client = api
    identity = str(UUID(int=8))
    requests = [("post", BASE + "/preview", payload()),
                ("post", BASE, full_payload()),
                ("patch", BASE + "/" + identity,
                 {**{k: v for k, v in full_payload().items() if k != "request_id"}, "revision": 1}),
                ("post", BASE + "/" + identity + "/cancel", {"revision": 1}),
                ("get", BASE + "/" + identity, None),
                ("get", BASE + "/" + identity + "/history", None)]
    for method, url, data in requests:
        if method != "get":
            assert client.request(method, url, json=data).status_code == 403
        state.scope.user["sections"] = ["profit-loss"]
        assert client.request(method, url, json=data, headers=ORIGIN).status_code == 403
        state.scope.user["sections"] = ["profit-loss-accounting"]
        assert client.request(method, url, json=data, headers=ORIGIN).status_code == 200


def test_changed_preview_amounts_conflict_without_write(api):
    state, client = api
    data = full_payload(method="revenue")
    for part in data["parts"]:
        part["amount"] = "5.00"
    response = client.post(BASE, json=data, headers=ORIGIN)
    assert response.status_code == 409
    assert state.writes == 0


def test_pending_skips_sales_and_previews_and_list_has_own_route(api):
    state, client = api
    data = full_payload(method="pending")
    data["expense_date"] = date.today().isoformat()
    result = client.post(BASE, json=data, headers=ORIGIN)
    assert result.status_code == 200, result.text
    assert result.json()["parts"] == [{"department_id": str(A)}, {"department_id": str(B)}]
    assert state.calls == 0
    assert client.post(BASE + "/preview", json=payload("pending"),
                       headers=ORIGIN).status_code == 422
    url = BASE + "/pending?start=2026-01-01&end=2026-01-31"
    assert client.get(url).json() == {"rows": [], "total": 0}
    state.scope.user["sections"] = ["profit-loss"]
    assert client.get(url).status_code == 403


def test_large_weight_rounding_keeps_exact_total(api):
    _, client = api
    data = payload("weights", "999999999999.99", (C, A, B))
    for part, weight in zip(data["parts"], ["999999999999", "0.000000000001", "2"],
                            strict=True):
        part["weight"] = weight
    # The tiny second share must never become a zero-value child ledger expense.
    assert client.post(BASE + "/preview", json=data, headers=ORIGIN).status_code == 422
    data["parts"][1]["weight"] = "3"
    parts = client.post(BASE + "/preview", json=data, headers=ORIGIN).json()["parts"]
    assert sum(Decimal(part["amount"]) for part in parts) == Decimal(data["amount"])


def test_owner_before_allocation_and_after_include_pending(api):
    state, _ = api
    state.scope.user.update(role="owner", sections=[])
    totals = {"direct_expenses": "20.00", "allocated_expenses": "5.00",
              "pending_expenses": "10.00", "expenses": "35.00"}
    ledger = SimpleNamespace(totals=lambda *args: totals)
    sales = {**state.sales, "totals": {"revenue": "100.00", "cost": "30.00"}}
    repo = SimpleNamespace(sales=lambda *args, **kwargs: sales)
    result = _report(state.scope, repo, ledger, None, date(2026, 1, 1), date(2026, 1, 31))
    assert result["operating_profit_before_allocation"] == "50.00"
    assert result["operating_profit"] == "35.00"
    sales["complete"] = False
    result = _report(state.scope, repo, ledger, None, date(2026, 1, 1), date(2026, 1, 31))
    assert result["operating_profit_before_allocation"] is None
    assert result["pending_expenses"] == "10.00"


def test_confirmed_zero_revenue_is_valid_zero_share_and_can_be_confirmed(api):
    state, client = api
    state.sales["rows"][0]["revenue"] = "0.00"
    response = client.post(BASE + "/preview", json=payload("revenue"), headers=ORIGIN)
    assert response.status_code == 200, response.text
    parts = response.json()["parts"]
    assert [part["amount"] for part in parts] == ["0.00", "10.00"]
    data = full_payload(method="revenue")
    for part, preview in zip(data["parts"], parts, strict=True):
        part["amount"] = preview["amount"]
    assert client.post(BASE, json=data, headers=ORIGIN).status_code == 200
    state.sales["rows"][1]["revenue"] = "0.00"
    assert client.post(BASE + "/preview", json=payload("revenue"),
                       headers=ORIGIN).status_code == 422


def test_saved_revenue_calculation_reuses_callers_transaction(api):
    state, _ = api
    marker = object()

    def sales(scope, kind, start, end, **kwargs):
        assert kwargs["db"] is marker
        return state.sales

    parts = calculate_parts(state.scope, SimpleNamespace(sales=sales),
                            AllocationPreview(**payload("revenue")), db=marker)
    assert [p["amount"] for p in parts] == [Decimal("2.50"), Decimal("7.50")]
    repo = object.__new__(Repository)

    def forbidden_connection():
        raise AssertionError("Must reuse the current transaction")

    repo.connection = forbidden_connection
    results = iter([[{
        "business_date": date(2026, 1, 1), "observed_at": datetime(2026, 1, 2, tzinfo=ZONE),
        "checks": [{"exact_match": True, "sync_scope_mode": "all", "partial_scope": False}],
    }], []])
    db = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchall=lambda: next(results)))
    result = repo.sales(state.scope, "daily", date(2026, 1, 1), date(2026, 1, 1),
                        with_scope_coverage=True, db=db)
    assert result["complete"] and result["incomplete_scope_dates"] == []
