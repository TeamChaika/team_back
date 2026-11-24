"""A selected group narrows every data scope without broadening user grants."""

from contextlib import contextmanager
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.portal import create_portal
from app.web.repository import Repository
from app.web.settings import WebSettings
from app.web.sync_policy import SyncPolicy
from tests.test_portal import USER, FakeRepository, login, provider

A, B, C, CHILD = (UUID(int=i) for i in (10, 20, 30, 40))
SA, SB, SC, NESTED = (UUID(int=i) for i in (101, 102, 103, 104))


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class PermissionDatabase:
    """Only supplies source records; actual Repository.scope resolves the selection."""

    role = "manager"

    def execute(self, sql, params=None):
        if "chaika.web_users" in sql:
            return Rows([{"id": USER, "role": self.role, "display_name": "Test"}])
        if "chaika.corporate_nodes" in sql:
            return Rows(
                [
                    {"id": id_, "parent_id": None, "name": name, "code": name, "type": "DEPARTMENT"}
                    for id_, name in ((A, "A"), (B, "B"), (C, "C"))
                ]
                + [{"id": CHILD, "parent_id": A, "name": "Child", "code": None, "type": "GROUP"}]
            )
        if "chaika.web_department_access" in sql:
            return Rows([{"department_id": A}, {"department_id": B}])
        if "chaika.stores" in sql:
            return Rows(
                [
                    {"id": SA, "parent_id": A},
                    {"id": SB, "parent_id": B},
                    {"id": SC, "parent_id": C},
                    {"id": NESTED, "parent_id": CHILD},
                ]
            )
        if "chaika.rms_bindings" in sql:
            return Rows(
                [
                    {"source_id": name, "department_id": id_}
                    for name, id_ in (("rms-a", A), ("rms-b", B), ("rms-c", C))
                ]
            )
        raise AssertionError(sql)


@pytest.fixture
def permission_repo():
    db = PermissionDatabase()
    repo = object.__new__(Repository)

    @contextmanager
    def connection():
        yield db

    repo.connection = connection
    return repo, db


def test_group_unions_stores_rms_and_employee_codes(permission_repo):
    repo, _ = permission_repo
    scope = repo.scope(USER, (B, A, B))
    assert scope.selection_ids == (A, B)
    assert scope.ids == [A, B]
    assert set(scope.store_ids) == {SA, SB, NESTED}
    assert set(scope.rms_ids) == {"rms-a", "rms-b"}
    assert scope.codes == ["A", "B"]
    assert not scope.unrestricted


def test_single_and_all_remain_compatible(permission_repo):
    repo, _ = permission_repo
    single = repo.scope(USER, B)
    assert single.selected == B
    assert single.ids == [B]
    assert single.store_ids == (SB,)
    assert single.rms_ids == ("rms-b",)
    assert repo.scope(USER).ids == [A, B]
    assert repo.scope(USER, ()).selected is None


@pytest.mark.parametrize("selection", [(A, C), (A, UUID(int=999))])
def test_one_forbidden_restaurant_rejects_entire_group(permission_repo, selection):
    repo, _ = permission_repo
    with pytest.raises(HTTPException) as error:
        repo.scope(USER, selection)
    assert error.value.status_code == 403


def test_owner_filter_is_not_unrestricted_even_when_all_listed_are_selected(permission_repo):
    repo, db = permission_repo
    db.role = "owner"
    assert repo.scope(USER).unrestricted
    assert not repo.scope(USER, (A, B, C)).unrestricted
    assert set(repo.scope(USER, (A, B)).store_ids) == {SA, SB, NESTED}


def test_sales_sync_only_filters_grants_and_preserves_normal_selections(permission_repo):
    repo, _ = permission_repo
    repo.sync_policy = lambda: SyncPolicy(1, frozenset([str(A), str(C)]), frozenset([str(A)]))
    endpoints = FakeRepository()
    endpoints.scope = repo.scope
    endpoints.implicit_report_scope = repo.implicit_report_scope
    endpoints.sales = lambda scope, *args: {
        "ids": [str(i) for i in scope.ids],
        "stores": sorted(str(i) for i in scope.store_ids),
    }
    app = create_portal(
        Settings(),
        WebSettings(_env_file=None, anon_key="test"),
        auth_transport=httpx.MockTransport(provider),
        repository=endpoints,
    )
    with TestClient(app, base_url="http://127.0.0.1:8013") as client:
        assert login(client).status_code == 200
        url = "/api/sales/daily?start=2026-08-01&end=2026-08-01"
        assert client.get(url).json()["ids"] == [str(A), str(B)]
        assert client.get(url + f"&department_id={B}").json()["ids"] == [str(B)]
        assert client.get(url + "&sync_only=true").json() == {
            "ids": [str(A)],
            "stores": sorted([str(SA), str(NESTED)]),
        }
        assert client.get(url + "&sync_only=true&include_archived=false").status_code == 423
        assert client.get(url + f"&sync_only=true&department_id={B}").status_code == 422


