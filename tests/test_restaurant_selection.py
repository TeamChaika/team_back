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
