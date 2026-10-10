"""Owner-only durable provisioning requests; HTTP never starts tenant processes."""

from contextlib import nullcontext
from ipaddress import ip_address
from pathlib import Path
from uuid import UUID

from fastapi import Request
from pydantic import Field, StrictInt

from .models import CompanyWrite, Model
from .provisioning import STEPS
from .provisioning_acceptance import OwnerAcceptance
from .repository import Problem
from .runtime_registry import ReadyRuntime

BASE = "/api/saas-admin/companies/{company_id}/provisioning"


class ProvisioningWrite(Model):
    expected_version: StrictInt = Field(ge=1)


def public_targets(targets):
    """Normalize the explicit public gateway target; never infer it from DNS.

    Legacy split-origin manifests keep working: their API target is the gateway,
    while their old frontend target points at static hosting and must not be shown.
    """
    result = {}
    for key, target in (targets or {}).items():
        if key not in ("edge", "frontend", "api") or set(target) != {"type", "value"}:
            raise ValueError("Invalid public DNS target")
        kind, value = target["type"], target["value"]
        if kind in ("A", "AAAA"):
            address = ip_address(value)
            if not address.is_global or address.version != (4 if kind == "A" else 6):
                raise ValueError("DNS target must be public")
        elif kind == "CNAME":
            canonical = CompanyWrite.domain_valid(value)
            if canonical != value:
                raise ValueError("CNAME target must be a canonical hostname")
            if not value:
                raise ValueError("DNS target is required")
        else:
            raise ValueError("Invalid public DNS record type")
        result[key] = {"type": kind, "value": value}
    if not result or set(result) == {"edge"}:
        return result
    if set(result) == {"frontend", "api"}:
        return {"edge": result["api"]}
    raise ValueError("Provide one edge target or the complete legacy frontend/API pair")


