"""Primary Dashboard financial access and report boundaries."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.web.auth import Auth
from app.web.coverage import ZONE, financial_scope_gaps
from app.web.finance_routes import create_finance_router
from app.web.permissions import finance_level, sections_for
from app.web.repository import Repository, Scope
from app.web.sync_policy import SyncPolicy

VENUE = UUID(int=21)
STORE = UUID(int=22)
DAY = date(2026, 1, 1)


@pytest.mark.parametrize(
    "user,level",
    [
        ({"role": "owner", "sections": []}, "owner"),
        ({"role": "manager"}, None),
        ({"role": "manager", "is_portal_admin": True, "sections": []}, None),
        ({"role": "analyst", "sections": ["profit-loss-accounting"]}, "accountant"),
        ({"role": "manager", "sections": ["profit-loss"]}, "manager"),
        ({"role": "owner", "warehouse_scope_mode": "selected"}, None),
    ],
)
def test_finance_permissions_are_explicit(user, level):
    assert finance_level(user) == level
    if level:
        assert "profit-loss" in sections_for(user)
    elif user.get("warehouse_scope_mode") != "selected":
        assert "profit-loss" not in sections_for(user)


@pytest.fixture
def api():
    class Ledger:
        def totals(self, scope, start, end):
            assert set(scope.ids) == {VENUE}
            return {"expenses": "20.15", "cash": "5.00", "bank": "15.15", "by_category": []}

        def list(self, scope, start, end, *, limit, offset):
            return {"rows": [], "total": 0}

        def create(self, scope, payload):
            return {"department_id": str(payload.department_id)}

        def update(self, scope, expense_id, payload):
            return {"revision": payload.revision + 1}

        def cancel(self, scope, expense_id, payload):
            return {"revision": payload.revision + 1}

    class Repo:
        def sales(self, scope, kind, start, end, **kwargs):
            assert list(scope.ids) == [VENUE]
            assert kind == "daily"
            assert kwargs["with_scope_coverage"]
            state.sale_calls += 1
            return state.sales

    class Live:
        def get(self, scope):
            assert list(scope.ids) == [VENUE]
            if state.outage:
                raise HTTPException(503, "Сервис временно недоступен")
            return None, {"source": "stored"}

    user = {"role": "owner", "sections": [], "id": UUID(int=1)}
    venues = ({"id": VENUE, "type": "DEPARTMENT"}, {"id": STORE, "type": "CENTRALSTORE"})
    scope = Scope(user, venues, None, (), ())
    state = SimpleNamespace(
        scope=scope,
        sale_calls=0,
        outage=False,
        sales={
            "rows": [{"department_id": str(VENUE)}],
            "totals": {"revenue": "100.00", "cost": "30.50", "gross_profit": "69.50"},
            "complete": True,
            "partial_days": [],
            "incomplete_scope_dates": [],
            "reconciliation_issues": [],
        },
    )

    def access():
        return state.scope

    app = FastAPI()
    auth = SimpleNamespace(settings=SimpleNamespace(origin="https://dashboard.chaika.team"))
    app.state.auth = SimpleNamespace(check_origin=lambda req: Auth.check_origin(auth, req))
    app.include_router(create_finance_router(access, Repo(), Live(), ledger=Ledger()))
    with TestClient(app) as client:
        yield state, client


def test_owner_period_finances_use_exact_cost_and_only_venues(api):
    state, client = api
    url = f"/api/profit-loss/report?start={DAY}&end={DAY}"
    data = client.get(url).json()
    assert data["operating_profit"] == "49.35"
    assert data["expenses"] == "20.15" and data["complete"] and not data["provisional"]
    for key, value in [
        ("complete", False),
        ("incomplete_scope_dates", [str(DAY)]),
        ("partial_days", [{"date": str(DAY), "observed_at": str(DAY)}]),
        ("reconciliation_issues", [{"field": "DishDiscountSumInt"}]),
    ]:
        old = state.sales[key]
        state.sales[key] = value
        incomplete = client.get(url).json()
        assert not incomplete["complete"] and incomplete["operating_profit"] is None
        assert incomplete["revenue"] is None and incomplete["expenses"] == "20.15"
        state.sales[key] = old
    state.sales["totals"]["cost"] = None
    assert client.get(url).json()["operating_profit"] is None


def test_confirmed_no_sales_deducts_accrual_but_null_cost_is_unavailable(api):
    state, client = api
    state.sales.update(rows=[], totals={"revenue": None, "cost": None})
    period = {"start": str(DAY), "end": str(DAY)}
    report = client.get("/api/profit-loss/report", params=period).json()
    assert report["revenue"] == report["cost"] == "0.00"
    assert report["operating_profit"] == "-20.15"
    state.sales["incomplete_scope_dates"] = [str(DAY)]
    assert client.get("/api/profit-loss/report", params=period).json()["operating_profit"] is None
    state.sales.update(rows=[{"department_id": str(VENUE)}], incomplete_scope_dates=[])
    assert client.get("/api/profit-loss/report", params=period).json()["operating_profit"] is None


def test_zero_sale_evidence_for_each_day_and_venue():
    days = [DAY + timedelta(days=offset) for offset in range(3)]
    second_venue = UUID(int=23)
    selection = [VENUE, second_venue]
    marker = {
        "exact_match": True,
        "sync_scope_mode": "selected",
        "partial_scope": True,
        "sync_department_ids": [str(venue) for venue in selection],
    }
    reports = [{"business_date": day, "checks": []} for day in days]
    reports[1]["checks"] = [marker]
    rows = [{"business_date": days[0], "department_id": v} for v in selection]
    rows.append({"business_date": days[2], "department_id": VENUE})
    assert financial_scope_gaps(reports, rows, selection) == [days[2]]
    reports[2]["checks"] = [{"exact_match": True, "partial_scope": False, "sync_scope_mode": "all"}]
    assert financial_scope_gaps(reports, rows, selection) == []
    reports[2]["checks"] = []
    reports[2]["department_filter"] = {"filterType": "IncludeValues", "values": [str(VENUE)]}
    assert financial_scope_gaps(reports, rows, selection) == [days[2]]
    reports[2]["department_filter"]["values"].append(str(second_venue))
    assert financial_scope_gaps(reports, rows, selection) == []
    rows[0]["frozen"] = True
    assert financial_scope_gaps(reports, rows, selection) == [days[0]]


def test_current_day_is_provisional_and_wrong_roles_never_fetch_sales(api):
    state, client = api
    today = datetime.now(ZONE).date()
    endpoint = f"/api/profit-loss/report?start={today}&end={today}"
    state.sales["partial_days"] = [{"date": str(today)}]
    assert client.get(endpoint).json()["provisional"]
    for grant in ("profit-loss", "profit-loss-accounting"):
        state.scope.user.update(role="manager", sections=[grant])
        assert client.get(endpoint).status_code == 403
        assert client.get(endpoint.replace("report", "expenses")).status_code == 200
    state.scope.user.update(role="owner", warehouse_scope_mode="selected")
    assert client.get(endpoint).status_code == 403
    assert state.sale_calls == 1


def test_unavailable_live_uses_saved_sales_and_keeps_journal_accessible(api):
    state, client = api
    state.outage = True
    today = datetime.now(ZONE).date()
    period = {"start": str(today), "end": str(today)}
    url = "/api/profit-loss/report"
    state.sales["reconciliation_issues"] = [{"field": "GuestNum"}]
    assert client.get(url, params=period).json()["operating_profit"] == "49.35"
    state.sales["complete"] = False
    data = client.get(url, params=period).json()
    assert data["expenses"] == "20.15" and data["operating_profit"] is None
    assert client.get("/api/profit-loss/expenses", params=period).status_code == 200


def test_central_store_selection_cannot_fall_back_to_restaurants(api):
    state, client = api
    state.scope = replace(state.scope, selected=STORE)
    assert client.get(f"/api/profit-loss/expenses?start={DAY}&end={DAY}").status_code == 403


def test_finance_only_metadata_has_restaurants_without_network_sales_or_balance_dates(api):
    state, _ = api
    state.scope.user.update(role="manager", sections=["profit-loss-accounting"])
    repo = object.__new__(Repository)
    repo.sync_policy = SyncPolicy
    meta = repo.metadata(state.scope)
    assert [row["id"] for row in meta["departments"]] == [str(VENUE)]
    assert meta["sales_dates"] == [] and meta["balance_dates"] == []


def test_invalid_period_rejected_before_financial_queries(api):
    state, client = api
    resp = client.get(f"/api/profit-loss/report?start={DAY}&end={DAY - timedelta(days=1)}")
    assert resp.status_code == 422
    assert state.sale_calls == 0


def test_mutations_require_dashboard_origin(api):
    _, client = api
    base = "/api/profit-loss/expenses"
    fields = {
        "expense_date": str(DAY),
        "paid_date": None,
        "amount": "1.99",
        "category": "rent",
        "payment_method": "bank",
        "comment": "",
    }
    for method, url, payload in [
        ("post", base, {**fields, "request_id": str(UUID(int=55)), "department_id": str(VENUE)}),
        ("patch", f"{base}/{UUID(int=56)}", {**fields, "revision": 1}),
        ("post", f"{base}/{UUID(int=56)}/cancel", {"revision": 1}),
    ]:
        assert client.request(method, url, json=payload).status_code == 403
        ok = client.request(
            method, url, json=payload, headers={"Origin": "https://dashboard.chaika.team"}
        )
        assert ok.status_code == 200, ok.text
