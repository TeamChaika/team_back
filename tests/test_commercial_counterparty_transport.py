"""Bounded XML and form contract with MockTransport only."""

from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import uuid4
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx
import pytest

from app.commercial_invoices.counterparty_models import code_for, normalize, wire_fields
from app.commercial_invoices.counterparty_transport import CounterpartyTransport, request


@pytest.fixture
def job():
    identifier = uuid4()
    payload = normalize(
        {
            "request_id": str(uuid4()),
            "entity_type": "person",
            "name": "Тест & Имя",
            "phone": "+79990000000",
            "inn": "",
            "kpp": "",
            "address": "Только тест",
        }
    )
    return {"iiko_id": identifier, "code": code_for(identifier), "payload": payload}


def xml(job, **changes):
    root = Element("employee")
    fields = {"id": str(job["iiko_id"]), **wire_fields(job["payload"], job["code"]), **changes}
    for key, value in fields.items():
        SubElement(root, key).text = value
    return tostring(root)


def client(handler):
    return httpx.Client(
        base_url="https://iiko.invalid/resto/api/", transport=httpx.MockTransport(handler)
    )


def test_post_only_allowed_business_fields_with_fixed_flags(job):
    seen = []
    with client(
        lambda request: seen.append(request) or httpx.Response(201, content=xml(job))
    ) as http:
        transport = CounterpartyTransport(SimpleNamespace())
        assert transport.create(http, "synthetic-token", job) == "unknown"
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path.endswith("/employees/byId/" + str(job["iiko_id"]))
    values = parse_qs(request.content.decode(), keep_blank_values=True)
    assert values == {
        key: [value] for key, value in wire_fields(job["payload"], job["code"]).items()
    }
    assert values["supplier"] == ["true"]
    assert values["employee"] == values["client"] == ["false"]
    assert "kpp" not in values and "entity_type" not in values
    assert "application/x-www-form-urlencoded" in request.headers["content-type"]


@pytest.mark.parametrize(
    "changes",
    [
        {"employee": "true"},
        {"supplier": "false"},
        {"client": "true"},
        {"deleted": "true"},
        {"representsStore": "true"},
        {"representedStoreId": str(uuid4())},
        {"id": str(uuid4())},
        {"code": "other"},
        {"phone": "changed"},
        {"name": "changed"},
        {"taxpayerIdNumber": "1234567890"},
    ],
)
def test_readback_requires_exact_external_card(job, changes):
    with client(lambda _: httpx.Response(200, content=xml(job, **changes))) as http:
        assert CounterpartyTransport(SimpleNamespace()).lookup(http, "token", job) is None


def test_readback_projects_only_business_fields(job):
    with client(
        lambda _: httpx.Response(200, content=xml(job, login="private", snils="private"))
    ) as http:
        row = CounterpartyTransport(SimpleNamespace()).lookup(http, "token", job)
    assert set(row) == {
        "id",
        "name",
        "inn",
        "address",
        "kpp",
        "entity_type",
        "source",
        "local_fields",
    }
    assert row["entity_type"] == "person"


def test_registry_excludes_staff_and_internal_store(job):
    content = (
        b"<employees>"
        + xml(job)
        + xml(job, employee="true")
        + xml(job, representsStore="true")
        + b"</employees>"
    )
    with client(lambda _: httpx.Response(200, content=content)) as http:
        rows = CounterpartyTransport(SimpleNamespace()).registry(http, "token")
    assert len(rows) == 1 and rows[0]["id"] == str(job["iiko_id"])


def test_bounds_and_forbidden_entities(job):
    with client(lambda _: httpx.Response(200, content=b"x" * 21)) as http:
        with pytest.raises(ValueError):
            request(http, "token", "GET", "suppliers", limit=20)
    malicious = b'<!DOCTYPE employee [<!ENTITY x "unsafe">]><employee><name>&x;</name></employee>'
    with client(lambda _: httpx.Response(200, content=malicious)) as http:
        with pytest.raises(ValueError):
            CounterpartyTransport(SimpleNamespace()).lookup(http, "token", job)


def test_transient_error_is_not_absence(job):
    with client(lambda _: httpx.Response(503)) as http:
        with pytest.raises(ValueError):
            CounterpartyTransport(SimpleNamespace()).exists(
                http, "token", identifier=job["iiko_id"]
            )
    with client(lambda _: httpx.Response(404)) as http:
        assert not CounterpartyTransport(SimpleNamespace()).exists(
            http, "token", identifier=job["iiko_id"]
        )
