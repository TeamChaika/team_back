"""A temporary password cannot authorize business reads or writes, including old sessions."""

import pytest

from tests.test_document_only_scope import repository
from tests.test_portal import USER, login
from tests.test_portal import client as client

ORIGIN = {"Origin": "http://127.0.0.1:8013"}


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/purchase-prices"),
        ("GET", "/api/sales/daily?start=2026-09-01&end=2026-09-01"),
        ("GET", "/api/documents/waybill"),
        ("GET", "/api/documents/writeoff/1"),
        ("GET", "/api/documents/waybill/export"),
        ("POST", "/api/documents/waybill/1/approve"),
        ("GET", "/api/deposits/creation-venues"),
        ("GET", "/api/management/accounts"),
        ("GET", "/api/profile/telegram"),
        ("POST", "/api/profile/telegram/link"),
        ("POST", "/api/profile/telegram/unlink"),
        ("GET", "/api/assistant/conversations"),
        ("GET", "/api/indicators/filters"),
    ],
)
def test_existing_session_is_restricted_on_next_business_request(client, method, path):
    assert login(client).status_code == 200
    assert client.get("/api/purchase-prices").status_code == 200
    client.repo.password_change_required = True
    response = client.request(method, path, headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "password_change_required"


def test_login_refresh_and_minimal_identity_remain_available_without_unlocking(client):
    client.repo.password_change_required = True
    assert login(client).json()["password_change_required"] is True
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["user"] == {
        "id": str(USER),
        "display_name": "Менеджер",
        "role": "manager",
        "password_change_required": True,
    }
    for field in ("departments", "sales_dates", "balance_dates", "sections", "modules"):
        assert me.json()[field] == []
    assert me.json()["can_manage"] is False
    refreshed = client.post("/api/auth/refresh", headers=ORIGIN)
    assert refreshed.status_code == 200
    assert refreshed.json()["password_change_required"] is True
    assert client.repo.password_change_required
    assert client.post("/api/auth/logout", headers=ORIGIN).status_code == 200
    assert client.get("/api/me").status_code == 401


def test_required_account_without_restaurant_grants_can_reach_password_form():
    repo, db = repository()
    db.sections = ["sales"]
    db.password_change_required = True
    scope = repo.portal_scope(USER)
    assert scope.user["password_change_required"]
    assert scope.departments == ()
    assert scope.store_ids == ()
    assert scope.rms_ids == ()
    assert repo.scope(USER).user["password_change_required"]


def test_inactive_account_is_not_admitted_to_onboarding(client):
    client.repo.password_change_required = True
    client.repo.active = False
    assert login(client).status_code == 403
