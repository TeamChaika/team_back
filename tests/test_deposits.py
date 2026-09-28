"""The dashboard bridges only a validated session to fixed deposit endpoints."""

from dataclasses import replace
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.portal import create_portal
from app.web.auth import ACCESS_COOKIE
from app.web.deposits import XLSX
from app.web.settings import WebSettings
from tests.test_portal import USER, FakeRepository, login, provider
from tests.test_restaurant_selection import permission_repo as permission_repo_fixture

permission_repo = permission_repo_fixture

DEPOSIT = UUID(int=20)
ROUTES = [
    ("GET", "/api/deposits", {}),
    ("GET", "/api/deposits/export", {}),
    ("GET", "/api/deposits/permissions", {}),
    ("GET", "/api/deposits/venues", {}),
    ("GET", "/api/deposits/access", {}),
    ("GET", f"/api/deposits/{DEPOSIT}", {}),
    ("POST", "/api/deposits/access/grant", {"json": {"user_id": str(USER), "venue": "A"}}),
    (
        "POST",
        "/api/deposits/access/update",
        {"json": {"user_id": str(USER), "venue": "A", "is_all": True}},
    ),
    ("POST", "/api/deposits/access/revoke", {"json": {"user_id": str(USER), "venue": "A"}}),
]


class DepositRepository(FakeRepository):
    deposit_only = False
    directory_reads = 0

    def portal_scope(self, user_id):
        scope = super().scope(user_id)
        return (
            replace(scope, user={**scope.user, "role": "deposits"}) if self.deposit_only else scope
        )

    def scope(self, *args):
        if self.deposit_only:
            raise HTTPException(403, "Only deposits")
        return super().scope(*args)

    def deposit_users(self):
        self.directory_reads += 1
        return [{"id": str(USER), "display_name": "Test"}]


@pytest.fixture
def client():
    calls = []
    state = {"status": 200, "json": {"items": [], "total": 0}}

    def upstream(request):
        calls.append(request)
        if state.get("raise"):
            raise httpx.ConnectError("private internal host and credential")
        if state["status"] != 200:
            return httpx.Response(state["status"], text="private upstream diagnostics")
        if request.url.path.endswith("/export"):
            return httpx.Response(200, content=b"PK-xlsx", headers={"Content-Type": XLSX})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path.endswith("/user-venues"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=state["json"])

    repo = DepositRepository()
    app = create_portal(
        Settings(_env_file=None),
        WebSettings(_env_file=None, anon_key="test"),
        auth_transport=httpx.MockTransport(provider),
        repository=repo,
        deposits_transport=httpx.MockTransport(upstream),
    )
    with TestClient(app, base_url="http://127.0.0.1:8013") as client:
        client.repo = repo
        client.upstream_calls = calls
        client.upstream_state = state
        yield client


@pytest.mark.parametrize("token", [None, "forged"])
def test_all_deposit_routes_deny_missing_or_invalid_session(client, token):
    if token:
        client.cookies.set(ACCESS_COOKIE, token)
    for method, path, kwargs in ROUTES:
        assert client.request(method, path, **kwargs).status_code == 401
    assert not client.upstream_calls


def test_disabled_portal_account_cannot_use_deposits(client):
    login(client)
    client.repo.active = False
    assert client.get("/api/deposits").status_code == 403
    assert not client.upstream_calls


def test_deposit_only_login_and_refresh_work_but_iiko_stays_forbidden(client):
    client.repo.deposit_only = True
    assert login(client).status_code == 200
    assert client.get("/api/me").json()["modules"] == ["deposits"]
    assert (
        client.post("/api/auth/refresh", headers={"Origin": "http://127.0.0.1:8013"}).status_code
        == 200
    )
    assert client.get("/api/deposits").status_code == 200
    assert client.get("/api/overview?start=2026-09-01&end=2026-09-01").status_code == 403
    assert client.get("/api/resources/employees").status_code == 403


