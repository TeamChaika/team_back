"""Archive integrity and fail-closed publication contracts without live credentials."""

import gzip
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app import snapshot_archive as archive


@pytest.fixture
def snapshot():
    raw = b"<items><name>example</name></items>"
    return dict(
        id=uuid4(),
        source_id="primary",
        resource="products",
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        observed_at=datetime.now(UTC),
        payload={
            "items": [{"name": "Продукт", "price": 12.25}],
            "null": None,
            "bool": True,
            "integer": 12345678901234567890,
        },
    )


def decode(data, snapshot, scope="chaika"):
    return archive.decode_snapshot(
        data,
        scope,
        snapshot["id"],
        snapshot["source_id"],
        snapshot["resource"],
        snapshot["sha256"],
        len(snapshot["raw"]),
    )


def test_roundtrip_deterministic_and_json_semantics(snapshot):
    first = archive.encode_snapshot(snapshot, "chaika")
    assert first == archive.encode_snapshot(snapshot, "chaika")
    assert decode(first, snapshot) == {"raw": snapshot["raw"], "payload": snapshot["payload"]}
    with pytest.raises(archive.ArchiveError, match="identity_mismatch"):
        decode(first, snapshot, "c_" + "a" * 32 + "_analytics")


def test_corruption_and_decompression_limit(snapshot, monkeypatch):
    data = archive.encode_snapshot(snapshot, "chaika")
    with pytest.raises(archive.ArchiveError):
        decode(data[:-4], snapshot)
    monkeypatch.setattr(archive, "MAX_ENVELOPE_BYTES", 10)
    with pytest.raises(archive.ArchiveError, match="size_limit"):
        decode(data, snapshot)


def test_raw_and_identity_tampering(snapshot):
    value = json.loads(gzip.decompress(archive.encode_snapshot(snapshot, "chaika")))
    value["raw_base64"] = "YWJj"
    with pytest.raises(archive.ArchiveError, match="raw_integrity"):
        decode(gzip.compress(json.dumps(value).encode()), snapshot)
    with pytest.raises(archive.ArchiveError, match="scope_invalid"):
        archive.object_key("../foreign", snapshot["id"], "a" * 64)


class MemoryStorage:
    def put(self, key, data):
        self.data = data

    def get(self, key):
        return self.data


def external_row(snapshot, pointer):
    return dict(
        id=snapshot["id"],
        source_id=snapshot["source_id"],
        resource=snapshot["resource"],
        sha256=snapshot["sha256"],
        source_bytes=len(snapshot["raw"]),
        raw=None,
        normalized=None,
        payload_object_key=pointer.object_key,
        payload_object_sha256=pointer.object_sha256,
        payload_object_bytes=pointer.object_bytes,
        payload_format_version=pointer.format_version,
        payload_verified_at=pointer.verified_at,
    )


def test_upload_readback_then_external_read(snapshot, monkeypatch):
    client = MemoryStorage()
    monkeypatch.setattr(archive, "storage", lambda _: client)
    config = archive.ArchiveConfig(mode="s3")
    pointer = archive.archive_snapshot(snapshot, config)
    row = external_row(snapshot, pointer)
    assert archive.load_snapshot_payload(row, config)["payload"] == snapshot["payload"]
    row["payload_object_key"] = "foreign/key"
    with pytest.raises(archive.ArchiveError, match="pointer_invalid"):
        archive.load_snapshot_payload(row, config)
    row["payload_object_key"] = pointer.object_key
    client.data = client.data[:-1]
    with pytest.raises(archive.ArchiveError, match="object_integrity"):
        archive.load_snapshot_payload(row, config)


def test_upload_failure_prevents_database_insert(snapshot, monkeypatch):
    from app import sync_references

    calls = []

    def fail(_):
        raise archive.ArchiveError("snapshot_archive_upload_failed")

    monkeypatch.setattr(sync_references, "archive_snapshot", fail)

    class Database:
        def execute(self, *args):
            calls.append(args)

    with pytest.raises(archive.ArchiveError):
        sync_references.append_snapshot(Database(), uuid4(), snapshot)
    assert not calls


