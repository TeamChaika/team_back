"""Synthetic tenant sources only: no real iiko, shared dashboard data or credentials."""

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.saas_admin import dashboard_reports as reports
from app.saas_admin import dashboard_transport as transport
from app.saas_admin.dashboard_service import DashboardService
from app.saas_admin.dashboard_source import DashboardSource
from app.saas_admin.pg_tenant_access import PostgresTenantAccess
from app.saas_admin.repository import Problem
from app.saas_admin.server import create_app

DEPARTMENT = str(UUID(int=10))
DISH = str(UUID(int=20))
NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
START = date(2026, 10, 7)


def source(company="one", fingerprint="v1"):
    return DashboardSource(
        company,
        company,
        "member-" + company,
        "Admin",
        1,
        fingerprint,
        "https://" + company + ".iiko.it",
        "api",
        "synthetic-secret",
    )


def columns():
    fields = set(reports.FIELDS.values()) | {
        "Department.Id",
        "OpenDate.Typed",
        "DishId",
        "DishName",
        "DeletedWithWriteoff",
        "OrderDeleted",
    }
    return json.dumps(
        {
            key: {"aggregationAllowed": True, "groupingAllowed": True, "filteringAllowed": True}
            for key in fields
        }
    ).encode()


def hierarchy():
    return (
        f"<corporateItemDtoes><corporateItemDto><id>{DEPARTMENT}</id>"
        "<name>Restaurant</name><type>DEPARTMENT</type><code>1</code>"
        "</corporateItemDto></corporateItemDtoes>"
    ).encode()


class Provider:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.empty = False
        self.missing_cost = False

    def __call__(self, selected, operations):
        self.calls.append((selected.company_id, operations))
        if self.fail:
            raise Problem(503, "iiko_timeout", "Synthetic timeout")
        result = []
        for operation, request in operations:
            if operation == "departments":
                result.append(hierarchy())
                continue
            if operation == "columns":
                result.append(columns())
                continue
            assert request["filters"]["Department.Id"]["values"] == [DEPARTMENT]
            money = "100.25" if selected.company_id == "one" else "800.50"
            row = {
                "DishDiscountSumInt": money,
                "ProductCostBase.ProductCost": "25.00",
                "GuestNum": 9,
                "UniqOrderId": 7,
                "DishAmountInt": "2",
            }
            if self.missing_cost:
                del row["ProductCostBase.ProductCost"]
            for group in request["groupByRowFields"]:
                row[group] = {
                    "Department.Id": DEPARTMENT,
                    "OpenDate.Typed": "2026-10-07",
                    "DishId": DISH,
                    "DishName": "Dish",
                }[group]
            result.append(json.dumps({"data": [] if self.empty else [row]}).encode())
        return result


def service(provider):
    return DashboardService(provider, now=lambda: NOW)


def test_identical_iiko_uuids_are_isolated_by_company_and_source_revision():
    provider = Provider()
    app = service(provider)
    one = app.sales(source(), START, START, [], "daily")
    two = app.sales(source("two"), START, START, [], "daily")
    assert one["totals"]["revenue"] == "100.25"
    assert two["totals"]["revenue"] == "800.50"
    assert len(provider.calls) == 4
    assert app.sales(source(), START, START, [], "daily") == one
    assert len(provider.calls) == 4
    app.sales(source(fingerprint="changed-credentials"), START, START, [], "daily")
    assert len(provider.calls) == 6
    assert "synthetic-secret" not in json.dumps(one)


def test_unknown_department_rejected_before_sales_query():
    provider = Provider()
    with pytest.raises(Problem) as error:
        service(provider).sales(source(), START, START, [str(UUID(int=99))], "daily")
    assert error.value.status == 403
    assert len(provider.calls) == 1


