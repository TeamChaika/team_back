"""Catalogs retain only business fields and reject invalid or unbounded inputs."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app.commercial_invoices import catalogs

PRODUCT, UNIT, PARTY = (str(UUID(int=i)) for i in (1, 2, 3))


def product_rows():
    return [
        {"id": PRODUCT, "name": "Synthetic", "mainUnit": UNIT, "type": "GOODS", "deleted": False}
    ]


def units():
    return [{"id": UNIT, "name": "шт.", "rootType": "MeasureUnit", "deleted": False}]


def party(**fields):
    values = {
        "id": PARTY,
        "name": "Synthetic business",
        "supplier": "true",
        "employee": "false",
        "deleted": "false",
        "representsStore": "false",
        "taxpayerIdNumber": "1234567890",
        "address": "Synthetic address",
        "cardNumber": "must-not-survive",
        "snils": "must-not-survive",
        **fields,
    }
    return "<employee>" + "".join(f"<{k}>{v}</{k}>" for k, v in values.items()) + "</employee>"


def test_products_join_their_exact_main_unit():
    result = catalogs.parse_products(json.dumps(product_rows()), json.dumps(units()))
    assert result == [{"id": PRODUCT, "name": "Synthetic", "unit_id": UNIT, "unit": "шт."}]
    with pytest.raises(ValueError):
        catalogs.parse_products(json.dumps(product_rows()), "[]")
    with pytest.raises(ValueError):
        catalogs.parse_products(json.dumps(product_rows() * 2), json.dumps(units()))


def test_counterparty_private_fields_not_persisted():
    result = catalogs.parse_counterparties(("<employees>" + party() + "</employees>").encode())
    assert result == [
        {
            "id": PARTY,
            "name": "Synthetic business",
            "inn": "1234567890",
            "kpp": "",
            "address": "Synthetic address",
            "source": "iiko_suppliers",
        }
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"employee": "true"},
        {"deleted": "true"},
        {"representsStore": "true"},
        {"representedStoreId": str(UUID(int=5))},
        {"supplier": "false"},
    ],
)
def test_internal_or_non_business_counterparties_excluded(fields):
    assert (
        catalogs.parse_counterparties(("<employees>" + party(**fields) + "</employees>").encode())
        == []
    )


def test_atomic_refresh_publishes_only_after_all_gets_succeed():
    requests, writes = [], []

    def responder(request):
        requests.append(request)
        if request.url.path.endswith("/products/list"):
            return httpx.Response(200, json=product_rows())
        if request.url.path.endswith("/entities/list"):
            return httpx.Response(200, json=units())
        return httpx.Response(500)

    @contextmanager
    def session():
        with httpx.Client(
            base_url="https://iiko.test/resto/api/", transport=httpx.MockTransport(responder)
        ) as client:
            yield client, "test-token"

    @contextmanager
    def connection():
        writes.append("transaction")
        yield None

    service = SimpleNamespace(
        provider=SimpleNamespace(session=session), database=SimpleNamespace(connection=connection)
    )
    with pytest.raises(ValueError):
        catalogs.refresh(service)
    assert writes == []
    assert all(r.method == "GET" for r in requests)
    assert requests[0].url.params.get_list("types") == ["GOODS", "PREPARED"]


def test_failed_daily_refresh_is_throttled(monkeypatch):
    attempts = []
    result = SimpleNamespace(fetchone=lambda: None)

    @contextmanager
    def connection(**kwargs):
        yield SimpleNamespace(execute=lambda *args: result)

    service = SimpleNamespace(database=SimpleNamespace(connection=connection))
    monkeypatch.setattr(catalogs, "refresh", lambda svc: attempts.append(svc))
    now = datetime(2026, 10, 7, 4, tzinfo=UTC)
    assert catalogs.refresh_if_due(service, now)
    assert not catalogs.refresh_if_due(service, now + timedelta(minutes=1))
    assert len(attempts) == 1
