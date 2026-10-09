"""Private process registry. Routing is enabled only by durable acceptance evidence."""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx

REQUIRED_CHECKS = frozenset(
    {
        "migrations",
        "database_roles",
        "identity",
        "connections",
        "initial_sync",
        "modules",
        "payments",
        "dns_tls",
        "runtime_health",
    }
)


SETUP_CHECKS = frozenset(
    {"migrations", "database_roles", "identity", "connections", "initial_sync"}
)

WORKING_CHECKS = REQUIRED_CHECKS - {"modules", "payments"}


def completed(checks, required):
    from .initial_sync_plan import initial_sync_compatible

    return all(
        isinstance(checks.get(step), dict)
        and checks[step].get("ok") is True
        and bool(checks[step].get("evidence"))
        and (step != "initial_sync" or initial_sync_compatible(checks[step]))
        for step in required
    )


@dataclass(frozen=True)
class ReadyRuntime:
    company_id: str
    configuration_version: int
    socket_path: str

    def __post_init__(self):
        company = UUID(self.company_id)
        path = Path(self.socket_path)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or path.parent.name != f"c_{company.hex}"
            or path.name != "portal.sock"
        ):
            raise ValueError("Runtime must use its company-owned private portal socket")


class RuntimeRegistry:
    def __init__(self, repository):
        self.repository = repository

    def resolve(self, company):
        if company.get("archived_at") or company.get("status") != "active":
            return None
        with self.repository.connect() as db:
            row = db.execute(
                "SELECT configuration_version, socket_path, checks, state "
                "FROM runtime_provisioning WHERE company_id=%s",
                (company["id"],),
            ).fetchone()
        if not row or row["state"] != "ready" or row["configuration_version"] != company["version"]:
            return None
        if not completed(row["checks"], REQUIRED_CHECKS):
            return None
        return ReadyRuntime(str(company["id"]), row["configuration_version"], row["socket_path"])

    def _working_row(self, company):
        if company.get("archived_at") or company.get("status") != "active":
            return None
        with self.repository.connect() as db:
            row = db.execute(
                "SELECT configuration_version,socket_path,checks FROM runtime_provisioning "
                "WHERE company_id=%s",
                (company["id"],),
            ).fetchone()
        if not row or row["configuration_version"] != company["version"]:
            return None
        return row

    def resolve_working(self, company):
        """Current verified foundation; this does not establish any module's readiness."""
        row = self._working_row(company)
        if row is None or not completed(row["checks"], WORKING_CHECKS):
            return None
        return ReadyRuntime(str(company["id"]), row["configuration_version"], row["socket_path"])

    def feature_readiness(self, company):
        from .feature_readiness import feature_readiness

        row = self._working_row(company)
        checks = row["checks"] if row else {}
        return feature_readiness(company, checks, working=completed(checks, WORKING_CHECKS))

    def working_status(self, company):
        from .feature_readiness import feature_readiness

        row = self._working_row(company)
        checks = row["checks"] if row else {}
        working = completed(checks, WORKING_CHECKS)
        return {
            "working_dashboard_available": working,
            "feature_readiness": feature_readiness(company, checks, working=working),
        }

    @contextmanager
    def payment_configuration_change(self, company):
        """Serialize a terminal mutation with provisioning, revoke before forwarding.

        The session lock survives the revocation commit and is held until the
        private portal has finished its mutation. Failed requests remain revoked.
        """
        from .repository import Problem

        with self.repository.connect(True) as db:
            key = "provision:" + str(company["id"])
            locked = db.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked", (key,)
            ).fetchone()["locked"]
            if not locked:
                raise Problem(
                    503, "provisioning_busy", "Проверка компании выполняется; повторите позже"
                )
            try:
                revoked = db.execute(
                    "UPDATE runtime_provisioning SET checks=checks-'payments',state='failed',"
                    "step='payments',error_code='payment_configuration_changed',updated_at=now() "
                    "WHERE company_id=%s AND configuration_version=%s RETURNING company_id",
                    (company["id"], company["version"]),
                ).fetchone()
                if not revoked:
                    raise Problem(409, "company_version_changed", "Настройки компании изменились")
                db.commit()
                yield
            finally:
                db.rollback()
                db.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))

    def resolve_existing(self, company):
        """Previously accepted process for account recovery and persisted payment reads.

        It never permits new payment creation or general business mutations. Exact
        current domain and operation capability remain enforced at gateway/portal.
        """
        if company.get("archived_at") or company.get("status") != "active":
            return None
        with self.repository.connect() as db:
            row = db.execute(
                "SELECT active_socket_path,active_version FROM runtime_provisioning "
                "WHERE company_id=%s",
                (company["id"],),
            ).fetchone()
        if not row or not row["active_socket_path"] or not row["active_version"]:
            return None
        return ReadyRuntime(str(company["id"]), row["active_version"], row["active_socket_path"])

    def resolve_setup(self, company):
        """Current, healthy private process for owner configuration, never acceptance."""
        if company.get("archived_at") or company.get("status") != "active":
            return None
        with self.repository.connect() as db:
            row = db.execute(
                "SELECT configuration_version,socket_path,checks FROM runtime_provisioning "
                "WHERE company_id=%s",
                (company["id"],),
            ).fetchone()
        if not row or row["configuration_version"] != company["version"]:
            return None
        if not completed(row["checks"], SETUP_CHECKS):
            return None
        try:
            runtime = ReadyRuntime(str(company["id"]), company["version"], row["socket_path"])
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=runtime.socket_path),
                timeout=2,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.get(
                    "http://runtime/_runtime/health",
                    headers={"host": "api." + company["domain"]},
                )
                value = response.json() if response.status_code == 200 else {}
            if (
                not isinstance(value, dict)
                or value.get("status") != "ok"
                or value.get("company_id") != str(company["id"])
                or value.get("configuration_version") != company["version"]
            ):
                return None
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return None
        return runtime