def test_overview_contract_reuses_formula_and_uses_ungrouped_counters():
    provider = Provider()
    value = service(provider).overview(source(), START, START, [], "day")
    assert value["current"]["totals"]["checks"] == "7"
    assert value["current"]["totals"]["guests"] == "9"
    assert value["current"]["totals"]["markup"] == "301.00"
    assert value["restaurants"][0]["totals"]["checks"] is None
    assert value["trend"][0]["current"]["totals"]["checks"] is None
    assert set(value["trends"]) == {"day", "week", "month"}
    assert value["top_dishes"][0]["dish_id"] == DISH
    queries = provider.calls[-1][1]
    assert queries[3][1]["groupByRowFields"] == []
    assert queries[4][1]["groupByRowFields"] == []
    for index in (1, 2):
        assert queries[index][1]["groupByRowFields"] == ["Department.Id", "DishId", "DishName"]
    assert queries[1][1]["filters"]["OpenDate.Typed"]["from"] == "2026-10-07T00:00:00"
    assert queries[2][1]["filters"]["OpenDate.Typed"]["to"] == "2026-10-07T00:00:00"
    assert value["data_status"]["cache_seconds"] == 300


def test_successful_empty_is_zero_but_missing_cost_is_unknown():
    provider = Provider()
    provider.empty = True
    value = service(provider).sales(source(), START, START, [], "daily")
    assert value["rows"] == []
    assert value["totals"]["revenue"] == "0"
    assert value["totals"]["checks"] == "0"
    provider.empty = False
    provider.missing_cost = True
    value = service(provider).sales(source(), START, START, [], "daily")
    assert value["totals"]["cost"] is None
    assert value["totals"]["markup"] is None


def test_error_never_becomes_empty_data_and_expires_cache():
    provider = Provider()
    clock = [0]
    app = DashboardService(provider, clock=lambda: clock[0], now=lambda: NOW)
    app.sales(source(), START, START, [], "daily")
    clock[0] = 301
    provider.fail = True
    with pytest.raises(Problem) as error:
        app.sales(source(), START, START, [], "daily")
    assert error.value.code == "iiko_timeout"


@pytest.mark.parametrize(
    "body",
    [
        b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><corporateItemDtoes>&x;</corporateItemDtoes>',
        b"<html>error</html>",
        b"<corporateItemDtoes><wrong/></corporateItemDtoes>",
    ],
)
def test_xml_rejects_entities_and_wrong_shapes(body):
    with pytest.raises(Problem):
        reports.departments(body)


@pytest.mark.parametrize("value", ["NaN", "Infinity", True, "not-money", "1e99"])
def test_invalid_money_is_never_returned(value):
    request = reports.query(START, START, [DEPARTMENT], [], ["revenue"])
    with pytest.raises(Problem):
        reports.rows(
            json.dumps({"data": [{"DishDiscountSumInt": value}]}).encode(),
            request,
            START,
            START,
            [DEPARTMENT],
        )


class Gate:
    def get(self, company_id):
        return {"modules": {"analytics": True}, "subscription": {"policy": "legacy"}}

    blocked = None
    revision = 1

    def tenant_dashboard_source(self, token, slug):
        if token != "valid" or slug != "one":
            raise Problem(401, "unauthorized", "Denied")
        if self.blocked:
            raise Problem(403, self.blocked, "Denied")
        return replace(source(), version=self.revision)


def test_gate_is_checked_on_cache_hit_and_after_fetch(tmp_path):
    repo = Gate()
    app = create_app(tmp_path, repository=repo)
    provider = Provider()
    app.state.tenant_dashboard.fetch = provider
    app.state.tenant_dashboard.now = lambda: NOW
    with TestClient(app, base_url="http://127.0.0.1:8210") as client:
        client.cookies.set("saas_tenant_session", "valid", path="/api/saas-tenant")
        path = "/api/saas-tenant/one/dashboard/sales/daily?start=2026-10-07&end=2026-10-07"
        assert client.get(path).status_code == 200
        before = len(provider.calls)
        for code in (
            "module_disabled",
            "unauthorized",
            "password_change_required",
            "company_inactive",
        ):
            repo.blocked = code
            assert client.get(path).status_code == 403
        assert len(provider.calls) == before
        repo.blocked = None
        assert client.get(path.replace("/one/", "/two/")).status_code == 401
        assert client.get(path.replace("sales/daily", "sales/payments")).status_code == 422
        assert client.get("/api/saas-tenant/one/dashboard/no-such-api").status_code == 404


