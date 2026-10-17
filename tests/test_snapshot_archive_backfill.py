"""Real PostgreSQL checks for verified offload and preserved relational history."""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
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
