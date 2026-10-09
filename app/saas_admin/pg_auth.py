"""PostgreSQL BFF sessions backed exclusively by Supabase Auth."""

import json
import secrets
import time

from .repository import Problem, digest


class PostgresAuth:
    @staticmethod
    def _rate(db, peer, tenant=False):
        prefix = "tenant:" if tenant else "owner:"
        keys = [prefix + "global", prefix + "peer:" + peer]
        # Lock fixed global row before peer to serialize counters across workers.
        for key in keys:
            db.execute("INSERT INTO attempts VALUES (%s,0,0) ON CONFLICT DO NOTHING", (key,))
            row = db.execute("SELECT * FROM attempts WHERE key=%s FOR UPDATE", (key,)).fetchone()
            if row["until"] > time.time() and row["count"] >= (
                50 if key.endswith(":global") else 10
            ):
                raise Problem(429, "rate_limited", "Слишком много попыток. Повторите позже")
        return keys

    @staticmethod
    def _failed(db, keys):
        now = time.time()
        for key in keys:
            db.execute(
                "UPDATE attempts SET count=CASE WHEN until<=%s THEN 1 ELSE count+1 END,"
                "until=CASE WHEN until<=%s THEN %s ELSE until END WHERE key=%s",
                (now, now, now + 900, key),
            )

    def _issue(self, db, row, tokens, tenant=False):
        table, field = ("tenant_sessions", "admin_id") if tenant else ("sessions", "owner_id")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = time.time() + 8 * 3600
        if tenant and row["must_change"]:
            expires = min(expires, row["temporary_expires"])
        db.execute(
            f"INSERT INTO {table} (token_hash,{field},csrf,expires,tokens_ciphertext) VALUES "
            f"(%s,%s,%s,%s,%s)",
            (digest(token), row["id"], csrf, expires, self.vault.encrypt(json.dumps(tokens))),
        )
        return token, csrf

    def _verified(self, db, token, tenant=False, slug=None, rate_peer=None, token_hash=None):
        hashed = token_hash or digest(token)
        table, members, field = (
            ("tenant_sessions", "memberships", "admin_id")
            if tenant
            else ("sessions", "platform_memberships", "owner_id")
        )
        if rate_peer is not None:
            self._rate(db, rate_peer, True)
        if tenant:
            # All tenant operations lock company, membership, then session.
            scope = db.execute(
                "SELECT m.company_id,m.id FROM tenant_sessions s JOIN memberships m ON "
                "m.id=s.admin_id WHERE s.token_hash=%s",
                (hashed,),
            ).fetchone()
            if not scope:
                raise Problem(401, "unauthorized", "Требуется вход")
            db.execute("SELECT id FROM companies WHERE id=%s FOR SHARE", (scope["company_id"],))
            db.execute("SELECT id FROM memberships WHERE id=%s FOR UPDATE", (scope["id"],))
        session = db.execute(
            f"SELECT * FROM {table} WHERE token_hash=%s AND expires>%s FOR UPDATE",
            (hashed, time.time()),
        ).fetchone()
        if not session:
            raise Problem(401, "unauthorized", "Требуется вход")
        row = db.execute(
            f"SELECT * FROM {members} WHERE id=%s AND active=true", (session[field],)
        ).fetchone()
        if (
            not row
            or not row["auth_user_id"]
            or (tenant and row["role"] not in {"company_admin", "employee"})
        ):
            raise Problem(401, "unauthorized", "Доступ закрыт")
        if tenant:
            company = db.execute(
                "SELECT body FROM companies WHERE id=%s AND slug=%s AND status<>%s AND "
                "archived_at IS NULL FOR SHARE",
                (row["company_id"], slug, "suspended"),
            ).fetchone()
            if not company or (
                row["must_change"] and (row["temporary_expires"] or 0) <= time.time()
            ):
                raise Problem(401, "unauthorized", "Доступ закрыт")
            row["body"] = company["body"]
        try:
            tokens = json.loads(self.vault.decrypt(session["tokens_ciphertext"]))
        except Exception:
            raise Problem(401, "unauthorized", "Требуется повторный вход") from None
        if tokens["expires_at"] <= time.time() + 30:
            # Serialized by the session row lock; never retry an ambiguous rotation.
            try:
                tokens = self.auth.refresh(tokens["refresh_token"])
                if tokens["expires_at"] <= time.time() + 30:
                    raise Problem(401, "unauthorized", "Требуется повторный вход")
            except Problem:
                db.execute(f"DELETE FROM {table} WHERE token_hash=%s", (hashed,))
                # Caller must commit this invalidation before propagating.
                return None
            db.execute(
                f"UPDATE {table} SET tokens_ciphertext=%s WHERE token_hash=%s",
                (self.vault.encrypt(json.dumps(tokens)), hashed),
            )
            # Persist rotation before later authorization/CSRF checks can roll back.
            db.commit()
            db.execute("SET LOCAL search_path TO restcontrol,pg_catalog")
            db.execute("SET LOCAL statement_timeout='30s'")
            db.execute("SET LOCAL lock_timeout='5s'")
            return self._verified(db, token, tenant, slug, rate_peer, token_hash=hashed)
        user = self.auth.user(tokens["access_token"])
        if str(user.get("id")) != str(row["auth_user_id"]) or str(tokens["user_id"]) != str(
            row["auth_user_id"]
        ):
            raise Problem(401, "unauthorized", "Доступ закрыт")
        row["csrf"], row["_tokens"] = session["csrf"], tokens
        return row

    @staticmethod
    def _owner_body(row, csrf):
        return {
            "user": {k: str(row[k]) for k in ("id", "username", "display_name")},
            "csrf_token": csrf,
        }

    def login(self, username, password, peer):
        result = None
        with self.connect(True) as db:
            keys = self._rate(db, peer)
            row = db.execute(
                "SELECT * FROM platform_memberships WHERE lower(username)=%s AND active=true",
                (username.strip().casefold(),),
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
            if valid:
                token, csrf = self._issue(db, row, tokens)
                result = token, self._owner_body(row, csrf)
            else:
                self._failed(db, keys)
        if result is None:
            raise Problem(
                401, "invalid_credentials", "Неверный логин или пароль либо доступ закрыт"
            )
        return result

    def session(self, token):
        with self.connect(True) as db:
            row = self._verified(db, token)
            result = self._owner_body(row, row["csrf"]) if row else None
        if result is None:
            raise Problem(401, "unauthorized", "Требуется повторный вход")
        return result

    def logout(self, token):
        with self.connect(True) as db:
            db.execute("DELETE FROM sessions WHERE token_hash=%s", (digest(token),))