def test_disabled_keeps_legacy_insert(snapshot, monkeypatch):
    from app import sync_references

    monkeypatch.setattr(sync_references, "archive_snapshot", lambda _: None)
    calls = []

    class Database:
        def execute(self, *args):
            calls.append(args)

    sync_references.append_snapshot(Database(), uuid4(), snapshot)
    assert "payload_object_key" not in calls[0][0]
    assert calls[0][1][-2] == snapshot["raw"]


def test_inline_reader_checks_raw(snapshot):
    row = dict(
        raw=snapshot["raw"],
        normalized=snapshot["payload"],
        sha256=snapshot["sha256"],
        source_bytes=len(snapshot["raw"]),
    )
    assert archive.load_snapshot_payload(row)["payload"] == snapshot["payload"]
    row["raw"] = b"bad"
    with pytest.raises(archive.ArchiveError, match="raw_integrity"):
        archive.load_snapshot_payload(row)


def test_secrets_hidden_and_bad_endpoint_rejected(monkeypatch):
    monkeypatch.setenv("CHAIKA_SNAPSHOT_ARCHIVE_MODE", "s3")
    monkeypatch.setenv("CHAIKA_SNAPSHOT_ARCHIVE_SECRET_KEY", "hidden-secret")
    monkeypatch.setenv("CHAIKA_SNAPSHOT_ARCHIVE_ACCESS_KEY", "hidden-access")
    monkeypatch.setenv("CHAIKA_SNAPSHOT_ARCHIVE_ENDPOINT", "http://example.invalid")
    with pytest.raises(archive.ArchiveError, match="configuration_invalid") as caught:
        archive.ArchiveConfig.from_env()
    assert "hidden" not in str(caught.value)
    assert "hidden" not in repr(archive.ArchiveConfig(secret_key="hidden"))


def test_inventory_resume_restores_external_payload(snapshot, monkeypatch):
    from datetime import date

    from app import sync_inventory
    from app.sync_references import Source

    snapshot["payload"].update(source_fingerprint="fingerprint", sync_business_date="2026-10-17")
    client = MemoryStorage()
    config = archive.ArchiveConfig(mode="s3")
    monkeypatch.setattr(archive, "storage", lambda _: client)
    pointer = archive.archive_snapshot(snapshot, config)
    row = external_row(snapshot, pointer)
    row["observed_at"] = snapshot["observed_at"]
    monkeypatch.setattr(
        sync_inventory,
        "load_snapshot_payload",
        lambda value: archive.load_snapshot_payload(value, config),
    )

    class Result:
        def fetchone(self):
            return ("inventory", "failed", {"snapshot_ids": {"products": str(snapshot["id"])}})

    class Database:
        def execute(self, query, params):
            if query.startswith("SELECT job"):
                return Result()
            columns = query.removeprefix("SELECT ").split(" FROM ", 1)[0].split(",")
            return [tuple(row[column] for column in columns)]

    source = Source("primary", "Chain", "https://example.invalid", "fingerprint")
    result = sync_inventory.restore_snapshots(Database(), uuid4(), source, date(2026, 10, 17))
    assert result["products"]["payload"] == snapshot["payload"]
    with pytest.raises(sync_inventory.SyncError, match="scope_mismatch"):
        sync_inventory.restore_snapshots(Database(), uuid4(), source, date(2026, 10, 16))


def test_readback_mismatch_refuses_descriptor(snapshot, monkeypatch):
    class BadStorage(MemoryStorage):
        def get(self, key):
            return self.data[:-1]

    monkeypatch.setattr(archive, "storage", lambda _: BadStorage())
    with pytest.raises(archive.ArchiveError, match="object_integrity"):
        archive.archive_snapshot(snapshot, archive.ArchiveConfig(mode="s3"))
