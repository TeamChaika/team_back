"""Website security contracts, without real accounts or upstream iiko requests."""

from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.portal import create_portal
from app.web.auth import ACCESS_COOKIE
from app.web.repository import Scope, has_store_scope
from app.web.settings import WebSettings

USER = UUID(int=1)
DEPARTMENT = UUID(int=2)
OTHER = UUID(int=3)


class FakeRepository:
    active = True

    def scope(self, user_id, selected=None):
        if not self.active or user_id != USER or selected not in (None, DEPARTMENT):
            raise HTTPException(403, "Нет доступа")
        return Scope(
            {"id": USER, "display_name": "Менеджер", "role": "manager"},
            ({"id": DEPARTMENT, "code": "1"},),
            selected,
            (UUID(int=4),),
            ("rms-test",),
        )

    def metadata(self, scope):
        return {"role": scope.user["role"]}

    def resources(self, scope, resource, start, end, q, offset, status=None):
        return {"departments": [str(x) for x in scope.ids]}

    def detail(self, scope, resource, item_id):
        if item_id == OTHER:
            raise HTTPException(404, "Недоступно")
        return {"id": str(item_id)}

    def sales(self, scope, kind, start, end):
        return {}

    def overview(self, scope, start, end, granularity):
        return {"ids": [str(i) for i in scope.ids], "granularity": granularity}

    def purchase_prices(
        self,
        scope,
        kind,
        exclude_household=True,
        selection=None,
        *,
        recent_only=True,
        include_impact=False,
    ):
        if selection:
            assert recent_only is False
            return {"rows": []}
        return {
            "ids": [str(i) for i in scope.ids],
            "kind": kind,
            "exclude_household": exclude_household,
            "recent_only": recent_only,
            "include_impact": include_impact,
        }

    def purchase_impact(self, scope, selection, analysis_department_id=None, all_departments=False):
        return {
            "ids": [str(i) for i in scope.ids],
            "selection": [str(i) for i in selection],
            "analysis_department_id": str(analysis_department_id)
            if analysis_department_id
            else None,
            "all_departments": all_departments,
        }


def provider(request):
    if request.url.path.endswith("/token"):
        return httpx.Response(
            200,
            json={
                "access_token": "verified",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "user": {"user_metadata": {"role": "owner"}},
            },
        )
    if request.url.path.endswith("/user"):
        if request.headers.get("authorization") != "Bearer verified":
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"id": str(USER), "user_metadata": {"role": "owner"}})
    return httpx.Response(204)


@pytest.fixture
def client():
    repo = FakeRepository()
    app = create_portal(
        Settings(),
        WebSettings(_env_file=None, anon_key="test"),
        auth_transport=httpx.MockTransport(provider),
        repository=repo,
    )
    with TestClient(app, base_url="http://127.0.0.1:8013") as client:
        client.repo = repo
        yield client


def login(client):
    return client.post(
        "/api/auth/login",
        headers={"Origin": "http://127.0.0.1:8013"},
        json={"email": "test@example.invalid", "password": "private-password"},
    )


def test_no_anonymous_or_forged_access(client):
    assert client.get("/api/me").status_code == 401
    client.cookies.set(ACCESS_COOKIE, "forged")
    assert client.get("/api/me").status_code == 401


def test_latest_purchase_prices_require_scope_but_no_period(client):
    url = "/api/purchase-prices"
    assert client.get(url).status_code == 401
    login(client)
    assert client.get(url).json() == {
        "ids": [str(DEPARTMENT)],
        "kind": "unlinked",
        "exclude_household": True,
        "recent_only": True,
        "include_impact": False,
    }
    assert client.get(url + "?exclude_household=false").json()["exclude_household"] is False
    assert client.get(url + "?exclude_household=invalid").status_code == 422
    assert client.get(url + "?recent_only=false").json()["recent_only"] is False
    assert client.get(url + "?recent_only=invalid").status_code == 422
    assert client.get(url + "?department_id=" + str(OTHER)).status_code == 403
    assert client.get(url + "?kind=arbitrary").status_code == 422
    assert client.get(url + "?include_impact=true").json()["include_impact"] is True
    assert client.get(url + "?include_impact=invalid").status_code == 422
    # A previous global date selection cannot narrow the latest product list.
    assert client.get(url + "?start=2026-08-01&end=2026-09-10").json() == client.get(url).json()


