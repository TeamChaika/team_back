"""Central account side effects; tenant capabilities never receive Auth admin keys."""

import hashlib
import hmac
import json
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row, tuple_row

from app.tenancy.config import TenantRuntime
from app.tenancy.migrations import _check_identity_role

from .auth_validation import csrf_matches
from .pg_tenant_access import TEMP_SECONDS
from .repository import Problem, digest, stamp


@dataclass(frozen=True)
class IdentityTarget:
    """Operator configuration: never constructed from an HTTP request body."""

    runtime: TenantRuntime
    dsn: str = field(repr=False)

    def __post_init__(self):
        if self.runtime.mode != "tenant":
            raise ValueError("Identity target requires a company runtime")

    @contextmanager
    def connection(self):
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=10) as db:
            role = db.execute(
                "SELECT current_user AS name,session_user AS login,"
                "rolsuper,rolcreaterole,rolcreatedb,"
                "rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            if (
                role["name"] != f"{self.runtime.key}_identity_runtime"
                or role["login"] != role["name"]
                or any(
                    role[k]
                    for k in (
                        "rolsuper",
                        "rolcreaterole",
                        "rolcreatedb",
                        "rolreplication",
                        "rolbypassrls",
                    )
                )
                or db.execute(
                    "SELECT 1 FROM pg_auth_members WHERE member=current_user::regrole"
                ).fetchone()
            ):
                raise ValueError("Identity target must use its dedicated unprivileged login")
            with db.cursor(row_factory=tuple_row) as cursor:
                _check_identity_role(cursor, self.runtime)
            db.execute("SET LOCAL search_path TO pg_catalog")
            db.execute("SET LOCAL statement_timeout='15000ms'")
            yield db

    def _call(self, routine, *args):
        with self.connection() as db:
            return db.execute(
                sql.SQL("SELECT {}({}) AS value").format(
                    sql.Identifier(self.runtime.analytics_schema, routine),
                    sql.SQL(",").join(sql.Placeholder() for _ in args),
                ),
                args,
            ).fetchone()["value"]

    def is_admin(self, user):
        return self._call("company_identity_actor_is_admin", user) is True

    def provision(self, user, email, marker, display_name):
        return self._call("provision_company_identity", user, email, marker, display_name)

    def password_complete(self, user):
        self._call("set_company_identity_password_required", user, False)


