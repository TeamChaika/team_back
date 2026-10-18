"""Explicit operator-only startup proof; never changes PostgreSQL or S3."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

PREFIX = "CHAIKA_ARCHIVE_VERIFY "
MODULES = ("snapshot_archive.py", "sync_inventory.py", "sync_references.py", "web/indicators.py")


def verify_archive_startup() -> None:
    requested = os.environ.get("CHAIKA_ARCHIVE_VERIFY_SNAPSHOT_IDS", "")
    if not requested:
        return
    started_at = datetime.now(UTC).isoformat()
    try:
        if len(requested) > 80:
            raise ValueError("invalid_ids")
        ids = [UUID(value.strip()) for value in requested.split(",")]
        if len(ids) != 2 or len(set(ids)) != 2:
            raise ValueError("invalid_ids")

        import psycopg
        from psycopg.rows import dict_row

        from app.core.config import Settings
        from app.snapshot_archive import ArchiveConfig, load_snapshot_payload

        config = ArchiveConfig.from_env()
        if config.mode != "s3" or config.scope != "chaika":
            raise ValueError("invalid_archive_mode")
        dsn = Settings().database_url.get_secret_value()
        if not dsn:
            raise ValueError("missing_database")
        snapshots = []
        with psycopg.connect(
            dsn,
            autocommit=True,
            connect_timeout=10,
            row_factory=dict_row,
            options=(
                "-c default_transaction_read_only=on "
                "-c statement_timeout=10000 -c lock_timeout=2000"
            ),
        ) as db:
            for key in ids:
                row = db.execute(
                    "SELECT id,source_id,resource,sha256,source_bytes,raw,normalized,"
                    "payload_object_key,payload_object_sha256,payload_object_bytes,"
                    "payload_format_version,payload_verified_at "
                    "FROM chaika.raw_snapshots WHERE id=%s",
                    (key,),
                    binary=True,
                ).fetchone()
                if row is None:
                    raise ValueError("missing_snapshot")
                inline = row["raw"] is not None and row["normalized"] is not None
                external = row["raw"] is None and row["normalized"] is None
                if not (inline or external):
                    raise ValueError("incomplete_snapshot")
                restored = load_snapshot_payload(row, config)
                if (
                    len(restored["raw"]) != row["source_bytes"]
                    or hashlib.sha256(restored["raw"]).hexdigest() != row["sha256"]
                ):
                    raise ValueError("snapshot_integrity")
                snapshots.append(
                    {
                        "id": str(key),
                        "storage": "inline" if inline else "external",
                        "source_sha256": row["sha256"],
                        "source_bytes": row["source_bytes"],
                        "verified": True,
                    }
                )
                del restored, row
        if {row["storage"] for row in snapshots} != {"inline", "external"}:
            raise ValueError("mixed_storage_required")
        directory = Path(__file__).resolve().parent
        print(
            PREFIX
            + json.dumps(
                {
                    "status": "verified",
                    "verification_started_at": started_at,
                    "pid": os.getpid(),
                    "uid": os.getuid(),
                    "snapshots": snapshots,
                    "module_sha256": {
                        "app/" + name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                        for name in MODULES
                    },
                }
            ),
            flush=True,
        )
    except Exception:
        print(
            PREFIX
            + json.dumps({"status": "failed", "code": "archive_runtime_verification_failed"}),
            flush=True,
        )
        raise SystemExit(1) from None
