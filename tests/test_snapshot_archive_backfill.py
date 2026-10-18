"""Real PostgreSQL checks for verified offload and preserved relational history."""

import hashlib
import json
import os
import sys
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import snapshot_archive as archive
from tools import archive_snapshots as backfill


@pytest.fixture
def db():
    url = os.environ.get("CHAIKA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("Set isolated PostgreSQL CHAIKA_TEST_DATABASE_URL")
    if "127.0.0.1:15438/" not in url:
        pytest.fail("Only disposable localhost:15438 is allowed")
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as connection:
        if not connection.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='chaika' "
            "AND table_name='raw_snapshots' AND column_name='payload_object_key'"
        ).fetchone():
            with connection.transaction():
                connection.execute(
                    Path("supabase/migrations/20261017140000_snapshot_archive.sql").read_text()
                )
        with connection.transaction():
            yield connection
            raise psycopg.Rollback()


@pytest.fixture
def record(db):
    source, run, key = "archive-test-" + uuid4().hex, uuid4(), uuid4()
    db.execute(
        "INSERT INTO chaika.sources(id,label,base_url,fingerprint) VALUES(%s,%s,%s,%s)",
        (source, "archive-test", "https://example.invalid", "a" * 64),
    )
    db.execute(
        "INSERT INTO chaika.sync_runs(id,job,status,finished_at) "
        "VALUES(%s,'inventory','failed',now())",
        (run,),
    )
    raw, observed = b"<items>archive-test</items>", datetime.now(UTC)
    db.execute(
        "INSERT INTO chaika.raw_snapshots(id,run_id,source_id,resource,observed_at,sha256,"
        "source_bytes,raw,normalized) VALUES(%s,%s,%s,'products',%s,%s,%s,%s,%s)",
        (
            key,
            run,
            source,
            observed,
            hashlib.sha256(raw).hexdigest(),
            len(raw),
            raw,
            Jsonb({"items": [{"id": "sample", "value": 1.25}], "optional": None}),
        ),
    )
    db.execute(
        "INSERT INTO chaika.stores(source_id,id,name,first_seen_at,last_seen_at,last_snapshot_id) "
        "VALUES(%s,%s,'archive-test-store',%s,%s,%s)",
        (source, uuid4(), observed, observed, key),
    )
    return db.execute("SELECT * FROM chaika.raw_snapshots WHERE id=%s", (key,)).fetchone()


@pytest.fixture
def config(monkeypatch):
    class MemoryStorage:
        def __init__(self):
            self.values = {}

        def put(self, key, data):
            self.values[key] = data

        def get(self, key):
            return self.values[key]

    client = MemoryStorage()
    monkeypatch.setattr(archive, "storage", lambda _: client)
    return archive.ArchiveConfig(mode="s3"), client


def current(db, record):
    return db.execute("SELECT * FROM chaika.raw_snapshots WHERE id=%s", (record["id"],)).fetchone()


def test_verified_offload_preserves_metadata_and_foreign_key(db, record, config):
    settings, client = config
    result = backfill.offload_one(db, "chaika", record, settings, apply=True, guard=lambda: None)
    saved = current(db, record)
    assert result["offloaded"] and saved["raw"] is None and saved["normalized"] is None
    for name in ("id", "run_id", "source_id", "resource", "observed_at", "sha256", "source_bytes"):
        assert saved[name] == record[name]
    assert (
        db.execute(
            "SELECT last_snapshot_id FROM chaika.stores WHERE source_id=%s", (record["source_id"],)
        ).fetchone()["last_snapshot_id"]
        == record["id"]
    )
    restored = archive.load_snapshot_payload(saved, settings)
    assert restored == {"raw": record["raw"], "payload": record["normalized"]}
    assert len(client.values) == 1