def test_purchase_impact_requires_auth_and_preserves_selected_price_scope(client):
    url = f"/api/purchase-prices/impact?product_id={USER}&store_id={UUID(int=4)}&unit_id={USER}"
    assert client.get(url).status_code == 401
    login(client)
    r = client.get(url + "&linked=true&month=2026-08-01")
    assert r.status_code == 200
    assert r.json() == {
        "ids": [str(DEPARTMENT)],
        "selection": [str(USER), str(UUID(int=4)), str(USER), "True"],
        "analysis_department_id": None,
        "all_departments": False,
    }
    assert client.get(url + "&department_id=" + str(OTHER)).status_code == 403
    assert client.get(url + "&linked=true&month=2026-01-01").json() == r.json()
    assert client.get(url + "&all_departments=true").json()["all_departments"] is True
    assert client.get(url + "&analysis_department_id=" + str(DEPARTMENT)).json()[
        "analysis_department_id"
    ] == str(DEPARTMENT)
    assert client.get(url + "&analysis_department_id=invalid").status_code == 422
    network_url = f"/api/purchase-prices/impact?product_id={USER}&unit_id={USER}"
    assert client.get(network_url).json()["selection"] == [str(USER), "None", str(USER), "False"]
    assert client.get(url.replace(str(USER), "bad-id")).status_code == 422
    client.repo.active = False
    assert client.get(url).status_code == 403


def test_price_history_validates_scope_and_does_not_leak_unknown_products(client):
    url = f"/api/purchase-prices/history?product_id={USER}&store_id={DEPARTMENT}&unit_id={USER}"
    assert client.get(url).status_code == 401
    login(client)
    assert client.get(url).status_code == 404
    network_url = f"/api/purchase-prices/history?product_id={USER}&unit_id={USER}"
    assert client.get(network_url).status_code == 404
    assert client.get(url + "&department_id=" + str(OTHER)).status_code == 403
    assert client.get("/api/purchase-prices/history?product_id=invalid").status_code == 422


def test_login_uses_database_role_and_safe_cookies(client):
    response = login(client)
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert all("HttpOnly" in c and "SameSite=strict" in c for c in cookies)
    assert client.get("/api/me").json() == {"role": "manager"}
    assert "access_token" not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    timing = client.get("/api/me").headers["Server-Timing"]
    assert all(f"{stage};dur=" in timing for stage in ("auth", "permissions", "total"))


def test_login_recovers_from_temporary_session_check_failure_without_replaying_password():
    calls = {"token": 0, "user": 0}

    def temporary_provider(request):
        key = request.url.path.rsplit("/", 1)[-1]
        if key in calls:
            calls[key] += 1
        if key == "user" and calls[key] == 1:
            return httpx.Response(503)
        return provider(request)

    app = create_portal(
        Settings(),
        WebSettings(_env_file=None, anon_key="test"),
        auth_transport=httpx.MockTransport(temporary_provider),
        repository=FakeRepository(),
    )
    with TestClient(app, base_url="http://127.0.0.1:8013") as client:
        assert login(client).status_code == 200
        assert calls == {"token": 1, "user": 2}
        assert client.get("/api/me").status_code == 200


def test_cross_origin_login_and_refresh_rejected(client):
    for path in ("/api/auth/login", "/api/auth/refresh", "/api/auth/logout"):
        response = client.post(
            path,
            headers={"Origin": "https://evil.invalid"},
            json={"email": "a@b.c", "password": "secret"},
        )
        assert response.status_code == 403


def test_disabled_user_and_department_tampering(client):
    login(client)
    assert client.get(f"/api/resources/invoices?department_id={OTHER}").status_code == 403
    response = client.get(f"/api/resources/invoices?department_id={DEPARTMENT}")
    assert response.json()["departments"] == [str(DEPARTMENT)]
    assert client.get(f"/api/resources/invoices/{OTHER}").status_code == 404
    assert (
        client.get("/api/topology?source_id=another-rms&day=2026-09-10&order_number=1").status_code
        == 403
    )
    client.repo.active = False
    assert client.get("/api/me").status_code == 403


