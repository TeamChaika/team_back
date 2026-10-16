"""Actual settings migration and restricted-role isolation across two companies."""

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.operation_settings import allowed_store, load_settings, settings_schema
from app.tenancy.sql import render
from app.web.operation_settings import save_operation_settings
from tests.test_operation_settings import (
    ADMIN,
    SOURCE,
    TestAdministration,
    create_catalog,
    payload,
)
from tests.test_operation_settings import connection as connection
from tests.test_operation_settings import database as database
from tests.test_tenant_migrations import runtime_for


def test_two_tenants_migrate_and_read_only_their_own_settings(database):
    first, second = runtime_for(uuid4()), runtime_for(uuid4())
    template = Path("migrations/tenant/20261016123000_tenant_operation_settings.sql").read_text()
    for runtime in (first, second):
        role = sql.Identifier(runtime.database_role)
        namespace = sql.Identifier(runtime.analytics_schema)
        database.execute(sql.SQL("CREATE ROLE {}").format(role))
        create_catalog(database, runtime.analytics_schema)
        database.execute(render(template, runtime, runtime_role=runtime.database_role))
        database.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(namespace, role))
        database.execute(
            sql.SQL("GRANT SELECT ON {}.stores,{}.corporate_nodes TO {}").format(
                namespace, namespace, role
            )
        )
    try:
        database.tenant_runtime = first
        save_operation_settings(
            TestAdministration(database),
            ADMIN,
            payload(
                writeoff={"mode": "selected", "store_ids": []},
            ),
        )
        database.tenant_runtime = second
        assert load_settings(database)["revision"] == 0
        assert allowed_store(database, "writeoff", SOURCE)
        save_operation_settings(
            TestAdministration(database),
            ADMIN,
            payload(
                waybill={"mode": "selected", "store_ids": []},
            ),
        )
        for runtime, disabled, enabled in (
            (first, "writeoff", "waybill"),
            (second, "waybill", "writeoff"),
        ):
            database.tenant_runtime = runtime
            assert settings_schema(database) == runtime.analytics_schema
            database.execute(
                sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(runtime.database_role))
            )
            assert load_settings(database)["revision"] == 1
            assert not allowed_store(database, disabled, SOURCE)
            assert allowed_store(database, enabled, SOURCE)
            foreign = second if runtime is first else first
            with pytest.raises(psycopg.errors.InsufficientPrivilege), database.transaction():
                database.execute(
                    sql.SQL("SELECT * FROM {}.operation_settings").format(
                        sql.Identifier(foreign.analytics_schema)
                    )
                )
            with pytest.raises(psycopg.errors.InsufficientPrivilege), database.transaction():
                database.execute("SELECT * FROM chaika.operation_settings")
            database.execute("RESET ROLE")
    finally:
        database.execute("RESET ROLE")
        del database.tenant_runtime


def test_tenant_platform_owner_audits_real_actor_and_rejects_other_company(database):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.tenancy.actor import ActorContext
    from app.web.administration import Administration

    runtime = runtime_for(uuid4())
    database.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(runtime.database_role)))
    create_catalog(database, runtime.analytics_schema)
    database.execute(
        render(
            Path("migrations/tenant/20261016123000_tenant_operation_settings.sql").read_text(),
            runtime,
            runtime_role=runtime.database_role,
        )
    )

    @contextmanager
    def connection():
        yield database

    repo = SimpleNamespace(
        runtime=runtime,
        _pool=SimpleNamespace(
            open=lambda: None,
            connection=connection,
        ),
    )
    actor = ActorContext(runtime.company_id, ADMIN, "platform_owner", "Owner")
    database.tenant_runtime = runtime
    try:
        saved = save_operation_settings(
            Administration(repo),
            actor,
            payload(
                writeoff={"mode": "selected", "store_ids": []},
            ),
        )
        assert saved["updated_by"] == str(ADMIN)
        audit = database.execute(
            sql.SQL("SELECT actor_id FROM {}.operation_settings_audit").format(
                sql.Identifier(runtime.analytics_schema)
            )
        ).fetchone()
        assert audit["actor_id"] == ADMIN
        foreign = ActorContext(uuid4(), ADMIN, "platform_owner", "Owner")
        with pytest.raises(HTTPException) as error:
            save_operation_settings(Administration(repo), foreign, payload(revision=1))
        assert error.value.status_code == 403
        assert load_settings(database)["revision"] == 1
    finally:
        del database.tenant_runtime