def test_cli_binary_payload_fetch_roundtrip(db, record, config, monkeypatch, tmp_path):
    raw = bytes(range(256)) * 1024
    payload = {"items": [None, True, False, 123, 1.25, "Чайка 🐦"], "nested": {"value": []}}
    db.execute(
        "UPDATE chaika.raw_snapshots SET raw=%s,normalized=%s,sha256=%s,source_bytes=%s "
        "WHERE id=%s",
        (raw, Jsonb(payload), hashlib.sha256(raw).hexdigest(), len(raw), record["id"]),
    )
    formats = []
    execute = db.execute

    def tracked_execute(*args, **kwargs):
        cursor = execute(*args, **kwargs)
        if cursor.description and "raw" in {column.name for column in cursor.description}:
            formats.append(
                tuple(cursor.pgresult.fformat(i) for i in range(cursor.pgresult.nfields))
            )
        return cursor

    monkeypatch.setattr(db, "execute", tracked_execute)
    monkeypatch.setattr(backfill.psycopg, "connect", lambda **kwargs: nullcontext(db))
    monkeypatch.setattr(
        backfill,
        "select_batch",
        lambda *args, **kwargs: [
            {"id": record["id"], "source_bytes": len(raw), "stored_bytes": len(raw)}
        ],
    )
    monkeypatch.setattr(backfill.ArchiveConfig, "from_env", lambda: config[0])
    dsn, guard, manifest = (tmp_path / name for name in ("db.json", "guard.json", "manifest.jsonl"))
    dsn.write_text("{}")
    dsn.chmod(0o600)
    guard.write_text(json.dumps({"checked_at": time.time(), "available_bytes": 2 * 1024**3}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "archive_snapshots",
            "--schema",
            "chaika",
            "--dsn-file",
            str(dsn),
            "--manifest",
            str(manifest),
            "--apply",
            "--guard-file",
            str(guard),
        ],
    )

    backfill.main()

    assert formats == [(1,) * 7]
    saved = current(db, record)
    assert saved["raw"] is None and saved["normalized"] is None
    assert archive.load_snapshot_payload(saved, config[0]) == {"raw": raw, "payload": payload}
    assert json.loads(manifest.read_text())["offloaded"] is True


def test_sql_numeric_semantics_mismatch_preserves_inline(db, record, config):
    db.execute(
        "UPDATE chaika.raw_snapshots SET normalized="
        "'{\"amount\":0.1000000000000000000000000000000001}'::jsonb WHERE id=%s",
        (record["id"],),
    )
    record = current(db, record)
    with pytest.raises(RuntimeError, match="json_mismatch"):
        backfill.offload_one(db, "chaika", record, config[0], apply=True, guard=lambda: None)
    saved = current(db, record)
    assert saved["raw"] == record["raw"] and saved["payload_object_key"] is None
    assert (
        db.execute(
            "SELECT normalized->>'amount' AS amount FROM chaika.raw_snapshots WHERE id=%s",
            (record["id"],),
        ).fetchone()["amount"]
        == "0.1000000000000000000000000000000001"
    )


def test_guard_failure_preserves_inline(db, record, config):
    def guard():
        raise RuntimeError("archive_disk_guard_stale_or_low")

    with pytest.raises(RuntimeError, match="guard"):
        backfill.offload_one(db, "chaika", record, config[0], apply=True, guard=guard)
    assert current(db, record)["raw"] == record["raw"]


def test_fetch_failure_preserves_inline(db, record, config, monkeypatch):
    def fail(_):
        raise archive.ArchiveError("snapshot_archive_download_failed")

    monkeypatch.setattr(config[1], "get", fail)
    with pytest.raises(archive.ArchiveError, match="download_failed"):
        backfill.offload_one(db, "chaika", record, config[0], apply=True, guard=lambda: None)
    assert current(db, record)["raw"] == record["raw"]


