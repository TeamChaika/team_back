"""Write-only connection secrets, versioned changes, and sanitized check records."""

import hashlib
import json
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .models import metadata_url

EMPTY_CHECK = {"status": "not_checked", "code": None, "message": None, "checked_at": None}


def problem(status, code, message, field=None):
    from .repository import Problem

    raise Problem(status, code, message, field)


def configured(company, enabled_only=False):
    items = {"chain": company["chain_url"]} if company["chain_url"] else {}
    items.update(
        {
            str(item["id"]): item["url"]
            for item in company["rms"]
            if not enabled_only or item["enabled"]
        }
    )
    return items


class ConnectionRepository:
    def apply_credentials(self, db, company, old, credentials):
        desired = configured(company) if not company["archived_at"] else {}
        previous = configured(old) if old else {}
        credentials = credentials or {}
        if set(credentials) - set(desired):
            problem(422, "validation_error", "Подключение не найдено", "connection_credentials")
        rows = {
            r["connection_id"]: r
            for r in db.execute("SELECT * FROM connections WHERE company_id=?", (company["id"],))
        }
        changed = False
        for connection_id, url in desired.items():
            row = rows.get(connection_id)
            incoming = credentials.get(connection_id)
            if incoming is None:
                if previous.get(connection_id) != url:
                    problem(
                        422,
                        "credentials_required",
                        "Укажите логин и пароль подключения",
                        "connection_credentials." + connection_id,
                    )
                continue
            login = incoming["login"]
            password = incoming.get("password")
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
                """INSERT INTO connections VALUES (?,?,?,?,?)
                ON CONFLICT(company_id,connection_id) DO UPDATE SET
                url=excluded.url,ciphertext=excluded.ciphertext,check_json=excluded.check_json""",
                (
                    company["id"],
                    connection_id,
                    url,
                    self.vault.encrypt(json.dumps(payload)),
                    json.dumps(EMPTY_CHECK),
                ),
            )
            changed = True
        for key in rows:
            if key not in desired:
                db.execute(
                    "DELETE FROM connections WHERE company_id=? AND connection_id=?",
                    (company["id"], key),
                )
                changed = True
        return changed

    def present(self, db, company):
        from .models import derived

        company = derived(company)
        urls = configured(company, enabled_only=True)
        if not urls:
            company["integration_state"] = "not_configured"
            return company
        rows = {
            row["connection_id"]: row
            for row in db.execute(
                "SELECT connection_id,url,check_json FROM connections WHERE company_id=?",
                (company["id"],),
            )
        }
        states = [
            json.loads(rows[key]["check_json"])["status"]
            if key in rows and rows[key]["url"] == url
            else "not_checked"
            for key, url in urls.items()
        ]
        company["integration_state"] = (
            "failed" if "failed" in states else "not_checked" if "not_checked" in states else "ok"
        )
        return company

    def connections(self, company_id):
        with self.connect() as db:
            company = self._get(db, company_id)
            rows = {
                r["connection_id"]: r
                for r in db.execute("SELECT * FROM connections WHERE company_id=?", (company_id,))
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
                        "check": json.loads(row["check_json"]) if row else dict(EMPTY_CHECK),
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
                "SELECT * FROM connections WHERE company_id=? AND connection_id=?",
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
        canonical_target = f"{parsed.hostname}:{parsed.port or 443}/resto/api"
        target = hashlib.sha256(canonical_target.encode()).hexdigest()
        with self.connect(True) as db:
            cooldown = db.execute(
                "SELECT until FROM check_targets WHERE target=?", (target,)
            ).fetchone()
            if cooldown and cooldown["until"] > now:
                problem(
                    429, "rate_limited", "Повторная проверка этого адреса доступна через минуту"
                )
            db.execute(
                "INSERT INTO check_targets VALUES (?,?) ON CONFLICT(target) "
                "DO UPDATE SET until=excluded.until",
                (target, now + 60),
            )
            row = db.execute(
                "SELECT * FROM check_attempts WHERE owner_id=?", (owner_id,)
            ).fetchone()
            if row and row["until"] > now and row["count"] >= 10:
                problem(429, "rate_limited", "Не более 10 проверок в минуту")
            db.execute(
                """INSERT INTO check_attempts VALUES (?,1,?) ON CONFLICT(owner_id)
                DO UPDATE SET count=CASE WHEN until<=? THEN 1 ELSE count+1 END,
                until=CASE WHEN until<=? THEN excluded.until ELSE until END""",
                (owner_id, now + 60, now, now),
            )

    def record_check(self, company_id, connection_id, expected, check, actor):
        from .repository import stamp

        with self.connect(True) as db:
            company = self._get(db, company_id)
            if company["version"] != expected:
                problem(
                    409, "version_conflict", "Настройки изменены: результат проверки не сохранён"
                )
            db.execute(
                "UPDATE connections SET check_json=? WHERE company_id=? AND connection_id=?",
                (json.dumps(check), company_id, connection_id),
            )
            company["version"] += 1
            company["updated_at"] = stamp()
            db.execute(
                "UPDATE companies SET version=?,body=? WHERE id=?",
                (company["version"], json.dumps(company, ensure_ascii=False), company_id),
            )
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    company_id,
                    actor["id"],
                    actor["display_name"],
                    "connection_checked",
                    json.dumps(["connection_check." + connection_id]),
                    company["updated_at"],
                ),
            )
            row = db.execute(
                "SELECT * FROM connections WHERE company_id=? AND connection_id=?",
                (company_id, connection_id),
            ).fetchone()
            secret = json.loads(self.vault.decrypt(row["ciphertext"]))
            item = {
                "id": connection_id,
                "url": row["url"],
                "login": secret["login"],
                "password_set": True,
                "check": dict(check),
            }
            snapshot = {"company_version": company["version"], "connection": item}
        return snapshot
