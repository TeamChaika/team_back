from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from uuid import UUID

import pytest

from app.tenancy.config import (
    RuntimeConfigurationError,
    TenantRuntime,
    identifier,
    load_runtime,
    schema_names,
)
from app.tenancy.locks import advisory_lock_key
from app.tenancy.sql import render, validate_database_runtime

A = UUID("11111111-2222-4333-8444-555555555555")
B = UUID("11111111-2222-4333-8444-555555555556")


def runtime(company=A):
    return TenantRuntime(
        mode="tenant",
        company_id=company,
        **{f"{area}_schema": name for area, name in schema_names(company).items()},
        timezone="Europe/Moscow",
        frontend_origin="https://customer.example",
        api_origin="https://api.customer.example",
        configuration_version=2,
        runtime_directory=Path("/run/restcontrol") / f"c_{company.hex}",
        database_role=f"c_{company.hex}_runtime",
    )


def environment():
    tenant = runtime()
    return {
        "RESTCONTROL_RUNTIME_MODE": "tenant",
        "RESTCONTROL_TENANT_COMPANY_ID": str(A),
        "RESTCONTROL_TENANT_TIMEZONE": tenant.timezone,
        "RESTCONTROL_TENANT_FRONTEND_ORIGIN": tenant.frontend_origin,
        "RESTCONTROL_TENANT_API_ORIGIN": tenant.api_origin,
        "RESTCONTROL_TENANT_CONFIGURATION_VERSION": "2",
        "RESTCONTROL_TENANT_RUNTIME_DIRECTORY": str(tenant.runtime_directory),
        "RESTCONTROL_TENANT_DATABASE_ROLE": tenant.database_role,
    }


def test_legacy_has_no_tenant_configuration_or_database_side_effects():
    assert TenantRuntime.from_env({}) == TenantRuntime()
    assert TenantRuntime().schema("analytics") == "chaika"
    validate_database_runtime(object(), TenantRuntime())


def test_env_is_explicit_and_never_reads_chaika_fallbacks():
    env = environment()
    env["CHAIKA_WEB_ORIGIN"] = "https://dashboard.chaika.team"
    assert TenantRuntime.from_env(env) == runtime()
    for key in list(environment()):
        incomplete = environment()
        del incomplete[key]
        with pytest.raises(ValueError):
            TenantRuntime.from_env(incomplete)


def test_runtime_is_frozen_and_resolved_only_once(monkeypatch):
    with pytest.raises(FrozenInstanceError):
        runtime().timezone = "UTC"
    for key, value in environment().items():
        monkeypatch.setenv(key, value)
    load_runtime.cache_clear()
    try:
        resolved = load_runtime()
        monkeypatch.setenv("RESTCONTROL_TENANT_COMPANY_ID", str(B))
        assert load_runtime() is resolved
    finally:
        load_runtime.cache_clear()


@pytest.mark.parametrize("value", ["chaika.x", 'a";DROP SCHEMA public;--', "A", "", "a" * 64])
def test_invalid_identifiers(value):
    with pytest.raises(RuntimeConfigurationError):
        identifier(value)
    with pytest.raises(RuntimeConfigurationError):
        render("SELECT * FROM {analytics}.{table}", runtime(), table=value)


@pytest.mark.parametrize(
    "changes",
    [
        {"company_id": None},
        {"company_id": UUID(int=0)},
        {"analytics_schema": "chaika"},
        {"documents_schema": schema_names(B)["documents"]},
        {"database_role": "postgres"},
        {"timezone": "invalid/zone"},
        {"configuration_version": 0},
        {"configuration_version": True},
        {"runtime_directory": Path("/tmp/shared")},
        {"runtime_directory": Path("../") / f"c_{A.hex}"},
        {"collector_port": 0},
        {"collector_port": True},
        {"mode": "unknown"},
    ],
)
def test_invalid_runtime_rejected(changes):
    with pytest.raises(ValueError):
        replace(runtime(), **changes)


@pytest.mark.parametrize(
    "origin",
    [
        "https://dashboard.chaika.team",
        "https://api.chaika.team",
        "http://customer.example",
        "https://customer.example/",
        "https://customer.example?q=1",
        "https://customer.example#x",
        "https://u:p@customer.example",
        "https://*.example",
        "https://customer.example:bad",
        " https://customer.example",
        "https://CUSTOMER.example",
        "https://customer.example\\other",
    ],
)
def test_exact_origin_no_chaika_fallback(origin):
    with pytest.raises(ValueError):
        replace(runtime(), frontend_origin=origin)


def test_runtime_paths_locks_and_sql_cannot_cross_company():
    a, b = runtime(), runtime(B)
    assert a.collector_socket != b.collector_socket
    assert a.runtime_path("checkpoint.json") != b.runtime_path("checkpoint.json")
    for name in ["../secret", "/tmp/secret", "..", "nested/file"]:
        with pytest.raises(ValueError):
            a.runtime_path(name)
    assert advisory_lock_key(a, "sales", "primary") != advisory_lock_key(b, "sales", "primary")
    assert advisory_lock_key(a, "sales", "primary") != advisory_lock_key(a, "documents", "primary")
    assert advisory_lock_key(a, "sales", "primary") == advisory_lock_key(a, "sales", "primary")
    assert advisory_lock_key(a, "a:b", "c") != advisory_lock_key(a, "a", "b:c")
    query = render("SELECT * FROM {analytics}.{table} WHERE id = %s", a, table="orders")
    assert query.as_string() == f'SELECT * FROM "{a.analytics_schema}"."orders" WHERE id = %s'
    assert render("SELECT '{{}}'::jsonb FROM {documents}.users", b).as_string() == (
        f"SELECT '{{}}'::jsonb FROM \"{b.documents_schema}\".users"
    )
    assert render("SELECT * FROM {analytics}.orders", TenantRuntime()).as_string() == (
        'SELECT * FROM "chaika".orders'
    )


@pytest.mark.parametrize(
    "template",
    [
        "SELECT {unknown}",
        "SELECT {analytics!r}",
        "SELECT {analytics:>10}",
        "SELECT {analytics.upper}",
        "SELECT {}",
    ],
)
def test_template_only_accepts_declared_identifier_placeholders(template):
    with pytest.raises(RuntimeConfigurationError):
        render(template, runtime())


def test_template_cannot_override_schemas():
    with pytest.raises(RuntimeConfigurationError):
        render("SELECT * FROM {analytics}.orders", runtime(), analytics="chaika")
