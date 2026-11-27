"""Catalog maintenance through the finance API against an isolated Postgres ledger."""

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_finance_ledger import ledger_db as ledger_db

from app.web.finance_routes import create_finance_router
from app.web.repository import Scope

BASE = "/api/profit-loss"
ORIGIN = {"Origin": "https://dashboard.example"}


@pytest.fixture
def directory(ledger_db):
    ledger, db, users, venues = ledger_db
    active = SimpleNamespace(scope=None)

    def as_user(index):
        uid = users[index].user["id"]
        row = db.execute("SELECT id,role,sections FROM web_users WHERE id=%s", (uid,)).fetchone()
        active.scope = Scope(
            row, tuple({"id": i, "type": "DEPARTMENT"} for i in venues), None, (), ()
        )

    def guard(request):
        if request.headers.get("origin") != ORIGIN["Origin"]:
            raise HTTPException(403, "Origin")

    app = FastAPI()
    app.state.auth = SimpleNamespace(check_origin=guard)
    app.include_router(create_finance_router(lambda: active.scope, ledger.repo, None))
    with TestClient(app) as client:
        yield client, as_user, venues, db, users


def test_category_edits_are_versioned_and_hidden_from_new_expenses_when_archived(directory):
    client, user, _, _, _ = directory
    categories = BASE + "/catalog/categories"
    user(0)
    assert len(client.get(BASE + "/categories").json()["rows"]) == 7
    assert client.get(BASE + "/catalog").status_code == 403
    assert client.post(categories, headers=ORIGIN, json={"name": "Travel"}).status_code == 403
    user(2)  # accounting with one venue can maintain shared expense categories
    assert client.post(categories, json={"name": "Travel"}).status_code == 403
    assert client.post(categories, headers=ORIGIN, json={"name": "  "}).status_code == 422
    new = client.post(categories, headers=ORIGIN, json={"name": "Travel"}).json()
    code = new["code"]
    assert code.startswith("cat_") and new["revision"] == 1
    assert client.post(categories, headers=ORIGIN, json={"name": " travel "}).status_code == 409
    url = categories + "/" + code
    edit = {"revision": 1, "name": "Travel costs", "archived": False}
    assert client.patch(url, headers=ORIGIN, json=edit).json()["revision"] == 2
    assert client.patch(url, headers=ORIGIN, json=edit).status_code == 409
    assert client.patch(
        url, headers=ORIGIN, json={**edit, "revision": 2, "archived": True}
    ).is_success
    assert code not in {r["code"] for r in client.get(BASE + "/categories").json()["rows"]}
    old = next(r for r in client.get(BASE + "/catalog").json()["categories"] if r["code"] == code)
    assert old["archived"] and old["name"] == "Travel costs"
    assert client.patch(url, headers=ORIGIN, json={**edit, "revision": 3}).is_success
    assert {"code": code, "name": "Travel costs"} in client.get(BASE + "/categories").json()["rows"]


def test_approver_visibility_global_assignment_and_overlap(directory):
    client, user, venues, db, users = directory
    first, second = map(str, venues)
    url = BASE + "/catalog/approvers"
    full = {"name": "Network partner", "department_ids": [], "available_to_all": True}
    local = {"name": "Supervisor", "department_ids": [first], "available_to_all": False}
    user(3)
    global_row = client.post(url, headers=ORIGIN, json=full).json()
    second_row = client.post(url, headers=ORIGIN, json={**local, "department_ids": [second]}).json()
    multi = client.post(
        url,
        headers=ORIGIN,
        json={**local, "name": "Operations lead", "department_ids": [first, second]},
    ).json()
    user(2)
    catalog = client.get(BASE + "/catalog").json()
    assert not catalog["can_manage_global_approvers"]
    assert {r["id"] for r in catalog["venues"]} == {first}
    people = {p["id"]: p for p in catalog["approvers"]}
    assert set(people) == {global_row["id"], multi["id"]}
    assert people[multi["id"]]["department_ids"] == [first]
    assert not people[multi["id"]]["can_edit"] and not people[global_row["id"]]["can_edit"]
    attempt = {**local, "revision": 1, "archived": True}
    assert (
        client.patch(url + "/" + second_row["id"], headers=ORIGIN, json=attempt).status_code == 404
    )
    assert client.patch(url + "/" + multi["id"], headers=ORIGIN, json=attempt).status_code == 403
    assert (
        client.patch(url + "/" + global_row["id"], headers=ORIGIN, json=attempt).status_code == 403
    )
    assert client.post(url, headers=ORIGIN, json=full).status_code == 403
    assert (
        client.post(url, headers=ORIGIN, json={**local, "department_ids": [second]}).status_code
        == 403
    )
    single = client.post(url, headers=ORIGIN, json=local).json()
    assert single["can_edit"] and single["department_ids"] == [first]
    assert client.post(url, headers=ORIGIN, json=local).status_code == 409
    first_url = url + "/" + single["id"]
    assert client.patch(first_url, headers=ORIGIN, json=attempt).json()["archived"]
    assert client.patch(first_url, headers=ORIGIN, json=attempt).status_code == 409
    operational = client.get(BASE + "/approvers").json()["rows"]
    assert single["id"] not in {p["id"] for p in operational}
    db.execute("UPDATE web_users SET all_departments=true WHERE id=%s", (users[2].user["id"],))
    assert client.get(BASE + "/catalog").json()["can_manage_global_approvers"]
    reassign = {
        **local,
        "name": "Restored supervisor",
        "department_ids": [second],
        "revision": 2,
        "archived": False,
    }
    assert client.patch(first_url, headers=ORIGIN, json=reassign).json()["department_ids"] == [
        second
    ]
    only_global = {**full, "revision": 3, "name": "Restored supervisor", "archived": False}
    result = client.patch(first_url, headers=ORIGIN, json=only_global).json()
    assert result["available_to_all"] and result["department_ids"] == []
    assert not db.execute(
        "SELECT FROM finance_department_approvers WHERE approver_id=%s", (UUID(single["id"]),)
    ).fetchall()