def test_revocation_during_source_fetch_never_returns_data(tmp_path):
    repo = Gate()
    app = create_app(tmp_path, repository=repo)
    provider = Provider()

    def fetch_then_revoke(*args):
        value = provider(*args)
        repo.blocked = "module_disabled"
        return value

    app.state.tenant_dashboard.fetch = fetch_then_revoke
    with TestClient(app, base_url="http://127.0.0.1:8210") as client:
        client.cookies.set("saas_tenant_session", "valid", path="/api/saas-tenant")
        response = client.get("/api/saas-tenant/one/dashboard/me")
        assert response.status_code == 403
        assert "Restaurant" not in response.text


@pytest.mark.parametrize(
    "url", ["http://one.iiko.it", "https://one.iiko.it:444", "https://one.iiko.it/private"]
)
def test_transport_blocks_unapproved_source_before_dns(monkeypatch, url):
    monkeypatch.setattr(transport, "resolve_public", lambda *args: pytest.fail("No DNS expected"))
    with pytest.raises(Problem):
        transport.fetch(replace(source(), url=url), [("departments", None)])


def test_transport_pins_address_posts_report_and_always_logs_out(monkeypatch):
    calls = []
    monkeypatch.setattr(transport, "resolve_public", lambda *args: "8.8.8.8")

    def request(host, port, ip, path, deadline, headers=None, **kwargs):
        calls.append((ip, path, kwargs))
        if "/auth?" in path:
            return 200, b"synthetic-token"
        if path.endswith("logout"):
            return 200, b"Connection released: synthetic-token"
        assert kwargs["method"] == "POST"
        return 302, b"not accepted"

    monkeypatch.setattr(transport, "request", request)
    with pytest.raises(Problem):
        transport.fetch(source(), [("sales", {"reportType": "SALES"})])
    assert len(calls) == 3
    assert {call[0] for call in calls} == {"8.8.8.8"}
    assert calls[-1][1].endswith("/logout")


def test_logout_failure_discards_successful_payload(monkeypatch):
    monkeypatch.setattr(transport, "resolve_public", lambda *args: "8.8.8.8")

    def request(host, port, ip, path, *args, **kwargs):
        if "/auth?" in path:
            return 200, b"synthetic-token"
        return (500, b"error") if path.endswith("logout") else (200, b"valid-payload")

    monkeypatch.setattr(transport, "request", request)
    with pytest.raises(Problem) as error:
        transport.fetch(source(), [("departments", None)])
    assert error.value.code == "iiko_logout_failed"


def test_transport_requests_xml_departments_and_json_olap(monkeypatch):
    monkeypatch.setattr(transport, "resolve_public", lambda *args: "8.8.8.8")
    received = []

    def request(host, port, ip, path, deadline, headers=None, **kwargs):
        if "/auth?" in path:
            return 200, b"synthetic-token"
        if path.endswith("logout"):
            return 200, b"synthetic-token"
        received.append((path, headers["Accept"], kwargs["method"]))
        return 200, b"synthetic-response"

    monkeypatch.setattr(transport, "request", request)
    transport.fetch(source(), [("departments", None), ("columns", None), ("sales", {})])
    assert received == [
        ("/resto/api/corporation/departments?revisionFrom=-1", "application/xml", "GET"),
        ("/resto/api/v2/reports/olap/columns?reportType=SALES", "application/json", "GET"),
        ("/resto/api/v2/reports/olap", "application/json", "POST"),
    ]


