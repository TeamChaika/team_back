"""Private process registry. Routing is enabled only by durable acceptance evidence."""

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
        with self.repository.connect() as db:
            row = db.execute(
                "SELECT configuration_version, socket_path, checks, state "
                "FROM runtime_provisioning WHERE company_id=%s",
                (company["id"],),
            ).fetchone()
        if not row or row["state"] != "ready" or row["configuration_version"] != company["version"]:
            return None
        if any(
            not isinstance(row["checks"].get(key), dict)
            or row["checks"][key].get("ok") is not True
            or not row["checks"][key].get("evidence")
            for key in REQUIRED_CHECKS
        ):
            return None
        return ReadyRuntime(str(company["id"]), row["configuration_version"], row["socket_path"])

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
        if any(
            not isinstance(row["checks"].get(key), dict)
            or row["checks"][key].get("ok") is not True
            or not row["checks"][key].get("evidence")
            for key in SETUP_CHECKS
        ):
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
