"""Explicit trusted CLI for real company provisioning; never imported by tenant HTTP.

Operator manifest is mode 0600 and kept outside tenant directories. The operator
DSN may migrate/create roles; it is never written into child environments. Payments
cannot pass while the documented provider lacks its mandatory settlement proof.
"""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from app.tenancy.config import TenantRuntime, schema_names
from app.tenancy.migrations import provision_tenant
from app.tenancy.sql import render, validate_database_runtime
from app.tenant_payments.store import validate_payment_connection

from .connection_check import check_connection
from .connections import configured
from .postgres_repository import PostgresRepository
from .provisioning import Provisioner
from .runtime_process_identity import ProcessIdentity, read_central_secret, read_operator_json


def private_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("Operator manifest must be a private regular file")
    return json.loads(path.read_text())


def atomic_private_json(path, data):
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".new", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class RuntimeOperator:
    def __init__(self, configuration, repository=None, *, settings_service=None):
        self.config = configuration
        target_fields = ("host", "hostaddr", "port", "dbname")
        operator_target = conninfo_to_dict(configuration["operator_dsn"])
        for field in ("runtime_dsn", "payments_dsn", "registry_dsn"):
            candidate = conninfo_to_dict(configuration[field])
            if any(candidate.get(key) != operator_target.get(key) for key in target_fields):
                raise ValueError("All schemas must use the same explicitly configured database")
        self.repo = repository or PostgresRepository(
            configuration["registry_dsn"], configuration["registry_data_directory"]
        )
        self._owns_auth = False
        if getattr(self.repo, "auth", None) is None and configuration.get("auth_url"):
            from .supabase_auth import SupabaseAuthClient

            self.repo.auth = SupabaseAuthClient(
                configuration["auth_url"].rstrip("/") + "/auth/v1",
                configuration["auth_anon_key"],
                configuration.get("auth_admin_key", ""),
            )
            self._owns_auth = True
        from .company_module_settings import CompanyModuleSettings

        self.settings_service = settings_service or CompanyModuleSettings(self.repo)
        self.company = self.repo.get(configuration["company_id"])
        company_id = UUID(str(self.company["id"]))
        self.runtime = TenantRuntime(
            mode="tenant",
            company_id=company_id,
            **{f"{k}_schema": v for k, v in schema_names(company_id).items()},
            timezone=self.company.get("timezone") or self.company["subscription"]["timezone"],
            frontend_origin="https://" + self.company["domain"],
            api_origin="https://api." + self.company["domain"],
            runtime_directory=Path(configuration["runtime_root"]) / f"c_{company_id.hex}",
            configuration_version=self.company["version"],
            database_role=f"c_{company_id.hex}_runtime",
        )
        self.manifest = self.runtime.runtime_path("environment.json")

    def close(self):
        if self._owns_auth:
            self.repo.auth.close()
            self._owns_auth = False

    def current(self, company_id, version):
        if (
            str(company_id) != str(self.runtime.company_id)
            or version != self.runtime.configuration_version
        ):
            raise ValueError("Operator adapters are bound to one company configuration")
        current = self.repo.get(company_id)
        if (
            current["version"] != version
            or current.get("archived_at")
            or current["status"] != "active"
        ):
            raise ValueError("Company configuration changed or is inactive")
        return current

    def process_identity(self, *, create: bool = False) -> ProcessIdentity:
        return ProcessIdentity(self.config, self.runtime.company_id, create=create)

    def prepare_manifest(self):
        from .runtime_module_settings import apply_company_settings

        current = self.current(str(self.runtime.company_id), self.runtime.configuration_version)
        apply_company_settings(self.config, current, self.settings_service)
        runtime = self.runtime
        identity = self.process_identity(create=True)
        url, login, password = self.repo.check_inputs(
            str(runtime.company_id), "chain", runtime.configuration_version
        )
        url = url.rstrip("/")
        if url.endswith("/resto"):
            url += "/api"
        elif not url.endswith("/resto/api"):
            url += "/resto/api"
        connections = []
        for connection in self.company["rms"]:
            if not connection["enabled"]:
                continue
            endpoint, user, secret = self.repo.check_inputs(
                str(runtime.company_id), str(connection["id"]), runtime.configuration_version
            )
            endpoint = endpoint.rstrip("/")
            if endpoint.endswith("/resto"):
                endpoint += "/api"
            elif not endpoint.endswith("/resto/api"):
                endpoint += "/resto/api"
            connections.append(
                {
                    "id": "rms-" + UUID(str(connection["id"])).hex,
                    "label": connection.get("name") or str(connection["id"]),
                    "base_url": endpoint,
                    "login": user,
                    "password": secret,
                }
            )
        secret_file = runtime.runtime_path("verifier.key")
        if self.config.get("verifier_grants"):
            for grant in self.config["verifier_grants"]:
                filename = {
                    "portal": "verifier.key",
                    "documents-worker": "documents-worker.key",
                }.get(grant["role"])
                if filename is None or str(grant["company_id"]) != str(runtime.company_id):
                    raise ValueError("Invalid company verifier grant")
                identity.write_private(
                    filename, read_central_secret(grant["secret_file"], self.config)
                )
        elif identity.policy["mode"] == "local-test":
            if not secret_file.exists():
                identity.write_private("verifier.key", secrets.token_urlsafe(48))
        else:
            raise ValueError("Linux tenant requires central pinned verifier capabilities")
        # Input DSNs are separate role credentials, never the operator or registry DSN.
        for field, role in [
            ("runtime_dsn", runtime.database_role),
            ("payments_dsn", f"{runtime.key}_payments_runtime"),
        ]:
            if conninfo_to_dict(self.config[field]).get("user") != role:
                raise ValueError("Child DSN must name its explicit restricted role")
        env = {
            "RESTCONTROL_RUNTIME_MODE": "tenant",
            "RESTCONTROL_TENANT_COMPANY_ID": str(runtime.company_id),
            "RESTCONTROL_TENANT_CONFIGURATION_VERSION": str(runtime.configuration_version),
            "RESTCONTROL_TENANT_TIMEZONE": runtime.timezone,
            "RESTCONTROL_TENANT_FRONTEND_ORIGIN": runtime.frontend_origin,
            "RESTCONTROL_TENANT_API_ORIGIN": runtime.api_origin,
            "RESTCONTROL_TENANT_RUNTIME_DIRECTORY": str(runtime.runtime_directory),
            "RESTCONTROL_TENANT_DATABASE_ROLE": runtime.database_role,
            "RESTCONTROL_TENANT_DATABASE_URL": self.config["runtime_dsn"],
            "RESTCONTROL_TENANT_PAYMENTS_DATABASE_URL": self.config["payments_dsn"],
            "RESTCONTROL_TENANT_IIKO_BASE_URL": url,
            "RESTCONTROL_TENANT_IIKO_LOGIN": login,
            "RESTCONTROL_TENANT_IIKO_PASSWORD": password,
            "RESTCONTROL_TENANT_IIKO_CONNECTIONS_JSON": json.dumps({"connections": connections}),
            "RESTCONTROL_TENANT_WEB_SUPABASE_URL": self.config["auth_url"],
            "RESTCONTROL_TENANT_WEB_ANON_KEY": self.config["auth_anon_key"],
            "RESTCONTROL_TENANT_WEB_DOCUMENTS_ENABLED": "true",
            "RESTCONTROL_TENANT_DOCUMENTS_NATIVE_ENABLED": "true",
            "RESTCONTROL_TENANT_DOCUMENTS_DATABASE_URL": self.config["runtime_dsn"],
            "RESTCONTROL_TENANT_DOCUMENTS_IIKO_URL": url,
            "RESTCONTROL_TENANT_DOCUMENTS_IIKO_LOGIN": login,
            "RESTCONTROL_TENANT_DOCUMENTS_IIKO_PASSWORD_HASH": hashlib.sha1(
                password.encode()
            ).hexdigest(),
            "RESTCONTROL_TENANT_VERIFIER_SECRET_FILE": str(secret_file),
            "RESTCONTROL_TENANT_VERIFIER_SOCKET": self.config["verifier_socket"],
            "RESTCONTROL_TENANT_SYNC_ENABLED": "false",
            "RESTCONTROL_TENANT_LIVE_SALES_ENABLED": "false",
        }
        settings_groups = {
            "document_settings": (
                "DOCUMENTS_",
                {
                    "commercial_enabled",
                    "commercial_submit_enabled",
                    "commercial_counterparty_create_enabled",
                    "commercial_seller_json",
                    "bot_token",
                    "bot_username",
                    "worker_enabled",
                },
            ),
            "assistant_settings": (
                "AI_",
                {
                    "provider",
                    "api_key",
                    "timeweb_agent_id",
                    "model",
                    "requests_per_hour",
                    "max_output_tokens",
                },
            ),
            "collector_settings": ("", {"sync_enabled", "live_sales_enabled", "sync_api_key"}),
        }
        for group, (prefix, allowed) in settings_groups.items():
            values = self.config.get(group, {})
            if not isinstance(values, dict) or set(values) - allowed:
                raise ValueError("Unknown tenant settings field")
            for name, value in values.items():
                if isinstance(value, (dict, list)):
                    value = json.dumps(value)
                elif isinstance(value, bool):
                    value = str(value).lower()
                env["RESTCONTROL_TENANT_" + prefix + name.upper()] = str(value)
        env.update(
            {k: v for k, v in identity.environment().items() if k.startswith("RESTCONTROL_TENANT_")}
        )
        identity.write_private(self.manifest.name, json.dumps(env))
        return env

    def child_environment(self, role="collector"):
        if role not in {"portal", "collector", "documents-worker", "scheduler"}:
            raise ValueError("Unknown process role")
        self.current(str(self.runtime.company_id), self.runtime.configuration_version)
        identity = self.process_identity()
        try:
            env = json.loads(identity.read_private(self.manifest.name))
        except RecursionError:
            raise ValueError("Child manifest nesting is invalid") from None
        if not isinstance(env, dict) or any(not isinstance(value, str) for value in env.values()):
            raise ValueError("Child manifest must contain string environment values")
        if any(
            k != "RESTCONTROL_RUNTIME_MODE" and not k.startswith("RESTCONTROL_TENANT_") for k in env
        ):
            raise ValueError("Child manifest contains a non-tenant setting")
        if env.get("RESTCONTROL_TENANT_COMPANY_ID") != str(self.runtime.company_id) or env.get(
            "RESTCONTROL_TENANT_CONFIGURATION_VERSION"
        ) != str(self.runtime.configuration_version):
            raise ValueError("Child manifest does not match current company configuration")
        if role != "portal":
            env = {
                key: value
                for key, value in env.items()
                if not key.startswith(
                    (
                        "RESTCONTROL_TENANT_PAYMENTS_",
                        "RESTCONTROL_TENANT_VERIFIER_",
                        "RESTCONTROL_TENANT_AI_",
                    )
                )
            }
        if role in {"collector", "scheduler"}:
            env = {
                key: value
                for key, value in env.items()
                if not key.startswith("RESTCONTROL_TENANT_DOCUMENTS_")
            }
        if role == "documents-worker":
            matches = [
                grant
                for grant in self.config.get("verifier_grants", [])
                if grant["company_id"] == str(self.runtime.company_id) and grant["role"] == role
            ]
            if len(matches) != 1:
                raise ValueError("A distinct document-worker verifier capability is required")
            secret = self.runtime.runtime_path("documents-worker.key")
            secret_value = identity.read_private(secret.name, limit=4096).strip()
            central_value = read_central_secret(matches[0]["secret_file"], self.config)
            if secret_value != central_value:
                raise ValueError("Worker capability differs from its central pinned copy")
            portal_value = identity.read_private("verifier.key", limit=4096).strip()
            if secret_value == portal_value:
                raise ValueError("Document worker requires its own private capability")
            env["RESTCONTROL_TENANT_VERIFIER_SECRET_FILE"] = str(secret)
            env["RESTCONTROL_TENANT_VERIFIER_SOCKET"] = self.config["verifier_socket"]
            if env.get("RESTCONTROL_TENANT_DOCUMENTS_WORKER_ENABLED") != "true":
                raise ValueError("Document worker must be explicitly enabled")
        return {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            **env,
            **self.process_identity().environment(),
        }

    def foreground_command(self, role):
        """Supervisor-owned processes: never implicitly start external writers."""
        if role == "portal":
            return [
                sys.executable,
                "-m",
                "app.tenancy.bootstrap",
                "--config",
                str(self.manifest),
            ], self.child_environment(role)
        if role == "collector":
            return [
                sys.executable,
                "-m",
                "app.tenancy.bootstrap",
                "--collector",
            ], self.child_environment(role)
        if role == "documents-worker":
            return [sys.executable, "-m", "app.documents.worker"], self.child_environment(role)
        if role == "scheduler":
            env = self.child_environment(role)
            if env.get("RESTCONTROL_TENANT_SYNC_ENABLED") != "true" or not env.get(
                "RESTCONTROL_TENANT_SYNC_API_KEY"
            ):
                raise ValueError("Scheduler requires explicit company sync configuration")
            env["RESTCONTROL_TENANT_EXTERNAL_COLLECTOR"] = "true"
            return [sys.executable, "-m", "app.scheduler", "--worker"], env
        raise ValueError("Unknown foreground role")

    def launch(self, role):
        if role not in {"portal", "collector"}:
            raise ValueError("Unknown process role")
        socket = self.runtime.runtime_path(role + ".sock")
        if socket.exists():
            # Never start a duplicate or remove an existing socket blindly.
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(socket)),
                base_url="http://runtime",
                timeout=5,
                trust_env=False,
            ) as client:
                response = client.get(
                    "/_runtime/health",
                    headers={"host": urlsplit(self.runtime.api_origin).netloc},
                )
                if (
                    response.status_code != 200
                    or response.json().get("company_id") != str(self.runtime.company_id)
                    or response.json().get("configuration_version")
                    != self.runtime.configuration_version
                ):
                    raise ValueError("Existing socket does not match the configured runtime")
            return
        if self.config.get("supervisor_managed"):
            from .provisioning import PendingCheck

            raise PendingCheck("runtime_process_pending", {"process": role})
        command, environment = self.foreground_command(role)
        # No stdout or HTTP logs containing credentials or capabilities are retained.
        process = subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            **self.process_identity().spawn_options(),
        )
        atomic_private_json(
            self.runtime.runtime_path(role + ".pid.json"),
            {
                "pid": process.pid,
                "company_id": str(self.runtime.company_id),
                "version": self.runtime.configuration_version,
            },
        )
        for _ in range(200):
            if process.poll() is not None:
                raise ValueError("Runtime process failed to start")
            if socket.exists():
                return
            time.sleep(0.05)
        process.terminate()
        raise ValueError("Runtime process startup timed out")

    def migrations(self, *_):
        with psycopg.connect(self.config["operator_dsn"], autocommit=True) as db:
            applied = provision_tenant(db, self.runtime)
            roles = [
                ("runtime_dsn", self.runtime.database_role),
                ("payments_dsn", f"{self.runtime.key}_payments_runtime"),
            ]
            if self.config.get("identity_dsn"):
                roles.append(("identity_dsn", f"{self.runtime.key}_identity_runtime"))
            for field, role in roles:
                password = conninfo_to_dict(self.config[field]).get("password")
                if not password:
                    raise ValueError("Explicit per-role password required")
                db.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
        return {
            "ok": True,
            "evidence": {
                "migration_count": len(applied),
                "version": self.runtime.configuration_version,
            },
        }

    def database_roles(self, *_):
        with psycopg.connect(self.config["runtime_dsn"], autocommit=True) as db:
            validate_database_runtime(db, self.runtime)
        with psycopg.connect(
            self.config["payments_dsn"], row_factory=dict_row, autocommit=True
        ) as db:
            validate_payment_connection(db, self.runtime)
        return {"ok": True, "evidence": "Both restricted login roles validated"}

    def identity(self, *_):
        with self.repo.connect() as db:
            members = db.execute(
                "SELECT auth_user_id FROM memberships WHERE company_id=%s "
                "AND active AND auth_user_id IS NOT NULL",
                (self.runtime.company_id,),
            ).fetchall()
        with psycopg.connect(self.config["runtime_dsn"]) as db:
            registered = {
                str(row[0])
                for row in db.execute(
                    render("SELECT id FROM {analytics}.portal_identities", self.runtime)
                )
            }
        if any(str(row["auth_user_id"]) not in registered for row in members):
            raise ValueError("Company identities must be provisioned first")
        return {"ok": True, "evidence": {"registered_members": len(members)}}

    def connections(self, *_):
        count = 0
        for key in configured(self.company, enabled_only=True):
            result = check_connection(
                *self.repo.check_inputs(
                    str(self.runtime.company_id), key, self.runtime.configuration_version
                )
            )
            if result["status"] != "ok":
                raise ValueError("Connection check did not succeed")
            count += 1
        if not count:
            raise ValueError("No company connections configured")
        return {"ok": True, "evidence": {"checked_connections": count}}

    def initial_sync(self, *_):
        start = date.fromisoformat(self.config["history_from"])
        end = date.fromisoformat(self.config["history_to"])
        if start > end:
            raise ValueError("Invalid initial history interval")
        self.launch("collector")
        jobs = [
            ("sync_references", []),
            ("sync_inventory", []),
            ("sync_employees", []),
            ("sync_dictionaries", []),
            ("sync_store_balances", []),
            ("sync_sales_history", ["--date-from", str(start), "--date-to", str(end)]),
            ("sync_documents", ["--date-from", str(start), "--date-to", str(end)]),
            ("sync_event_history", ["--date-from", str(start), "--date-to", str(end)]),
        ]
        for module, args in jobs:
            result = subprocess.run(
                [sys.executable, "-m", "app." + module, *args],
                env=self.child_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=6 * 3600,
                check=False,
                **self.process_identity().spawn_options(),
            )
            if result.returncode:
                raise ValueError("Initial synchronization failed")
        return {
            "ok": True,
            "evidence": {
                "history_from": str(start),
                "history_to": str(end),
                "completed_jobs": [item[0] for item in jobs],
            },
        }

    def modules(self, *_):
        from .runtime_acceptance import check_modules

        return check_modules(self)

    def payments(self, *_):
        from .entitlements import evaluate_feature
        from .provisioning import PendingCheck

        if not evaluate_feature(
            self.company,
            "payments.create",
            operation="create",
            staff_allowed=True,
            warehouse_allowed=True,
        ).allowed:
            return {"ok": True, "evidence": {"enabled": False, "reason": "feature_not_enabled"}}
        with psycopg.connect(self.config["payments_dsn"], row_factory=dict_row) as db:
            validate_payment_connection(db, self.runtime)
            rows = db.execute(
                render(
                    "SELECT t.id,v.mode FROM {payments}.terminals t "
                    "JOIN {payments}.terminal_versions v "
                    "ON v.id=t.current_version_id JOIN {payments}.venues p "
                    "ON p.default_terminal_id=t.id "
                    "WHERE t.active AND p.active AND v.encrypted_key IS NOT NULL",
                    self.runtime,
                )
            ).fetchall()
        if not rows:
            raise PendingCheck("payment_terminal_required", {"configured_terminals": 0})
        verified = []
        with psycopg.connect(self.config["payments_dsn"], row_factory=dict_row) as db:
            for terminal in rows:
                check = db.execute(
                    render(
                        "SELECT c.id,c.terminal_version_id,c.mode FROM {payments}.terminal"
                        "_checks c "
                        "JOIN {payments}.terminals t ON t.current_version_id=c.terminal_version_id "
                        "WHERE t.id=%s AND c.subscription_end_date>CURRENT_DATE AND c.qrt_is_b2c "
                        "AND c.requires_receipt IS FALSE AND c.is_nomenclature IS FALSE "
                        "AND c.is_cash_link IS FALSE ORDER BY c.checked_at DESC LIMIT 1",
                        self.runtime,
                    ),
                    (terminal["id"],),
                ).fetchone()
                if check is None:
                    raise PendingCheck(
                        "payment_terminal_check_required", {"configured_terminals": len(rows)}
                    )
                proof = db.execute(
                    render(
                        "SELECT a.id FROM {payments}.attempts a "
                        "JOIN {payments}.terminal_checks c ON c.id=a.terminal_check_id "
                        "JOIN {payments}.acceptance_intents i ON i.attempt_id=a.id "
                        "WHERE i.company_id=%s "
                        "AND i.consumed_at IS NOT NULL AND i.mode=%s "
                        "AND i.terminal_version_id=a.terminal_version_id "
                        "AND i.terminal_id=a.terminal_id AND i.deposit_id=a.deposit_id "
                        "AND i.request_id=a.request_id "
                        "AND i.amount_minor=a.amount_minor AND i.currency=a.currency "
                        "AND a.terminal_version_id=%s AND a.state='paid' AND a.creation_"
                        "confirmed "
                        "AND a.creation_currency=a.currency AND a.provider_amount_minor=a."
                        "amount_minor "
                        "AND c.terminal_version_id=a.terminal_version_id AND c.mode=%s LIMIT 1",
                        self.runtime,
                    ),
                    (
                        self.runtime.company_id,
                        terminal["mode"],
                        check["terminal_version_id"],
                        terminal["mode"],
                    ),
                ).fetchone()
                if proof is None:
                    raise PendingCheck(
                        "payment_settlement_proof_required",
                        {
                            "configured_terminals": len(rows),
                            "reason": "Verified settlement amount/currency/merchant required",
                        },
                    )
                verified.append(
                    {
                        "terminal_id": str(terminal["id"]),
                        "check_id": str(check["id"]),
                        "settled_attempt_id": str(proof["id"]),
                        "configuration_version": self.runtime.configuration_version,
                        "mode": terminal["mode"],
                    }
                )
        return {"ok": True, "evidence": {"terminals": verified}}

    def dns_tls(self, *_):
        for origin, path in [
            (self.runtime.frontend_origin, "/"),
            (self.runtime.api_origin, "/api/saas-context"),
        ]:
            with httpx.Client(
                timeout=15, verify=True, trust_env=False, follow_redirects=False
            ) as client:
                response = client.get(
                    origin + path, headers={"origin": self.runtime.frontend_origin}
                )
                if response.status_code != 200:
                    raise ValueError("DNS/TLS or public routing failed")
                if path != "/":
                    value = response.json()
                    if value.get("company", {}).get("id") != str(self.runtime.company_id):
                        raise ValueError("Public domain points to another company")
        return {"ok": True, "evidence": "Both exact public HTTPS origins verified"}

    def runtime_health(self, *_):
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(self.runtime.runtime_path("portal.sock"))),
            base_url="http://runtime",
            timeout=5,
            trust_env=False,
        ) as client:
            response = client.get(
                "/_runtime/health", headers={"host": urlsplit(self.runtime.api_origin).netloc}
            )
            response.raise_for_status()
            body = response.json()
            if (
                body.get("company_id") != str(self.runtime.company_id)
                or body.get("configuration_version") != self.runtime.configuration_version
            ):
                raise ValueError("Wrong process runtime")
        return {"ok": True, "evidence": body}

    def verifier_app(self):
        """Build the documented control factory with company identity capabilities."""
        from app.tenancy.bootstrap import VerifierGrant, create_verifier_app

        from .company_accounts import CompanyAccounts, IdentityTarget
        from .supabase_auth import SupabaseAuthClient

        grants = []
        for item in self.config["verifier_grants"]:
            secret_file = Path(item["secret_file"])
            secret = read_central_secret(secret_file, self.config)
            grants.append(VerifierGrant(str(UUID(item["company_id"])), item["role"], secret))
        targets = {}
        definitions = self.config.get("identity_targets")
        if definitions is None:
            definitions = [
                {
                    "company_id": str(self.runtime.company_id),
                    "identity_dsn": self.config["identity_dsn"],
                }
            ]
        target_fields = ("host", "hostaddr", "port", "dbname")
        database = conninfo_to_dict(self.config["operator_dsn"])
        for item in definitions:
            if (
                item.get("environment_file")
                and self.config["process_isolation"]["mode"] != "local-test"
            ):
                raise ValueError(
                    "Linux identity targets cannot read tenant-owned environment overrides"
                )
            company_id = str(UUID(item["company_id"]))
            runtime = (
                TenantRuntime.from_env(private_json(item["environment_file"]))
                if item.get("environment_file")
                else self.runtime
            )
            if str(runtime.company_id) != company_id:
                raise ValueError("Identity target runtime belongs to another company")
            target_dsn = conninfo_to_dict(item["identity_dsn"])
            if target_dsn.get("user") != f"{runtime.key}_identity_runtime" or any(
                target_dsn.get(key) != database.get(key) for key in target_fields
            ):
                raise ValueError("Identity target must use its own narrow role and database")
            targets[company_id] = IdentityTarget(runtime, item["identity_dsn"])
        if getattr(self.repo, "auth", None) is None:
            self.repo.auth = SupabaseAuthClient(
                self.config["auth_url"].rstrip("/") + "/auth/v1",
                self.config["auth_anon_key"],
                self.config["auth_admin_key"],
            )
            self._owns_auth = True
        self.company_accounts = CompanyAccounts(self.repo, targets)
        return create_verifier_app(self.repo, grants, company_accounts=self.company_accounts)

    def serve_verifier(self):
        from .runtime_process_identity import serve_verifier_socket

        app = self.verifier_app()
        try:
            serve_verifier_socket(app, self.config["verifier_socket"], self.config)
        finally:
            self.close()

    def poll_once(self):
        """Consume only this operator-configured company's durable requests."""
        with self.repo.connect() as db:
            row = db.execute(
                "SELECT configuration_version,state FROM runtime_provisioning WHERE compa"
                "ny_id=%s AND state IN ('pending','running')",
                (self.runtime.company_id,),
            ).fetchone()
        if row is None:
            return False
        if row["configuration_version"] != self.runtime.configuration_version:
            with self.repo.connect(True) as db:
                db.execute(
                    "UPDATE runtime_provisioning SET state='failed',error_code='configuration"
                    "_changed',updated_at=now() WHERE company_id=%s AND configuration_version"
                    "=%s AND state IN ('pending','running')",
                    (self.runtime.company_id, row["configuration_version"]),
                )
            return True
        try:
            self.prepare_manifest()
        except Exception:
            with self.repo.connect(True) as db:
                db.execute(
                    "UPDATE runtime_provisioning SET state='failed',"
                    "error_code='configuration_failed',updated_at=now() "
                    "WHERE company_id=%s AND configuration_version=%s "
                    "AND state IN ('pending','running')",
                    (self.runtime.company_id, self.runtime.configuration_version),
                )
            return True
        self.provisioner().run(str(self.runtime.company_id))
        return True

    def provisioner(self):
        from .provisioning import STEPS

        def adapter(step):
            def run(company_id, version):
                self.current(company_id, version)
                return getattr(self, step)(company_id, version)

            return run

        return Provisioner(self.repo, {step: adapter(step) for step in STEPS})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "action",
        choices=[
            "render",
            "enqueue",
            "run",
            "start-portal",
            "start-collector",
            "serve-verifier",
            "status",
            "work",
            "work-once",
            "run-documents-worker",
            "run-portal",
            "run-collector",
            "run-scheduler",
        ],
    )
    args = parser.parse_args()
    try:
        operator = RuntimeOperator(read_operator_json(args.config))
        if args.action.startswith("run-"):
            command, environment = operator.foreground_command(args.action.removeprefix("run-"))
            operator.process_identity().exec(command, environment)
        elif args.action == "work":
            while True:
                current_operator = RuntimeOperator(read_operator_json(args.config))
                try:
                    current_operator.poll_once()
                finally:
                    current_operator.close()
                time.sleep(3)
        elif args.action == "work-once":
            operator.poll_once()
        elif args.action == "render":
            operator.prepare_manifest()
        elif args.action == "enqueue":
            operator.prepare_manifest()
            operator.provisioner().enqueue(
                operator.company, str(operator.runtime.runtime_path("portal.sock"))
            )
        elif args.action == "run":
            if not operator.provisioner().run(str(operator.runtime.company_id)):
                raise ValueError("Provisioning incomplete")
        elif args.action == "serve-verifier":
            operator.serve_verifier()
        elif args.action.startswith("start-"):
            operator.launch(args.action.removeprefix("start-"))
        else:
            with operator.repo.connect() as db:
                state = db.execute(
                    "SELECT state,step,error_code,attempts,updated_at "
                    "FROM runtime_provisioning WHERE company_id=%s",
                    (operator.runtime.company_id,),
                ).fetchone()
            print(json.dumps(state, default=str))
            return
        print(
            json.dumps(
                {
                    "company_id": str(operator.runtime.company_id),
                    "action": args.action,
                    "completed": True,
                }
            )
        )
    except Exception:
        print(
            json.dumps(
                {"action": args.action, "completed": False, "error_code": "operator_step_failed"}
            )
        )
        raise SystemExit(1) from None
    finally:
        if "operator" in locals():
            operator.close()


if __name__ == "__main__":
    main()