def test_json_duplicates_summary_and_unrequested_department_rejected():
    request = reports.query(START, START, [DEPARTMENT], [], ["revenue"])
    for body in (
        b'{"data":[],"data":[]}',
        b'{"data":[],"summary":[{"value":100}]}',
    ):
        with pytest.raises(Problem):
            reports.rows(body, request, START, START, [DEPARTMENT])
    request = reports.query(START, START, [DEPARTMENT], ["Department.Id"], ["revenue"])
    body = json.dumps({"data": [{"Department.Id": str(UUID(int=99)), "DishDiscountSumInt": 100}]})
    with pytest.raises(Problem):
        reports.rows(body.encode(), request, START, START, [DEPARTMENT])


def test_fractional_guests_and_missing_dish_uuid_are_supported():
    request = reports.query(START, START, [DEPARTMENT], [], ["revenue", "guests"])
    values = reports.rows(
        b'{"data":[{"DishDiscountSumInt":10,"GuestNum":1.5}]}', request, START, START, [DEPARTMENT]
    )
    assert str(values[0]["guests"]) == "1.5"
    request = reports.query(START, START, [DEPARTMENT], ["DishId", "DishName"], ["revenue"])
    values = reports.rows(
        b'{"data":[{"DishDiscountSumInt":10,"DishId":null,"DishName":"Old dish"}]}',
        request,
        START,
        START,
        [DEPARTMENT],
    )
    assert values[0]["DishId"] is None


def test_source_failure_cooldown_spans_report_filters():
    provider = Provider()
    app = service(provider)
    app.references(source())
    provider.fail = True
    with pytest.raises(Problem):
        app.sales(source(), START, START, [], "daily")
    count = len(provider.calls)
    with pytest.raises(Problem):
        app.sales(source(), START, START, [], "dishes")
    assert len(provider.calls) == count


class SourceRepository(PostgresTenantAccess):
    def __init__(self):
        self.row = {
            "id": "member-one",
            "company_id": "one",
            "display_name": "Admin",
            "must_change": False,
            "body": {
                "id": "one",
                "name": "First",
                "version": 1,
                "status": "active",
                "chain_url": "https://one.iiko.it",
                "modules": {"analytics": True},
            },
        }
        self.connection = {"url": "https://one.iiko.it", "ciphertext": "encrypted-first"}
        self.committed = False

        class Vault:
            def decrypt(self, ciphertext):
                assert ciphertext == "encrypted-first"
                return '{"login":"api","password":"only-first-company"}'

        self.vault = Vault()

    @contextmanager
    def connect(self, write=False):
        yield self
        self.committed = True

    def _verified(self, db, token, tenant, slug):
        assert token == "valid" and tenant is True and slug == "one"
        return self.row

    def _tenant_verified(self, db, token, slug):
        return self._verified(db, token, True, slug)

    def execute(self, sql, params):
        assert "FROM connections WHERE company_id=%s" in sql
        assert "connection_id='chain'" in sql
        assert params == ("one",)
        assert "chaika" not in sql
        return self

    def fetchone(self):
        return self.connection


def test_repository_uses_verified_company_only_and_no_rms_fallback():
    repo = SourceRepository()
    value = repo.tenant_dashboard_source("valid", "one")
    assert value.company_id == "one"
    assert value.password == "only-first-company"
    assert value.password not in repr(value)
    repo.connection = None
    with pytest.raises(Problem) as error:
        repo.tenant_dashboard_source("valid", "one")
    assert error.value.code == "chain_required"


def test_employee_cannot_bypass_local_warehouse_rights_through_old_summary():
    repo = SourceRepository()
    repo.row["role"] = "employee"
    with pytest.raises(Problem) as error:
        repo.tenant_dashboard_source("valid", "one")
    assert error.value.code == "full_portal_required"


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("status", "draft", "company_inactive"),
        ("modules", {"analytics": False}, "module_disabled"),
        ("chain_url", "https://another.iiko.it", "chain_required"),
    ],
)
def test_repository_source_capability_checks(field, value, code):
    repo = SourceRepository()
    repo.row["body"][field] = value
    with pytest.raises(Problem) as error:
        repo.tenant_dashboard_source("valid", "one")
    assert error.value.code == code


