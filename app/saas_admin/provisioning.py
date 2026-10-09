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
                "THEN runtime_provisioning.checks ELSE '{}'::jsonb "
                "END,state='pending',error_code=NULL,updated_at=now()",
                (company["id"], company["version"], socket_path),
            )

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
                for step in STEPS:
                    if checks.get(step, {}).get("ok") is True and checks[step].get("evidence"):
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
