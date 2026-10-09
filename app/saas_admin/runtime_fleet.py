"""Prepare private per-company operator files from one explicit platform template.

This is mechanical credential/config preparation only. It does not authorize an
owner, fabricate acceptance evidence, launch processes or submit external writes.
"""

import argparse
import json
import logging
import os
import secrets
import signal
import socket as unix_socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

import httpx
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from .postgres_repository import PostgresRepository
from .runtime_process_identity import (
    ProcessIdentity,
    central_directory,
    read_operator_json,
    write_central,
)


class FleetPreparer:
    def __init__(self, configuration, repository=None, *, settings_service=None):
        self.config = configuration
        self.template = configuration["operator_template"]
        forbidden = {
            "company_id",
            "runtime_dsn",
            "payments_dsn",
            "identity_dsn",
            "identity_targets",
            "acceptance_session_file",
            "verifier_grants",
            "document_settings",
            "assistant_settings",
            "collector_settings",
        }
        if forbidden.intersection(self.template):
            raise ValueError(
                "Fleet template must not contain company credentials or acceptance identity"
            )
        self.root = Path(configuration["operator_directory"])
        runtime_root = Path(self.template["runtime_root"])
        if not self.root.is_absolute() or ".." in self.root.parts or not runtime_root.is_absolute():
            raise ValueError("Private roots must be absolute")
        if self.root.resolve().is_relative_to(runtime_root.resolve()):
            raise ValueError(
                "Central operator files must remain outside tenant runtime directories"
            )
        central_directory(self.root, self.template, create=True)
        self.repo = repository or PostgresRepository(
            self.template["registry_dsn"], self.template["registry_data_directory"]
        )

        from .company_module_settings import CompanyModuleSettings

        self.settings_service = settings_service or CompanyModuleSettings(self.repo)

    def prepare(self, company):
        company_id = UUID(str(company["id"]))
        if company.get("status") != "active" or company.get("archived_at"):
            raise ValueError("Company must be active")
        path = self.root / ("c_" + company_id.hex + ".json")
        # Serialize concurrent/restarted preparers without ever rotating active credentials.
        import fcntl

        descriptor = os.open(self.root / ".prepare.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if path.exists():
                saved = read_operator_json(path)
                if saved["company_id"] != str(company_id):
                    raise ValueError("Existing operator belongs to another company")
                saved["supervisor_managed"] = True
                settings = saved.setdefault("document_settings", {})
                settings["worker_enabled"] = bool(
                    company.get("modules", {}).get("documents")
                    or company.get("modules", {}).get("commercial_invoices")
                )
                settings["commercial_enabled"] = bool(
                    company.get("modules", {}).get("commercial_invoices")
                )
                from .runtime_module_settings import apply_company_settings

                apply_company_settings(saved, company, self.settings_service)
                write_central(path, json.dumps(saved), self.template)
                return path
            configuration = {**self.template, "company_id": str(company_id)}
            target = conninfo_to_dict(self.template["operator_dsn"])
            target.pop("user", None)
            target.pop("password", None)
            # SET ROLE/search_path options from a DDL operator must never cross this boundary.
            target.pop("options", None)
            for field, suffix in [
                ("runtime_dsn", "runtime"),
                ("payments_dsn", "payments_runtime"),
                ("identity_dsn", "identity_runtime"),
            ]:
                configuration[field] = make_conninfo(
                    **target,
                    user=f"c_{company_id.hex}_{suffix}",
                    password=secrets.token_urlsafe(36),
                )
            identity = ProcessIdentity(configuration, company_id, create=True)
            grants = []
            for role, filename in [
                ("portal", "verifier.key"),
                ("documents-worker", "documents-worker.key"),
            ]:
                key = self.root / ("c_" + company_id.hex + "." + filename)
                if not key.exists():
                    write_central(key, secrets.token_urlsafe(48), self.template)
                # The central copy is authoritative, outside the writable tenant directory.
                from .runtime_process_identity import read_central_secret

                secret = read_central_secret(key, self.template)
                identity.write_private(filename, secret)
                grants.append(
                    {"company_id": str(company_id), "role": role, "secret_file": str(key)}
                )
            configuration["verifier_grants"] = grants
            configuration["supervisor_managed"] = True
            if self.template.get("acceptance_root"):
                configuration["acceptance_session_file"] = str(
                    Path(self.template["acceptance_root"]) / ("c_" + company_id.hex + ".acceptance")
                )
            configuration["collector_settings"] = {
                "sync_enabled": True,
                "live_sales_enabled": True,
                "sync_api_key": secrets.token_urlsafe(36),
            }
            configuration["document_settings"] = {
                "worker_enabled": bool(
                    company.get("modules", {}).get("documents")
                    or company.get("modules", {}).get("commercial_invoices")
                ),
                "commercial_enabled": bool(company.get("modules", {}).get("commercial_invoices")),
            }
            from .runtime_module_settings import apply_company_settings

            apply_company_settings(configuration, company, self.settings_service)
            write_central(path, json.dumps(configuration), self.template)
            return path
        finally:
            os.close(descriptor)

    def prepare_pending(self):
        with self.repo.connect() as db:
            pending = db.execute(
                "SELECT company_id FROM runtime_provisioning "
                "WHERE state='pending' ORDER BY updated_at"
            ).fetchall()
        prepared = []
        for row in pending:
            try:
                prepared.append(self.prepare(self.repo.get(str(row["company_id"]))))
            except (ValueError, OSError, KeyError, subprocess.SubprocessError):
                logging.getLogger(__name__).warning(
                    "Tenant preparation failed; binding retained for explicit retry"
                )
        return prepared


class FleetSupervisor:
    """Supervise only explicitly prepared tenant children; never install OS services."""

    def __init__(self, preparer, popen=subprocess.Popen):
        self.preparer, self.popen = preparer, popen
        self.children, self.retry_after, self.versions = {}, {}, {}
        self.restart_after = {}
        self.launch_errors = {}

    def start(self, path, action, version=None):
        key = (str(path), action)
        if key in self.launch_errors and time.monotonic() < self.restart_after.get(key, 0):
            return False
        try:
            result = self._start(path, action, version)
            self.launch_errors.pop(key, None)
            return result
        except (ValueError, OSError, KeyError, subprocess.SubprocessError):
            self.launch_errors[key] = "runtime_launch_failed"
            self.restart_after[key] = time.monotonic() + 30
            logging.getLogger(__name__).warning(
                "Tenant process launch failed (%s); retry delayed",
                action
                if action
                in {"work", "run-portal", "run-collector", "run-documents-worker", "run-scheduler"}
                else "invalid-action",
            )
            return False

    def _start(self, path, action, version=None):
        key = (str(path), action)
        process = self.children.get(key)
        if (
            process is not None
            and process.poll() is not None
            and time.monotonic() < self.restart_after.get(key, 0)
        ):
            return False
        if process is not None and process.poll() is None:
            if self.versions.get(key) == version:
                return False
            self.stop(key)
        if action in {"run-portal", "run-collector"}:
            config = read_operator_json(path)
            address = (
                Path(config["runtime_root"])
                / ("c_" + UUID(config["company_id"]).hex)
                / (action.removeprefix("run-") + ".sock")
            )
            if address.exists() or address.is_symlink():
                if address.is_symlink() or not stat.S_ISSOCK(address.stat().st_mode):
                    raise ValueError("Refusing an unexpected runtime socket path")
                inode = address.stat().st_ino
                with unix_socket.socket(unix_socket.AF_UNIX) as probe:
                    probe.settimeout(1)
                    try:
                        probe.connect(str(address))
                    except ConnectionRefusedError:
                        if address.exists() and address.stat().st_ino == inode:
                            address.unlink()
                    else:
                        return False  # Existing live process is never silently adopted or replaced.
        self.versions[key] = version
        self.restart_after[key] = time.monotonic() + 10
        command = [
            sys.executable,
            "-m",
            "app.saas_admin.runtime_operator",
            "--config",
            str(path),
            action,
        ]
        environment = {"PATH": os.defpath, "LANG": "C.UTF-8"}
        options = {}
        if action.startswith("run-"):
            from .runtime_operator import RuntimeOperator

            config = read_operator_json(path)
            operator = RuntimeOperator(config, self.preparer.repo)
            try:
                command, environment = operator.foreground_command(action.removeprefix("run-"))
                options = operator.process_identity().spawn_options()
            finally:
                operator.close()
        self.children[key] = self.popen(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            **options,
        )
        return True

    def stop(self, key):
        process = self.children.pop(key, None)
        if process is None:
            return
        socket, identity = None, None
        if key[1] in {"run-portal", "run-collector"}:
            config = read_operator_json(key[0])
            socket = (
                Path(config["runtime_root"])
                / ("c_" + UUID(config["company_id"]).hex)
                / (key[1].removeprefix("run-") + ".sock")
            )
            if socket.exists() and process.poll() is None:
                try:
                    with httpx.Client(
                        transport=httpx.HTTPTransport(uds=str(socket)),
                        base_url="http://runtime",
                        timeout=2,
                        trust_env=False,
                    ) as client:
                        response = client.get(
                            "/_runtime/health",
                            headers={
                                "host": "api."
                                + self.preparer.repo.get(config["company_id"])["domain"]
                            },
                        )
                        body = response.json()
                    if (
                        response.status_code == 200
                        and body.get("process_id") == getattr(process, "pid", None)
                        and body.get("company_id") == config["company_id"]
                    ):
                        identity = socket.stat().st_ino
                except (httpx.HTTPError, ValueError, OSError):
                    pass
        if process.poll() is None:
            if getattr(process, "pid", None) is not None:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if getattr(process, "pid", None) is not None:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()
        if socket and socket.exists() and socket.stat().st_ino == identity:
            socket.unlink()  # Only the socket of this terminated, owned child.

    def tick(self):
        self.preparer.prepare_pending()
        for path in self.preparer.root.glob("c_*.json"):
            try:
                self.tick_company(path)
            except (ValueError, OSError, KeyError, subprocess.SubprocessError):
                logging.getLogger(__name__).warning(
                    "Tenant supervisor binding failed; other companies remain running"
                )

    def tick_company(self, path):
        config = read_operator_json(path)
        company = self.preparer.repo.get(config["company_id"])
        if company.get("status") != "active" or company.get("archived_at"):
            for key in list(self.children):
                if key[0] == str(path):
                    self.stop(key)
            return
        version = company["version"]
        for key in list(self.children):
            if key[0] == str(path) and self.versions.get(key) != version:
                self.stop(key)
        with self.preparer.repo.connect() as db:
            state = db.execute(
                "SELECT checks,error_code,state,configuration_version "
                "FROM runtime_provisioning WHERE company_id=%s",
                (company["id"],),
            ).fetchone()
        from app.tenancy.migrations import migration_fingerprint

        checks = state["checks"] if state and state["configuration_version"] == version else {}
        migration = checks.get("migrations", {})
        evidence = migration.get("evidence")
        migrated = (
            migration.get("ok") is True
            and isinstance(evidence, dict)
            and evidence.get("manifest_fingerprint") == migration_fingerprint()
        )
        if not migrated:
            # Cached database-role success must not launch or retain clients while
            # an upgraded migration needs exclusive table locks. Stop only owned
            # company services, before allowing the operator to begin that work.
            for key in list(self.children):
                if key[0] == str(path) and key[1] != "work":
                    self.stop(key)
        self.start(path, "work", version)
        if not migrated:
            return
        if checks.get("database_roles", {}).get("ok"):
            self.start(path, "run-collector", version)
            self.start(path, "run-portal", version)
            if config.get("document_settings", {}).get("worker_enabled"):
                self.start(path, "run-documents-worker", version)
        if checks.get("initial_sync", {}).get("ok"):
            self.start(path, "run-scheduler", version)
        missing = checks.get("modules", {}).get("evidence", {}).get("missing", [])
        transient = state["error_code"] == "runtime_process_pending" or (
            missing and set(missing) <= {"documents_worker_not_ready", "scheduler_not_ready"}
        )
        if state["state"] == "failed" and transient:
            due = self.retry_after.setdefault(str(path), time.monotonic() + 30)
            if time.monotonic() >= due:
                from .runtime_operator import RuntimeOperator

                operator = RuntimeOperator(config, self.preparer.repo)
                operator.provisioner().enqueue(
                    company, str(operator.runtime.runtime_path("portal.sock"))
                )
                self.retry_after[str(path)] = time.monotonic() + 60

    def close(self):
        for key in list(self.children):
            self.stop(key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "action", choices=["prepare", "work", "serve-verifier"], default="prepare", nargs="?"
    )
    args = parser.parse_args()
    os.umask(0o077)
    config = read_operator_json(args.config)
    preparer = FleetPreparer(config)
    if args.action == "serve-verifier":
        from .fleet_discovery import build_fleet_verifier
        from .runtime_process_identity import serve_verifier_socket

        app, repo, _ = build_fleet_verifier(config, preparer.repo)
        socket = Path(config["operator_template"]["verifier_socket"])
        try:
            serve_verifier_socket(app, socket, config["operator_template"])
        finally:
            repo.auth.close()
    elif args.action == "work":
        supervisor = FleetSupervisor(preparer)

        def interrupt(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupt)
        signal.signal(signal.SIGINT, interrupt)
        try:
            while True:
                supervisor.tick()
                time.sleep(3)
        except KeyboardInterrupt:
            pass
        finally:
            supervisor.close()
    else:
        prepared = preparer.prepare_pending()
        print(f"Prepared {len(prepared)} private company configurations; no processes started")


if __name__ == "__main__":
    main()
