"""Encrypted owner settings: real PostgreSQL, auth and optimistic updates."""

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.saas_admin.company_module_settings import CompanyModuleSettings, ModuleSettingsWrite
from app.saas_admin.repository import Problem
from app.saas_admin.server import create_app
from tests.test_saas_admin_postgres import database as database
from tests.test_saas_admin_postgres import imported as imported
from tests.test_saas_admin_postgres import pg_server as pg_server

TOKEN = "123456789:" + "s" * 32
KEY = "synthetic-provider-private-key"
SELLER = dict(
    name="Company",
    inn="1234567890",
    bank_name="Bank",
    bic="123456789",
    account="1" * 20,
    correspondent_account="2" * 20,
)


def test_encryption_isolation_versions_and_secret_clear(imported):
    repo, _, actor, first, second, _ = imported
    first = repo.get(first["id"])
    service = CompanyModuleSettings(repo)
    assert service.get(first["id"], actor)["missing"] == dict(
        seller=True, telegram=True, assistant=True
    )
    body = ModuleSettingsWrite(
        expected_version=first["version"],
        seller=SELLER,
        telegram=dict(username="company_bot", token=TOKEN),
        assistant=dict(provider="openai", key=KEY),
    )
    result = service.update(first["id"], body, actor)
    assert result["company_version"] == first["version"] + 1
    assert not any(result["missing"].values())
    assert TOKEN not in json.dumps(result) and KEY not in json.dumps(result)
    assert service.get(second["id"], actor)["missing"]["telegram"]
    with repo.connect() as db:
        raw = str(db.execute("SELECT * FROM company_module_settings").fetchall())
        assert TOKEN not in raw and KEY not in raw and "Company" not in raw
    audit = json.dumps(repo.events(first["id"], 100, 0))
    assert TOKEN not in audit and KEY not in audit and "1234567890" not in audit
    assert service.runtime_settings(first["id"])["assistant_settings"]["api_key"] == KEY
    assert (
        service.runtime_settings(first["id"], expected_version=result["company_version"])[
            "assistant_settings"
        ]["api_key"]
        == KEY
    )
    with pytest.raises(Problem) as stale:
        service.runtime_settings(first["id"], expected_version=first["version"])
    assert stale.value.code == "version_conflict"

    with pytest.raises(Problem) as exc:
        service.update(first["id"], body, actor)
    assert exc.value.code == "version_conflict"
    result = service.update(
        first["id"],
        ModuleSettingsWrite(
            expected_version=result["company_version"],
            expected_revision=result["integrations_revision"],
            telegram=dict(username="newcompany_bot", token=""),
        ),
        actor,
    )
    assert result["telegram"]["token_configured"]
    result = service.update(
        first["id"],
        ModuleSettingsWrite(
            expected_version=result["company_version"],
            expected_revision=result["integrations_revision"],
            telegram=dict(username="", clear_token=True),
            seller=None,
            assistant=dict(provider="openrouter"),
        ),
        actor,
    )
    assert result["missing"] == dict(seller=True, telegram=True, assistant=True)
    assert service.runtime_settings(first["id"])["assistant_settings"]["api_key"] == ""


def test_owner_http_boundary_csrf_and_safe_validation(imported, tmp_path):
    repo, _, actor, first, second, _ = imported
    first = repo.get(first["id"])
    app = create_app(tmp_path / "app", repository=repo)
    real_session = repo.session
    repo.session = lambda token: (
        {"user": actor, "csrf_token": "safe-csrf"} if token == "owner" else real_session(token)
    )
    url = "/api/saas-admin/companies/" + first["id"] + "/module-settings"
    with TestClient(app, base_url="http://127.0.0.1:8210") as client:
        assert client.get(url).status_code == 401
        client.cookies.set("saas_owner_session", "owner")
        assert client.get(url).status_code == 200
        body = dict(
            expected_version=first["version"], telegram=dict(username="company_bot", token=TOKEN)
        )
        assert (
            client.patch(url, json=body, headers={"Origin": "http://127.0.0.1:8210"}).status_code
            == 403
        )
        headers = {"Origin": "http://127.0.0.1:8210", "X-CSRF-Token": "safe-csrf"}
        bad = client.patch(
            url, json={**body, "telegram": dict(token="sensitive-invalid-token")}, headers=headers
        )
        assert bad.status_code == 422 and "sensitive-invalid-token" not in bad.text
        response = client.patch(url, json=body, headers=headers)
        assert response.status_code == 200 and TOKEN not in response.text
        assert client.patch(url, json=body, headers=headers).status_code == 409
        assert client.get(url.replace(first["id"], str(uuid4()))).status_code == 404
        assert client.get(url, headers={"Host": "other.example.com"}).status_code == 403
    with pytest.raises(Problem) as exc:
        CompanyModuleSettings(repo).get(first["id"], {"id": str(uuid4())})
    assert exc.value.status == 403


@pytest.mark.parametrize(
    "field,value",
    [
        ("inn", "123"),
        ("inn", "1" * 11),
        ("kpp", "12"),
        ("bic", "1" * 8),
        ("account", "2" * 19),
        ("correspondent_account", "a" * 20),
        ("name", ""),
    ],
)
def test_invalid_seller(field, value):
    with pytest.raises(ValidationError):
        ModuleSettingsWrite(expected_version=1, seller={**SELLER, field: value})


def test_explicit_clear_conflict_and_no_unknown_settings():
    for extra in [
        dict(telegram=dict(token=TOKEN, clear_token=True)),
        dict(assistant=dict(key=KEY, clear_key=True)),
        dict(assistant=dict(provider="unknown")),
        dict(assistant=dict(auth_admin_key=KEY)),
        dict(telegram=None),
    ]:
        with pytest.raises(ValidationError):
            ModuleSettingsWrite(expected_version=1, **extra)


def test_runtime_overlay_reads_encrypted_current_company_and_clears_old_files(imported):
    from app.saas_admin.runtime_module_settings import apply_company_settings

    repo, _, actor, first, second, _ = imported
    first = repo.get(first["id"])
    second = repo.get(second["id"])
    service = CompanyModuleSettings(repo)
    saved = service.update(
        first["id"],
        ModuleSettingsWrite(
            expected_version=first["version"],
            telegram=dict(username="company_bot", token=TOKEN),
            assistant=dict(provider="openrouter", key=KEY, model="own-model"),
        ),
        actor,
    )
    first = repo.get(first["id"])
    assert first["version"] == saved["company_version"]
    own = apply_company_settings({}, first, service)
    assert own["document_settings"]["bot_token"] == TOKEN
    assert own["assistant_settings"]["api_key"] == KEY
    old_file = {
        "document_settings": {"bot_token": TOKEN},
        "assistant_settings": {"api_key": KEY, "timeweb_agent_id": "old-agent"},
    }
    foreign = apply_company_settings(old_file, second, service)
    assert foreign["document_settings"]["bot_token"] == ""
    assert foreign["assistant_settings"]["api_key"] == ""
    assert "timeweb_agent_id" not in foreign["assistant_settings"]
