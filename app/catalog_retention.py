"""Remove only a replaced catalog file whose immutable S3 copy is verified."""

import hashlib
import json
import logging
import os
import stat
from pathlib import Path
from uuid import UUID

from psycopg.rows import dict_row

from app import snapshot_archive as archive

log = logging.getLogger(__name__)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def prune_previous_catalog(db, source, resource, directory: Path, previous: dict | None) -> bool:
    """Call under the source lock, after commit, with an autocommit connection."""
    if previous is None or resource not in {"products", "product_groups"}:
        return False
    try:
        directory = Path(directory)
        folder = "products" if resource == "products" else "groups"
        if directory.name != folder or any(p.is_symlink() for p in (directory, *directory.parents)):
            return False
        if previous["source_fingerprint"] != source.fingerprint:
            return False
        snapshot = previous["snapshot"]
        key = str(UUID(snapshot["snapshot_id"]))
        path, pointer = directory / f"{key}.json", directory / "current.json"

        def replaced():
            if pointer.is_symlink() or not pointer.is_file():
                return False
            current = json.loads(pointer.read_text())
            current_id = str(UUID(current["snapshot"]["snapshot_id"]))
            current_file = directory / f"{current_id}.json"
            return (
                current["source_fingerprint"] == source.fingerprint
                and current_id != key
                and not current_file.is_symlink()
                and current_file.is_file()
            )

        config = archive.ArchiveConfig.from_env()
        if config.mode == "disabled" or not replaced():
            return False
        with db.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute(
                "SELECT id,source_id,resource,sha256,source_bytes,raw IS NULL AS raw_external,"
                "normalized IS NULL AS normalized_external,payload_object_key,"
                "payload_object_sha256,payload_object_bytes,payload_format_version,"
                "payload_verified_at FROM chaika.raw_snapshots "
                "WHERE id=%s AND source_id=%s AND resource=%s AND sha256=%s AND source_bytes=%s",
                (key, source.id, resource, snapshot["sha256"], snapshot["source_bytes"]),
            ).fetchone()
        if (
            row is None
            or not row["raw_external"]
            or not row["normalized_external"]
            or not row["payload_verified_at"]
            or row["payload_format_version"] != 1
            or not 0 < row["payload_object_bytes"] <= archive.MAX_OBJECT_BYTES
        ):
            return False
        object_key = archive.object_key(config.scope, key, row["payload_object_sha256"])
        if row["payload_object_key"] != object_key:
            return False
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as raw:
            before = os.fstat(raw.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                return False
            if (
                before.st_size != snapshot["source_bytes"]
                or hashlib.file_digest(raw, "sha256").hexdigest() != snapshot["sha256"]
                or _identity(os.fstat(raw.fileno())) != _identity(before)
            ):
                return False
        # The verified row already proves envelope identity; avoid expanding its large JSON again.
        compressed = archive.storage(config).get(object_key)
        if (
            len(compressed) != row["payload_object_bytes"]
            or hashlib.sha256(compressed).hexdigest() != row["payload_object_sha256"]
            or not replaced()
            or _identity(path.lstat()) != _identity(before)
        ):
            return False
        path.unlink()
        return True
    except Exception as error:
        log.warning("Catalog retention skipped (%s)", type(error).__name__)
        return False
