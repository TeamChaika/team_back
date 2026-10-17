"""Explicit, resumable RAW offload. Never deletes snapshot rows or S3 objects."""

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.snapshot_archive import (
    ArchiveConfig,
    ArchiveError,
    archive_snapshot,
    load_snapshot_payload,
    validate_scope,
)


def disk_guard(path, minimum):
    """Operator's independent df observer must keep a fresh private guard file."""
    value = json.loads(Path(path).read_text())
    age = time.time() - value["checked_at"]
    if not 0 <= age <= 30 or value["available_bytes"] < minimum:
        raise RuntimeError("archive_disk_guard_stale_or_low")


def offload_one(db, schema, row, config, *, apply=False, guard=None):
    snapshot = dict(row, payload=row["normalized"])
    if len(row["raw"]) != row["source_bytes"]:
        raise ArchiveError("snapshot_archive_raw_integrity")
    pointer = archive_snapshot(snapshot, config)
    external = dict(
        row,
        raw=None,
        normalized=None,
        payload_object_key=pointer.object_key,
        payload_object_sha256=pointer.object_sha256,
        payload_object_bytes=pointer.object_bytes,
        payload_format_version=pointer.format_version,
        payload_verified_at=pointer.verified_at,
    )
    # Exercise the actual restore path as well as upload verification before removal.
    restored = load_snapshot_payload(external, config)
    if restored["raw"] != bytes(row["raw"]) or restored["payload"] != row["normalized"]:
        raise ArchiveError("snapshot_archive_roundtrip_mismatch")
    changed = False
    if apply:
        if guard is None:
            raise RuntimeError("archive_disk_guard_required")
        guard()
        with db.transaction():
            db.execute("SET LOCAL lock_timeout='2s'")
            db.execute("SET LOCAL statement_timeout='15s'")
            # Equality prevents a lossy JSON decoder or concurrent edit being archived.
            result = db.execute(
                sql.SQL("""
                UPDATE {}.raw_snapshots SET raw=NULL,normalized=NULL,
                    payload_object_key=%s,payload_object_sha256=%s,
                    payload_object_bytes=%s,payload_format_version=%s,payload_verified_at=%s
                WHERE id=%s AND sha256=%s AND source_bytes=%s AND raw IS NOT NULL
                    AND normalized=%s AND payload_object_key IS NULL
                RETURNING id
            """).format(sql.Identifier(schema)),
                (
                    pointer.object_key,
                    pointer.object_sha256,
                    pointer.object_bytes,
                    pointer.format_version,
                    pointer.verified_at,
                    row["id"],
                    row["sha256"],
                    row["source_bytes"],
                    Jsonb(restored["payload"]),
                ),
            ).fetchone()
            if result is None:
                raise RuntimeError("archive_snapshot_changed_or_json_mismatch")
            changed = True
    return {
        "schema": schema,
        "id": str(row["id"]),
        "source_sha256": row["sha256"],
        "source_bytes": row["source_bytes"],
        "offloaded": changed,
        **{k: str(v) if k == "verified_at" else v for k, v in asdict(pointer).items()},
    }


def private_json(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise RuntimeError("archive_operator_configuration_not_private")
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--dsn-file", required=True, help="Private JSON psycopg parameters")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--guard-file")
    parser.add_argument("--minimum-free-bytes", type=int, default=768 * 1024 * 1024)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--workers", type=int, default=1, choices=range(1, 5))
    args = parser.parse_args()
    validate_scope(args.schema)
    if args.limit < 1 or args.minimum_free_bytes < 512 * 1024 * 1024:
        parser.error("Positive limit and at least 512 MiB free-space floor required")
    if args.apply and not args.guard_file:
        parser.error("--apply requires a fresh independent --guard-file")
    config = replace(ArchiveConfig.from_env(), scope=args.schema)
    if config.mode != "s3":
        parser.error("Privileged operator requires explicit direct S3 mode")
    parameters = private_json(args.dsn_file)
    parameters.update(application_name="restcontrol-snapshot-offload", connect_timeout=10)

    def guard():
        return disk_guard(args.guard_file, args.minimum_free_bytes)

    if args.apply:
        guard()
    with psycopg.connect(**parameters, autocommit=True, row_factory=dict_row) as db:
        ids = [
            row["id"]
            for row in db.execute(
                sql.SQL(
                    "SELECT id FROM {}.raw_snapshots WHERE raw IS NOT NULL "
                    "ORDER BY ctid DESC LIMIT %s"
                ).format(sql.Identifier(args.schema)),
                (args.limit,),
            )
        ]

    def process(key):
        if args.apply:
            guard()
        with psycopg.connect(**parameters, autocommit=True, row_factory=dict_row) as db:
            row = db.execute(
                sql.SQL(
                    "SELECT id,source_id,resource,sha256,source_bytes,raw,normalized "
                    "FROM {}.raw_snapshots WHERE id=%s AND raw IS NOT NULL"
                ).format(sql.Identifier(args.schema)),
                (key,),
            ).fetchone()
            if row is None:
                return {"schema": args.schema, "id": str(key), "skipped": True}
            return offload_one(db, args.schema, row, config, apply=args.apply, guard=guard)

    manifest = Path(args.manifest)
    if manifest.is_symlink():
        raise RuntimeError("archive_manifest_symlink")
    descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    count = total_bytes = 0
    with os.fdopen(descriptor, "a") as output, ThreadPoolExecutor(args.workers) as pool:
        # Bound pending jobs and memory; map over at most one worker batch.
        for offset in range(0, len(ids), args.workers):
            for result in pool.map(process, ids[offset : offset + args.workers]):
                output.write(json.dumps(result) + "\n")
                output.flush()
                os.fsync(output.fileno())
                count += 1
                total_bytes += result.get("object_bytes", 0)
            if count % 25 < args.workers or count == len(ids):
                print(
                    json.dumps(
                        {"verified": count, "object_bytes": total_bytes, "apply": args.apply}
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    try:
        main()
    except psycopg.Error:
        raise SystemExit("archive_database_operation_failed") from None
    except (ArchiveError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
