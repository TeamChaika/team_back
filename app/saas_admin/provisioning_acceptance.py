"""Short-lived real owner delegation, retained outside the tenant runtime."""

import os
import secrets
import tempfile
import time
from pathlib import Path
from uuid import UUID

from .platform_sso import pkce_challenge
from .repository import Problem, digest


class OwnerAcceptance:
    def __init__(self, repository, root, company, parent):
        self.repo = repository
        self.root = Path(root)
        self.path = self.root / ("c_" + UUID(str(company["id"])).hex + ".acceptance")
        self.company = company
        self.parent = parent
        self.token = None
        self.previous = None
        self.published = False

    def __enter__(self):
        if not self.parent:
            raise Problem(401, "unauthorized", "Требуется вход владельца")
        verifier, state, nonce = [secrets.token_urlsafe(32) for _ in range(3)]
        grant = self.repo.authorize_platform(
            self.parent, self.company["id"], state, nonce, pkce_challenge(verifier)
        )
        frontend, api = self.repo._targets(self.company)
        self.token, _ = self.repo.exchange_platform(
            grant["code"], verifier, state, nonce, self.company["slug"], frontend, api
        )
        try:
            with self.repo.connect(True) as db:
                row = db.execute(
                    "UPDATE platform_tenant_sessions SET expires=LEAST(expires,%s) "
                    "WHERE token_hash=%s AND parent_hash=%s AND company_id=%s RETURNING token_hash",
                    (
                        time.time() + 1800,
                        digest(self.token),
                        digest(self.parent),
                        self.company["id"],
                    ),
                ).fetchone()
                if not row:
                    raise Problem(401, "unauthorized", "Требуется повторный вход владельца")
        except BaseException:
            self.repo.tenant_logout(self.token)
            raise
        return self

    def _write(self, token):
        descriptor, temporary = tempfile.mkstemp(prefix=".acceptance-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write(token + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def publish(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode & 0o077:
            raise Problem(503, "acceptance_storage_invalid", "Закрытое хранилище не настроено")
        if self.path.is_symlink():
            raise Problem(503, "acceptance_storage_invalid", "Закрытое хранилище не настроено")
        if self.path.exists():
            if not self.path.is_file() or self.path.stat().st_mode & 0o077:
                raise Problem(503, "acceptance_storage_invalid", "Закрытое хранилище не настроено")
            self.previous = self.path.read_text().strip()
        self._write(self.token)
        self.published = True

    def __exit__(self, kind, value, traceback):
        try:
            if kind is not None and self.published:
                if self.previous:
                    self._write(self.previous)
                else:
                    self.path.unlink(missing_ok=True)
            if kind is None and self.published and self.previous:
                self.repo.tenant_logout(self.previous)
        finally:
            if kind is not None or not self.published:
                self.repo.tenant_logout(self.token)


def renew_owner_acceptance(repository, company, token):
    """Renew only a central worker's modules-stage handle against its live parent.

    The caller must select this token from canonical central acceptance storage.
    No parent credential is reconstructed, stored, or returned.
    """
    renewed = None
    with repository.connect(True) as db:
        current = repository._get(db, company["id"])
        targets = repository._targets(current)
        if (
            current["version"] != company["version"]
            or current["slug"] != company["slug"]
            or repository._targets(company) != targets
        ):
            raise Problem(409, "version_conflict", "Компания изменена. Повторите запуск")
        handle = db.execute(
            "SELECT * FROM platform_tenant_sessions WHERE token_hash=%s AND company_id=%s",
            (digest(token), company["id"]),
        ).fetchone()
        if not handle or (handle["frontend_origin"], handle["api_origin"]) != targets:
            raise Problem(401, "unauthorized", "Требуется новый запуск владельцем")
        owner = repository._verified_hash(db, handle["parent_hash"])
        if owner is not None:
            # Auth refresh can commit; recheck saved scope and stage afterwards.
            current = repository._get(db, company["id"])
            if (
                current["version"] != company["version"]
                or current["slug"] != company["slug"]
                or repository._targets(current) != targets
            ):
                raise Problem(409, "version_conflict", "Компания изменена. Повторите запуск")
            job = db.execute(
                "SELECT 1 FROM runtime_provisioning WHERE company_id=%s "
                "AND configuration_version=%s AND state='running' AND step='modules' FOR UPDATE",
                (company["id"], company["version"]),
            ).fetchone()
            if not job:
                raise Problem(409, "acceptance_stage_required", "Проверка компании ещё не запущена")
            parent = db.execute(
                "SELECT expires FROM sessions WHERE token_hash=%s AND expires>%s",
                (handle["parent_hash"], time.time()),
            ).fetchone()
            if parent:
                row = db.execute(
                    "UPDATE platform_tenant_sessions SET expires=%s "
                    "WHERE token_hash=%s AND parent_hash=%s AND company_id=%s "
                    "AND frontend_origin=%s AND api_origin=%s RETURNING expires",
                    (
                        min(parent["expires"], time.time() + 1800),
                        digest(token),
                        handle["parent_hash"],
                        company["id"],
                        *targets,
                    ),
                ).fetchone()
                renewed = row["expires"] if row else None
    if renewed is None:
        raise Problem(401, "unauthorized", "Требуется новый запуск владельцем")
    return renewed
