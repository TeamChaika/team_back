import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.iiko.client import IikoClient
from app.main import create_app
from app.services.iiko_auth import IikoAuthService

TOKEN = "test-employees-token"
BASE = "/api/v1/iiko/employees"
ROLE = str(UUID(int=99))


def employee(number=1, code="001", extra=""):
    return (
        f"<employee><id>{UUID(int=number)}</id><code>{code}</code>"
        "<name>Тест &amp; сотрудник</name><firstName>Тест</firstName>"
        f"<mainRoleId>{ROLE}</mainRoleId><rolesIds>{ROLE}</rolesIds>"
        "<mainRoleCode>001</mainRoleCode><roleCodes>001</roleCodes>"
        "<departmentCodes>0001</departmentCodes><departmentCodes>0002</departmentCodes>"
        "<deleted>false</deleted><employee>true</employee><supplier>false</supplier><client>false</client>"
        "<password>private-password</password><pinCode>private-pin</pinCode>"
        "<phone>private-phone</phone><publicExternalData><entry><key>private-key</key></entry></publicExternalData>"
        f"{extra}</employee>"
    )


def wrap(items):
    return (
        f'<employees xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">{items}</employees>'
    ).encode()


def config(**overrides):
    return Settings(
        _env_file=None,
        iiko_base_url="https://iiko.example/resto/api",
        iiko_login="api-test",
        iiko_password="test-secret",
        **overrides,
    )


class Source:
    def __init__(self, body=None, statuses=None):
        self.body = body if body is not None else wrap(employee() + employee(2, ""))
        self.statuses = iter(statuses or [200] * 10)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text=TOKEN)
        assert request.method == "GET"
        assert request.url.path == "/resto/api/employees"
        assert dict(request.url.params) == {"includeDeleted": "false", "revisionFrom": "-1"}
        assert request.headers["cookie"] == f"key={TOKEN}"
        assert request.headers["accept"] == "application/xml"
        return httpx.Response(next(self.statuses), content=self.body)


def app(source, directory, settings=None):
    return create_app(
        settings or config(),
        iiko_transport=httpx.MockTransport(source),
        employees_directory=directory,
    )


def test_load_pages_links_raw_and_restore_without_iiko(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE).status_code == 409 and not source.requests
        response = client.post(f"{BASE}/load")
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["total"] == snapshot["employee_flag_count"] == 2
        assert snapshot["without_code"] == snapshot["unique_role_ids"] == 1
        assert snapshot["revision_from"] == -1 and snapshot["include_deleted"] is False
        page = client.get(BASE, params={"limit": 1})
        first = page.json()["items"][0]
        assert first["id"] == str(UUID(int=1)) and first["code"] == "001"
        assert first["name"] == "Тест & сотрудник" and first["first_name"] == "Тест"
        assert first["main_role_id"] == ROLE and first["role_ids"] == [ROLE]
        assert first["main_role_code"] == "001" and first["role_codes"] == ["001"]
        assert first["department_codes"] == ["0001", "0002"]
        assert (
            first["employee"] is True and first["supplier"] is False and first["deleted"] is False
        )
        assert first["responsibility_department_codes"] is None
        assert client.get(BASE, params={"offset": 1, "limit": 1}).json()["items"][0]["code"] == ""
        assert client.get(BASE, params={"offset": 2}).json()["items"] == []
        assert len(source.requests) == 2
        raw = tmp_path / f"{snapshot['snapshot_id']}.xml"
        assert raw.read_bytes() == source.body
        assert hashlib.sha256(source.body).hexdigest() == snapshot["sha256"]
        assert raw.stat().st_mode & 0o777 == 0o600
        metadata = tmp_path / "current.json"
        assert metadata.stat().st_mode & 0o777 == 0o600
        assert TOKEN not in response.text
        for private in ["private-password", "private-pin", "private-key"]:
            assert private not in page.text and private not in metadata.read_text()
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE).json()["snapshot"] == snapshot
        assert len(source.requests) == 3


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b"<employees>",
        b"<error>private-error</error>",
        b"<employees><wrong/></employees>",
        b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><employees>&x;</employees>',
        b'<!DOCTYPE x [<!ENTITY x "private-error">]><employees>&x;</employees>',
        wrap(employee() + employee()),
        wrap(employee().replace(f"<id>{UUID(int=1)}</id>", "<id>bad</id>")),
        wrap(employee().replace("<code>001</code>", "")),
        wrap(employee().replace("<code>001</code>", "<code>001</code><code>002</code>")),
        wrap(employee().replace("<code>001</code>", "<code><nested/></code>")),
        wrap(employee().replace("<deleted>false</deleted>", "<deleted>true</deleted>")),
        wrap(employee().replace("<employee>true</employee>", "<employee>maybe</employee>")),
        wrap(employee().replace(f"<rolesIds>{ROLE}</rolesIds>", "<rolesIds>bad</rolesIds>")),
        wrap(
            employee().replace(
                "<roleCodes>001</roleCodes>", '<roleCodes xsi:nil="true">001</roleCodes>'
            )
        ),
        wrap(employee().replace("<roleCodes>001</roleCodes>", '<roleCodes xsi:nil="invalid"/>')),
    ],
)
def test_invalid_xml_does_not_replace_previous_snapshot(tmp_path, body):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        snapshot = client.post(f"{BASE}/load").json()
        source.body = body
        response = client.post(f"{BASE}/load")
        assert response.status_code == 502 and "private-error" not in response.text
        assert client.get(BASE).json()["snapshot"] == snapshot
        assert not list(tmp_path.glob("*.part"))


