"""Resumable operator job; adapters must perform real checks and return evidence.

No background work is launched in HTTP requests. The operator repeatedly calls
run() after enqueuing; transaction advisory locks serialize each company. External
adapters MUST be idempotent on (company_id, configuration_version, step).
"""

from psycopg.types.json import Jsonb

from .runtime_registry import REQUIRED_CHECKS, ReadyRuntime

STEPS = (
    "migrations",
    "database_roles",
    "identity",
    "connections",
    "initial_sync",
    "modules",
    "payments",
    "dns_tls",
    "runtime_health",
)
assert set(STEPS) == REQUIRED_CHECKS


def additive_migration_refresh(checks):
    """Only the reviewed append-only bot maintenance may retain module evidence."""
    import hashlib
    import json

    from app.tenancy.migrations import load_migrations

    previous = checks.get("migrations", {})
    if previous.get("ok") is not True:
        return False
    fingerprint = previous.get("evidence", {}).get("manifest_fingerprint")
    entries = [(item.name, item.area, item.checksum) for item in load_migrations()]
    approved = {
        "20261010110000_tenant_telegram_bot_namespace.sql",
    }
    for count in range(1, len(entries)):
        if hashlib.sha256(json.dumps(entries[:count]).encode()).hexdigest() == fingerprint:
            return all(name in approved for name, _area, _checksum in entries[count:])
    return False


class PendingCheck(Exception):
    """Sanitized, adapter-owned missing configuration, never arbitrary exception text."""

    def __init__(self, code, details):
        self.code, self.details = code, details
        super().__init__(code)