@pytest.mark.parametrize(
    "assignment",
    [
        "raw=NULL",
        "normalized=NULL",
        "payload_object_key='unexpected'",
        "raw=NULL,normalized=NULL",
        "raw=NULL,normalized=NULL,payload_object_key='x',"
        "payload_object_sha256=repeat('a',64),payload_object_bytes=1,"
        "payload_format_version=1,payload_verified_at=NULL",
    ],
)
def test_migration_rejects_incomplete_storage_states(db, record, assignment):
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            db.execute(
                "UPDATE chaika.raw_snapshots SET " + assignment + " WHERE id=%s", (record["id"],)
            )
    assert current(db, record)["raw"] == record["raw"]


@pytest.fixture
def batch_table(db):
    schema = "archive_batch_" + uuid4().hex
    db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    db.execute(
        sql.SQL(
            "CREATE TABLE {}.raw_snapshots "
            "(id uuid PRIMARY KEY, source_bytes bigint, raw bytea, normalized jsonb)"
        ).format(sql.Identifier(schema))
    )

    def insert(size, *, external=False):
        key, raw = uuid4(), os.urandom(size)
        db.execute(
            sql.SQL("INSERT INTO {}.raw_snapshots VALUES (%s,%s,%s,%s)").format(
                sql.Identifier(schema)
            ),
            (
                key,
                size,
                None if external else raw,
                None if external else Jsonb({"size": size}),
            ),
        )
        return key

    return schema, insert


def test_batch_defaults_keep_tail_order_and_limit(db, batch_table):
    schema, insert = batch_table
    first, second, third = insert(100), insert(200), insert(300)
    selected = backfill.select_batch(db, schema, limit=2)
    assert [row["id"] for row in selected] == [third, second]
    assert first not in [row["id"] for row in selected]
    assert all(set(row) == {"id", "source_bytes", "stored_bytes"} for row in selected)


def test_batch_filters_tiny_external_and_source_oversize(db, batch_table):
    schema, insert = batch_table
    wanted = insert(12000)
    insert(100)
    insert(20000)
    insert(12000, external=True)
    selected = backfill.select_batch(
        db,
        schema,
        limit=100,
        min_stored_bytes=8192,
        max_source_bytes=16000,
    )
    assert [row["id"] for row in selected] == [wanted]
    assert selected[0]["stored_bytes"] >= 12000


def test_batch_budget_is_aggregate_and_inclusive(db, batch_table):
    schema, insert = batch_table
    for _ in range(3):
        insert(12000)
    all_rows = backfill.select_batch(db, schema, limit=100)
    exact = sum(row["stored_bytes"] for row in all_rows[:2])
    selected = backfill.select_batch(db, schema, limit=100, max_stored_bytes=exact)
    assert selected == all_rows[:2]
    selected = backfill.select_batch(db, schema, limit=100, max_stored_bytes=exact - 1)
    assert selected == all_rows[:1]


def test_batch_oversized_tail_does_not_starve_smaller_rows(db, batch_table):
    schema, insert = batch_table
    wanted = insert(12000)
    insert(30000)
    selected = backfill.select_batch(db, schema, limit=100, max_stored_bytes=16000)
    assert [row["id"] for row in selected] == [wanted]


def test_batch_no_eligible_rows_is_empty(db, batch_table):
    schema, insert = batch_table
    insert(12000)
    assert backfill.select_batch(db, schema, limit=100, max_stored_bytes=1) == []


@pytest.mark.parametrize(
    "limits",
    [
        {"limit": 0},
        {"min_stored_bytes": -1},
        {"max_stored_bytes": 0},
        {"max_source_bytes": 0},
        {"min_stored_bytes": 20, "max_stored_bytes": 10},
    ],
)
def test_batch_rejects_invalid_limits_before_query(limits):
    class NoQuery:
        def execute(self, *args):
            pytest.fail("Invalid limits must be rejected without querying")

    with pytest.raises(ValueError, match="batch_limits_invalid"):
        backfill.select_batch(NoQuery(), "chaika", **({"limit": 1} | limits))