def test_invalidated_session_deletion_is_committed_before_unauthorized():
    repo = SourceRepository()
    repo.row = None
    with pytest.raises(Problem) as error:
        repo.tenant_dashboard_source("valid", "one")
    assert error.value.status == 401
    assert repo.committed


def test_dish_periods_are_explicit_without_fabricated_dates_or_order_dependent_names():
    start = date(2026, 10, 6)
    request = reports.query(
        start,
        START,
        [DEPARTMENT],
        ["Department.Id", "DishId", "DishName"],
        ["revenue", "quantity"],
    )
    source_rows = [{"DishName": "Dish Z"}, {"DishName": "Dish A"}]
    raw = [
        {
            **row,
            "Department.Id": DEPARTMENT,
            "DishId": DISH,
            "DishDiscountSumInt": "10.25",
            "DishAmountInt": "1",
        }
        for row in source_rows
    ]
    parsed = reports.rows(json.dumps({"data": raw}).encode(), request, start, START, [DEPARTMENT])
    assert all("OpenDate.Typed" not in row for row in parsed)
    previous = [{**parsed[0], "revenue": Decimal("5.00"), "DishName": "Prior period name"}]
    empty_totals = reports.aggregate([], {"revenue", "cost", "checks", "guests"}, 2)
    value = reports.overview(
        SimpleNamespace(ids=[DEPARTMENT], departments=[{"id": DEPARTMENT, "name": "Restaurant"}]),
        start,
        START,
        "day",
        [],
        parsed,
        previous,
        empty_totals,
        empty_totals,
        NOW,
        {"revenue", "quantity"},
    )
    assert value["top_dishes"][0]["dish_name"] == "Dish Z"
    assert value["top_dishes"][0]["revenue"] == Decimal("20.50")
    assert value["top_dishes"][0]["previous_revenue"] == Decimal("5.00")
    assert value["top_dishes"][0]["change"]["absolute"] == Decimal("15.50")
    assert value["top_dishes"][0]["quantity"] == Decimal("2")
    reversed_value = reports.overview(
        SimpleNamespace(ids=[DEPARTMENT], departments=[{"id": DEPARTMENT, "name": "Restaurant"}]),
        start,
        START,
        "day",
        [],
        list(reversed(parsed)),
        previous,
        empty_totals,
        empty_totals,
        NOW,
        {"revenue", "quantity"},
    )
    assert reversed_value["top_dishes"] == value["top_dishes"]
    assert request["filters"]["OpenDate.Typed"] == {
        "filterType": "DateRange",
        "periodType": "CUSTOM",
        "from": "2026-10-06T00:00:00",
        "to": "2026-10-08T00:00:00",
        "includeLow": True,
        "includeHigh": False,
    }


def test_named_plan_override_blocks_current_tenant_read_before_iiko(tmp_path):
    class RestrictedGate(Gate):
        def get(self, company_id):
            return {
                "modules": {"analytics": True},
                "subscription": {
                    "policy": "plans_v1",
                    "plan_id": "full",
                    "start_date": "2026-01-01",
                    "overrides": {"analytics.sales": {"mode": "deny"}},
                },
            }

    app = create_app(tmp_path, repository=RestrictedGate())

    def forbidden_fetch(*args):
        raise AssertionError("Disabled feature reached iiko")

    app.state.tenant_dashboard.fetch = forbidden_fetch
    with TestClient(app, base_url="http://127.0.0.1:8210") as client:
        client.cookies.set("saas_tenant_session", "valid", path="/api/saas-tenant")
        response = client.get(
            "/api/saas-tenant/one/dashboard/sales/daily?start=2026-10-07&end=2026-10-07"
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "feature_disabled"
