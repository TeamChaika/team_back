"""Consistent private PostgreSQL recovery snapshots, without Auth or live sessions.

Restore requires an operator connection and an empty, already migrated schema.
The runtime connection cannot rewrite append-only audit or platform grants.
"""

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.types.json import Jsonb

from .config import validate_private_key

TABLES = (
    "companies", "platform_memberships", "memberships", "connections", "events",
    "tenant_events", "auth_provisioning", "attempts", "check_attempts",
    "check_targets", "imports", "sessions", "tenant_sessions",
    "platform_tenant_events", "platform_sso_codes", "platform_tenant_sessions",
    "company_account_requests", "runtime_provisioning", "company_module_settings",
)
SESSION_TABLES = {"sessions", "tenant_sessions", "platform_sso_codes", "platform_tenant_sessions"}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _write(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _schema(db):
    rows = db.execute(
        "SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid=c.oid "
        "WHERE n.nspname='restcontrol' AND c.relkind='r' AND a.attnum>0 "
        "AND NOT a.attisdropped ORDER BY c.relname,a.attnum"
    ).fetchall()
    if {row[0] for row in rows} != set(TABLES):
        raise ValueError("Unsupported restcontrol schema; update backup tooling first")
    return [list(row) for row in rows]


def _validate_ciphertexts(data, key):
    cipher = Fernet(key)
    for row in data["connections"]:
        value = json.loads(cipher.decrypt(row["ciphertext"].encode()))
        if not isinstance(value.get("login"), str) or not isinstance(value.get("password"), str):
            raise ValueError("Invalid encrypted connection")
    for row in data["auth_provisioning"]:
        if row.get("temporary_ciphertext"):
            cipher.decrypt(row["temporary_ciphertext"].encode())
    for row in data["company_module_settings"]:
        from .company_module_settings import ModuleSettingsWrite, Seller

        value = json.loads(cipher.decrypt(row["ciphertext"].encode()))
        if not isinstance(value, dict) or set(value) != {"seller", "telegram", "assistant"}:
            raise ValueError("Invalid encrypted module settings")
        fields = {
            "telegram": {"username", "token"},
            "assistant": {"provider", "model", "agent_id", "key"},
        }
        if value["seller"] is not None:
            fields["seller"] = set(Seller.model_fields)
        if any(
            not isinstance(value[name], dict) or set(value[name]) != required
            for name, required in fields.items()
        ):
            raise ValueError("Invalid encrypted module settings")
        try:
            ModuleSettingsWrite.model_validate({"expected_version": 1, **value})
        except ValueError:
            # Validation diagnostics may contain credential input. Keep backup
            # errors generic, including when the encrypted payload is malformed.
            raise ValueError("Invalid encrypted module settings") from None


def snapshot_postgres(dsn, source, destination):
    """Read one repeatable-read snapshot; never change the running registry."""
    validate_private_key(source)
    key_path = Path(source) / "credentials.key"
    key = key_path.read_bytes()
    target = Path(destination)
    target.mkdir(mode=0o700)  # Existing targets, including symlinks, are rejected.
    try:
        data = {}
        with psycopg.connect(dsn, connect_timeout=10) as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            db.execute("SET LOCAL statement_timeout='60000ms'")
            schema = _schema(db)
            for name in TABLES:
                data[name] = [] if name in SESSION_TABLES else [
                    row[0] for row in db.execute(
                        sql.SQL("SELECT to_jsonb(t) FROM restcontrol.{} t "
                                "ORDER BY to_jsonb(t)::text")
                        .format(sql.Identifier(name))
                    )
                ]
        _validate_ciphertexts(data, key)
        if key_path.read_bytes() != key:
            raise ValueError("Credential key changed during backup; retry")
        payload = _encoded(data)
        metadata = {
            "format": "restcontrol-postgres-v1", "created_at": datetime.now(UTC).isoformat(),
            "schema": schema, "schema_sha256": _hash(_encoded(schema)),
            "data_sha256": _hash(payload), "key_sha256": _hash(key),
            "counts": {name: len(rows) for name, rows in data.items()},
            "sessions_included": False, "auth_passwords_included": False,
        }
        _write(target / "registry.json", payload)
        _write(target / "credentials.key", key)
        _write(target / "manifest.json", _encoded(metadata))
        read_snapshot(target)
    except BaseException:
        shutil.rmtree(target)
        raise
    return metadata


def read_snapshot(source):
    validate_private_key(source)
    root = Path(source)
    for name in ("manifest.json", "registry.json"):
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ValueError("Snapshot files must be private regular files")
    manifest = json.loads((root / "manifest.json").read_bytes())
    payload = (root / "registry.json").read_bytes()
    key = (root / "credentials.key").read_bytes()
    if manifest.get("format") != "restcontrol-postgres-v1" or (
        manifest.get("data_sha256") != _hash(payload)
        or manifest.get("key_sha256") != _hash(key)
        or manifest.get("schema_sha256") != _hash(_encoded(manifest.get("schema")))
    ):
        raise ValueError("Snapshot format or checksum mismatch")
    data = json.loads(payload)
    if set(data) != set(TABLES) or any(not isinstance(rows, list) for rows in data.values()):
        raise ValueError("Invalid snapshot tables")
    if any(data[name] for name in SESSION_TABLES):
        raise ValueError("Restored sessions must be empty")
    if manifest.get("counts") != {name: len(rows) for name, rows in data.items()}:
        raise ValueError("Snapshot counts mismatch")
    _validate_ciphertexts(data, key)
    return data, manifest, key


def restore_postgres(source, operator_dsn, destination):
    """Restore all-or-nothing; never overwrite key files or populated tables.

    Existing Supabase Auth users must resolve the preserved membership IDs; no
    Auth rows, hashes or passwords are included or modified by this procedure.
    """
    data, manifest, key = read_snapshot(source)
    target = Path(destination)
    target.mkdir(mode=0o700)
    try:
        with psycopg.connect(operator_dsn, connect_timeout=10) as db:
            db.execute("SET LOCAL statement_timeout='60000ms'")
            db.execute("SET LOCAL lock_timeout='5000ms'")
            role = db.execute(
                "SELECT current_user,rolsuper FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            if role[0] == "restcontrol_backend":
                raise ValueError("Restore requires an operator connection, never the runtime role")
            owns_tables = db.execute(
                "SELECT bool_and(pg_has_role(current_user,c.relowner,'USAGE')) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='restcontrol' AND c.relkind='r'"
            ).fetchone()[0]
            if not role[1] and not owns_tables:
                raise ValueError("Restore operator must own every restcontrol table")
            db.execute("SET LOCAL row_security=off")
            # Lock every destination before checking emptiness to exclude writes.
            for name in TABLES:
                db.execute(sql.SQL("LOCK TABLE restcontrol.{} IN ACCESS EXCLUSIVE MODE")
                           .format(sql.Identifier(name)))
            if _schema(db) != manifest["schema"]:
                raise ValueError("Destination schema does not match the snapshot")
            for name in TABLES:
                if db.execute(sql.SQL("SELECT 1 FROM restcontrol.{} LIMIT 1")
                              .format(sql.Identifier(name))).fetchone():
                    raise ValueError("Restore destination must be empty")
            for name in TABLES:
                if data[name]:
                    db.execute(
                        sql.SQL("INSERT INTO restcontrol.{} SELECT * FROM "
                                "jsonb_populate_recordset(NULL::restcontrol.{},%s)")
                        .format(sql.Identifier(name), sql.Identifier(name)), (Jsonb(data[name]),),
                    )
            # A database snapshot cannot attest that a saved Unix socket, process
            # or external configuration still belongs to this restored runtime.
            db.execute(
                "UPDATE restcontrol.runtime_provisioning SET state='failed', "
                "step='migrations',checks='{}',active_socket_path=NULL,active_version=NULL, "
                "error_code='restored_runtime_requires_validation',updated_at=now()"
            )
            # Write before commit: a filesystem failure rolls back every row.
            _write(target / "credentials.key", key)
            _write(target / "restore-manifest.json", _encoded(manifest))
    except BaseException:
        shutil.rmtree(target)
        raise
    return manifest
