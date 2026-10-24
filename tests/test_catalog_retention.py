"""Previous-only catalog retention, without live PostgreSQL or S3."""

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import catalog_retention as retention
from app import snapshot_archive as archive


class Database:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def cursor(self, **_):
        return nullcontext(self)

    def execute(self, sql, params):
        self.calls.append((sql, params))
        assert sql.startswith("SELECT ")
        return self

    def fetchone(self):
        return self.row


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    folder = tmp_path / "products"
    folder.mkdir()
    old, new = uuid4(), uuid4()
    raw = b'{"old": "catalog"}'
    old_file, new_file = folder / f"{old}.json", folder / f"{new}.json"
    old_file.write_bytes(raw)
    new_file.write_bytes(b"new current catalog")
    source = SimpleNamespace(id="primary", fingerprint="same-source")
    previous = dict(
        source_fingerprint=source.fingerprint,
        snapshot=dict(
            snapshot_id=str(old), sha256=hashlib.sha256(raw).hexdigest(), source_bytes=len(raw)
        ),
    )
    current = dict(source_fingerprint=source.fingerprint, snapshot=dict(snapshot_id=str(new)))
    pointer = folder / "current.json"
    pointer.write_text(json.dumps(current))
    config = archive.ArchiveConfig(mode="s3")
    compressed = archive.encode_snapshot(
        dict(
            id=old,
            source_id=source.id,
            resource="products",
            sha256=previous["snapshot"]["sha256"],
            raw=raw,
            observed_at=datetime.now(UTC),
            payload={"items": []},
        ),
        config.scope,
    )
    digest = hashlib.sha256(compressed).hexdigest()
    row = dict(
        id=old,
        source_id=source.id,
        resource="products",
        sha256=previous["snapshot"]["sha256"],
        source_bytes=len(raw),
        raw_external=True,
        normalized_external=True,
        payload_object_key=archive.object_key(config.scope, old, digest),
        payload_object_sha256=digest,
        payload_object_bytes=len(compressed),
        payload_format_version=1,
        payload_verified_at=datetime.now(UTC),
    )
    gets = []

    def get(key):
        gets.append(key)
        return compressed

    storage = SimpleNamespace(get=get)
    monkeypatch.setattr(archive.ArchiveConfig, "from_env", lambda: config)
    monkeypatch.setattr(archive, "storage", lambda _: storage)
    monkeypatch.setattr(
        archive, "decode_snapshot", lambda *_: pytest.fail("must not decode envelope")
    )
    return SimpleNamespace(
        directory=folder,
        old_file=old_file,
        new_file=new_file,
        pointer=pointer,
        previous=previous,
        current=current,
        source=source,
        db=Database(row),
        storage=storage,
        compressed=compressed,
        gets=gets,
        monkeypatch=monkeypatch,
    )


def prune(catalog, resource="products"):
    return retention.prune_previous_catalog(
        catalog.db, catalog.source, resource, catalog.directory, catalog.previous
    )


@pytest.mark.parametrize(
    "resource,folder", [("products", "products"), ("product_groups", "groups")]
)
def test_removes_only_replaced_verified_file(catalog, resource, folder):
    original = catalog.directory
    renamed = original.with_name(folder)
    if renamed != original:
        original.rename(renamed)
        catalog.directory = renamed
        catalog.old_file = renamed / catalog.old_file.name
        catalog.new_file = renamed / catalog.new_file.name
        catalog.pointer = renamed / "current.json"
    history = catalog.directory / f"{uuid4()}.json"
    history.write_bytes(b"older history stays")
    assert prune(catalog, resource)
    assert not catalog.old_file.exists()
    assert catalog.new_file.is_file() and catalog.pointer.is_file() and history.is_file()
    assert len(catalog.gets) == 1
    assert catalog.db.calls[0][1][1:3] == ("primary", resource)


@pytest.mark.parametrize(
    "field,value",
    [
        ("raw_external", False),
        ("normalized_external", False),
        ("payload_verified_at", None),
        ("payload_format_version", 2),
        ("payload_object_bytes", 0),
        ("payload_object_bytes", archive.MAX_OBJECT_BYTES + 1),
        ("payload_object_key", "another-company/object"),
    ],
)
def test_unverified_or_inline_database_pointer_keeps_file(catalog, field, value):
    catalog.db.row[field] = value
    assert not prune(catalog)
    assert catalog.old_file.is_file() and not catalog.gets


@pytest.mark.parametrize(
    "case", ["missing-row", "no-previous", "wrong-source", "still-current", "disabled"]
)
def test_ineligible_catalog_keeps_file(catalog, case):
    if case == "missing-row":
        catalog.db.row = None
    elif case == "no-previous":
        catalog.previous = None
    elif case == "wrong-source":
        catalog.previous["source_fingerprint"] = "different-source"
    elif case == "still-current":
        catalog.pointer.write_text(json.dumps(catalog.previous))
    else:
        catalog.monkeypatch.setattr(
            archive.ArchiveConfig, "from_env", lambda: archive.ArchiveConfig()
        )
    assert not prune(catalog)
    assert catalog.old_file.is_file() and not catalog.gets


@pytest.mark.parametrize(
    "case", ["corrupt-local", "symlink-local", "symlink-current", "hardlink-local"]
)
def test_unsafe_local_file_keeps_every_file(catalog, case):
    if case == "corrupt-local":
        catalog.old_file.write_bytes(b"changed bytes")
    elif case == "symlink-local":
        catalog.old_file.unlink()
        catalog.old_file.symlink_to(catalog.new_file)
    elif case == "symlink-current":
        target = catalog.directory / "other-pointer.json"
        target.write_text(catalog.pointer.read_text())
        catalog.pointer.unlink()
        catalog.pointer.symlink_to(target)
    else:
        (catalog.directory / "hardlink.json").hardlink_to(catalog.old_file)
    assert not prune(catalog)
    assert catalog.old_file.exists() and catalog.new_file.is_file() and not catalog.gets


@pytest.mark.parametrize(
    "case",
    ["corrupt-object", "wrong-object-size", "current-changed", "file-replaced", "file-changed"],
)
def test_rechecks_after_remote_get(catalog, case):
    def get(_):
        if case == "corrupt-object":
            return b"x" * len(catalog.compressed)
        if case == "wrong-object-size":
            return catalog.compressed + b"x"
        if case == "current-changed":
            catalog.pointer.write_text(json.dumps(catalog.previous))
        elif case == "file-replaced":
            replacement = catalog.directory / "replacement"
            replacement.write_bytes(catalog.old_file.read_bytes())
            replacement.replace(catalog.old_file)
        elif case == "file-changed":
            catalog.old_file.write_bytes(b"x" * catalog.old_file.stat().st_size)
        return catalog.compressed

    catalog.storage.get = get
    assert not prune(catalog)
    assert catalog.old_file.is_file() and catalog.new_file.is_file()


def test_storage_failure_is_nonfatal_and_never_logs_secret(catalog, caplog):
    def fail(_):
        raise RuntimeError("credential-must-not-appear")

    catalog.storage.get = fail
    assert not prune(catalog)
    assert catalog.old_file.is_file()
    assert "RuntimeError" in caplog.text and "credential-must-not-appear" not in caplog.text


def test_tenant_scope_cannot_use_primary_object(catalog):
    catalog.monkeypatch.setattr(
        archive.ArchiveConfig,
        "from_env",
        lambda: archive.ArchiveConfig(mode="broker", scope="c_" + "a" * 32 + "_analytics"),
    )
    assert not prune(catalog)
    assert catalog.old_file.is_file() and not catalog.gets
