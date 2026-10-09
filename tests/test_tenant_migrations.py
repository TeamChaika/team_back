"""Template contracts; execution acceptance is in the PostgreSQL suite."""

import re
from dataclasses import replace
from uuid import UUID

import pytest

from app.tenancy.config import RuntimeConfigurationError, TenantRuntime, schema_names
from app.tenancy.migrations import load_migrations, provision_tenant
from app.tenancy.sql import render


def runtime_for(company=None):
    company = company or UUID("1ae6eef8-13b3-4c38-9278-df9dbef28537")
    return TenantRuntime(
        mode="tenant",
        company_id=company,
        **{f"{area}_schema": schema for area, schema in schema_names(company).items()},
        database_role=f"c_{company.hex}_runtime",
        timezone="UTC",
        frontend_origin="https://customer.example",
        api_origin="https://api.customer.example",
        runtime_directory=f"/tmp/restcontrol/c_{company.hex}",
    )


def test_baselines_are_identifier_templates_without_customer_rows():
    migrations = load_migrations()
    assert [m.area for m in migrations] == [
        "analytics",
        "documents",
        "documents",
        "documents",
        "documents",
        "payments",
        "analytics",
        "analytics",
        "analytics",
        "payments",
        "payments",
        "analytics",
    ]
    runtime = runtime_for()
    for migration in migrations:
        query = render(
            migration.template,
            runtime,
            runtime_role=runtime.database_role,
            payments_role=f"{runtime.key}_payments_runtime",
            identity_role=f"{runtime.key}_identity_runtime",
            migration_owner_role="tenant_test_migrator",
        ).as_string()
        # Routine bodies may provision rows only when called; migration itself
        # must remain empty of company/customer data.
        ddl = re.sub(r"\$identity\$.*?\$identity\$", "", query, flags=re.DOTALL)
        for forbidden in (
            "chaika.",
            "chaika_iiko_documents.",
            "auth.users",
            "portal_warehouse_scope_test",
            "INSERT INTO",
            "BEGIN;",
            "COMMIT;",
        ):
            assert forbidden not in ddl
        assert len(migration.checksum) == 64
        assert replace(migration, template=migration.template + "\n").checksum != migration.checksum
    for migration, table in zip(
        migrations,
        (
            "portal_identity_metadata",
            "authentication_user",
            "native_dispatch",
            "commercial_counterparty_operations",
            "native_actor_audit",
            "terminal_versions",
            "provision_company_identity",
            "portal_account_initializations",
            "consume_company_password_recovery",
            "terminal_checks",
            "acceptance_intents",
            "provision_company_primary_admin",
        ),
        strict=True,
    ):
        assert table in migration.template


def test_never_provisions_legacy():
    with pytest.raises(RuntimeConfigurationError):
        provision_tenant(None, TenantRuntime())
