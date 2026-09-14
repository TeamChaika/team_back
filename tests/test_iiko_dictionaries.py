import json
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from test_iiko_employees import config

from app.integrations.iiko.dictionaries import DICTIONARIES
from app.integrations.iiko.errors import IikoError
from app.main import create_app
from app.services.iiko_dictionaries import read_dictionary

BASE = "/api/v1/iiko/dictionaries"
ID = str(UUID(int=1))
STORE = str(UUID(int=2))
XML = (
    f'<employees xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><employee><id>{ID}</id>'
    "<name>Test supplier</name><code></code><deleted>false</deleted><supplier>true</supplier>"
    "<employee>false</employee><client>false</client><representsStore>true</representsStore>"
    f"<representedStoreId>{STORE}</representedStoreId><login>private-login</login>"
    "<cardNumber>private-card</cardNumber><phone>private-phone</phone>"
    "<publicExternalData><entry>private</entry></publicExternalData></employee></employees>"
).encode()
PAYLOADS = {
    "accounts": json.dumps(
        [
            {
                "id": ID,
                "name": "Test account",
                "rootType": "Account",
                "code": None,
                "deleted": False,
                "accountParentId": None,
                "parentCorporateId": None,
                "type": "CASH",
                "system": False,
                "customTransactionsAllowed": True,
            }
        ]
    ).encode(),
    "counteragents": XML,
    "measure-units": json.dumps(
        [{"id": ID, "name": "кг", "rootType": "MeasureUnit", "code": "001", "deleted": False}]
    ).encode(),
    "categories": json.dumps([{"id": ID, "name": "Category", "deleted": True}]).encode(),
}


@pytest.mark.parametrize("kind", list(DICTIONARIES))
def test_protocol_raw_paging_and_restore(tmp_path, kind):
    calls = []
    spec = DICTIONARIES[kind]

    def handle(request):
        calls.append(request)
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text="test-token")
        assert request.method == "GET" and request.url.path == "/resto/api/" + spec.endpoint
        assert dict(request.url.params) == spec.params
        assert request.headers["cookie"] == "key=test-token"
        assert request.headers["accept"] == (
            "application/xml" if spec.format == "xml" else "application/json"
        )
        return httpx.Response(200, content=PAYLOADS[kind])

    def app():
        return create_app(
            config(), iiko_transport=httpx.MockTransport(handle), dictionaries_directory=tmp_path
        )

    with TestClient(app()) as client:
        assert client.get(f"{BASE}/{kind}").status_code == 409
        loaded = client.post(f"{BASE}/{kind}/load")
        assert loaded.status_code == 200
        info = loaded.json()
        assert info["total"] == 1 and info["request"] == spec.params
        page = client.get(f"{BASE}/{kind}").json()
        row = page["items"][0]
        assert row["id"] == ID and "private" not in json.dumps(page)
        if kind == "accounts":
            assert row["root_type"] == "Account" and row["type"] == "CASH"
        if kind == "counteragents":
            assert row["represented_store_id"] == STORE and row["code"] == ""
        if kind == "measure-units":
            assert row["root_type"] == "MeasureUnit" and row["code"] == "001"
        if kind == "categories":
            assert row["deleted"] is True and row["root_type"] is None
        assert (
            client.get(f"{BASE}/{kind}", params={"snapshot_id": str(UUID(int=9))}).status_code
            == 409
        )
        path = tmp_path / kind / f"{info['snapshot_id']}.{spec.format}"
        assert path.read_bytes() == PAYLOADS[kind] and path.stat().st_mode & 0o777 == 0o600
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"
    before = len(calls)
    with TestClient(app()) as client:
        assert client.get(f"{BASE}/{kind}").json()["snapshot"] == info
    assert len(calls) == before


@pytest.mark.parametrize("kind", list(DICTIONARIES))
def test_duplicate_ids_rejected(tmp_path, kind):
    raw = PAYLOADS[kind]
    if kind == "counteragents":
        raw = raw.replace(
            b"</employee>",
            b"</employee>"
            + raw.split(b"<employee>")[1].split(b"</employees>")[0].join([b"<employee>", b""]),
        )
    else:
        rows = json.loads(raw)
        raw = json.dumps(rows * 2).encode()
    path = tmp_path / "raw"
    path.write_bytes(raw)
    with pytest.raises(IikoError):
        read_dictionary(path, len(raw), kind=kind)


@pytest.mark.parametrize(
    "raw",
    [
        b'<!DOCTYPE employees [<!ENTITY x "unsafe">]><employees/>',
        XML.replace(b"<code></code>", b"<code>A</code><code>B</code>"),
        XML.replace(b"<representsStore>true", b"<representsStore>bad"),
        XML.replace(b"<supplier>true", b"<supplier>TRUE"),
    ],
)
def test_bad_xml_is_rejected_without_details(tmp_path, raw):
    path = tmp_path / "raw"
    path.write_bytes(raw)
    with pytest.raises(IikoError) as error:
        read_dictionary(path, len(raw), kind="counteragents")
    assert "private" not in str(error.value)


def test_json_scope_duplicate_fields_and_size(tmp_path):
    path = tmp_path / "raw"
    for raw in [
        b"{}",
        PAYLOADS["measure-units"].replace(b"MeasureUnit", b"Account"),
        b'[{"id":"x","id":"y"}]',
    ]:
        path.write_bytes(raw)
        with pytest.raises(IikoError):
            read_dictionary(path, len(raw), kind="measure-units")
    path.write_bytes(PAYLOADS["categories"])
    with pytest.raises(IikoError):
        read_dictionary(path, 1, kind="categories")


def test_unknown_resource_and_failed_reload_do_not_replace_snapshot(tmp_path):
    responses = iter([PAYLOADS["categories"], b"broken-json"])

    def handle(request):
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text="test-token")
        return httpx.Response(200, content=next(responses))

    with TestClient(
        create_app(
            config(), iiko_transport=httpx.MockTransport(handle), dictionaries_directory=tmp_path
        )
    ) as client:
        assert client.post(f"{BASE}/arbitrary/load").status_code == 422
        before = client.post(f"{BASE}/categories/load").json()
        assert client.post(f"{BASE}/categories/load").status_code == 502
        assert client.get(f"{BASE}/categories").json()["snapshot"] == before