def test_empty_response_and_duplicate_codes_are_valid(tmp_path):
    source = Source(wrap(""))
    with TestClient(app(source, tmp_path)) as client:
        assert client.post(f"{BASE}/load").json()["total"] == 0
        assert client.get(BASE).json()["items"] == []
        source.body = wrap(employee() + employee(2))
        assert client.post(f"{BASE}/load").json()["total"] == 2


@pytest.mark.parametrize(
    "params", [{"offset": -1}, {"limit": 0}, {"limit": 201}, {"snapshot_id": "bad"}]
)
def test_invalid_pages_never_call_iiko(tmp_path, params):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE, params=params).status_code == 422
        assert not source.requests


def test_old_snapshot_id_detects_a_new_load(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        first = client.post(f"{BASE}/load").json()
        second = client.post(f"{BASE}/load").json()
        assert client.get(BASE, params={"snapshot_id": first["snapshot_id"]}).status_code == 409
        assert client.get(BASE, params={"snapshot_id": second["snapshot_id"]}).status_code == 200


@pytest.mark.parametrize("change", ["bytes", "source", "missing"])
def test_restore_checks_raw_integrity_and_source(tmp_path, change):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        snapshot = client.post(f"{BASE}/load").json()
    raw = tmp_path / f"{snapshot['snapshot_id']}.xml"
    settings = config()
    if change == "bytes":
        raw.write_bytes(raw.read_bytes().replace(b"001", b"002"))
    elif change == "missing":
        raw.unlink()
    else:
        settings = settings.model_copy(update={"iiko_login": "different-login"})
    count = len(source.requests)
    with TestClient(app(source, tmp_path, settings)) as client:
        assert client.get(BASE).status_code == (409 if change == "source" else 500)
        assert len(source.requests) == count


def test_size_limit_and_publication_failure_preserve_state(tmp_path, monkeypatch):
    with TestClient(
        app(Source(), tmp_path, config(iiko_employees_max_response_bytes=10))
    ) as client:
        assert client.post(f"{BASE}/load").json()["error"]["code"] == "iiko_employees_too_large"
        assert not list(tmp_path.iterdir())
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        snapshot = client.post(f"{BASE}/load").json()
        original = Path.replace

        def broken_publish(path, target):
            if target.name == "current.json":
                raise PermissionError("private-error")
            return original(path, target)

        monkeypatch.setattr(Path, "replace", broken_publish)
        response = client.post(f"{BASE}/load")
        assert response.status_code == 500 and "private-error" not in response.text
        assert client.get(BASE).json()["snapshot"] == snapshot
        assert json.loads((tmp_path / "current.json").read_text())["snapshot"] == snapshot


@pytest.mark.parametrize("statuses", [[401, 200], [401, 401], [403], [500]])
def test_only_401_is_retried_once(tmp_path, statuses):
    source = Source(statuses=statuses)
    with TestClient(app(source, tmp_path)) as client:
        response = client.post(f"{BASE}/load")
        assert response.status_code == (200 if statuses[-1] == 200 else 502)
        assert sum(r.url.path.endswith("/employees") for r in source.requests) == len(statuses)


def test_employees_share_request_lock_with_groups_and_logout(tmp_path):
    async def scenario():
        active = maximum = 0
        paths = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal active
                await asyncio.sleep(0.01)
                yield b"<employees/>"
                active -= 1

        def handler(request):
            nonlocal active, maximum
            paths.append(request.url.path.rsplit("/", 1)[-1])
            if paths[-1] in {"auth", "logout"}:
                return httpx.Response(200, text=TOKEN)
            active += 1
            maximum = max(active, maximum)
            return httpx.Response(200, stream=Stream())

        settings = config()
        auth = IikoAuthService(settings, IikoClient(settings, httpx.MockTransport(handler)))
        try:
            await asyncio.gather(
                auth.download_employees(tmp_path / "employees.xml"),
                auth.download_groups(tmp_path / "groups.json"),
                auth.logout(),
            )
            assert maximum == 1 and paths == ["auth", "employees", "list", "logout"]
        finally:
            await auth.aclose()

    asyncio.run(scenario())


def test_employee_timeout_keeps_session_available_for_logout(tmp_path):
    async def handler(request):
        if request.url.path.endswith("/employees"):
            await asyncio.sleep(1)
        return httpx.Response(200, text=TOKEN)

    with TestClient(app(handler, tmp_path, config(iiko_employees_timeout_seconds=0.01))) as client:
        assert client.post(f"{BASE}/load").status_code == 504
        assert not list(tmp_path.iterdir())
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_absent_empty_and_nil_lists_preserve_source_semantics(tmp_path):
    minimal = f"<employee><id>{UUID(int=1)}</id><code/><name>Системная запись</name></employee>"
    source = Source(wrap(minimal))
    with TestClient(app(source, tmp_path)) as client:
        snapshot = client.post(f"{BASE}/load").json()
        assert snapshot["department_lists_omitted"] == 1
        data = client.get(BASE).json()["items"][0]
        assert data["role_ids"] is None and data["department_codes"] is None
        assert data["employee"] is None and data["deleted"] is None
        source.body = wrap(
            minimal.replace(
                "</employee>",
                '<rolesIds xsi:nil="true"/><roleCodes/>'
                '<departmentCodes xsi:nil="true"/>'
                "<responsibilityDepartmentCodes>0001</responsibilityDepartmentCodes></employee>",
            )
        )
        assert client.post(f"{BASE}/load").status_code == 200
        data = client.get(BASE).json()["items"][0]
        assert data["role_ids"] == [None] and data["role_codes"] == [""]
        assert data["department_codes"] == [None]
        assert data["responsibility_department_codes"] == ["0001"]


def test_multiple_roles_order_duplicates_and_nonemployees_are_preserved(tmp_path):
    xml = employee(
        extra=f"<rolesIds>{UUID(int=88)}</rolesIds><roleCodes>002</roleCodes><roleCodes>001</roleCodes>"
    )
    xml = xml.replace("<employee>true</employee>", "<employee>false</employee>")
    with TestClient(app(Source(wrap(xml)), tmp_path)) as client:
        assert client.post(f"{BASE}/load").json()["employee_flag_count"] == 0
        item = client.get(BASE).json()["items"][0]
        assert item["role_ids"] == [ROLE, str(UUID(int=88))]
        assert item["role_codes"] == ["001", "002", "001"]
        assert item["employee"] is False


def test_openapi_exposes_only_reading_and_omits_credential_fields(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        schema = client.get("/openapi.json").json()
        assert {p for p in schema["paths"] if p.startswith(BASE)} == {BASE, f"{BASE}/load"}
        assert set(schema["paths"][BASE]) == {"get"}
        fields = schema["components"]["schemas"]["IikoEmployee"]["properties"]
        assert {"main_role_id", "role_ids", "main_role_code", "role_codes"} <= set(fields)
        assert not {"password", "pinCode", "pin_code", "cardNumber", "login"} & set(fields)
        assert not any("egais" in path.lower() for path in schema["paths"])
        assert not source.requests