class ProvisioningRequests:
    def __init__(self, repository, runtime_root=None, dns_targets=None, acceptance_root=None):
        self.repository = repository
        self.root = Path(runtime_root) if runtime_root else None
        if self.root and (not self.root.is_absolute() or ".." in self.root.parts):
            raise ValueError("Private runtime root must be absolute")
        self.acceptance_root = Path(acceptance_root) if acceptance_root else None
        if self.acceptance_root and (
            not self.acceptance_root.is_absolute()
            or ".." in self.acceptance_root.parts
            or (self.root and self.acceptance_root.resolve().is_relative_to(self.root.resolve()))
        ):
            raise ValueError("Acceptance storage must be absolute and outside tenant runtime")
        self.targets = public_targets(dns_targets)

    def present(self, company, row):
        checks = row.get("checks", {}) if row else {}
        current = bool(row and row["configuration_version"] == company["version"])
        ready = (
            current
            and row["state"] == "ready"
            and all(
                isinstance(checks.get(step), dict)
                and checks[step].get("ok") is True
                and bool(checks[step].get("evidence"))
                for step in STEPS
            )
        )
        domain = company.get("domain")
        records = [
            {"name": domain, **target}
            for target in self.targets.values()
            if domain
        ]
        return {
            "company_id": str(company["id"]),
            "company_version": company["version"],
            "state": "ready"
            if ready
            else (row["state"] if current and row["state"] != "ready" else "not_started"),
            "ready": ready,
            "configured": bool(self.root and hasattr(self.repository, "_require_owner")),
            "step": row.get("step") if current else None,
            "updated_at": row.get("updated_at") if row else None,
            "error_code": row.get("error_code") if current else None,
            "steps": [
                {
                    "id": step,
                    "completed": current
                    and isinstance(checks.get(step), dict)
                    and checks[step].get("ok") is True
                    and bool(checks[step].get("evidence")),
                }
                for step in STEPS
            ],
            "dns_records": records,
            "dns_configured": bool(domain and len(records) == 1),
            "terminals": {
                "state": "not_verified",
                "message": "Готовность терминалов ещё не подтверждена",
            },
        }

    def status(self, company_id):
        company = self.repository.get(company_id)
        if not hasattr(self.repository, "_require_owner"):
            return self.present(company, None)
        with self.repository.connect() as db:
            row = db.execute(
                "SELECT configuration_version,state,step,checks,error_code,updated_at "
                "FROM runtime_provisioning WHERE company_id=%s",
                (company["id"],),
            ).fetchone()
        return self.present(company, row)

    def enqueue(self, company_id, expected_version, actor, retry=False, *, owner_token=None):
        if not self.root or not hasattr(self.repository, "_require_owner"):
            raise Problem(503, "provisioning_unconfigured", "Оператор запуска не настроен")
        acceptance = nullcontext(None)
        if self.acceptance_root:
            company = self.repository.get(company_id)
            if company["version"] != expected_version:
                raise Problem(409, "version_conflict", "Компания изменена. Обновите карточку")
            acceptance = OwnerAcceptance(
                self.repository, self.acceptance_root, company, owner_token
            )
        with acceptance as captured:
            return self._enqueue(company_id, expected_version, actor, retry, captured)

    def _enqueue(self, company_id, expected_version, actor, retry, acceptance):
        if not self.root or not hasattr(self.repository, "_require_owner"):
            raise Problem(503, "provisioning_unconfigured", "Оператор запуска не настроен")
        with self.repository.connect(True) as db:
            self.repository._require_owner(db, actor)
            company = self.repository._get(db, company_id)
            if company["version"] != expected_version:
                raise Problem(409, "version_conflict", "Компания изменена. Обновите карточку")
            locked = db.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked",
                ("provision:" + str(company["id"]),),
            ).fetchone()["locked"]
            if not locked:
                raise Problem(409, "provisioning_busy", "Запуск компании уже выполняется")
            row = db.execute(
                "SELECT * FROM runtime_provisioning WHERE company_id=%s FOR UPDATE",
                (company["id"],),
            ).fetchone()
            current = bool(row and row["configuration_version"] == expected_version)
            if current and row["state"] in ("pending", "running", "ready"):
                return self.present(company, row)
            if current and row["state"] == "failed" and not retry:
                raise Problem(409, "retry_required", "Используйте повтор запуска")
            socket_path = str(self.root / ("c_" + UUID(str(company["id"])).hex) / "portal.sock")
            ReadyRuntime(str(company["id"]), expected_version, socket_path)
            if acceptance:
                acceptance.publish()
            row = db.execute(
                "INSERT INTO runtime_provisioning(company_id,configuration_version,socket_path) "
                "VALUES(%s,%s,%s) ON CONFLICT(company_id) DO UPDATE SET "
                "configuration_version=excluded.configuration_version, "
                "socket_path=excluded.socket_path, "
                "checks=CASE WHEN "
                "runtime_provisioning.configuration_version=excluded.configuration_version "
                "THEN runtime_provisioning.checks ELSE '{}'::jsonb END, "
                "state='pending',step=NULL,error_code=NULL,updated_at=now() "
                "RETURNING *",
                (company["id"], expected_version, socket_path),
            ).fetchone()
            return self.present(company, row)


def mount_provisioning_routes(app, service, owner_dependency):
    @app.get(BASE)
    def status(company_id: str, session=owner_dependency):
        return service.status(company_id)

    @app.post(BASE + "/start", status_code=202)
    def start(company_id: str, body: ProvisioningWrite, request: Request, session=owner_dependency):
        return service.enqueue(
            company_id,
            body.expected_version,
            session["user"],
            owner_token=request.cookies.get("saas_owner_session"),
        )

    @app.post(BASE + "/retry", status_code=202)
    def retry(company_id: str, body: ProvisioningWrite, request: Request, session=owner_dependency):
        return service.enqueue(
            company_id,
            body.expected_version,
            session["user"],
            retry=True,
            owner_token=request.cookies.get("saas_owner_session"),
        )
