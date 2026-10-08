"""Private SQLite repository; registry changes and audit share one transaction."""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .connections import ConnectionRepository
from .models import CompanyWrite
from .tenant_access import TenantAccessRepository
from .vault import Vault


class Problem(Exception):
    def __init__(self, status, code, message, field=None):
        self.status, self.code, self.message, self.field = status, code, message, field


def stamp():
    return datetime.now(UTC).isoformat()


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


class Repository(ConnectionRepository, TenantAccessRepository):
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise ValueError("Data directory cannot be a symlink")
        directory.chmod(0o700)
        self.path = directory / "registry.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Database cannot be a symlink")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise ValueError("Unsupported registry schema version")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS owners (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    display_name TEXT NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
                    csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    key TEXT PRIMARY KEY, count INTEGER, until REAL);
                CREATE TABLE IF NOT EXISTS companies (
                    id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, domain TEXT UNIQUE,
                    name TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
                    archived_at TEXT, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
                    actor_id TEXT NOT NULL REFERENCES owners(id), actor_name TEXT NOT NULL,
                    action TEXT NOT NULL, changed_fields TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS connections (
                    company_id TEXT NOT NULL REFERENCES companies(id), connection_id TEXT NOT NULL,
                    url TEXT NOT NULL,ciphertext TEXT NOT NULL,check_json TEXT NOT NULL,
                    PRIMARY KEY(company_id,connection_id));
                CREATE TABLE IF NOT EXISTS check_attempts (
                    owner_id TEXT PRIMARY KEY REFERENCES owners(id),count INTEGER,until REAL);
                CREATE TABLE IF NOT EXISTS check_targets (target TEXT PRIMARY KEY,until REAL);
                CREATE TABLE IF NOT EXISTS tenant_admins (
                    id TEXT PRIMARY KEY, company_id TEXT UNIQUE NOT NULL REFERENCES companies(id),
                    username TEXT NOT NULL, display_name TEXT NOT NULL,
                    salt TEXT NOT NULL,password_hash TEXT NOT NULL,
                    must_change INTEGER NOT NULL,temporary_expires REAL);
                CREATE TABLE IF NOT EXISTS tenant_sessions (
                    token_hash TEXT PRIMARY KEY,admin_id TEXT NOT NULL REFERENCES tenant_admins(id),
                    csrf TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tenant_events (
                    id TEXT PRIMARY KEY,company_id TEXT NOT NULL REFERENCES companies(id),
                    admin_id TEXT NOT NULL REFERENCES tenant_admins(id),action TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                PRAGMA user_version=3;
                COMMIT;
            """)

        # Never recreate a key when encrypted credentials already exist.
        if not (directory / "credentials.key").exists():
            with self.connect() as db:
                if db.execute("SELECT 1 FROM connections LIMIT 1").fetchone():
                    raise ValueError("Credential key missing; restore the original private key")
        self.vault = Vault(directory)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.create_function("casefold", 1, lambda value: value.casefold())
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA journal_mode=WAL")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def bootstrap(self, username, password, display_name):
        if (
            not username.strip()
            or len(username) > 100
            or len(password) < 14
            or len(password) > 1024
        ):
            raise ValueError("Username required; password length must be 14–1024")
        salt = secrets.token_hex(16)
        with self.connect(True) as db:
            if db.execute("SELECT 1 FROM owners").fetchone():
                raise ValueError("Owner already initialized")
            db.execute(
                "INSERT INTO owners VALUES (?,?,?,?,?)",
                (str(uuid4()), username, display_name, salt, password_hash(password, salt)),
            )

    def login(self, username, password, peer):
        now = time.time()
        # Global and per-peer bounds protect the sole owner, including username rotation.
        keys = ["global", "peer:" + peer]
        error = None
        result = None
        with self.connect(True) as db:
            db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            for key in keys:
                row = db.execute("SELECT * FROM attempts WHERE key=?", (key,)).fetchone()
                if row and row["until"] > now and row["count"] >= (50 if key == "global" else 10):
                    raise Problem(429, "rate_limited", "Слишком много попыток. Повторите позже")
            owner = db.execute("SELECT * FROM owners WHERE username=?", (username,)).fetchone()
            salt = owner["salt"] if owner else "00" * 16
            candidate = password_hash(password, salt)
            if not owner or not hmac.compare_digest(candidate, owner["password_hash"]):
                for key in keys:
                    db.execute(
                        """INSERT INTO attempts VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET
                        count=CASE WHEN until<=? THEN 1 ELSE count+1 END,
                        until=CASE WHEN until<=? THEN excluded.until ELSE until END""",
                        (key, now + 900, now, now),
                    )
                error = Problem(401, "invalid_credentials", "Неверный логин или пароль")
            else:
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                db.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?)",
                    (digest(token), owner["id"], csrf, now + 8 * 3600),
                )
                result = (
                    token,
                    {
                        "user": {k: owner[k] for k in ("id", "username", "display_name")},
                        "csrf_token": csrf,
                    },
                )
        if error:
            raise error
        return result

    def session(self, token):
        with self.connect() as db:
            row = db.execute(
                """SELECT owners.*, sessions.csrf FROM sessions JOIN owners
                ON owner_id=owners.id WHERE token_hash=? AND expires>?""",
                (digest(token), time.time()),
            ).fetchone()
            if not row:
                raise Problem(401, "unauthorized", "Требуется вход владельца")
            return {
                "user": {k: row[k] for k in ("id", "username", "display_name")},
                "csrf_token": row["csrf"],
            }

    def logout(self, token):
        with self.connect(True) as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))

    @staticmethod
    def _get(db, company_id):
        row = db.execute(
            "SELECT body FROM companies WHERE id=? AND archived_at IS NULL", (company_id,)
        ).fetchone()
        if not row:
            raise Problem(404, "not_found", "Компания не найдена")
        return json.loads(row["body"])

    def get(self, company_id):
        with self.connect() as db:
            return self.present(db, self._get(db, company_id))

    def listing(self, q, status, limit, offset):
        domain_query = q.casefold()
        try:
            domain_query = CompanyWrite.domain_valid(q)
        except ValueError:
            pass
        clause = (
            "archived_at IS NULL AND (instr(casefold(name), casefold(?))>0 "
            "OR instr(slug,lower(?))>0 OR instr(coalesce(domain,''),?)>0)"
        )
        args = [q, q, domain_query]
        if status:
            clause += " AND status=?"
            args.append(status)
        with self.connect() as db:
            total = db.execute("SELECT count(*) FROM companies WHERE " + clause, args).fetchone()[0]
            rows = db.execute(
                "SELECT body FROM companies WHERE " + clause + " ORDER BY name,id LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            items = [self.present(db, json.loads(r["body"])) for r in rows]
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def save(self, values, actor, company_id=None, expected=None, archive=False, credentials=None):
        with self.connect(True) as db:
            old = self._get(db, company_id) if company_id else None
            if old and old["version"] != expected:
                raise Problem(409, "version_conflict", "Запись изменена. Обновите карточку")
            values = {k: v for k, v in values.items() if k != "connection_credentials"}
            current = stamp()
            company = dict(values)
            company.update(
                id=company_id or str(uuid4()),
                version=old["version"] + 1 if old else 1,
                created_at=old["created_at"] if old else current,
                updated_at=current,
                archived_at=current if archive else None,
            )
            fields = sorted(k for k in values if old is None or old.get(k) != values[k])
            if archive:
                fields = ["archived_at"]
            try:
                db.execute(
                    """INSERT INTO companies VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET slug=excluded.slug, domain=excluded.domain,
                    name=excluded.name,status=excluded.status,version=excluded.version,
                    archived_at=excluded.archived_at,body=excluded.body""",
                    (
                        company["id"],
                        company["slug"],
                        company["domain"],
                        company["name"],
                        company["status"],
                        company["version"],
                        company["archived_at"],
                        json.dumps(company, ensure_ascii=False),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                field = "domain" if "domain" in str(exc) else "slug"
                raise Problem(409, "duplicate", "Домен или идентификатор уже занят", field) from exc
            if archive or company["status"] == "suspended" or (
                old and old["slug"] != company["slug"]
            ):
                db.execute("DELETE FROM tenant_sessions WHERE admin_id IN "
                           "(SELECT id FROM tenant_admins WHERE company_id=?)", (company["id"],))
            if self.apply_credentials(db, company, old, credentials):
                fields.append("connection_credentials")
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    company["id"],
                    actor["id"],
                    actor["display_name"],
                    "archived" if archive else "updated" if old else "created",
                    json.dumps(fields),
                    current,
                ),
            )
            return self.present(db, company)

    def events(self, company_id, limit, offset):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone():
                raise Problem(404, "not_found", "Компания не найдена")
            total = db.execute(
                "SELECT count(*) FROM events WHERE company_id=?", (company_id,)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM events WHERE company_id=? ORDER BY created_at DESC,id "
                "LIMIT ? OFFSET ?",
                (company_id, limit, offset),
            ).fetchall()
        return {
            "items": [
                {
                    "id": r["id"],
                    "company_id": r["company_id"],
                    "actor": {"id": r["actor_id"], "display_name": r["actor_name"]},
                    "action": r["action"],
                    "changed_fields": json.loads(r["changed_fields"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
