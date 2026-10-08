"""Private, validated registry snapshots; never print or return secret material."""

import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet


def private_registry(directory):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError("Registry must be an existing private directory (0700)")
    for name in ("registry.sqlite3", "credentials.key"):
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ValueError("Registry database and key must be private regular files (0600)")
    return root


def readonly_database(root):
    return sqlite3.connect((root / "registry.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)


def validate_registry(directory):
    root = private_registry(directory)
    try:
        cipher = Fernet((root / "credentials.key").read_bytes())
        with closing(readonly_database(root)) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != 3:
                raise ValueError("Registry must use schema 3")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("Registry integrity check failed")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Registry foreign key check failed")
            required = {
                "owners",
                "sessions",
                "attempts",
                "companies",
                "events",
                "connections",
                "check_attempts",
                "check_targets",
                "tenant_admins",
                "tenant_sessions",
                "tenant_events",
            }
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not required.issubset(tables):
                raise ValueError("Registry schema is incomplete")
            if not db.execute("SELECT 1 FROM owners").fetchone():
                raise ValueError("Registry owner must be initialized")
            for (value,) in db.execute("SELECT ciphertext FROM connections"):
                cipher.decrypt(value.encode())
    except (sqlite3.Error, ValueError) as exc:
        raise ValueError("Registry validation failed; restore a valid database and key") from exc
    except Exception as exc:
        raise ValueError("Registry credentials cannot be decrypted with this key") from exc
    return root


def snapshot_registry(source, destination, *, clear_sessions=False):
    """Create a new private directory. Source sessions and business data remain untouched."""
    source = validate_registry(source)
    target = Path(destination)
    key = (source / "credentials.key").read_bytes()
    target.mkdir(mode=0o700)  # Never replace an existing destination.
    try:
        database = target / "registry.sqlite3"
        fd = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with (
            closing(readonly_database(source)) as original,
            closing(sqlite3.connect(database)) as copy,
        ):
            original.backup(copy)
            if clear_sessions:
                copy.execute("DELETE FROM sessions")
                copy.execute("DELETE FROM tenant_sessions")
            copy.commit()
        if key != (source / "credentials.key").read_bytes():
            raise ValueError("Credential key changed during backup; retry")
        fd = os.open(target / "credentials.key", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        validate_registry(target)
        with database.open("rb") as stream:
            os.fsync(stream.fileno())
    except BaseException:
        shutil.rmtree(target)
        raise


def restore_registry(source, destination):
    snapshot_registry(source, destination, clear_sessions=True)