class CompanyAccounts:
    def __init__(self, repository, targets):
        self.repository = repository
        self.targets = {str(UUID(str(key))): value for key, value in targets.items()}
        for key, value in self.targets.items():
            if str(value.runtime.company_id) != key:
                raise ValueError("Identity target belongs to another company")

    def _verified(self, company_id, token, csrf, *, admin=False):
        company_id = str(UUID(str(company_id)))
        target = self.targets.get(company_id)
        if target is None:
            raise Problem(503, "identity_target_missing", "Создание сотрудников ещё не настроено")
        actor, session = self.repository.tenant_actor_session(token, company_id)
        if str(actor.company_id) != company_id or not csrf_matches(csrf, session["csrf_token"]):
            raise Problem(403, "csrf_failed", "Обновите страницу")
        if admin:
            if session.get("must_change_password"):
                raise Problem(403, "password_change_required", "Сначала смените временный пароль")
            if actor.kind != "platform_owner" and not target.is_admin(actor.auth_user_id):
                raise Problem(403, "forbidden", "Требуется администратор компании")
        return actor, session, target

    @staticmethod
    def _input(body):
        if not isinstance(body, dict) or set(body) != {
            "request_id",
            "email",
            "display_name",
            "password",
            "profile_fingerprint",
        }:
            raise Problem(422, "invalid_account", "Проверьте поля сотрудника")
        try:
            marker = str(UUID(body["request_id"]))
            email, name, password, profile = (
                body[k] for k in ("email", "display_name", "password", "profile_fingerprint")
            )
            if not all(isinstance(v, str) for v in (email, name, password, profile)):
                raise ValueError()
            email, name = email.strip().casefold(), name.strip()
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
                raise ValueError()
            if not 1 <= len(name) <= 150 or not 8 <= len(password) <= 128:
                raise ValueError()
            if not re.fullmatch(r"[0-9a-f]{64}", profile):
                raise ValueError()
            canonical = json.dumps([email, name, password, profile], ensure_ascii=True).encode()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise Problem(422, "invalid_account", "Проверьте поля сотрудника") from None
        return marker, email, name, password, canonical

    def create(self, company_id, token, csrf, body):
        marker, email, name, password, canonical = self._input(body)
        actor, _, target = self._verified(company_id, token, csrf, admin=True)
        repo = self.repository
        fresh = False
        with repo.connect(True) as db:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("account:" + marker,)
            )
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("account-email:" + str(company_id) + ":" + email,),
            )
            pending = db.execute(
                "SELECT * FROM company_account_requests WHERE request_id=%s FOR UPDATE", (marker,)
            ).fetchone()
            identity = db.execute(
                "SELECT id,email,provision_id FROM auth_identities WHERE lower(email)=%s", (email,)
            ).fetchone()
            if pending:
                proof = repo.vault.decrypt(pending["proof_key_ciphertext"]).encode()
                if (
                    str(pending["company_id"]) != str(company_id)
                    or str(pending["actor_id"]) != str(actor.auth_user_id)
                    or not hmac.compare_digest(
                        pending["fingerprint"],
                        hmac.new(proof, canonical, hashlib.sha256).hexdigest(),
                    )
                ):
                    raise Problem(409, "request_conflict", "Этот запрос уже использован")
                if identity and str(identity.get("provision_id")) != marker:
                    raise Problem(409, "identity_conflict", "Эта почта уже используется")
                if pending["state"] == "rejected":
                    raise Problem(409, "auth_rejected", "Исправьте данные и создайте новый запрос")
                if pending["state"] == "complete":
                    return {"id": str(pending["auth_user_id"]), "request_id": marker}
            else:
                if (
                    identity
                    or db.execute(
                        "SELECT 1 FROM company_account_requests WHERE company_id=%s AND email=%s "
                        "AND state<>'rejected'",
                        (company_id, email),
                    ).fetchone()
                ):
                    raise Problem(409, "identity_exists", "Эта почта уже используется")
                proof = secrets.token_urlsafe(32)
                db.execute(
                    "INSERT INTO company_account_requests "
                    "(request_id,company_id,actor_id,email,display_name,fingerprint,"
                    "proof_key_ciphertext,state) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,'pending')",
                    (
                        marker,
                        company_id,
                        actor.auth_user_id,
                        email,
                        name,
                        hmac.new(proof.encode(), canonical, hashlib.sha256).hexdigest(),
                        repo.vault.encrypt(proof),
                    ),
                )
                fresh = True
        # Committed journal before Auth. A repeated pending call only reconciles;
        # it must never blindly repeat the potentially successful Auth POST.
        if fresh:
            try:
                repo.auth.create_user(email, password, marker)
            except Problem as exc:
                if exc.code in {"auth_rejected", "rate_limited"}:
                    # An explicit upstream rejection can be corrected with a new
                    # request. Transport/5xx ambiguity always remains pending.
                    with repo.connect(True) as db:
                        exists = db.execute(
                            "SELECT 1 FROM auth_identities WHERE lower(email)=%s "
                            "AND provision_id=%s",
                            (email, marker),
                        ).fetchone()
                        if not exists:
                            db.execute(
                                "UPDATE company_account_requests SET state='rejected' "
                                "WHERE request_id=%s AND state='pending'",
                                (marker,),
                            )
                raise
        self._verified(company_id, token, csrf, admin=True)
        with repo.connect(True) as db:
            db.execute(
                "SELECT * FROM company_account_requests WHERE request_id=%s FOR UPDATE", (marker,)
            ).fetchone()
            identity = db.execute(
                "SELECT id,email,provision_id FROM auth_identities WHERE lower(email)=%s", (email,)
            ).fetchone()
            if not identity:
                raise Problem(
                    503,
                    "provisioning_pending",
                    "Результат создания уточняется. Повторите тот же запрос",
                )
            if str(identity.get("provision_id")) != marker:
                raise Problem(409, "identity_conflict", "Эта почта уже используется")
            member = db.execute(
                "SELECT * FROM memberships WHERE company_id=%s AND auth_user_id=%s",
                (company_id, identity["id"]),
            ).fetchone()
            if member is None:
                db.execute(
                    "INSERT INTO memberships "
                    "(id,company_id,auth_user_id,username,display_name,role,is_primary_admin,active,"
                    "auth_exclusive,must_change,temporary_expires) "
                    "VALUES(%s,%s,%s,%s,%s,'employee',false,true,true,true,%s)",
                    (uuid4(), company_id, identity["id"], email, name, time.time() + TEMP_SECONDS),
                )
            elif member["role"] != "employee" or member["is_primary_admin"]:
                raise Problem(409, "membership_conflict", "Требуется проверка учётной записи")
            db.execute(
                "UPDATE company_account_requests SET state='identity_ready',auth_user_id=%s "
                "WHERE request_id=%s",
                (identity["id"], marker),
            )
        self._verified(company_id, token, csrf, admin=True)
        target.provision(identity["id"], email, marker, name)
        with repo.connect(True) as db:
            changed = db.execute(
                "UPDATE company_account_requests SET state='complete' "
                "WHERE request_id=%s AND state<>'complete' RETURNING request_id",
                (marker,),
            ).fetchone()
            if changed:
                db.execute(
                    "INSERT INTO platform_tenant_events VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        uuid4(),
                        company_id,
                        actor.auth_user_id,
                        actor.display_name,
                        "employee_created",
                        str(identity["id"]),
                        "success",
                        stamp(),
                    ),
                )
        return {"id": str(identity["id"]), "request_id": marker}

    @staticmethod
    def _recovery_member(db, company_id, subject):
        # Block concurrent membership reassignment, including phantom new rows, while
        # the provider changes this globally shared Auth identity.
        db.execute("LOCK TABLE memberships,companies IN SHARE ROW EXCLUSIVE MODE")
        company = db.execute(
            "SELECT status,archived_at FROM companies WHERE id=%s", (company_id,)
        ).fetchone()
        members = db.execute(
            "SELECT * FROM memberships WHERE auth_user_id=%s FOR UPDATE", (subject,)
        ).fetchall()
        if (
            not company
            or company["status"] != "active"
            or company["archived_at"]
            or len(members) != 1
            or str(members[0]["company_id"]) != company_id
            or not members[0]["active"]
            or not members[0]["auth_exclusive"]
            or db.execute(
                "SELECT 1 FROM platform_memberships WHERE auth_user_id=%s", (subject,)
            ).fetchone()
            or not db.execute("SELECT 1 FROM auth_identities WHERE id=%s", (subject,)).fetchone()
        ):
            raise Problem(
                400, "recovery_invalid", "Ссылка недействительна. Запросите новую в Telegram"
            )
        return members[0]

    def recover_password(self, company_id, raw_token, new_password):
        company_id = str(UUID(str(company_id)))
        if not isinstance(raw_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", raw_token):
            raise Problem(400, "recovery_invalid", "Ссылка недействительна")
        if not isinstance(new_password, str) or not 8 <= len(new_password) <= 128:
            raise Problem(422, "password_policy", "Нужен новый пароль от 8 символов")
        target = self.targets.get(company_id)
        if target is None:
            raise Problem(503, "identity_target_missing", "Восстановление ещё не настроено")
        hashed = hashlib.sha256(raw_token.encode()).hexdigest()
        subject = target._call("consume_company_password_recovery", hashed)
        if subject is None:
            raise Problem(
                400, "recovery_invalid", "Ссылка недействительна. Запросите новую в Telegram"
            )
        repo = self.repository
        # Consumption and local session revocation are durable BEFORE the external
        # side effect. Ambiguous provider responses cannot restore this capability.
        with repo.connect(True) as db:
            member = self._recovery_member(db, company_id, subject)
            db.execute("DELETE FROM tenant_sessions WHERE admin_id=%s", (member["id"],))
        with target.connection() as tenant_db:
            locked = tenant_db.execute(
                sql.SQL("SELECT {}(%s) AS value").format(
                    sql.Identifier(
                        target.runtime.analytics_schema, "lock_company_password_recovery"
                    )
                ),
                (hashed,),
            ).fetchone()["value"]
            if locked != subject:
                raise Problem(
                    400, "recovery_invalid", "Ссылка недействительна. Запросите новую в Telegram"
                )
            with repo.connect(True) as db:
                member = self._recovery_member(db, company_id, subject)
                result = repo.auth.reset_password(subject, new_password)
                if not isinstance(result, dict) or str(result.get("id")) != str(subject):
                    raise Problem(
                        503, "password_ambiguous", "Результат смены пароля не подтверждён"
                    )
                db.execute("DELETE FROM tenant_sessions WHERE admin_id=%s", (member["id"],))
                db.execute(
                    "UPDATE memberships SET must_change=false,temporary_expires=NULL WHERE id=%s",
                    (member["id"],),
                )
                db.execute(
                    "INSERT INTO tenant_events VALUES(%s,%s,%s,%s,%s)",
                    (uuid4(), company_id, member["id"], "password_recovered", stamp()),
                )
            # Use the SAME tenant transaction: its binding/profile row locks remain
            # held through Auth confirmation and local completion.
            try:
                tenant_db.execute(
                    sql.SQL("SELECT {}(%s,false)").format(
                        sql.Identifier(
                            target.runtime.analytics_schema,
                            "set_company_identity_password_required",
                        )
                    ),
                    (subject,),
                )
            except psycopg.Error:
                tenant_db.rollback()
                return {"ok": True, "completed": False}
        return {"ok": True, "completed": True}

    def password(self, company_id, token, csrf, current, new):
        actor, _, target = self._verified(company_id, token, csrf)
        if (
            not isinstance(current, str)
            or not isinstance(new, str)
            or current == new
            or not 1 <= len(current) <= 256
            or not 8 <= len(new) <= 128
        ):
            raise Problem(422, "password_policy", "Нужен новый пароль от 8 символов")
        repo = self.repository
        with repo.connect() as db:
            company = repo._get(db, company_id)
        if actor.kind != "platform_owner":
            opaque, session = repo.tenant_password(
                token, company["slug"], current, new, csrf, "company-password:" + company_id
            )
        else:
            opaque, session = self._owner_password(company, token, csrf, current, new)
        # A changed Auth password cannot be rolled back if tenant synchronization
        # fails. Always return the new opaque session so the caller stays logged in.
        completed = True
        if actor.kind != "platform_owner":
            try:
                target.password_complete(actor.auth_user_id)
            except (psycopg.Error, ValueError):
                completed = False
        return {"token": opaque, "csrf_token": session["csrf_token"], "completed": completed}

    def _owner_password(self, company, token, csrf, current, new):
        repo = self.repository
        with repo.connect(True) as db:
            row = repo._tenant_verified(db, token, company["slug"])
            if not row or not row.get("_platform_owner") or not csrf_matches(csrf, row["csrf"]):
                raise Problem(403, "forbidden", "Требуется вход владельца")
            keys = repo._rate(db, "company-owner-password:" + str(row["auth_user_id"]))
            try:
                verified = repo.auth.login(row["username"], current)
            except Problem as exc:
                if exc.status == 401:
                    repo._failed(db, keys)
                    db.commit()
                raise
            user = repo.auth.user(verified["access_token"])
            if str(verified["user_id"]) != str(row["auth_user_id"]) or str(user.get("id")) != str(
                row["auth_user_id"]
            ):
                raise Problem(403, "identity_conflict", "Не удалось подтвердить учётную запись")
            updated = repo.auth.password(verified["access_token"], new)
            if str(updated.get("id")) != str(row["auth_user_id"]):
                raise Problem(503, "password_ambiguous", "Не удалось подтвердить смену пароля")
            refreshed = repo.auth.login(row["username"], new)
            if str(refreshed["user_id"]) != str(row["auth_user_id"]):
                raise Problem(503, "password_ambiguous", "Требуется повторный вход")
            handle = db.execute(
                "SELECT * FROM platform_tenant_sessions WHERE token_hash=%s FOR UPDATE",
                (digest(token),),
            ).fetchone()
            db.execute(
                "UPDATE sessions SET tokens_ciphertext=%s WHERE token_hash=%s",
                (repo.vault.encrypt(json.dumps(refreshed)), handle["parent_hash"]),
            )
            opaque, new_csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.execute("DELETE FROM platform_tenant_sessions WHERE token_hash=%s", (digest(token),))
            db.execute(
                "INSERT INTO platform_tenant_sessions VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    digest(opaque),
                    handle["parent_hash"],
                    handle["company_id"],
                    handle["frontend_origin"],
                    handle["api_origin"],
                    new_csrf,
                    handle["expires"],
                ),
            )
            repo._platform_event(db, row, "password_changed")
            return opaque, {"csrf_token": new_csrf}
