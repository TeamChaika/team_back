"""Reference sync checks. DB tests use only the explicitly isolated local test port."""

import hashlib
import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from app.sync_references import (
    LOCK_ID,
    Source,
    SyncError,
    append_snapshot,
    capture_snapshot,
    check_local_api,
    publish,
    reference_lock,
    register_sources,
)


@pytest.fixture
def db():
    url = os.environ.get("CHAIKA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set CHAIKA_TEST_DATABASE_URL for an isolated migrated PostgreSQL")
    if "127.0.0.1:15438/" not in url:
        pytest.fail("DB tests must use isolated localhost:15438")
    with psycopg.connect(url, autocommit=True) as connection, connection.transaction():
        connection.execute("SET LOCAL ROLE chaika_backend")
        yield connection
        # The outer test transaction is always rolled back; committed migration is kept.
        raise psycopg.Rollback()


@pytest.fixture
def bundle():
    now = datetime.now(UTC)
    department_id, store_id, group_id = map(str, (uuid4(), uuid4(), uuid4()))
    sources = [
        Source(key, key, f"https://{key}.example/resto/api", "a" * 64)
        for key in ["primary", "rms-a"]
    ]
    snapshots = {}

    def add(key, resource, **payload):
        raw = b"test fixture: " + resource.encode()
        metadata = {
            "snapshot_id": str(uuid4()),
            "received_at": now.isoformat(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "source_bytes": len(raw),
            "connection_id": key,
            "total": len(payload.get("items", [])),
            **payload,
        }
        snapshots[key, resource] = {
            "id": metadata["snapshot_id"],
            "source_id": key,
            "resource": resource,
            "observed_at": now,
            "sha256": metadata["sha256"],
            "raw": raw,
            "payload": metadata,
        }

    for source in sources:
        add(
            source.id,
            "server_type",
            server_type="CHAIN" if source.id == "primary" else "REPLICATED_RMS",
        )
        add(
            source.id,
            "departments",
            items=[
                {"id": department_id, "name": "Restaurant", "type": "DEPARTMENT", "parent_id": None}
            ],
            missing_parent_ids=[],
        )
    add(
        "rms-a",
        "groups",
        items=[
            {
                "id": group_id,
                "name": "Group",
                "department_id": department_id,
                "group_service_mode": "TABLE_SERVICE",
            }
        ],
    )
    add(
        "primary",
        "stores",
        items=[{"id": store_id, "parent_id": department_id, "name": "Store", "type": "STORE"}],
    )
    add("primary", "replication", items=[], status_counts={})
    return sources, snapshots


def stage(db, bundle):
    sources, snapshots = bundle
    register_sources(db, sources)
    run_id = uuid4()
    db.execute("INSERT INTO chaika.sync_runs(id,status) VALUES (%s,'running')", (run_id,))
    for snapshot in snapshots.values():
        append_snapshot(db, run_id, snapshot)
    return sources, snapshots


def test_repeated_publication_has_no_duplicates_and_keeps_first_seen(db, bundle):
    sources, snapshots = stage(db, bundle)
    first = publish(db, sources, snapshots)
    second = publish(db, sources, snapshots)
    assert first == second
    assert second["matched_rms"] == 1
    assert db.execute("SELECT count(*) FROM chaika.stores").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 7
    assert db.execute("SELECT first_seen_at=last_seen_at FROM chaika.stores").fetchone()[0]


def test_invalid_parent_rolls_back_publication(db, bundle):
    sources, snapshots = stage(db, bundle)
    publish(db, sources, snapshots)
    broken = deepcopy(snapshots)
    broken["primary", "departments"]["payload"]["items"][0]["name"] = "Must roll back"
    broken["primary", "stores"]["payload"]["items"][0]["parent_id"] = str(uuid4())
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        publish(db, sources, broken)
    assert db.execute("SELECT name FROM chaika.corporate_nodes").fetchone()[0] == "Restaurant"


def test_raw_cannot_be_overwritten_or_deleted_by_backend(db, bundle):
    stage(db, bundle)
    for statement in [
        "UPDATE chaika.raw_snapshots SET raw='oops'",
        "DELETE FROM chaika.raw_snapshots",
    ]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute(statement)


def test_raw_hash_constraint(db, bundle):
    sources, snapshots = stage(db, bundle)
    snapshot = deepcopy(snapshots["primary", "stores"])
    snapshot["id"] = str(uuid4())
    snapshot["raw"] = b"tampered"
    run_id = db.execute("SELECT id FROM chaika.sync_runs").fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        append_snapshot(db, run_id, snapshot)


def test_existing_source_cannot_silently_change_server(db, bundle):
    sources, _ = stage(db, bundle)
    changed = Source("primary", "Other", "https://other.example/resto/api", "b" * 64)
    with pytest.raises(SyncError, match="source_identity_changed"):
        register_sources(db, [changed])
    assert (
        db.execute("SELECT base_url FROM chaika.sources WHERE id='primary'").fetchone()[0]
        == sources[0].base_url
    )


def test_schema_unavailable_to_anonymous_and_backend_cannot_create_tables(db):
    assert not db.execute("SELECT has_schema_privilege('anon','chaika','USAGE')").fetchone()[0]
    assert not db.execute(
        "SELECT has_schema_privilege('authenticated','chaika','USAGE')"
    ).fetchone()[0]
    assert not db.execute("SELECT has_schema_privilege(current_user,'chaika','CREATE')").fetchone()[
        0
    ]


def test_lock_excludes_second_sync(db):
    assert db.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()[0]
    try:
        with psycopg.connect(os.environ["CHAIKA_TEST_DATABASE_URL"], autocommit=True) as other:
            assert not other.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()[0]
    finally:
        db.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


def test_lock_is_released_after_failure(db):
    with pytest.raises(ValueError), reference_lock(db):
        raise ValueError("stop")
    with psycopg.connect(os.environ["CHAIKA_TEST_DATABASE_URL"], autocommit=True) as other:
        assert other.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()[0]
        other.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


@pytest.mark.parametrize(
    "url",
    [
        "https://remote.example",
        "http://localhost:8010",
        "http://user:pass@127.0.0.1:8010",
        "http://127.0.0.1/x",
    ],
)
def test_api_must_be_local(url):
    with pytest.raises(SyncError):
        check_local_api(url)


def test_raw_source_and_hash_validation(tmp_path: Path):
    source = Source("primary", "Chain", "https://chain.example", "a" * 64)
    folder = tmp_path / "connections" / "primary"
    folder.mkdir(parents=True)
    key = str(uuid4())
    raw = b'"CHAIN"'
    response = {
        "snapshot_id": key,
        "checked_at": datetime.now(UTC).isoformat(),
        "server_type": "CHAIN",
        "source_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    (folder / f"{key}.meta.json").write_text(json.dumps({"source_fingerprint": source.fingerprint}))
    (folder / f"{key}.txt").write_bytes(raw)
    assert capture_snapshot(tmp_path, source, "server_type", response)["raw"] == raw
    (folder / f"{key}.txt").write_bytes(b'"WRONG"')
    with pytest.raises(SyncError, match="raw_hash_mismatch"):
        capture_snapshot(tmp_path, source, "server_type", response)
