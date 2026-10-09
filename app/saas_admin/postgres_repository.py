"""Native PostgreSQL registry in the private restcontrol schema.

The service role is confined to this schema. HTTP guards authenticate each request;
company mutations independently recheck the platform grant inside their transaction.
"""

import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .connections import EMPTY_CHECK, configured, problem
from .models import CompanyWrite, derived, metadata_url
from .pg_auth import PostgresAuth
from .pg_tenant_access import PostgresTenantAccess
from .repository import Problem, stamp
from .vault import Vault


class PostgresRepository(PostgresAuth, PostgresTenantAccess):
    def __init__(self, dsn, data_dir, auth=None):
        self.dsn = dsn
        self.auth = self._auth = auth
        key = Path(data_dir) / "credentials.key"
        if not key.is_file():
            raise ValueError("Original credentials.key is required; no automatic key generation")
        self.vault = self._vault = Vault(data_dir)

    @contextmanager
    def connect(self, write=False):
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=10) as db:
            db.execute("SET LOCAL search_path TO restcontrol, pg_catalog")
            db.execute("SET LOCAL statement_timeout = '15000ms'")
            db.execute("SET LOCAL lock_timeout = '5000ms'")
            yield db

    _connect = connect

    @staticmethod
    def _require_owner(db, actor):
        row = db.execute(
            "SELECT id FROM platform_memberships WHERE id=%s AND active",
            (actor["id"],),
        ).fetchone()
        if not row:
            raise Problem(403, "forbidden", "Требуется доступ владельца платформы")

    @staticmethod
    def _get(db, company_id):
        try:
            company_id = str(UUID(str(company_id)))
        except ValueError:
            raise Problem(404, "not_found", "Компания не найдена") from None
        row = db.execute(
            "SELECT body FROM companies WHERE id=%s AND archived_at IS NULL FOR UPDATE",
            (company_id,),
        ).fetchone()
        if not row:
            raise Problem(404, "not_found", "Компания не найдена")
        return row["body"]

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
            "archived_at IS NULL AND (strpos(name_search,%s)>0 "
            "OR strpos(slug,lower(%s))>0 OR strpos(coalesce(domain,''),%s)>0)"
        )
        args = [q.casefold(), q, domain_query]
        if status:
            clause += " AND status=%s"
            args.append(status)
        with self.connect() as db:
            total = db.execute(
                "SELECT count(*) AS n FROM companies WHERE " + clause, args
            ).fetchone()["n"]
            rows = db.execute(
                "SELECT body FROM companies WHERE "
                + clause
                + " ORDER BY name,id LIMIT %s OFFSET %s",
                [*args, limit, offset],
            ).fetchall()
            return {
                "items": [self.present(db, r["body"]) for r in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    @staticmethod
    def _write_company(db, company):
        db.execute(
            "INSERT INTO companies(id,slug,domain,name,status,version,archived_at,"
            "body,name_search) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET "
            "slug=excluded.slug,domain=excluded.domain,name=excluded.name,name_search=excluded.name_search,"
            "status=excluded.status,version=excluded.version,"
            "archived_at=excluded.archived_at,body=excluded.body",
            (
                *[
                    company[k]
                    for k in ("id", "slug", "domain", "name", "status", "version", "archived_at")
                ],
                Jsonb(company),
                company["name"].casefold(),
            ),
        )

    @staticmethod
    def _audit(db, company_id, actor, action, fields, created_at=None):
        db.execute(
            "INSERT INTO events(id,company_id,actor_id,actor_name,action,changed_fields,"
            "created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                str(uuid4()),
                company_id,
                actor["id"],
                actor["display_name"],
                action,
                Jsonb(fields),
                created_at or stamp(),
            ),
        )

    def save(self, values, actor, company_id=None, expected=None, archive=False, credentials=None):
        try:
            with self.connect(True) as db:
                self._require_owner(db, actor)
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
                self._write_company(db, company)
                if (
                    archive
                    or company["status"] == "suspended"
                    or (old and old["slug"] != company["slug"])
                ):
                    self._revoke_company_sessions(db, company["id"])
                if self.apply_credentials(db, company, old, credentials):
                    fields.append("connection_credentials")
                self._audit(
                    db,
                    company["id"],
                    actor,
                    "archived" if archive else "updated" if old else "created",
                    fields,
                    current,
                )
                return self.present(db, company)
        except psycopg.errors.UniqueViolation as exc:
            field = "domain" if "domain" in (exc.diag.constraint_name or "") else "slug"
            raise Problem(409, "duplicate", "Домен или идентификатор уже занят", field) from None

    def events(self, company_id, limit, offset):
        try:
            company_id = str(UUID(str(company_id)))
        except ValueError:
            raise Problem(404, "not_found", "Компания не найдена") from None
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM companies WHERE id=%s", (company_id,)).fetchone():
                raise Problem(404, "not_found", "Компания не найдена")
            total = db.execute(
                "SELECT count(*) AS n FROM events WHERE company_id=%s", (company_id,)
            ).fetchone()["n"]
            rows = db.execute(
                "SELECT * FROM events WHERE company_id=%s ORDER BY created_at DESC,id "
                "LIMIT %s OFFSET %s",
                (company_id, limit, offset),
            ).fetchall()
            return {
                "items": [
                    {
                        "id": str(r["id"]),
                        "company_id": str(r["company_id"]),
                        "actor": {"id": str(r["actor_id"]), "display_name": r["actor_name"]},
                        "action": r["action"],
                        "changed_fields": r["changed_fields"],
                        "created_at": r["created_at"],
                    }
                    for r in rows
                ],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def apply_credentials(self, db, company, old, credentials):
        desired = configured(company) if not company["archived_at"] else {}
        previous = configured(old) if old else {}
        credentials = credentials or {}
        if set(credentials) - set(desired):
            problem(422, "validation_error", "Подключение не найдено", "connection_credentials")
        rows = {
            r["connection_id"]: r
            for r in db.execute("SELECT * FROM connections WHERE company_id=%s", (company["id"],))
        }
        changed = False
        for connection_id, url in desired.items():
            row, incoming = rows.get(connection_id), credentials.get(connection_id)
            if incoming is None:
                if previous.get(connection_id) != url:
                    problem(
                        422,
                        "credentials_required",
                        "Укажите логин и пароль подключения",
                        "connection_credentials." + connection_id,
                    )
                continue
            login, password = incoming["login"], incoming.get("password")
            stored = json.loads(self.vault.decrypt(row["ciphertext"])) if row else None
            if password is None:
                if not row or row["url"] != url or stored["login"] != login:
                    problem(
                        422,
                        "credentials_required",
                        "Для этого подключения нужен пароль",
                        "connection_credentials." + connection_id + ".password",
                    )
                password = stored["password"]
            payload = {"login": login, "password": password}
            if row and row["url"] == url and stored == payload:
                continue
            db.execute(
                "INSERT INTO connections(company_id,connection_id,url,ciphertext,check_json) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(company_id,connection_id) DO UPDATE SET "
                "url=excluded.url,ciphertext=excluded.ciphertext,check_json=excluded.check_json",
                (
                    company["id"],
                    connection_id,
                    url,
                    self.vault.encrypt(json.dumps(payload)),
                    Jsonb(EMPTY_CHECK),
                ),
            )
            changed = True
        for key in rows:
            if key not in desired:
                db.execute(
                    "DELETE FROM connections WHERE company_id=%s AND connection_id=%s",
                    (company["id"], key),
                )
                changed = True
        return changed

    def present(self, db, company):
        company = derived(company)
        urls = configured(company, enabled_only=True)
        rows = {
            r["connection_id"]: r
            for r in db.execute(
                "SELECT connection_id,url,check_json FROM connections WHERE company_id=%s",
                (company["id"],),
            )
        }
        states = [
            rows[key]["check_json"]["status"]
            if key in rows and rows[key]["url"] == url
            else "not_checked"
            for key, url in urls.items()
        ]
        company["integration_state"] = (
            "not_configured"
            if not states
            else "failed"
            if "failed" in states
            else "not_checked"
            if "not_checked" in states
            else "ok"
        )
        return company

    def connections(self, company_id):
        with self.connect() as db:
            company = self._get(db, company_id)
            rows = {
                r["connection_id"]: r
                for r in db.execute("SELECT * FROM connections WHERE company_id=%s", (company_id,))
            }
            items = []
            for connection_id, url in configured(company).items():
                row = rows.get(connection_id)
                secret = json.loads(self.vault.decrypt(row["ciphertext"])) if row else None
                items.append(
                    {
                        "id": connection_id,
                        "url": url,
                        "login": secret["login"] if secret else None,
                        "password_set": bool(secret),
                        "check": row["check_json"] if row else dict(EMPTY_CHECK),
                    }
                )
            return {"items": items}

    def check_inputs(self, company_id, connection_id, expected, url=None, login=None):
        with self.connect() as db:
            company = self._get(db, company_id)
            if company["version"] != expected:
                problem(409, "version_conflict", "Запись изменена. Обновите карточку")
            configured_url = configured(company).get(connection_id)
            row = db.execute(
                "SELECT * FROM connections WHERE company_id=%s AND connection_id=%s",
                (company_id, connection_id),
            ).fetchone()
            if not row or not configured_url or row["url"] != configured_url:
                problem(422, "credentials_required", "Сначала сохраните логин и пароль подключения")
            secret = json.loads(self.vault.decrypt(row["ciphertext"]))
            if (url is not None and metadata_url(url) != configured_url) or (
                login is not None and login != secret["login"]
            ):
                problem(
                    422, "credentials_required", "Для изменённого адреса или логина нужен пароль"
                )
            return configured_url, secret["login"], secret["password"]

    def reserve_check(self, owner_id, url):
        now = time.time()
        parsed = urlsplit(url)
        target = hashlib.sha256(
            f"{parsed.hostname}:{parsed.port or 443}/resto/api".encode()
        ).hexdigest()
        with self.connect(True) as db:
            self._require_owner(db, {"id": owner_id})
            # A transaction advisory lock also serializes the first insert for this target.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (target,))
            cooldown = db.execute(
                "SELECT until FROM check_targets WHERE target=%s", (target,)
            ).fetchone()
            if cooldown and cooldown["until"] > now:
                problem(
                    429, "rate_limited", "Повторная проверка этого адреса доступна через минуту"
                )
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,1))", (str(owner_id),))
            row = db.execute(
                "SELECT * FROM check_attempts WHERE owner_id=%s", (owner_id,)
            ).fetchone()
            if row and row["until"] > now and row["count"] >= 10:
                problem(429, "rate_limited", "Не более 10 проверок в минуту")
            db.execute(
                "INSERT INTO check_targets(target,until) VALUES(%s,%s) "
                "ON CONFLICT(target) DO UPDATE SET until=excluded.until",
                (target, now + 60),
            )
            db.execute(
                "INSERT INTO check_attempts(owner_id,count,until) VALUES(%s,1,%s) "
                "ON CONFLICT(owner_id) DO UPDATE SET count=CASE WHEN "
                "check_attempts.until<=%s THEN 1 ELSE check_attempts.count+1 END,"
                "until=CASE WHEN check_attempts.until<=%s THEN excluded.until "
                "ELSE check_attempts.until END",
                (owner_id, now + 60, now, now),
            )

    def record_check(self, company_id, connection_id, expected, check, actor):
        with self.connect(True) as db:
            self._require_owner(db, actor)
            company = self._get(db, company_id)
            if company["version"] != expected:
                problem(
                    409, "version_conflict", "Настройки изменены: результат проверки не сохранён"
                )
            row = db.execute(
                "UPDATE connections SET check_json=%s WHERE company_id=%s "
                "AND connection_id=%s RETURNING *",
                (Jsonb(check), company_id, connection_id),
            ).fetchone()
            if not row:
                problem(404, "not_found", "Подключение не найдено")
            company["version"] += 1
            company["updated_at"] = stamp()
            self._write_company(db, company)
            self._audit(
                db,
                company_id,
                actor,
                "connection_checked",
                ["connection_check." + connection_id],
                company["updated_at"],
            )
            secret = json.loads(self.vault.decrypt(row["ciphertext"]))
            return {
                "company_version": company["version"],
                "connection": {
                    "id": connection_id,
                    "url": row["url"],
                    "login": secret["login"],
                    "password_set": True,
                    "check": dict(check),
                },
            }

    def company_for_domain(self, hostname):
        """Resolve only a configured, non-archived company domain."""
        with self.connect() as db:
            row = db.execute(
                "SELECT body FROM companies WHERE domain=%s "
                "AND archived_at IS NULL AND status<>'suspended'",
                (hostname,),
            ).fetchone()
            return {k: row["body"][k] for k in ("id", "name", "slug")} if row else None

    def company_for_payment_domain(self, hostname):
        with self.connect() as db:
            row = db.execute(
                "SELECT c.body FROM company_payment_domains p "
                "JOIN companies c ON c.id=p.company_id "
                "WHERE p.domain=%s AND p.status='active' AND p.verified_at IS NOT NULL "
                "AND c.archived_at IS NULL AND c.status='active'",
                (hostname,),
            ).fetchone()
            return {k: row["body"][k] for k in ("id", "name", "slug", "domain")} if row else None

    def payment_origin(self, company_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT p.domain FROM company_payment_domains p "
                "JOIN companies c ON c.id=p.company_id "
                "WHERE p.company_id=%s AND p.status='active' AND p.verified_at IS NOT NULL "
                "AND c.archived_at IS NULL AND c.status='active'",
                (company_id,),
            ).fetchone()
            return "https://" + row["domain"] if row else ""

    @staticmethod
    def _revoke_company_sessions(db, company_id):
        db.execute(
            "DELETE FROM tenant_sessions WHERE admin_id IN "
            "(SELECT id FROM memberships WHERE company_id=%s)",
            (company_id,),
        )

        db.execute("DELETE FROM platform_tenant_sessions WHERE company_id=%s", (company_id,))
        db.execute("DELETE FROM platform_sso_codes WHERE company_id=%s", (company_id,))

    def validate_ready(self):
        """Fail closed on wrong credentials, missing schema or missing encryption key."""
        with self.connect() as db:
            role = db.execute(
                "SELECT current_user AS name,rolsuper,rolbypassrls FROM pg_roles "
                "WHERE rolname=current_user"
            ).fetchone()
            if role["name"] != "restcontrol_backend" or role["rolsuper"] or role["rolbypassrls"]:
                raise ValueError("Runtime must use restricted restcontrol_backend role")
            overlap = db.execute(
                "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname IN ('auth','chaika','chaika_deposits','chaika_iiko_documents') "
                "AND c.relkind IN ('r','p','v','m') AND "
                "has_table_privilege(current_user,c.oid,'SELECT,INSERT,UPDATE,DELETE') LIMIT 1"
            ).fetchone()
            if overlap:
                raise ValueError("Runtime role has unexpected access to another product schema")
            if not db.execute("SELECT 1 FROM platform_memberships WHERE active").fetchone():
                raise ValueError("No active platform membership")
            db.execute(
                "SELECT company_id,domain,status,revision FROM company_payment_domains LIMIT 0"
            )
            for row in db.execute("SELECT ciphertext FROM connections"):
                value = json.loads(self.vault.decrypt(row["ciphertext"]))
                if not isinstance(value.get("login"), str) or not isinstance(
                    value.get("password"), str
                ):
                    raise ValueError("Invalid encrypted connection")
            db.execute("SELECT 1 FROM memberships LIMIT 1")
        return {"storage": "supabase_postgres", "auth": "supabase"}

    def close(self):
        if self.auth is not None:
            self.auth.close()
