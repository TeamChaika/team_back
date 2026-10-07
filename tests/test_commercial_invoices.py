"""Synthetic money/auth/queue checks: no business endpoints are contacted."""

from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.commercial_invoices import dispatch, policy
from app.commercial_invoices.calculation import calculate


def line(**changes):
    return {
        "product_id": str(UUID(int=1)),
        "quantity": "3",
        "price": "100.01",
        "vat_rate": "20",
        "price_includes_vat": True,
        **changes,
    }


def test_included_tax_is_rounded_per_line_and_document_sums_saved_lines():
    items, totals = calculate([line(), line(quantity="0.001", price="5")])
    assert items[0]["total"] == "300.03"
    assert items[0]["vat"] == "50.01"
    assert items[0]["net"] == "250.02"
    assert items[1]["total"] == "0.01"
    assert totals == {"net": "250.03", "vat": "50.01", "total": "300.04"}


def test_exclusive_tax_and_null_vat_remain_distinct_from_zero_rate():
    items, totals = calculate(
        [line(price_includes_vat=False), line(vat_rate=None), line(vat_rate="0")]
    )
    assert items[0]["net"] == "300.03"
    assert items[0]["vat"] == "60.01"
    assert items[0]["total"] == "360.04"
    assert items[1]["vat_rate"] is None
    assert items[2]["vat_rate"] == "0"
    assert totals["total"] == "960.10"


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": "0"},
        {"quantity": "-1"},
        {"quantity": "1.0001"},
        {"quantity": 1},
        {"price": "NaN"},
        {"price": "Infinity"},
        {"price": "1e3"},
        {"price": 1.25},
        {"price": "1.00001"},
        {"vat_rate": 20},
        {"vat_rate": "99"},
        {"price_includes_vat": "true"},
        {"total": "0"},
    ],
)
def test_invalid_or_client_calculated_money_rejected(changes):
    with pytest.raises(HTTPException) as error:
        calculate([line(**changes)])
    assert error.value.status_code == 422


@pytest.mark.parametrize("rate", [None, "0", "5", "7", "10", "20", "22"])
@pytest.mark.parametrize("included", [True, False])
def test_rounding_conserves_line_and_invoice_totals(rate, included):
    items, totals = calculate(
        [
            line(quantity="1.337", price="99.9999", vat_rate=rate, price_includes_vat=included)
            for _ in range(7)
        ]
    )
    for item in items:
        assert Decimal(item["net"]) + Decimal(item["vat"]) == Decimal(item["total"])
    for key in totals:
        assert Decimal(totals[key]) == sum(Decimal(i[key]) for i in items)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class PolicyDB:
    def __init__(self, *, admin=True, sections=None, active=True, linked=True):
        self.admin, self.sections, self.active, self.linked = admin, sections or [], active, linked

    def execute(self, sql, args=()):
        if "FROM portal_access" in sql:
            return Result(
                [{"active": self.active, "is_portal_admin": self.admin, "sections": self.sections}]
            )
        if "FROM authentication_user" in sql:
            return Result([{"id": 1, "is_active": True}] if self.linked else [])
        if "FROM commercial_invoice_grants" in sql:
            return Result([])
        raise AssertionError(sql)


def test_portal_owner_does_not_receive_section_or_store_access():
    with pytest.raises(HTTPException) as error:
        policy.actor(PolicyDB(admin=True), UUID(int=1), kind="sale")
    assert error.value.status_code == 403
    db = PolicyDB(admin=True, sections=["outgoing"])
    actor = policy.actor(db, UUID(int=1), kind="sale")
    assert policy.stores_for(db, actor["id"], "sale") == []
    with pytest.raises(HTTPException):
        policy.require(db, actor["id"], "sale", "submit", UUID(int=2))


@pytest.mark.parametrize(
    "db",
    [PolicyDB(sections=["outgoing"], active=False), PolicyDB(sections=["outgoing"], linked=False)],
)
def test_inactive_or_unlinked_account_denied(db):
    with pytest.raises(HTTPException):
        policy.actor(db, UUID(int=1), kind="sale")


class Provider:
    def __init__(self, *, auth_error=False, outcome=None, send_error=False, logout_error=False):
        self.auth_error, self.send_error, self.logout_error = auth_error, send_error, logout_error
        self.outcome = outcome or {"state": "accepted"}
        self.sends = 0

    @contextmanager
    def session(self):
        if self.auth_error:
            raise OSError("offline")
        yield object(), "synthetic-token"
        if self.logout_error:
            raise OSError("logout unavailable")

    def send_authenticated(self, *args):
        self.sends += 1
        if self.send_error:
            raise TimeoutError("ambiguous write")
        return self.outcome


@pytest.mark.parametrize(
    "send_error,logout_error,outcome",
    [(True, False, "unknown"), (False, True, "accepted"), (False, False, "accepted")],
)
def test_uncertain_write_is_recorded_not_reposted(monkeypatch, send_error, logout_error, outcome):
    provider = Provider(send_error=send_error, logout_error=logout_error)
    service = SimpleNamespace(submit_enabled=True, database=object(), provider=provider)
    job = {"payload": {"xml": "<synthetic/>"}}
    captured = []
    monkeypatch.setattr(dispatch, "claim", lambda db: job)
    monkeypatch.setattr(dispatch, "reserve", lambda db, j: "sale")
    monkeypatch.setattr(dispatch, "complete", lambda db, j, result: captured.append(result))
    monkeypatch.setattr(
        dispatch, "retry", lambda *args: pytest.fail("Uncertain POST must never retry")
    )
    assert dispatch.deliver_one(service)
    assert provider.sends == 1
    assert captured == [{"state": outcome}]


def test_authentication_failure_can_retry_without_business_post(monkeypatch):
    provider = Provider(auth_error=True)
    service = SimpleNamespace(submit_enabled=True, database=object(), provider=provider)
    job, retried = {"document_id": "synthetic"}, []
    monkeypatch.setattr(dispatch, "claim", lambda db: job)
    monkeypatch.setattr(dispatch, "retry", lambda db, j: retried.append(j))
    assert dispatch.deliver_one(service)
    assert provider.sends == 0
    assert retried == [job]


def test_feature_disabled_never_claims_job(monkeypatch):
    monkeypatch.setattr(dispatch, "claim", lambda db: pytest.fail("Disabled worker must not claim"))
    assert not dispatch.deliver_one(SimpleNamespace(submit_enabled=False))
