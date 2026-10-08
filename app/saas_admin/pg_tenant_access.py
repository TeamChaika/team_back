"""Tenant memberships, explicit Auth provisioning and company-scoped sessions."""

import secrets
import time
from datetime import UTC, datetime
from uuid import uuid4

from psycopg.types.json import Jsonb

from .auth_validation import csrf_matches
from .repository import Problem, digest, stamp

TEMP_SECONDS = 72 * 3600


class PostgresTenantAccess:
    @staticmethod
    def _revoke_company_sessions(db, company_id):
        db.execute(
            "DELETE FROM tenant_sessions WHERE admin_id IN (SELECT id FROM memberships WHERE "
            "company_id=%s)",
            (company_id,),
        )

    @staticmethod
    def _access(db, company):
        row = db.execute(
            "SELECT * FROM memberships WHERE company_id=%s AND is_primary_admin=true",
            (company["id"],),
        ).fetchone()
        admin = None
        if row:
            status = "active" if row["auth_user_id"] else "activation_required"
            if row["auth_user_id"] and row["must_change"]:
                status = (
                    "expired" if (row["temporary_expires"] or 0) <= time.time() else "temporary"
                )
            if not row["active"] or company["status"] == "suspended" or company["archived_at"]:
                status = "blocked"
            admin = {
                **{k: str(row[k]) for k in ("id", "username", "display_name")},
                "must_change_password": row["must_change"],
                "temporary_expires_at": datetime.fromtimestamp(
                    row["temporary_expires"], UTC
                ).isoformat()
                if row["temporary_expires"]
                else None,
                "status": status,
                "can_reset_password": False,
            }
        return {
            "company_version": company["version"],
            "exists": bool(row),
            "login_path": "/tenant/" + company["slug"],
            "admin": admin,
            "can_reset_password": False,
        }

    def admin_access(self, company_id):
        with self.connect() as db:
            return self._access(db, self._get(db, company_id))

    def provision_admin(self, company_id, expected, actor, reset=False):
        if reset:
            # Shared Auth has other consumers; without proof of exclusivity a reset is forbidden.
            raise Problem(
                409,
                "shared_identity",
                "Смена пароля общей учётной записи выполняется её владельцем",
            )
        temporary = None
        with self.connect(True) as db:
            self._require_owner(db, actor)
            company = self._get(db, company_id)
            if company["version"] != expected:
                raise Problem(409, "version_conflict", "Запись изменена. Обновите карточку")
            if company["status"] == "suspended":
                raise Problem(403, "company_blocked", "Доступ компании приостановлен")
            membership = db.execute(
                "SELECT * FROM memberships WHERE company_id=%s AND is_primary_admin=true FOR "
                "UPDATE",
                (company_id,),
            ).fetchone()
            if membership and membership["auth_user_id"]:
                raise Problem(409, "access_exists", "Доступ уже создан")
            contact = company["primary_admin"]
            if not contact:
                raise Problem(422, "contact_required", "Сначала сохраните контакт администратора")
            email = membership["username"] if membership else contact["email"].casefold()
            identity = db.execute(
                "SELECT * FROM auth_identities WHERE lower(email)=%s", (email,)
            ).fetchone()
            pending = db.execute(
                "SELECT * FROM auth_provisioning WHERE company_id=%s FOR UPDATE", (company_id,)
            ).fetchone()
            if (
                identity
                and pending
                and pending["state"] == "pending"
                and str(identity.get("provision_id")) != str(pending["request_id"])
            ):
                raise Problem(
                    409, "provisioning_conflict", "Требуется проверка создания учётной записи"
                )
            if not identity:
                if pending:
                    raise Problem(
                        409,
                        "provisioning_pending",
                        "Результат создания уточняется. Повторное создание запрещено",
                    )
                marker = str(uuid4())
                temporary = secrets.token_urlsafe(24)
                db.execute(
                    "INSERT INTO auth_provisioning "
                    "(company_id,request_id,username,state,created_at,temporary_ciphertext) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (company_id, marker, email, "pending", stamp(), self.vault.encrypt(temporary)),
                )
            else:
                marker = None
                if pending and pending["state"] == "pending" and pending["temporary_ciphertext"]:
                    temporary = self.vault.decrypt(pending["temporary_ciphertext"])
        # The request marker is committed before any side effect on Auth.
        if not identity:
            user = self.auth.create_user(email, temporary, marker)
            if not user.get("id"):
                raise Problem(503, "auth_ambiguous", "Требуется проверка создания учётной записи")
            identity = {"id": user["id"], "provision_id": marker}
        with self.connect(True) as db:
            self._require_owner(db, actor)
            company = self._get(db, company_id)
            if company["version"] != expected or company["status"] == "suspended":
                raise Problem(409, "version_conflict", "Запись изменена. Обновите карточку")
            membership = db.execute(
                "SELECT * FROM memberships WHERE company_id=%s AND is_primary_admin=true FOR "
                "UPDATE",
                (company_id,),
            ).fetchone()
            if membership and membership["auth_user_id"]:
                raise Problem(409, "access_exists", "Доступ уже создан")
            # A reconciled creation whose password response was lost remains gated.
            created = temporary is not None or bool(
                pending and str(identity.get("provision_id")) == str(pending["request_id"])
            )
            if created and temporary is None:
                raise Problem(
                    409,
                    "provisioning_recovery_required",
                    "Учётная запись создана; требуется восстановление пароля владельцем",
                )
            expiry = time.time() + TEMP_SECONDS if created else None
            if membership:
                db.execute(
                    "UPDATE memberships SET "
                    "auth_user_id=%s,auth_exclusive=%s,must_change=%s,temporary_expires=%s "
                    "WHERE id=%s",
                    (identity["id"], created, created, expiry, membership["id"]),
                )
            else:
                db.execute(
                    "INSERT INTO memberships "
                    "(id,company_id,auth_user_id,username,display_name,auth_exclusive,"
                    "must_change,temporary_expires) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        str(uuid4()),
                        company_id,
                        identity["id"],
                        email,
                        contact["name"],
                        created,
                        created,
                        expiry,
                    ),
                )
            db.execute(
                "UPDATE auth_provisioning SET "
                "state=%s,auth_user_id=%s,temporary_ciphertext=NULL WHERE company_id=%s",
                ("complete", identity["id"], company_id),
            )
            company["version"] += 1
            company["updated_at"] = stamp()
            db.execute(
                "UPDATE companies SET version=%s,body=%s WHERE id=%s",
                (company["version"], Jsonb(company), company_id),
            )
            db.execute(
                "INSERT INTO events VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    str(uuid4()),
                    company_id,
                    actor["id"],
                    actor["display_name"],
                    "admin_access_created",
                    Jsonb(["admin_access"]),
                    stamp(),
                ),
            )
            return {
                **self._access(db, company),
                "temporary_password": temporary,
                "existing_account": not created,
            }

    @staticmethod
    def _tenant_body(row, csrf):
        company = row["body"]
        return {
            "user": {k: str(row[k]) for k in ("id", "username", "display_name", "company_id")},
            "company": {k: company[k] for k in ("id", "name", "slug")},
            "must_change_password": bool(row["must_change"]),
            "csrf_token": csrf,
        }

    def tenant_login(self, slug, username, password, peer, previous=""):
        result = None
        with self.connect(True) as db:
            keys = self._rate(db, peer, True)
            db.execute("SELECT id FROM companies WHERE slug=%s FOR SHARE", (slug,))
            row = db.execute(
                "SELECT m.*,c.body,c.status,c.archived_at FROM memberships m JOIN companies c "
                "ON c.id=m.company_id WHERE c.slug=%s AND lower(m.username)=%s AND "
                "m.active=true AND m.role='company_admin' FOR UPDATE OF m",
                (slug, username.strip().casefold()),
            ).fetchone()
            try:
                tokens = self.auth.login(username.strip().casefold(), password)
                user = self.auth.user(tokens["access_token"])
                valid = row and str(user.get("id")) == str(row["auth_user_id"]) == str(
                    tokens["user_id"]
                )
            except Problem as exc:
                if exc.status != 401:
                    raise
                valid = False
            if (
                valid
                and row["status"] != "suspended"
                and not row["archived_at"]
                and not (row["must_change"] and (row["temporary_expires"] or 0) <= time.time())
            ):
                db.execute(
                    "DELETE FROM tenant_sessions WHERE token_hash=%s AND admin_id=%s",
                    (digest(previous), row["id"]),
                )
                token, csrf = self._issue(db, row, tokens, True)
                result = token, self._tenant_body(row, csrf)
            else:
                self._failed(db, keys)
        if result is None:
            raise Problem(
                401, "invalid_credentials", "Неверный логин или пароль либо доступ закрыт"
            )
        return result

    def tenant_session(self, token, slug):
        with self.connect(True) as db:
            row = self._verified(db, token, True, slug)
            result = self._tenant_body(row, row["csrf"]) if row else None
        if result is None:
            raise Problem(401, "unauthorized", "Требуется повторный вход")
        return result

    def tenant_logout(self, token):
        with self.connect(True) as db:
            db.execute("DELETE FROM tenant_sessions WHERE token_hash=%s", (digest(token),))

    def tenant_password(self, token, slug, current, new, csrf, peer):
        if new == current or not 8 <= len(new) <= 1024:
            raise Problem(422, "password_policy", "Нужен новый пароль от 8 символов")
        result = None
        with self.connect(True) as db:
            row = self._verified(db, token, True, slug, rate_peer=peer)
            if row:
                if not csrf_matches(csrf, row["csrf"]):
                    raise Problem(403, "csrf_failed", "Обновите страницу")
                keys = self._rate(db, peer, True)
                try:
                    verified = self.auth.login(row["username"], current)
                    valid = str(verified["user_id"]) == str(row["auth_user_id"])
                except Problem as exc:
                    if exc.status != 401:
                        raise
                    valid = False
                if valid:
                    self.auth.password(verified["access_token"], new)
                    db.execute(
                        "UPDATE memberships SET must_change=false,temporary_expires=NULL WHERE "
                        "id=%s",
                        (row["id"],),
                    )
                    db.execute("DELETE FROM tenant_sessions WHERE admin_id=%s", (row["id"],))
                    db.execute(
                        "INSERT INTO tenant_events VALUES (%s,%s,%s,%s,%s)",
                        (str(uuid4()), row["company_id"], row["id"], "password_changed", stamp()),
                    )
                    row["must_change"] = False
                    token, new_csrf = self._issue(db, row, verified, True)
                    result = token, self._tenant_body(row, new_csrf)
                else:
                    self._failed(db, keys)
        if result is None:
            raise Problem(
                401, "invalid_credentials", "Неверный текущий пароль либо требуется повторный вход"
            )
        return result

    def tenant_workspace(self, token, slug):
        with self.connect(True) as db:
            row = self._verified(db, token, True, slug)
            if row and row["must_change"]:
                raise Problem(403, "password_change_required", "Сначала смените временный пароль")
            result = (
                {
                    "company": {k: row["body"][k] for k in ("id", "name", "slug", "modules")},
                    "admin": {k: str(row[k]) for k in ("id", "username", "display_name")},
                    "mode": "local",
                    "business_modules_ready": False,
                }
                if row
                else None
            )
        if result is None:
            raise Problem(401, "unauthorized", "Требуется повторный вход")
        return result