def test_fixed_host_and_verified_cookie_token_only(client):
    login(client)
    response = client.get(
        "/api/deposits?query=A%26B%20%22C%22&restaurant=Test",
        headers={"Authorization": "Bearer attacker-token"},
    )
    assert response.status_code == 200
    request = client.upstream_calls[-1]
    assert str(request.url).startswith("https://pay.chaika.team/api/v1/deposits/")
    assert request.url.params["query"] == 'A&B "C"'
    assert request.headers["Authorization"] == "Bearer verified"
    assert "cookie" not in request.headers


def test_export_keeps_session_and_applied_filters(client):
    login(client)
    response = client.get("/api/deposits/export?restaurant=Test&status_filter=paid&page=3")
    assert response.status_code == 200 and response.content == b"PK-xlsx"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].endswith('"deposits.xlsx"')
    upstream = client.upstream_calls[-1]
    assert upstream.url.params["restaurant"] == "Test"
    assert upstream.url.params["status_filter"] == "paid"
    assert upstream.url.params["page"] == "1"
    assert upstream.url.params["page_size"] == "1000"


def test_origin_required_for_every_access_mutation(client):
    login(client)
    for method, path, kwargs in ROUTES:
        if method == "POST":
            for origin in (None, "https://attacker.example"):
                headers = {"Origin": origin} if origin else {}
                assert client.post(path, headers=headers, **kwargs).status_code == 403
    assert not client.upstream_calls


def test_admin_role_is_enforced_by_deposit_service_even_for_dashboard_owner(client):
    login(client)
    client.upstream_state["status"] = 403
    for method, path, kwargs in ROUTES:
        if "/access" in path:
            assert (
                client.request(
                    method, path, headers={"Origin": "http://127.0.0.1:8013"}, **kwargs
                ).status_code
                == 403
            )
    assert client.repo.directory_reads == 0


def test_authorized_access_crud_maps_to_fixed_methods(client):
    login(client)
    response = client.get("/api/deposits/access")
    assert response.json()["users"][0]["id"] == str(USER)
    for path, expected_method in (("grant", "POST"), ("update", "PATCH"), ("revoke", "DELETE")):
        response = client.post(
            f"/api/deposits/access/{path}",
            headers={"Origin": "http://127.0.0.1:8013"},
            json={"user_id": str(USER), "venue": "Test"},
        )
        assert response.status_code == (204 if path == "revoke" else 200)
        assert client.upstream_calls[-1].method == expected_method


@pytest.mark.parametrize(
    "query",
    [
        "page_size=201",
        "sort_by=password",
        "url=http://evil",
        "min_amount=20&max_amount=10",
        "date_from=2026-09-02T00:00:00Z&date_to=2026-09-01T00:00:00Z",
    ],
)
def test_invalid_filters_never_reach_upstream(client, query):
    login(client)
    assert client.get("/api/deposits?" + query).status_code == 422
    assert not client.upstream_calls


@pytest.mark.parametrize("status", [301, 500, 502])
def test_upstream_errors_do_not_expose_diagnostics(client, status):
    login(client)
    client.upstream_state["status"] = status
    response = client.get("/api/deposits")
    assert response.status_code == 503
    assert "private" not in response.text


def test_transport_failure_and_invalid_json_fail_closed(client):
    login(client)
    client.upstream_state["raise"] = True
    assert client.get("/api/deposits").status_code == 503
    client.upstream_state.pop("raise")
    client.upstream_state["json"] = "unexpected text"
    assert client.get("/api/deposits").status_code == 503


@pytest.mark.usefixtures("permission_repo")
def test_real_repository_denies_iiko_even_if_deposit_account_has_old_grants(request):
    repo, db = request.getfixturevalue("permission_repo")
    db.role = "deposits"
    scope = repo.portal_scope(USER)
    assert scope.ids == [] and not scope.unrestricted
    assert repo.metadata(scope)["departments"] == []
    with pytest.raises(HTTPException) as failure:
        repo.scope(USER)
    assert failure.value.status_code == 403