def test_sales_sync_only_resolves_more_than_100_granted_venues(permission_repo, monkeypatch):
    repo, db = permission_repo
    departments = [UUID(int=n) for n in range(1000, 1151)]
    execute = db.execute

    def large_network(sql, params=None):
        if "chaika.corporate_nodes" in sql:
            return Rows(
                [
                    {
                        "id": venue,
                        "parent_id": None,
                        "name": str(venue),
                        "code": None,
                        "type": "DEPARTMENT",
                    }
                    for venue in departments
                ]
            )
        if "chaika.web_department_access" in sql:
            return Rows([{"department_id": venue} for venue in departments])
        return execute(sql, params)

    monkeypatch.setattr(db, "execute", large_network)
    enabled = departments[:125]
    repo.sync_policy = lambda: SyncPolicy(1, frozenset(map(str, enabled)))
    assert (
        repo.implicit_report_scope(repo.scope(USER), sync_only=True, include_archived=True).ids
        == enabled
    )


def test_overview_and_price_history_keep_the_same_permitted_scope(permission_repo):
    repo, _ = permission_repo
    repo.sync_policy = lambda: SyncPolicy(1, frozenset([str(A), str(C)]), frozenset([str(B)]))
    endpoints = FakeRepository()
    endpoints.scope = repo.scope
    endpoints.implicit_report_scope = repo.implicit_report_scope
    endpoints.sync_policy = repo.sync_policy

    def echo_prices(scope, kind, exclude_household, selection=None, **kwargs):
        report = {"ids": scope.ids, "stores": scope.store_ids}
        return {"rows": [report]} if selection else report

    endpoints.purchase_prices = echo_prices
    endpoints.sales = lambda scope, *args: {"ids": scope.ids}
    app = create_portal(
        Settings(),
        WebSettings(_env_file=None, anon_key="test"),
        auth_transport=httpx.MockTransport(provider),
        repository=endpoints,
    )
    with TestClient(app, base_url="http://127.0.0.1:8013") as client:
        assert login(client).status_code == 200
        overview = "/api/overview?start=2026-08-01&end=2026-08-01"
        for view in ("full", "trend"):
            assert client.get(
                overview + f"&view={view}&sync_only=true&include_archived=true"
            ).json()["ids"] == [str(A)]
        assert client.get(overview + f"&department_id={A}&sync_only=true").status_code == 422
        assert client.get(overview + "&include_archived=true").json()["ids"] == [str(A), str(B)]
        prices = "/api/purchase-prices?view=top_increases"
        history = f"/api/purchase-prices/history?product_id={A}&unit_id={B}"
        for query in ("&sync_only=true", "&include_archived=false"):
            for path in (prices, history):
                result = client.get(path + query).json()
                assert result["ids"] == [str(A)]
                assert set(result["stores"]) == {str(SA), str(NESTED)}
        assert client.get(prices).json()["ids"] == [str(A), str(B)]
        sales = "/api/sales/daily?start=2026-08-01&end=2026-08-01"
        assert client.get(sales + "&include_archived=false").json()["ids"] == [str(A)]
        assert client.get(sales + f"&department_id={B}").json()["ids"] == [str(B)]
        repo.sync_policy = lambda: SyncPolicy(2, frozenset([str(A), str(B)]))
        endpoints.sync_policy = repo.sync_policy
        selected = f"&department_id={A}&department_id={B}&include_archived=false"
        for path in (overview, prices, history):
            response = client.get(path + selected)
            assert response.status_code == 200
            assert response.json()["ids"] == [str(A), str(B)]


def test_repeated_query_parameters_apply_group_and_validate_each_value(permission_repo):
    repo, _ = permission_repo
    endpoints = FakeRepository()
    endpoints.scope = repo.scope
    app = create_portal(
        Settings(),
        WebSettings(_env_file=None, anon_key="test"),
        auth_transport=httpx.MockTransport(provider),
        repository=endpoints,
    )
    with TestClient(app, base_url="http://127.0.0.1:8013") as client:
        assert login(client).status_code == 200
        url = "/api/overview"
        dates = [("start", "2026-08-01"), ("end", "2026-08-01")]
        group = client.get(url, params=dates + [("department_id", str(i)) for i in (B, A, B)])
        assert group.status_code == 200
        assert group.json()["ids"] == [str(A), str(B)]
        assert client.get(url, params=dates + [("department_id", str(B))]).json()["ids"] == [str(B)]
        assert client.get(url, params=dates).json()["ids"] == [str(A), str(B)]
        denied = client.get(url, params=dates + [("department_id", str(i)) for i in (A, C)])
        assert denied.status_code == 403
        malformed = client.get(
            url, params=dates + [("department_id", str(A)), ("department_id", "bad")]
        )
        assert malformed.status_code == 422
        assert client.get(url, params=dates + [("department_id", str(A))] * 101).status_code == 422
