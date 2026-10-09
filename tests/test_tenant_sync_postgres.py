"""Real collector writes against freshly provisioned synthetic companies."""

from psycopg.conninfo import make_conninfo

from app.tenancy.migrations import provision_tenant
from tests.test_tenant_migrations_postgres import empty_database as empty_database
from tests.test_tenant_runtime_wiring import run_startup, tenant_environment


def test_real_sync_primary_identity_and_snapshots_do_not_cross_companies(empty_database):
    operator, dsn, new_tenant = empty_database
    first, second = new_tenant(), new_tenant()
    for runtime, label in [(first, "company-a"), (second, "company-b")]:
        provision_tenant(operator, runtime)
        env = tenant_environment(runtime.company_id)
        env["RESTCONTROL_TENANT_DATABASE_URL"] = make_conninfo(dsn, user=runtime.database_role)
        run_startup(
            (
                '\n'
                'import hashlib\n'
                'from datetime import datetime, timezone\n'
                'from uuid import uuid4\n'
                'from app.core.config import Settings\n'
                'from app.tenancy.connection import tenant_connect\n'
                'from app.tenancy.sql import ANALYTICS_SCHEMA\n'
                'from app.sync_references import Source, register_sources, '
                'append_snapshot, reference_lock\n'
                'label = LABEL\n'
                'with tenant_connect(Settings().database_url.get_secret_value(), '
                'autocommit=True) as db, reference_lock(db):\n'
                "    source=Source('primary', label, 'https://' + label + "
                "'.example/resto/api', 'a'*64)\n"
                '    register_sources(db,[source])\n'
                '    run=uuid4()\n'
                '    db.execute(f"INSERT INTO '
                '{ANALYTICS_SCHEMA}.sync_runs(id,status) VALUES(%s,\'running\')",(run,))\n'
                '    '
                "append_snapshot(db,run,{'id':uuid4(),'source_id':'primary','resou"
                "rce':'stores','observed_at':datetime.now(timezone.utc),'sha256':h"
                "ashlib.sha256(b'synthetic').hexdigest(),'raw':b'synthetic','paylo"
                "ad':{'tenant':label}})\n"
                "    assert db.execute(f'SELECT label FROM "
                "{ANALYTICS_SCHEMA}.sources WHERE id=%s',('primary',)).fetchone()[0]==label\n"
                "    assert db.execute(f'SELECT count(*) FROM "
                "{ANALYTICS_SCHEMA}.raw_snapshots').fetchone()[0]==1\n"
            ).replace("LABEL", repr(label)),
            env,
        )
    for runtime, label in [(first, "company-a"), (second, "company-b")]:
        row = operator.execute(
            f'SELECT label FROM "{runtime.analytics_schema}".sources WHERE id=%s', ("primary",)
        ).fetchone()
        assert row[0] == label
