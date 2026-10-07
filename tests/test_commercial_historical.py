"""Synthetic historical invoice snapshots; no live business reads or writes."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.commercial_invoices import historical
from app.commercial_invoices.pdf import _normalise, build_payment_qr_payload
from app.web.repository import Repository, Scope

SELLER = {
    "name": "ООО Тест",
    "inn": "1234567890",
    "kpp": "123456789",
    "bank_name": "Тестовый банк",
    "bic": "123456789",
    "account": "12345678901234567890",
    "correspondent_account": "12345678901234567890",
}

STORE, OTHER, DOCUMENT, BUYER, PRODUCT, UNIT = [UUID(int=i) for i in range(1, 7)]


def source():
    header = {
        "id": DOCUMENT,
        "status": "PROCESSED",
        "document_number": "TEST-42",
        "date_incoming": "2026-10-07T10:00:00",
        "default_store_id": STORE,
        "counteragent_id": BUYER,
        "linked_incoming_invoice_id": None,
        "linked_outgoing_invoice_id": None,
        "represents_store": False,
        "represented_store_id": None,
    }
    rows = [
        {
            "product_id": PRODUCT,
            "product": "Тестовый товар",
            "store_id": None,
            "container_id": None,
            "unit_id": UNIT,
            "unit": "кг",
            "amount": Decimal("2.5"),
            "price": Decimal("120.00"),
            "sum": Decimal("300.00"),
            "details": {"discount_sum": "0", "vat_percent": "20", "vat_sum": "50.00"},
            # A purchase/production cost must never affect the customer's amount.
            "cost": Decimal("7.00"),
        }
    ]
    buyer = {"id": str(BUYER), "name": "Тестовый покупатель", "source": "iiko_suppliers"}
    return header, rows, buyer


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class SourceDB:
    def __init__(self, header, rows):
        self.header, self.rows, self.calls = header, rows, []

    def execute(self, sql, args=()):
        assert sql.startswith("SELECT ")
        self.calls.append((sql, args))
        if "SELECT t.*" in sql:
            return Result([self.header] if self.header else [])
        if "outgoing_invoice_items i" in sql:
            return Result(self.rows[:201])
        raise AssertionError(sql)


class SourceRepo:
    resource_query = Repository.resource_query

    def __init__(self, header, rows):
        self.db = SourceDB(header, rows)
        self.connections = []

    @contextmanager
    def connection(self, *, repeatable=False):
        self.connections.append(repeatable)
        yield self.db


class CommercialDB:
    def __init__(self, buyer):
        self.catalog = [buyer]
        self.updated_at = datetime.now(UTC)
        self.connections = []

    @contextmanager
    def connection(self, *, readonly=False):
        self.connections.append(readonly)
        yield self

    def execute(self, sql, args=()):
        assert sql.startswith("SELECT ")
        assert args == ("commercial_sale_counterparties",)
        return Result([{"data": self.catalog, "updated_at": self.updated_at}])


@pytest.fixture
def scenario(monkeypatch):
    header, rows, buyer = source()
    repo, database = SourceRepo(header, rows), CommercialDB(buyer)
    service = SimpleNamespace(database=database, seller=SELLER)
    scope = Scope({"id": UUID(int=8), "role": "owner"}, (), None, (STORE,), ())
    grants = [STORE]
    actor_calls = []

    def actor(db, portal_id, *, kind):
        actor_calls.append((db, portal_id, kind))
        return {"id": 9}

    monkeypatch.setattr(historical, "actor", actor)
    monkeypatch.setattr(historical, "stores_for", lambda *args: grants)
    return SimpleNamespace(
        repo=repo,
        service=service,
        scope=scope,
        grants=grants,
        actor_calls=actor_calls,
        header=header,
        rows=rows,
        buyer=buyer,
    )


def generate(scenario):
    return historical.historical_pdf(scenario.repo, scenario.service, scenario.scope, DOCUMENT)


def test_actual_sale_amount_and_tax_are_preserved_with_payable_qr():
    header, rows, buyer = source()
    snapshot = historical._snapshot(header, rows, buyer, SELLER)
    assert snapshot["items"][0]["price"] == "120.00"
    assert snapshot["totals"] == {"net": "250.00", "vat": "50.00", "total": "300.00"}
    assert snapshot["state"] == "processed"
    assert "|Sum=30000|" in build_payment_qr_payload(snapshot)


def test_readonly_connections_and_exact_source_warehouse_scope(scenario):
    assert generate(scenario).startswith(b"%PDF-")
    assert scenario.repo.connections == [True]
    assert scenario.service.database.connections == [True]
    assert scenario.actor_calls == [(scenario.service.database, UUID(int=8), "sale")]
    assert scenario.repo.db.calls[0][1] == [DOCUMENT]


def test_restricted_analytics_scope_guard_is_in_the_header_snapshot_query(scenario):
    scenario.scope = Scope({"id": UUID(int=8), "role": "manager"}, (), None, (STORE,), ())
    generate(scenario)
    sql, args = scenario.repo.db.calls[0]
    assert "AND NOT EXISTS" in sql
    assert "i.present_in_latest" in sql
    assert args == [[STORE], [STORE], [STORE], DOCUMENT]


def test_analytics_invisible_document_cannot_print(scenario):
    scenario.repo.db.header = None
    with pytest.raises(HTTPException) as error:
        generate(scenario)
    assert error.value.status_code == 404
    assert len(scenario.repo.db.calls) == 1


@pytest.mark.parametrize("store", [OTHER, None])
def test_every_effective_warehouse_requires_explicit_grant_even_for_owner(scenario, store):
    scenario.rows.append(deepcopy(scenario.rows[0]))
    scenario.rows[-1]["store_id"] = store
    if store is None:
        scenario.header["default_store_id"] = None
    with pytest.raises(HTTPException) as error:
        generate(scenario)
    assert error.value.status_code == 403


def test_user_without_commercial_section_is_rejected_before_source_read(scenario, monkeypatch):
    def denied(*args, **kwargs):
        raise HTTPException(403, "Нет доступа")

    monkeypatch.setattr(historical, "actor", denied)
    with pytest.raises(HTTPException) as error:
        generate(scenario)
    assert error.value.status_code == 403
    assert scenario.repo.connections == []


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "NEW"},
        {"status": "DELETED"},
        {"counteragent_id": None},
        {"represents_store": True},
        {"represented_store_id": OTHER},
        {"linked_incoming_invoice_id": DOCUMENT},
        {"linked_outgoing_invoice_id": DOCUMENT},
        {"document_number": None},
        {"date_incoming": "invalid"},
    ],
)
def test_unsupported_source_documents_never_print(scenario, changes):
    scenario.header.update(changes)
    with pytest.raises(HTTPException) as error:
        generate(scenario)
    assert error.value.status_code == 422


@pytest.mark.parametrize(
    "changes",
    [
        {"container_id": UNIT},
        {"unit_id": None},
        {"unit": ""},
        {"product": None},
        {"amount": None},
        {"amount": Decimal("-1")},
        {"price": None},
        {"price": Decimal("0.01")},
        {"sum": Decimal("301")},
        {"amount": Decimal("NaN")},
        {"amount": Decimal("0")},
        {"details": {"vat_percent": None, "vat_sum": "0"}},
        {"details": {"vat_percent": "20", "vat_sum": None}},
        {"details": {"vat_percent": "20", "vat_sum": "51"}},
        {"details": {"vat_percent": "20", "vat_sum": "50", "discount_sum": "10"}},
    ],
)
def test_unsupported_or_inconsistent_source_lines_never_print(scenario, changes):
    scenario.rows[0].update(changes)
    with pytest.raises(HTTPException) as error:
        generate(scenario)
    assert error.value.status_code == 422


def test_explicit_zero_vat_is_not_relabelled_exempt():
    header, rows, buyer = source()
    rows[0]["details"].update(vat_percent="0", vat_sum="0")
    snapshot = historical._snapshot(header, rows, buyer, SELLER)
    _normalise(snapshot)
    assert snapshot["items"][0]["vat_rate"] == "0"
    assert "Без НДС" not in build_payment_qr_payload(snapshot)


def test_missing_or_stale_counterparty_catalog_never_invents_buyer(scenario):
    scenario.service.database.catalog = []
    with pytest.raises(HTTPException) as error:
        generate(scenario)
    assert error.value.status_code == 422
    scenario.service.database.updated_at -= timedelta(days=3)
    with pytest.raises(HTTPException) as error:
        generate(scenario)
    assert error.value.status_code == 503


def test_more_than_two_hundred_lines_is_rejected_before_any_pdf(scenario):
    scenario.rows.extend(deepcopy(scenario.rows[0]) for _ in range(200))
    with pytest.raises(HTTPException) as error:
        generate(scenario)
    assert error.value.status_code == 422


def test_detail_eligibility_validates_without_rendering_pdf(scenario, monkeypatch):
    def unexpected_render(*args):
        raise AssertionError("Eligibility must not render a PDF")

    monkeypatch.setattr(historical, "render_invoice_pdf", unexpected_render)
    assert historical.historical_pdf_allowed(
        scenario.repo, scenario.service, scenario.scope, DOCUMENT
    )
    scenario.rows[0]["sum"] = Decimal("301")
    assert not historical.historical_pdf_allowed(
        scenario.repo, scenario.service, scenario.scope, DOCUMENT
    )
    scenario.grants.clear()
    assert not historical.historical_pdf_allowed(
        scenario.repo, scenario.service, scenario.scope, DOCUMENT
    )


def test_pdf_rechecks_permission_after_eligible_detail(scenario):
    assert historical.historical_pdf_allowed(
        scenario.repo, scenario.service, scenario.scope, DOCUMENT
    )
    scenario.grants.clear()
    with pytest.raises(HTTPException) as error:
        generate(scenario)
    assert error.value.status_code == 403