def test_period_and_password_validation_are_sanitized(client):
    login(client)
    assert client.get("/api/sales/daily?start=2026-08-01&end=2026-09-10").status_code == 422
    password = "should-not-appear-in-response"
    response = client.post("/api/auth/login", json={"email": "x", "password": password})
    assert response.status_code == 422
    assert password not in response.text


def test_overview_is_scoped_and_validates_period_and_granularity(client):
    query = "/api/overview?start=2026-09-01&end=2026-09-10"
    assert client.get(query).status_code == 401
    login(client)
    assert client.get(query + f"&department_id={OTHER}").status_code == 403
    assert client.get(query + "&granularity=week").json() == {
        "ids": [str(DEPARTMENT)],
        "granularity": "week",
    }
    assert client.get(query + "&granularity=year").status_code == 422
    for start, end in [
        ("2026-09-10", "2026-09-01"),
        ("2026-08-01", "2026-09-10"),
        ("0001-01-01", "0001-01-01"),
    ]:
        assert client.get(f"/api/overview?start={start}&end={end}").status_code == 422


def test_logout_removes_local_session(client):
    login(client)
    response = client.post("/api/auth/logout", headers={"Origin": "http://127.0.0.1:8013"})
    assert response.status_code == 200
    assert client.get("/api/me").status_code == 401


def test_scope_ancestry_and_cycles():
    assert has_store_scope(3, {3: 2, 2: 1}, {1})
    assert not has_store_scope(3, {3: 2, 2: 3}, {1})
    assert not has_store_scope(3, {}, {1})
    scope = Scope({"role": "owner"}, (), None, (), ())
    assert scope.unrestricted
    assert not Scope({"role": "owner"}, (), DEPARTMENT, (), ()).unrestricted


def test_login_rate_limit(client):
    for _ in range(10):
        assert login(client).status_code == 200
    assert login(client).status_code == 429


def test_discount_details_require_origin_and_pass_only_visible_departments(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.portal.discount_details",
        lambda settings, payload, ids: calls.append(ids) or {"rows": []},
    )
    payload = {"report_id": str(OTHER), "ordinal": 0}
    assert client.post("/api/discount-details", json=payload).status_code == 401
    login(client)
    assert client.post("/api/discount-details", json=payload).status_code == 403
    assert calls == []
    headers = {"Origin": "http://127.0.0.1:8013"}
    assert client.post("/api/discount-details", json=payload, headers=headers).status_code == 200
    assert calls == [[DEPARTMENT]]
    assert (
        client.post(
            f"/api/discount-details?department_id={OTHER}", json=payload, headers=headers
        ).status_code
        == 403
    )


def test_topology_preserves_exact_order_uuid(client, monkeypatch):
    calls = []

    def reader(settings, source, day, number, *, order_id=None):
        calls.append((source, number, order_id))
        return {"root_order_id": str(order_id)}

    monkeypatch.setattr("app.portal.read_topology", reader)
    login(client)
    result = client.get(
        f"/api/topology?source_id=rms-test&day=2026-09-10&order_number=42&order_id={OTHER}"
    )
    assert result.status_code == 200 and calls == [("rms-test", 42, OTHER)]


def test_stock_suggestions_and_selection_use_authenticated_scope(client):
    assert client.get("/api/balance-products?q=milk").status_code == 401
    login(client)
    client.repo.balance_products = lambda scope, q, store_id: {
        "stores": [str(x) for x in scope.store_ids],
        "q": q,
        "store": str(store_id),
    }
    response = client.get(
        "/api/balance-products", params={"q": "milk", "store_id": str(UUID(int=4))}
    )
    assert response.status_code == 200 and response.json()["stores"] == [str(UUID(int=4))]
    assert client.get("/api/balance-products?store_id=invalid").status_code == 422
    client.repo.balances = lambda scope, q, offset, store_id, product_id: {
        "store": str(store_id),
        "product": str(product_id),
    }
    response = client.get(
        "/api/resources/balances",
        params={"store_id": str(UUID(int=4)), "product_id": str(UUID(int=5))},
    )
    assert response.json() == {"store": str(UUID(int=4)), "product": str(UUID(int=5))}
