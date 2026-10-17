"""External-only writes preserve the real restricted tenant SQL boundary."""

from psycopg.conninfo import make_conninfo

from app.tenancy.migrations import provision_tenant
from tests.test_tenant_migrations_postgres import empty_database as empty_database
from tests.test_tenant_runtime_wiring import run_startup, tenant_environment


def test_restricted_tenant_external_snapshot_roundtrip(empty_database):
    operator, dsn, tenant = empty_database
    runtime = tenant()
    provision_tenant(operator, runtime)
    env = tenant_environment(runtime.company_id)
    env["RESTCONTROL_TENANT_DATABASE_URL"] = make_conninfo(dsn, user=runtime.database_role)
    env["RESTCONTROL_TENANT_SNAPSHOT_ARCHIVE_MODE"] = "broker"
    env["RESTCONTROL_TENANT_SNAPSHOT_ARCHIVE_BROKER_SOCKET"] = "/run/test/archive.sock"
    run_startup(
        '''
import hashlib
from datetime import datetime, timezone
from uuid import uuid4
from psycopg.rows import dict_row
from app.core.config import Settings
from app.tenancy.connection import tenant_connect
from app.tenancy.sql import ANALYTICS_SCHEMA
from app.tenancy.config import load_runtime
from app.sync_references import Source, register_sources, append_snapshot
from app import snapshot_archive as archive
objects = {}
class Storage:
    def put(self, key, data): objects[key] = data
    def get(self, key): return objects[key]
archive.storage = lambda config: Storage()
assert archive.ArchiveConfig.from_env().scope == load_runtime().analytics_schema
with tenant_connect(Settings().database_url.get_secret_value(), autocommit=True) as db:
    register_sources(db, [Source('primary', 'test', 'https://example.org/resto/api', 'a'*64)])
    run, snapshot_id = uuid4(), uuid4()
    db.execute(f"INSERT INTO {ANALYTICS_SCHEMA}.sync_runs(id,status) VALUES(%s,'running')", (run,))
    append_snapshot(db, run, {'id':snapshot_id, 'source_id':'primary', 'resource':'stores',
        'observed_at':datetime.now(timezone.utc), 'sha256':hashlib.sha256(b'test').hexdigest(),
        'raw':b'test', 'payload':{'items':[]}})
    with db.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute(f'SELECT * FROM {ANALYTICS_SCHEMA}.raw_snapshots WHERE id=%s',
                             (snapshot_id,)).fetchone()
    assert row['raw'] is None and row['normalized'] is None
    assert '/' + load_runtime().analytics_schema + '/' in row['payload_object_key']
    assert archive.load_snapshot_payload(row) == {'raw':b'test', 'payload':{'items':[]}}
''',
        env,
    )
