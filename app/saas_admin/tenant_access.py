"""Local tenant identities, isolated sessions and explicit owner provisioning."""

import hmac
import json
import secrets
import time
from datetime import UTC, datetime
from uuid import uuid4

from .auth_validation import csrf_matches

TEMP_SECONDS = 72 * 3600


def primitives():
    from .repository import Problem, digest, password_hash, stamp

    return Problem, digest, password_hash, stamp


class TenantAccessRepository:
    @staticmethod
    def _access(db, company):
        row = db.execute(
            "SELECT * FROM tenant_admins WHERE company_id=?", (company["id"],)
        ).fetchone()
        admin = None
        if row:
            status = "active"
            if row["must_change"]:
                status = "expired" if row["temporary_expires"] <= time.time() else "temporary"
            if company["status"] == "suspended" or company["archived_at"]:
                status = "blocked"
            admin = {
                **{k: row[k] for k in ("id", "username", "display_name")},
                "must_change_password": bool(row["must_change"]),
                "temporary_expires_at": (
                    datetime.fromtimestamp(row["temporary_expires"], UTC).isoformat()
                    if row["temporary_expires"] else None
                ),
                "status": status,
            }
        return {"company_version": company["version"], "exists": bool(row),
                "login_path": "/tenant/" + company["slug"], "admin": admin}

    def admin_access(self, company_id: str) -> dict:
        with self.connect() as db:
            return self._access(db, self._get(db, company_id))

    def provision_admin(
        self, company_id: str, expected: int, actor: dict, reset: bool = False
    ) -> dict:
        Problem, _, password_hash, stamp = primitives()
        temporary = secrets.token_urlsafe(24)
        salt = secrets.token_hex(16)
        hashed = password_hash(temporary, salt)
        with self.connect(True) as db:
            company = self._get(db, company_id)
            if company["version"] != expected:
                raise Problem(409, "version_conflict", "Запись изменена. Обновите карточку")
            if company["status"] == "suspended":
                raise Problem(403, "company_blocked", "Доступ компании приостановлен")
            row = db.execute(
                "SELECT * FROM tenant_admins WHERE company_id=?", (company_id,)
            ).fetchone()
            if bool(row) != reset:
                raise Problem(409, "access_exists" if row else "access_missing",
                              "Доступ уже создан" if row else "Сначала создайте доступ")
            if reset:
                db.execute(
                    "UPDATE tenant_admins SET salt=?,password_hash=?,must_change=1,"
                    "temporary_expires=? WHERE company_id=?",
                    (salt, hashed, time.time() + TEMP_SECONDS, company_id),
                )
                db.execute("DELETE FROM tenant_sessions WHERE admin_id=?", (row["id"],))
            else:
                contact = company["primary_admin"]
                if not contact:
                    raise Problem(422, "contact_required",
                                  "Сначала сохраните контакт администратора")
                db.execute("INSERT INTO tenant_admins VALUES (?,?,?,?,?,?,1,?)", (
                    str(uuid4()), company_id, contact["email"].casefold(), contact["name"],
                    salt, hashed, time.time() + TEMP_SECONDS,
                ))
            company["version"] += 1
            company["updated_at"] = stamp()
            db.execute("UPDATE companies SET version=?,body=? WHERE id=?", (
                company["version"], json.dumps(company, ensure_ascii=False), company_id,
            ))
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", (
                str(uuid4()), company_id, actor["id"], actor["display_name"],
                "admin_access_reset" if reset else "admin_access_created",
                json.dumps(["admin_access"]), stamp(),
            ))
            return {**self._access(db, company), "temporary_password": temporary}

    @staticmethod
    def _tenant_session(db, token, slug):
        Problem, digest, _, _ = primitives()
        row = db.execute(
            "SELECT a.*,s.csrf,c.body,c.status,c.archived_at,c.slug FROM tenant_sessions s "
            "JOIN tenant_admins a ON a.id=s.admin_id JOIN companies c ON c.id=a.company_id "
            "WHERE s.token_hash=? AND s.expires>? AND c.slug=?",
            (digest(token), time.time(), slug),
        ).fetchone()
        if not row or row["status"] == "suspended" or row["archived_at"] or (
            row["must_change"] and row["temporary_expires"] <= time.time()
        ):
            raise Problem(401, "unauthorized", "Требуется вход администратора компании")
        return row

    @staticmethod
    def _session_body(row, company, csrf):
        return {
            "user": {k: row[k] for k in ("id", "username", "display_name", "company_id")},
            "company": {k: company[k] for k in ("id", "name", "slug")},
            "must_change_password": bool(row["must_change"]), "csrf_token": csrf,
        }

    def _issue_session(self, db, row, company):
        _, digest, _, _ = primitives()
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = time.time() + 8 * 3600
        if row["must_change"]:
            expires = min(expires, row["temporary_expires"])
        db.execute("INSERT INTO tenant_sessions VALUES (?,?,?,?)", (
            digest(token), row["id"], csrf, expires,
        ))
        return token, self._session_body(row, company, csrf)

    @staticmethod
    def _rate_limit(db, peer):
        Problem, _, _, _ = primitives()
        now = time.time()
        keys = ["tenant:global", "tenant:peer:" + peer]
        for key in keys:
            row = db.execute("SELECT * FROM attempts WHERE key=?", (key,)).fetchone()
            if row and row["until"] > now and row["count"] >= (
                50 if key == "tenant:global" else 10
            ):
                raise Problem(429, "rate_limited", "Слишком много попыток. Повторите позже")
        return keys

    @staticmethod
    def _failed_attempt(db, keys):
        now = time.time()
        for key in keys:
            db.execute(
                "INSERT INTO attempts VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET "
                "count=CASE WHEN until<=? THEN 1 ELSE count+1 END,"
                "until=CASE WHEN until<=? THEN excluded.until ELSE until END",
                (key, now + 900, now, now),
            )

    def tenant_login(
        self, slug: str, username: str, password: str, peer: str, previous: str = ""
    ) -> tuple[str, dict]:
        Problem, digest, password_hash, _ = primitives()
        result = None
        with self.connect(True) as db:
            keys = self._rate_limit(db, peer)
            db.execute("DELETE FROM tenant_sessions WHERE expires<=?", (time.time(),))
            row = db.execute(
                "SELECT a.*,c.body,c.status,c.archived_at FROM tenant_admins a "
                "JOIN companies c ON c.id=a.company_id WHERE c.slug=? AND a.username=?",
                (slug, username.strip().casefold()),
            ).fetchone()
            hashed = password_hash(password, row["salt"] if row else "00" * 16)
            if (not row or not hmac.compare_digest(hashed, row["password_hash"])
                or row["status"] == "suspended" or row["archived_at"]
                or (row["must_change"] and row["temporary_expires"] <= time.time())):
                self._failed_attempt(db, keys)
            else:
                db.execute("DELETE FROM tenant_sessions WHERE token_hash=?", (digest(previous),))
                result = self._issue_session(db, row, json.loads(row["body"]))
        if result is None:
            raise Problem(401, "invalid_credentials",
                          "Неверный логин или пароль либо доступ закрыт")
        return result

    def tenant_session(self, token: str, slug: str) -> dict:
        with self.connect() as db:
            row = self._tenant_session(db, token, slug)
            return self._session_body(row, json.loads(row["body"]), row["csrf"])

    def tenant_logout(self, token: str) -> None:
        _, digest, _, _ = primitives()
        with self.connect(True) as db:
            db.execute("DELETE FROM tenant_sessions WHERE token_hash=?", (digest(token),))

    def tenant_password(
        self, token: str, slug: str, current: str, new: str, csrf: str, peer: str
    ) -> tuple[str, dict]:
        Problem, _, password_hash, stamp = primitives()
        result = None
        with self.connect(True) as db:
            row = self._tenant_session(db, token, slug)
            if not csrf_matches(csrf, row["csrf"]):
                raise Problem(403, "csrf_failed", "Обновите страницу")
            keys = self._rate_limit(db, peer)
            if not hmac.compare_digest(password_hash(current, row["salt"]), row["password_hash"]):
                self._failed_attempt(db, keys)
            else:
                if new == current or len(new) < 8 or len(new) > 1024:
                    raise Problem(422, "password_policy", "Нужен новый пароль от 8 символов")
                salt = secrets.token_hex(16)
                db.execute("UPDATE tenant_admins SET salt=?,password_hash=?,must_change=0,"
                           "temporary_expires=NULL WHERE id=?", (
                               salt, password_hash(new, salt), row["id"],
                           ))
                db.execute("DELETE FROM tenant_sessions WHERE admin_id=?", (row["id"],))
                db.execute("INSERT INTO tenant_events VALUES (?,?,?,?,?)", (
                    str(uuid4()), row["company_id"], row["id"], "password_changed", stamp(),
                ))
                updated = dict(row)
                updated["must_change"] = 0
                result = self._issue_session(db, updated, json.loads(row["body"]))
        if result is None:
            raise Problem(401, "invalid_credentials", "Неверный текущий пароль")
        return result

    def tenant_workspace(self, token: str, slug: str) -> dict:
        Problem, _, _, _ = primitives()
        with self.connect() as db:
            row = self._tenant_session(db, token, slug)
            if row["must_change"]:
                raise Problem(403, "password_change_required", "Сначала смените временный пароль")
            company = json.loads(row["body"])
            return {
                "company": {k: company[k] for k in ("id", "name", "slug", "modules")},
                "admin": {k: row[k] for k in ("id", "username", "display_name")},
                "mode": "local", "business_modules_ready": False,
            }
