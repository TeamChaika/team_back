"""Guest-only domains: tenant requests, platform verifies DNS/TLS/app binding."""

from uuid import uuid4

from pydantic import Field, StrictBool, StrictInt, field_validator

from .company_module_settings import CompanyModuleSettings
from .models import CompanyWrite, Model
from .repository import Problem, stamp


class PaymentDomainWrite(Model):
    expected_version: StrictInt = Field(ge=1)
    expected_revision: StrictInt = Field(ge=1)
    domain: str | None = None

    @field_validator("domain")
    @classmethod
    def valid_domain(cls, value):
        return CompanyWrite.domain_valid(value) if value else None


class PaymentDomainActivation(Model):
    expected_version: StrictInt = Field(ge=1)
    expected_revision: StrictInt = Field(ge=1)
    domain: str
    dns_verified: StrictBool
    tls_verified: StrictBool
    frontend_verified: StrictBool

    @field_validator("domain")
    @classmethod
    def valid_domain(cls, value):
        return CompanyWrite.domain_valid(value)


class PaymentDomains:
    def __init__(self, repo, platform_host=""):
        self.repo = repo
        self.settings = CompanyModuleSettings(repo)
        self.platform_host = platform_host

    @staticmethod
    def present(company, row):
        return {
            "company_version": company["version"],
            "revision": row["revision"] if row else 1,
            "domain": row["domain"] if row else None,
            "status": row["status"] if row else "unconfigured",
            "payment_origin": "https://" + row["domain"]
            if row and row["status"] == "active"
            else None,
        }

    @staticmethod
    def row(db, company_id):
        return db.execute(
            "SELECT * FROM company_payment_domains WHERE company_id=%s", (company_id,)
        ).fetchone()

    @staticmethod
    def lock(db, company_id):
        # Same lock order as company settings/provisioning. Auth refresh happens first.
        locked = db.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked",
            ("provision:" + str(company_id),),
        ).fetchone()
        if not locked["locked"]:
            raise Problem(409, "runtime_busy", "Подготовка ещё идёт. Повторите позже")
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended('payment-domain-registry',0))")

    def changed(self, db, company):
        values = self.settings._read(db, company["id"])
        values["integrations_revision"] = values.get("integrations_revision", 1) + 1
        self.settings._save(db, company["id"], values)
        from .integration_rollout import invalidate_integrations

        invalidate_integrations(db, company, values["integrations_revision"], ["payment_domain"])

    def get(self, slug, token):
        with self.repo.connect() as db:
            _, company = self.settings._tenant(db, slug, token)
            return {
                **self.present(company, self.row(db, company["id"])),
                "integrations_revision": self.settings._read(db, company["id"]).get(
                    "integrations_revision", 1
                ),
            }

    def request(self, slug, token, csrf, body):
        with self.repo.connect(True) as db:
            actor, company = self.settings._tenant(db, slug, token, csrf)
            self.lock(db, company["id"])
            company = self.repo._get(db, company["id"])
            row = self.row(db, company["id"])
            if (
                company["version"] != body.expected_version
                or (row["revision"] if row else 1) != body.expected_revision
            ):
                raise Problem(409, "version_conflict", "Настройки изменены. Обновите страницу")
            if (row["domain"] if row else None) == body.domain:
                return {
                    **self.present(company, row),
                    "integrations_revision": self.settings._read(db, company["id"]).get(
                        "integrations_revision", 1
                    ),
                }
            if body.domain:
                if (
                    self.platform_host in {body.domain, "api." + body.domain}
                    or body.domain == "api." + self.platform_host
                ):
                    raise Problem(409, "domain_in_use", "Домен уже используется")
                collision = db.execute(
                    "SELECT 1 FROM companies WHERE domain=ANY(%s) OR 'api.'||domain=ANY(%s) "
                    "UNION ALL SELECT 1 FROM company_payment_domains WHERE "
                    "(domain=ANY(%s) OR 'api.'||domain=ANY(%s)) AND company_id<>%s",
                    ([body.domain, "api." + body.domain],) * 4 + (company["id"],),
                ).fetchone()
                if collision:
                    raise Problem(409, "domain_in_use", "Домен уже используется")
            db.execute(
                "INSERT INTO company_payment_domains(company_id,domain,status,revision) "
                "VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(company_id) DO UPDATE SET domain=excluded.domain,"
                "status=excluded.status,"
                "revision=excluded.revision,verified_at=NULL,updated_at=now()",
                (
                    company["id"],
                    body.domain,
                    "pending" if body.domain else "unconfigured",
                    body.expected_revision + 1,
                ),
            )
            self.changed(db, company)
            action = "payment_domain_requested" if body.domain else "payment_domain_removed"
            if actor.get("_platform_owner"):
                self.repo._audit(db, company["id"], actor, action, ["payment_domain"])
            else:
                db.execute(
                    "INSERT INTO tenant_events(id,company_id,admin_id,action,created_at) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (str(uuid4()), company["id"], actor["id"], action, stamp()),
                )
            return {
                **self.present(company, self.row(db, company["id"])),
                "integrations_revision": self.settings._read(db, company["id"]).get(
                    "integrations_revision", 1
                ),
            }

    def activate(self, company_id, actor, body):
        with self.repo.connect(True) as db:
            self.repo._require_owner(db, actor)
            self.lock(db, company_id)
            company = self.repo._get(db, company_id)
            row = self.row(db, company_id)
            if (
                not row
                or company["version"] != body.expected_version
                or row["revision"] != body.expected_revision
                or row["domain"] != body.domain
            ):
                raise Problem(409, "version_conflict", "Настройки изменены. Обновите страницу")
            if not all((body.dns_verified, body.tls_verified, body.frontend_verified)):
                raise Problem(
                    422, "domain_not_verified", "Подтвердите DNS, TLS и гостевую страницу"
                )
            db.execute(
                "UPDATE company_payment_domains SET status='active',revision=revision+1,"
                "verified_at=now(),updated_at=now() WHERE company_id=%s",
                (company_id,),
            )
            self.changed(db, company)
            self.repo._audit(db, company_id, actor, "payment_domain_activated", ["payment_domain"])
            return self.present(company, self.row(db, company_id))


def mount_payment_domains(app, repo, owner_dependency, platform_host):
    from fastapi import Request

    service = PaymentDomains(repo, platform_host)

    @app.get("/api/saas-tenant/{slug}/payment-domain")
    def get(slug: str, request: Request):
        return service.get(slug, request.cookies.get("saas_tenant_session", ""))

    @app.post("/api/saas-tenant/{slug}/payment-domain")
    def save(slug: str, body: PaymentDomainWrite, request: Request):
        return service.request(
            slug,
            request.cookies.get("saas_tenant_session", ""),
            request.headers.get("x-csrf-token", ""),
            body,
        )

    @app.post("/api/saas-admin/companies/{company_id}/payment-domain/activate")
    def activate(company_id: str, body: PaymentDomainActivation, session=owner_dependency):
        return service.activate(company_id, session["user"], body)
