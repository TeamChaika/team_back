"""Startup tests use fresh processes because runtime identity is immutable by design."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

COMPANY = UUID("11111111-2222-4333-8444-555555555555")
ROOT = Path(__file__).resolve().parents[1]


def tenant_environment(company=COMPANY):
    env = {key: value for key, value in os.environ.items() if not key.startswith("RESTCONTROL_")}
    env.update(
        {
            "RESTCONTROL_RUNTIME_MODE": "tenant",
            "RESTCONTROL_TENANT_COMPANY_ID": str(company),
            "RESTCONTROL_TENANT_TIMEZONE": "Asia/Tokyo",
            "RESTCONTROL_TENANT_FRONTEND_ORIGIN": "https://customer.example",
            "RESTCONTROL_TENANT_API_ORIGIN": "https://api.customer.example",
            "RESTCONTROL_TENANT_RUNTIME_DIRECTORY": f"/tmp/restcontrol/c_{company.hex}",
            "RESTCONTROL_TENANT_CONFIGURATION_VERSION": "1",
            "RESTCONTROL_TENANT_DATABASE_ROLE": f"c_{company.hex}_runtime",
            "RESTCONTROL_TENANT_DATABASE_URL": "postgresql://unused@localhost/unused",
            "RESTCONTROL_TENANT_IIKO_BASE_URL": "https://iiko.customer.example/resto/api",
            "RESTCONTROL_TENANT_IIKO_LOGIN": "tenant-login",
            "RESTCONTROL_TENANT_IIKO_PASSWORD": "tenant-test-password",
            "RESTCONTROL_TENANT_WEB_SUPABASE_URL": "https://project.supabase.co",
            "RESTCONTROL_TENANT_WEB_ANON_KEY": "tenant-test-anon",
            # These must have no effect on a tenant process.
            "CHAIKA_DATABASE_URL": "postgresql://postgres@legacy/legacy",
            "CHAIKA_IIKO_BASE_URL": "https://iiko.chaika.team/resto/api",
            "CHAIKA_WEB_ORIGIN": "https://dashboard.chaika.team",
            "CHAIKA_WEB_DEPOSITS_API_URL": "https://pay.chaika.team/api/v1",
            "CHAIKA_AI_API_KEY": "legacy-only-key",
        }
    )
    return env


def run_startup(code, env=None):
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env or tenant_environment(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_settings_and_existing_sql_modules_use_one_tenant_without_legacy_fallback():
    run_startup("""
from app.core.config import Settings
from app.web.settings import WebSettings
from app.web.coverage import ZONE
from app.web.purchase_prices import JOIN
from app.web.repository import DB, Repository
from app.web.auth import login_candidates
from app.web.assistant import AssistantSettings
from app.tenancy.config import load_runtime
settings, web = Settings(), WebSettings()
assert settings.iiko_login == "tenant-login"
assert not AssistantSettings().configured
assert "unused@localhost" in settings.database_url.get_secret_value()
assert web.origin == "https://customer.example" and web.secure_cookie
assert web.deposits_api_url == "" and web.documents_api_url == ""
assert str(ZONE) == "Asia/Tokyo"
assert DB == '"' + load_runtime().analytics_schema + '"'
assert DB in JOIN and "chaika." not in JOIN
assert Repository(settings).runtime is load_runtime()
try:
    login_candidates("79990000000")
except ValueError:
    pass
else:
    raise AssertionError("tenant phone login silently used Chaika aliases")
""")


def test_missing_explicit_credentials_cannot_load_project_or_chaika_defaults():
    for missing in ["DATABASE_URL", "IIKO_LOGIN", "IIKO_PASSWORD", "IIKO_BASE_URL"]:
        env = tenant_environment()
        del env[f"RESTCONTROL_TENANT_{missing}"]
        run_startup(
            """
from pydantic import ValidationError
from app.core.config import Settings
try:
    Settings()
except ValidationError:
    pass
else:
    raise AssertionError("accepted missing explicit tenant settings")
""",
            env,
        )


def test_collector_uses_own_endpoint():
    env = tenant_environment()
    env["RESTCONTROL_TENANT_COLLECTOR_PORT"] = "18010"
    run_startup(
        """
from unittest.mock import patch
from app.tenancy.io import collector_post
with patch("app.tenancy.io.httpx.post") as post:
    collector_post("/api/v1/iiko/connections/primary/logout", timeout=5)
    assert post.call_args.args[0].startswith("http://127.0.0.1:18010/")
    assert post.call_args.kwargs["trust_env"] is False
""",
        env,
    )
    run_startup("""
from unittest.mock import patch
from app.tenancy.io import collector_post
from app.tenancy.config import load_runtime
with patch("app.tenancy.io.httpx.HTTPTransport") as transport, patch("app.tenancy.io.httpx.Client"):
    collector_post("/api/v1/iiko/connections/primary/logout")
    assert transport.call_args.kwargs["uds"] == str(load_runtime().collector_socket)
""")


def test_existing_dynamic_sql_builders_reference_tenant_identifiers():
    run_startup("""
from types import SimpleNamespace
from app.core.config import Settings
from app.web.repository import Repository
from app.tenancy.config import load_runtime
repo = Repository(Settings())
scope = SimpleNamespace(unrestricted=True, warehouse_restricted=False, store_ids=[], rms_ids=[])
for resource in ("invoices", "outgoing", "transfers", "writeoffs"):
    result = repo.resource_query(scope, resource)
    text = str(result)
    assert "chaika." not in text, text
    assert load_runtime().analytics_schema in text, text
repo.close()
""")
