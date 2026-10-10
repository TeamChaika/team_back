"""One-use PKCE grants and delegated opaque handles; no tenant owner accounts."""

import base64
import hashlib
import secrets
import time
from uuid import UUID, uuid4

from app.tenancy.actor import ActorContext

from .repository import Problem, digest, stamp


def pkce_challenge(verifier):
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode()
    )


class PlatformSSO:
    @staticmethod
    def _targets(company):
        if not company.get("domain") or company["archived_at"] or company["status"] == "suspended":
            raise Problem(403, "sso_target_closed", "Домен компании недоступен")
        return "https://" + company["domain"], "https://api." + company["domain"]

    @staticmethod
    def _platform_event(db, row, action):
        db.execute(
            "INSERT INTO platform_tenant_events VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                str(uuid4()),
                row["company_id"],
                row["auth_user_id"],
                row["display_name"],
                action,
                None,
                "success",
                stamp(),
            ),
        )

    def authorize_platform(self, parent, company_id, state, nonce, challenge):
        result = None
        with self.connect(True) as db:
            row = self._verified(db, parent)
            if row:
                company = self._get(db, company_id)
                frontend, api = self._targets(company)
                code = secrets.token_urlsafe(32)
                db.execute("DELETE FROM platform_sso_codes WHERE expires<=%s", (time.time(),))
                db.execute(
                    "INSERT INTO platform_sso_codes VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        digest(code),
                        digest(parent),
                        company["id"],
                        frontend,
                        api,
                        state,
                        nonce,
                        challenge,
                        time.time() + 60,
                    ),
                )
                result = {"code": code, "state": state, "nonce": nonce, "frontend_origin": frontend}
        if result is None:
            raise Problem(401, "unauthorized", "Требуется вход владельца")
        return result

    def exchange_platform(self, code, verifier, state, nonce, slug, frontend_origin, api_origin):
        result = None
        with self.connect(True) as db:
            # Company -> parent -> child/code is the common lock order.
            grant = db.execute(
                "SELECT * FROM platform_sso_codes WHERE code_hash=%s", (digest(code),)
            ).fetchone()
            if not grant:
                raise Problem(401, "invalid_sso_code", "Повторите вход владельца")
            company = self._get(db, grant["company_id"])
            frontend, api = self._targets(company)
            if (
                company["slug"] != slug
                or frontend != frontend_origin
                or api_origin not in (frontend, api)
                or frontend != grant["frontend_origin"]
                or api != grant["api_origin"]
                or not secrets.compare_digest(grant["state"], state)
                or not secrets.compare_digest(grant["nonce"], nonce)
                or not secrets.compare_digest(grant["challenge"], pkce_challenge(verifier))
            ):
                raise Problem(403, "invalid_sso_target", "Неверный адрес или проверка входа")
            owner = self._verified_hash(db, grant["parent_hash"])
            if owner:
                # Refresh may commit and release locks; reread the exact target afterwards.
                current_company = self._get(db, grant["company_id"])
                if current_company["slug"] != slug or self._targets(current_company) != (
                    frontend,
                    api,
                ):
                    raise Problem(403, "invalid_sso_target", "Домен компании изменён")
                company = current_company
                grant = db.execute(
                    "DELETE FROM platform_sso_codes WHERE code_hash=%s AND expires>%s RETURNING *",
                    (digest(code), time.time()),
                ).fetchone()
                if not grant:
                    raise Problem(401, "invalid_sso_code", "Повторите вход владельца")
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                parent = db.execute(
                    "SELECT expires FROM sessions WHERE token_hash=%s", (grant["parent_hash"],)
                ).fetchone()
                db.execute(
                    "INSERT INTO platform_tenant_sessions VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        digest(token),
                        grant["parent_hash"],
                        company["id"],
                        frontend,
                        api,
                        csrf,
                        min(parent["expires"], time.time() + 8 * 3600),
                    ),
                )
                row = {
                    **owner,
                    "company_id": company["id"],
                    "body": company,
                    "must_change": False,
                    "csrf": csrf,
                    "_platform_owner": True,
                }
                self._platform_event(db, row, "session_entered")
                result = token, self._tenant_body(row, csrf)
        if result is None:
            raise Problem(401, "unauthorized", "Требуется повторный вход владельца")
        return result

    def _verified_hash(self, db, token_hash):
        return self._verified(db, "", token_hash=token_hash)

    def _platform_tenant_verified(self, db, token, slug):
        handle = db.execute(
            "SELECT * FROM platform_tenant_sessions WHERE token_hash=%s", (digest(token),)
        ).fetchone()
        if not handle:
            return None
        company = db.execute(
            "SELECT body FROM companies WHERE id=%s AND slug=%s "
            "AND archived_at IS NULL AND status<>%s FOR SHARE",
            (handle["company_id"], slug, "suspended"),
        ).fetchone()
        if not company:
            raise Problem(401, "unauthorized", "Доступ компании закрыт")
        frontend, api = self._targets(company["body"])
        if handle["frontend_origin"] != frontend or handle["api_origin"] != api:
            raise Problem(401, "unauthorized", "Домен компании изменён")
        owner = self._verified_hash(db, handle["parent_hash"])
        if not owner:
            return None
        company = db.execute(
            "SELECT body FROM companies WHERE id=%s AND slug=%s "
            "AND archived_at IS NULL AND status<>%s FOR SHARE",
            (handle["company_id"], slug, "suspended"),
        ).fetchone()
        if not company or self._targets(company["body"]) != (frontend, api):
            raise Problem(401, "unauthorized", "Домен компании изменён")
        current = db.execute(
            "SELECT csrf FROM platform_tenant_sessions WHERE token_hash=%s "
            "AND expires>%s FOR UPDATE",
            (digest(token), time.time()),
        ).fetchone()
        if not current:
            raise Problem(401, "unauthorized", "Требуется повторный вход")
        return {
            **owner,
            "company_id": handle["company_id"],
            "body": company["body"],
            "must_change": False,
            "csrf": current["csrf"],
            "_platform_owner": True,
        }

    def tenant_actor_session(self, token, company_id):
        with self.connect(True) as db:
            company = self._get(db, company_id)
            row = self._tenant_verified(db, token, company["slug"])
            result = (self._actor(row), self._tenant_body(row, row["csrf"])) if row else None
        if result is None:
            raise Problem(401, "unauthorized", "Требуется вход")
        return result

    def tenant_actor(self, token, company_id):
        return self.tenant_actor_session(token, company_id)[0]

    @staticmethod
    def _actor(row):
        return ActorContext(
            UUID(str(row["company_id"])),
            UUID(str(row["auth_user_id"])),
            "platform_owner" if row.get("_platform_owner") else "company_member",
            row["display_name"],
            None if row.get("_platform_owner") else UUID(str(row["id"])),
        )

    def authorize_platform_actor(self, company_id, auth_user_id):
        """Worker revalidation; a stored audit snapshot alone grants no access."""
        with self.connect() as db:
            row = db.execute(
                "SELECT display_name FROM platform_memberships WHERE auth_user_id=%s AND active",
                (str(auth_user_id),),
            ).fetchone()
            company = db.execute(
                "SELECT id FROM companies WHERE id=%s AND archived_at IS NULL AND status='active'",
                (str(company_id),),
            ).fetchone()
        if not row or not company:
            return None
        return ActorContext(
            UUID(str(company_id)), UUID(str(auth_user_id)), "platform_owner", row["display_name"]
        )

    def _tenant_verified(self, db, token, slug):
        if db.execute(
            "SELECT 1 FROM platform_tenant_sessions WHERE token_hash=%s", (digest(token),)
        ).fetchone():
            return self._platform_tenant_verified(db, token, slug)
        return self._verified(db, token, True, slug)
