"""Support observer for trusted fleet manifests; no credentials enter tenant children."""

import os
from uuid import UUID

from psycopg.conninfo import conninfo_to_dict

from app.support.models import Context
from app.support.store import SupportStore
from app.support_background import BackgroundMonitor
from app.tenancy.config import schema_names


def build_monitor(preparer):
    dsn = os.environ.get("SUPPORT_DATABASE_URL") or getattr(preparer, "template", {}).get(
        "registry_dsn"
    )
    return BackgroundMonitor(SupportStore(dsn), retention=True) if dsn else None


def observe(monitor, config, company):
    if monitor is None:
        return
    company_id = UUID(str(company["id"]))
    if UUID(config["company_id"]) != company_id:
        raise ValueError("Support runtime company mismatch")
    # Reuse the runtime's restricted principal, never the DDL operator DSN.
    if conninfo_to_dict(config["runtime_dsn"]).get("user") != f"c_{company_id.hex}_runtime":
        raise ValueError("Support requires the company runtime role")
    context = Context(str(company_id), company["name"], "system")
    monitor.poll(context, config["runtime_dsn"], schema_names(company_id))
    return context