class Provisioner:
    def __init__(self, repository, adapters):
        if set(adapters) != REQUIRED_CHECKS:
            raise ValueError("All required real provisioning adapters must be configured")
        self.repository, self.adapters = repository, adapters

    def enqueue(self, company, socket_path):
        ReadyRuntime(str(company["id"]), company["version"], socket_path)
        with self.repository.connect(True) as db:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("provision:" + str(company["id"]),),
            )
            db.execute(
                "INSERT INTO "
                "restcontrol.runtime_provisioning(company_id,configuration_version,socket_path) "
                "VALUES(%s,%s,%s) ON CONFLICT(company_id) DO UPDATE SET "
                "configuration_version=excluded.configuration_version, "
                "socket_path=excluded.socket_path,checks=CASE "
                "WHEN "
                "runtime_provisioning.configuration_version=excluded.configuration_version "
                "THEN runtime_provisioning.checks-'migration_refresh' ELSE '{}'::jsonb "
                "END,state='pending',error_code=NULL,updated_at=now()",
                (company["id"], company["version"], socket_path),
            )

    def enqueue_migration_refresh(self, company, socket_path):
        """Persist maintenance intent without re-running independent acceptance checks."""
        from app.tenancy.migrations import migration_fingerprint

        target = migration_fingerprint()
        ReadyRuntime(str(company["id"]), company["version"], socket_path)
        with self.repository.connect(True) as db:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("provision:" + str(company["id"]),),
            )
            row = db.execute(
                "SELECT * FROM restcontrol.runtime_provisioning WHERE company_id=%s "
                "AND configuration_version=%s FOR UPDATE",
                (company["id"], company["version"]),
            ).fetchone()
            existing = row["checks"].get("migration_refresh", {}) if row else {}
            if existing and existing.get("target_manifest_fingerprint") != target:
                return False
            if (
                not row
                or row["state"] not in {"ready", "failed"}
                or not (
                    additive_migration_refresh(row["checks"])
                    or (
                        row["checks"].get("migration_refresh", {}).get("configuration_version")
                        == company["version"]
                        and row["checks"]
                        .get("migration_refresh", {})
                        .get("target_manifest_fingerprint")
                        == target
                    )
                )
            ):
                return False
            checks = dict(row["checks"])
            checks["migration_refresh"] = checks.get("migration_refresh") or {
                "configuration_version": company["version"],
                "target_manifest_fingerprint": target,
                "state": row["state"],
                "step": row["step"],
                "error_code": row["error_code"],
            }
            db.execute(
                "UPDATE restcontrol.runtime_provisioning SET checks=%s,state='pending',"
                "error_code=NULL,updated_at=now() WHERE company_id=%s AND configuration_version=%s",
                (Jsonb(checks), company["id"], company["version"]),
            )
            return True

    def run(self, company_id):
        # Session lock survives per-stage commits; crashes release it automatically.
        with self.repository.connect(True) as db:
            locked = db.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked",
                ("provision:" + str(company_id),),
            ).fetchone()["locked"]
            if not locked:
                return False
            try:
                row = db.execute(
                    "SELECT * FROM restcontrol.runtime_provisioning WHERE company_id=%s FOR UPDATE",
                    (company_id,),
                ).fetchone()
                if not row:
                    raise ValueError("Provisioning must be enqueued first")
                version, checks = row["configuration_version"], dict(row["checks"])
                db.commit()
                from app.tenancy.migrations import migration_fingerprint

                refresh = checks.get("migration_refresh", {})
                migration_only = (
                    refresh.get("configuration_version") == version
                    and refresh.get("state") in {"ready", "failed"}
                    and refresh.get("target_manifest_fingerprint") == migration_fingerprint()
                )
                if refresh and not migration_only:
                    checks.pop("migration_refresh", None)
                    db.execute(
                        "UPDATE restcontrol.runtime_provisioning SET checks=%s "
                        "WHERE company_id=%s AND configuration_version=%s",
                        (Jsonb(checks), company_id, version),
                    )
                    db.commit()
                pending = []
                for step in ("migrations",) if migration_only else STEPS:
                    cached = checks.get(step, {})
                    proof = cached.get("evidence")
                    compatible = True
                    if step == "migrations":
                        from app.tenancy.migrations import migration_fingerprint

                        compatible = isinstance(proof, dict) and (
                            proof.get("manifest_fingerprint") == migration_fingerprint()
                        )
                    elif step == "initial_sync":
                        from .initial_sync_plan import initial_sync_compatible

                        compatible = initial_sync_compatible(cached)
                    elif step == "modules":
                        compatible = (
                            isinstance(proof, dict)
                            and str(proof.get("company_id")) == str(company_id)
                            and proof.get("configuration_version") == version
                            and isinstance(proof.get("services"), dict)
                        )
                    elif step == "payments":
                        compatible = isinstance(proof, dict) and (
                            proof.get("enabled") is False
                            or (
                                isinstance(proof.get("terminals"), list)
                                and bool(proof["terminals"])
                                and all(
                                    isinstance(item, dict) and item.get("terminal_version_id")
                                    for item in proof.get("terminals", [])
                                )
                            )
                        )
                    if cached.get("ok") is True and proof and compatible:
                        continue
                    db.execute(
                        "UPDATE restcontrol.runtime_provisioning SET "
                        "state='running',step=%s,attempts=attempts+1,updated_at=now() "
                        "WHERE company_id=%s AND configuration_version=%s",
                        (step, company_id, version),
                    )
                    db.commit()
                    try:
                        evidence = self.adapters[step](str(company_id), version)
                        if (
                            not isinstance(evidence, dict)
                            or evidence.get("ok") is not True
                            or not evidence.get("evidence")
                        ):
                            raise ValueError("Acceptance check did not succeed")
                        checks[step] = evidence
                        updated = db.execute(
                            "UPDATE restcontrol.runtime_provisioning SET "
                            "checks=%s,error_code=NULL,updated_at=now() "
                            "WHERE company_id=%s AND "
                            "configuration_version=%s RETURNING company_id",
                            (Jsonb(checks), company_id, version),
                        ).fetchone()
                        db.commit()
                        if not updated:
                            return False
                    except Exception as error:
                        db.rollback()
                        if not isinstance(error, PendingCheck):
                            checks.pop(step, None)
                            db.execute(
                                "UPDATE restcontrol.runtime_provisioning SET checks=%s "
                                "WHERE company_id=%s AND configuration_version=%s",
                                (Jsonb(checks), company_id, version),
                            )
                        if isinstance(error, PendingCheck):
                            checks[step] = {
                                "ok": False,
                                "code": error.code,
                                "evidence": error.details,
                            }
                            db.execute(
                                "UPDATE restcontrol.runtime_provisioning SET checks=%s "
                                "WHERE company_id=%s AND configuration_version=%s",
                                (Jsonb(checks), company_id, version),
                            )
                        db.execute(
                            "UPDATE restcontrol.runtime_provisioning SET "
                            "state='failed',error_code=%s,updated_at=now() "
                            "WHERE company_id=%s AND "
                            "configuration_version=%s",
                            (
                                error.code if isinstance(error, PendingCheck) else "check_failed",
                                company_id,
                                version,
                            ),
                        )
                        db.commit()
                        if isinstance(error, PendingCheck) and step in {"modules", "payments"}:
                            # Provider/module configuration is independent of public DNS
                            # and process health. Preserve failed evidence and inspect all
                            # remaining independent stages without inventing full readiness.
                            pending.append((step, error.code))
                            continue
                        return False
                if migration_only:
                    checks.pop("migration_refresh", None)
                    db.execute(
                        "UPDATE restcontrol.runtime_provisioning SET "
                        "checks=%s,state=%s,step=%s,error_code=%s,"
                        "updated_at=now() WHERE company_id=%s AND configuration_version=%s",
                        (
                            Jsonb(checks),
                            refresh["state"],
                            refresh.get("step"),
                            refresh.get("error_code"),
                            company_id,
                            version,
                        ),
                    )
                    db.commit()
                    return True
                if pending:
                    db.execute(
                        "UPDATE restcontrol.runtime_provisioning SET state='failed',"
                        "step=%s,error_code=%s,updated_at=now() WHERE company_id=%s "
                        "AND configuration_version=%s",
                        (*pending[0], company_id, version),
                    )
                    db.commit()
                    return False
                db.execute(
                    "UPDATE restcontrol.runtime_provisioning SET "
                    "state='ready',active_socket_path=socket_path,active_version=configuration_version,"
                    "step=NULL,updated_at=now() WHERE "
                    "company_id=%s AND configuration_version=%s",
                    (company_id, version),
                )
                db.commit()
                return True
            finally:
                db.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                    ("provision:" + str(company_id),),
                )
