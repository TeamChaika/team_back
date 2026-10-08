"""One-time consistent SQLite snapshot import; never imports passwords or sessions.

Run with an operator connection after applying the additive migration. Default is
read-only inspection. Runtime never imports this module or falls back to SQLite.
"""

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from uuid import UUID

import psycopg
from cryptography.fernet import Fernet
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

TABLES = ("owners", "companies", "connections", "events", "tenant_admins", "tenant_events")


def read_snapshot(directory):
    directory = Path(directory)
    path, key = directory / "registry.sqlite3", directory / "credentials.key"
    if path.is_symlink() or key.is_symlink() or not path.is_file() or not key.is_file():
        raise ValueError("Snapshot must contain regular registry.sqlite3 and credentials.key")
    if key.stat().st_mode & 0o077:
        raise ValueError("Snapshot key must have private permissions")
    fernet = Fernet(key.read_bytes())
    # A read transaction gives one consistent view even if WAL is present.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != 3:
            raise ValueError("Expected legacy registry schema version 3")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Invalid snapshot integrity")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Invalid snapshot foreign keys")
        data = {
            table: [
                {k: r[k] for k in r.keys() if k not in {"salt", "password_hash"}}
                for r in db.execute("SELECT * FROM " + table)
            ]
            for table in TABLES
        }
    if len(data["owners"]) != 1:
        raise ValueError("Expected exactly one legacy owner")
    for row in data["connections"]:
        secret = json.loads(fernet.decrypt(row["ciphertext"].encode()).decode())
        if not isinstance(secret.get("login"), str) or not isinstance(secret.get("password"), str):
            raise ValueError("Malformed encrypted connection")
    canonical = {
        table: sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))
        for table, rows in data.items()
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    manifest = {
        "source_digest": hashlib.sha256(encoded).hexdigest(),
        "counts": {k: len(v) for k, v in data.items()},
        "source_schema": 3,
        "sessions_imported": 0,
        "password_hashes_imported": 0,
    }
    return data, manifest


def import_snapshot(directory, dsn, owner_auth_id, owner_email="1@chaika.team"):
    data, manifest = read_snapshot(directory)
    owner_auth_id = UUID(str(owner_auth_id))
    with psycopg.connect(dsn, row_factory=dict_row) as db, db.transaction():
        db.execute("SET LOCAL search_path TO restcontrol,pg_catalog")
        db.execute("SET LOCAL lock_timeout='10000ms'")
        db.execute("SELECT pg_advisory_xact_lock(824721096)")
        previous = db.execute(
            "SELECT manifest FROM imports WHERE source_digest=%s", (manifest["source_digest"],)
        ).fetchone()
        if previous:
            return {**previous["manifest"], "status": "already_imported"}
        if (
            db.execute("SELECT 1 FROM companies LIMIT 1").fetchone()
            or db.execute("SELECT 1 FROM platform_memberships LIMIT 1").fetchone()
        ):
            raise ValueError("Destination registry is not empty; refusing to merge snapshots")
        identity = db.execute(
            "SELECT id,email FROM restcontrol.auth_identities WHERE id=%s", (owner_auth_id,)
        ).fetchone()
        if not identity or identity["email"].casefold() != owner_email.casefold():
            raise ValueError("Owner Auth UUID/email mismatch")
        owner = data["owners"][0]
        db.execute(
            "INSERT INTO platform_memberships(id,auth_user_id,username,display_name) "
            "VALUES(%s,%s,%s,%s)",
            (owner["id"], owner_auth_id, owner_email.casefold(), owner["display_name"]),
        )
        for r in data["companies"]:
            db.execute(
                "INSERT INTO companies(id,slug,domain,name,status,version,archived_at,"
                "body,name_search) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    *[
                        r[k]
                        for k in (
                            "id",
                            "slug",
                            "domain",
                            "name",
                            "status",
                            "version",
                            "archived_at",
                        )
                    ],
                    Jsonb(json.loads(r["body"])),
                    r["name"].casefold(),
                ),
            )
        for r in data["tenant_admins"]:
            identities = db.execute(
                "SELECT id FROM auth_identities WHERE lower(email)=%s", (r["username"].casefold(),)
            ).fetchall()
            if len(identities) > 1:
                raise ValueError("Ambiguous tenant Auth identity")
            auth_id = identities[0]["id"] if identities else None
            db.execute(
                "INSERT INTO memberships(id,company_id,auth_user_id,username,display_name,"
                "must_change,temporary_expires,auth_exclusive) VALUES(%s,%s,%s,%s,%s,%s,%s,false)",
                (
                    r["id"],
                    r["company_id"],
                    auth_id,
                    r["username"],
                    r["display_name"],
                    not bool(auth_id),
                    0 if not auth_id else None,
                ),
            )
        for r in data["connections"]:
            db.execute(
                "INSERT INTO connections(company_id,connection_id,url,ciphertext,check_json) "
                "VALUES(%s,%s,%s,%s,%s)",
                (
                    r["company_id"],
                    r["connection_id"],
                    r["url"],
                    r["ciphertext"],
                    Jsonb(json.loads(r["check_json"])),
                ),
            )
        for r in data["events"]:
            db.execute(
                "INSERT INTO events(id,company_id,actor_id,actor_name,action,changed_fields,"
                "created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    *[r[k] for k in ("id", "company_id", "actor_id", "actor_name", "action")],
                    Jsonb(json.loads(r["changed_fields"])),
                    r["created_at"],
                ),
            )
        for r in data["tenant_events"]:
            db.execute(
                "INSERT INTO tenant_events(id,company_id,admin_id,action,created_at) "
                "VALUES(%s,%s,%s,%s,%s)",
                tuple(r[k] for k in ("id", "company_id", "admin_id", "action", "created_at")),
            )
        # Verify stored values while the import transaction can still roll back.
        mapping = {"owners": "platform_memberships", "tenant_admins": "memberships"}
        for source, count in manifest["counts"].items():
            table = mapping.get(source, source)
            if db.execute("SELECT count(*) AS n FROM " + table).fetchone()["n"] != count:
                raise ValueError("Imported table count mismatch")
        expected = {
            (r["company_id"], r["connection_id"]): r["ciphertext"] for r in data["connections"]
        }
        actual = {
            (str(r["company_id"]), r["connection_id"]): r["ciphertext"]
            for r in db.execute("SELECT company_id,connection_id,ciphertext FROM connections")
        }
        if actual != expected:
            raise ValueError("Imported encrypted connection mismatch")
        db.execute(
            "INSERT INTO imports(source_digest,manifest) VALUES(%s,%s)",
            (manifest["source_digest"], Jsonb(manifest)),
        )
    return {**manifest, "status": "imported"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--apply", action="store_true", help="Apply to empty destination registry")
    parser.add_argument("--dsn-env", default="CHAIKA_SAAS_MIGRATION_DATABASE_URL")
    parser.add_argument("--owner-auth-id")
    parser.add_argument("--owner-email", default="1@chaika.team")
    args = parser.parse_args()
    if args.apply:
        if not args.owner_auth_id or not os.environ.get(args.dsn_env):
            parser.error("Apply requires owner Auth UUID and configured migration DSN environment")
        result = import_snapshot(
            args.snapshot, os.environ[args.dsn_env], args.owner_auth_id, args.owner_email
        )
    else:
        _, result = read_snapshot(args.snapshot)
        result["status"] = "inspection_only"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
