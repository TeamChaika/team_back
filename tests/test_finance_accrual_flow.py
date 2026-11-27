"""Accrual and role boundaries through the HTTP API and a real isolated PostgreSQL."""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from test_overview import seed_day
from test_reference_sync import bundle as bundle
from test_reference_sync import db as db
from test_reference_sync import stage

from app.core.config import Settings
from app.portal import create_portal
from app.sync_references import Source, publish
from app.web.auth import ACCESS_COOKIE
from app.web.repository import Repository
from app.web.settings import WebSettings


def test_expense_accrues_once_and_paid_amount_locks_for_manager(db, bundle, monkeypatch):
    # The OLAP fixture shares its primary source with the restaurant directory.
    sources, snapshots = bundle
    sources[0] = Source("primary", "Test", "https://test.example/api", "a" * 64)
    departments = snapshots["primary", "departments"]["payload"]
    venue = departments["items"][0]["id"]
    second_venue = str(uuid4())
    departments["items"].append({**departments["items"][0], "id": second_venue, "name": "Second"})
    departments["total"] = len(departments["items"])
    stage(db, bundle)
    publish(db, sources, snapshots)
    october = date(2026, 10, 31)
    november = date(2026, 11, 5)
    for day in (october, november):
        seed_day(
            db, day, [{"department_id": venue, "revenue": 1000, "cost": 400}], kinds=("daily",)
        )

    db.execute("SET LOCAL ROLE postgres")
    first_approver, second_approver = str(uuid4()), str(uuid4())
    for department, approver, name in (
        (venue, first_approver, "First venue supervisor"),
        (second_venue, second_approver, "Second venue partner"),
    ):
        db.execute("INSERT INTO chaika.finance_approvers(id,name) VALUES(%s,%s)", (approver, name))
        db.execute(
            "INSERT INTO chaika.finance_department_approvers(department_id,approver_id) "
            "VALUES(%s,%s)", (department, approver),
        )
    identities = {}
    for person, role, sections in (
        ("manager", "manager", ["profit-loss"]),
        ("other-manager", "manager", ["profit-loss"]),
        ("accountant", "manager", ["profit-loss-accounting"]),
        ("owner", "owner", ["sales"]),
    ):
        uid = uuid4()
        identities["Bearer " + person] = str(uid)
        db.execute("INSERT INTO auth.users(id) VALUES(%s)", (uid,))
        db.execute(
            "INSERT INTO chaika.web_users(id,display_name,role,sections,password_change_required) "
            "VALUES(%s,%s,%s,%s,false)",
            (uid, person, role, sections),
        )
        db.execute("INSERT INTO chaika.web_department_access VALUES(%s,'primary',%s)", (uid, venue))
        if person in {"accountant", "owner"}:
            db.execute(
                "INSERT INTO chaika.web_department_access VALUES(%s,'primary',%s)",
                (uid, second_venue),
            )
    db.execute("SET LOCAL ROLE chaika_backend")
    # publish() checks all constraints immediately in its own production transaction.
    # The API gets a fresh transaction with deferred allocation constraints in production.
    db.execute("SET CONSTRAINTS chaika.finance_allocation_balanced, "
               "chaika.finance_allocation_share_balanced DEFERRED")
    db.row_factory = dict_row

    # Share the outer test transaction between HTTP requests. Its fixture rolls it back.
    @contextmanager
    def connection(**_kwargs):
        yield db

    class VerificationPool:
        def open(self):
            pass

        def connection(self):
            return connection()

    settings = Settings(_env_file=None, live_sales_enabled=False)
    repo = Repository(settings)
    monkeypatch.setattr(repo, "connection", connection)
    monkeypatch.setattr(repo, "_pool", VerificationPool())

    def auth(request):
        uid = identities.get(request.headers.get("authorization"))
        if request.url.path.endswith("/user") and uid:
            return httpx.Response(200, json={"id": uid})
        return httpx.Response(401)

    origin = "http://127.0.0.1:8013"
    app = create_portal(
        settings,
        WebSettings(_env_file=None, anon_key="test", origin=origin),
        auth_transport=httpx.MockTransport(auth),
        repository=repo,
    )
    base = "/api/profit-loss"
    ledger = base + "/expenses"
    period = {"department_id": str(venue), "start": str(october), "end": str(october)}
    charge = {
        "expense_date": str(october),
        "paid_date": None,
        "category": "rent",
        "amount": "180.25",
        "payment_method": "bank",
        "comment": "Октябрьская аренда",
    }
    payload = {"request_id": str(uuid4()), "department_id": str(venue),
               "approver_id": first_approver, **charge}
    with TestClient(app, base_url=origin, headers={"Origin": origin}) as client:
        client.cookies.set(ACCESS_COOKIE, "manager")
        assert client.get(base + "/approvers").json()["rows"] == [
            {"department_id": str(venue), "id": first_approver, "name": "First venue supervisor",
             "available_to_all": False}
        ]
        wrong_venue = {**payload, "approver_id": second_approver}
        assert client.post(ledger, json=wrong_venue).status_code == 422
        response = client.post(ledger, json=payload)
        assert response.is_success, response.text
        expense = response.json()
        assert expense["approver_name"] == "First venue supervisor"
        assert client.post(ledger, json=payload).json()["id"] == expense["id"]
        url = ledger + "/" + expense["id"]
        paid = {**charge, "paid_date": str(november), "revision": expense["revision"]}
        response = client.patch(url, json=paid)
        assert response.is_success, response.text
        paid["revision"] = response.json()["revision"]
        assert client.patch(url, json={**paid, "amount": "200"}).status_code == 403
        assert client.get(base + "/report", params=period).status_code == 403

        client.cookies.set(ACCESS_COOKIE, "other-manager")
        assert client.get(ledger, params=period).json()["total"] == 0
        client.cookies.set(ACCESS_COOKIE, "accountant")
        assert client.get(ledger, params=period).json()["total"] == 1
        assert client.get(base + "/report", params=period).status_code == 403

        client.cookies.set(ACCESS_COOKIE, "owner")
        result = client.get(base + "/report", params=period)
        assert result.is_success, result.text
        report = result.json()
        assert Decimal(report["expenses"]) == Decimal("180.25")
        assert Decimal(report["operating_profit"]) == Decimal("419.75")
        payment_period = {**period, "start": str(november), "end": str(november)}
        report = client.get(base + "/report", params=payment_period).json()
        assert Decimal(report["expenses"]) == 0
        assert Decimal(report["operating_profit"]) == 600

        # The person who approved the original charge has since left this restaurant.
        db.execute("SET LOCAL ROLE postgres")
        db.execute("DELETE FROM chaika.finance_department_approvers WHERE approver_id=%s",
                   (first_approver,))
        db.execute("SET LOCAL ROLE chaika_backend")
        allocation = {
            **paid,
            "request_id": str(uuid4()),
            "method": "equal",
            # Omit the source venue's approver: preserve the manager's original approval.
            "parts": [{"department_id": venue},
                      {"department_id": second_venue, "approver_id": second_approver}],
        }
        client.cookies.set(ACCESS_COOKIE, "manager")
        assert client.post(url + "/allocate", json=allocation).status_code == 403
        client.cookies.set(ACCESS_COOKIE, "accountant")
        response = client.post(url + "/allocate", json=allocation)
        assert response.is_success, response.text
        group = response.json()
        db.execute("SET CONSTRAINTS ALL IMMEDIATE")
        assert group["source_expense_id"] == expense["id"]
        assert group["author_id"] == identities["Bearer manager"]
        assert {p["department_id"]: p["approver_name"] for p in group["parts"]} == {
            str(venue): "First venue supervisor", str(second_venue): "Second venue partner",
        }
        assert sorted(part["amount"] for part in group["parts"]) == ["90.12", "90.13"]
        assert client.post(url + "/allocate", json=allocation).json()["id"] == group["id"]
        client.cookies.set(ACCESS_COOKIE, "manager")
        original = client.get(ledger, params=period).json()["rows"]
        assert len(original) == 1 and original[0]["status"] == "converted"
        assert original[0]["approver_id"] == first_approver
        assert client.get(url + "/history").json()["rows"][0]["actor"] == "accountant"
        client.cookies.set(ACCESS_COOKIE, "owner")
        report = client.get(
            base + "/report", params={**period, "department_id": [venue, second_venue]},
        ).json()
        assert Decimal(report["expenses"]) == Decimal("180.25")
        assert report["allocated_expenses"] == report["expenses"]
        assert Decimal(report["direct_expenses"]) == 0
