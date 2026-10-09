"""Lazy trusted per-company bindings; no global credentials enter tenant services."""

from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from psycopg.conninfo import conninfo_to_dict

from .company_accounts import CompanyAccounts, IdentityTarget
from .runtime_operator import RuntimeOperator
from .runtime_process_identity import central_directory, read_central_secret, read_operator_json


class FleetDiscovery(Mapping):
    def __init__(self, configuration, repository):
        self.configuration, self.repository = configuration, repository
        self.root = Path(configuration["operator_directory"])
        central_directory(self.root, configuration["operator_template"])

    def operator(self, company_id):
        company_id = str(UUID(str(company_id)))
        path = self.root / ("c_" + UUID(company_id).hex + ".json")
        if not path.exists():
            raise KeyError(company_id)
        config = read_operator_json(path)
        template = self.configuration["operator_template"]
        if (
            config["company_id"] != company_id
            or config["runtime_root"] != template["runtime_root"]
            or config["verifier_socket"] != template["verifier_socket"]
        ):
            raise ValueError("Fleet binding does not match the platform")
        expected = conninfo_to_dict(template["operator_dsn"])
        for key in ("operator_dsn", "runtime_dsn", "payments_dsn", "identity_dsn"):
            target = conninfo_to_dict(config[key])
            if any(
                target.get(field) != expected.get(field)
                for field in ("host", "hostaddr", "port", "dbname")
            ):
                raise ValueError("Fleet binding points to another database")
        return RuntimeOperator(config, self.repository)

    def __getitem__(self, company_id):
        operator = self.operator(company_id)
        expected = f"{operator.runtime.key}_identity_runtime"
        if conninfo_to_dict(operator.config["identity_dsn"]).get("user") != expected:
            raise ValueError("Fleet identity role is not company-bound")
        return IdentityTarget(operator.runtime, operator.config["identity_dsn"])

    def __iter__(self):
        for path in self.root.glob("c_*.json"):
            if len(path.stem) == 34:
                yield str(UUID(path.stem[2:]))

    def __len__(self):
        return sum(1 for _ in self)

    def grants(self, company_id):
        from app.tenancy.bootstrap import VerifierGrant

        try:
            operator = self.operator(company_id)
        except (KeyError, ValueError):
            return []
        grants = []
        for item in operator.config["verifier_grants"]:
            filename = {"portal": "verifier.key", "documents-worker": "documents-worker.key"}.get(
                item["role"]
            )
            if filename is None or str(item["company_id"]) != str(operator.runtime.company_id):
                raise ValueError("Invalid company verifier role")
            path = Path(item["secret_file"])
            expected = self.root / (operator.runtime.key + "." + filename)
            if path != expected:
                raise ValueError("Verifier capability is outside its central company binding")
            secret = read_central_secret(path, operator.config)
            grants.append(VerifierGrant(str(operator.runtime.company_id), item["role"], secret))
        return grants


def build_fleet_verifier(configuration, repository=None):
    from app.tenancy.bootstrap import create_verifier_app

    from .postgres_repository import PostgresRepository
    from .supabase_auth import SupabaseAuthClient

    template = configuration["operator_template"]
    repo = repository or PostgresRepository(
        template["registry_dsn"], template["registry_data_directory"]
    )
    repo.auth = SupabaseAuthClient(
        template["auth_url"].rstrip("/") + "/auth/v1",
        template["auth_anon_key"],
        template["auth_admin_key"],
    )
    discovery = FleetDiscovery(configuration, repo)
    accounts = CompanyAccounts(repo, {})
    accounts.targets = discovery
    return create_verifier_app(repo, discovery.grants, company_accounts=accounts), repo, accounts
