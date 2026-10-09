"""Project already-authorized primary administrators into their empty company runtime.

No Auth account or credential is created or changed here. A completed, matching
central provisioning receipt authorizes only a fresh company administrator profile;
all existing profiles retain their local permissions and password state.
"""

from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.tenancy.sql import render

from .company_accounts import IdentityTarget
from .provisioning import PendingCheck


def _pending():
    return PendingCheck("company_identity_projection_required", {})


def ensure_company_identities(operator):
    runtime = operator.runtime
    projected = 0
    with operator.repo.connect(True) as central:
        company = central.execute(
            "SELECT version,status,archived_at FROM companies WHERE id=%s FOR UPDATE",
            (runtime.company_id,),
        ).fetchone()
        if (
            not company
            or company["version"] != runtime.configuration_version
            or company["status"] != "active"
            or company["archived_at"]
        ):
            raise _pending()
        members = central.execute(
            "SELECT m.*,i.email AS identity_email,i.provision_id AS identity_marker,"
            "p.request_id AS journal_request,p.auth_user_id AS journal_user,"
            "p.username AS journal_email,p.state AS journal_state,"
            "EXISTS(SELECT 1 FROM platform_memberships g WHERE g.auth_user_id=m.auth_user_id) "
            "AS platform_identity FROM memberships m "
            "LEFT JOIN auth_identities i ON i.id=m.auth_user_id "
            "LEFT JOIN auth_provisioning p ON p.company_id=m.company_id "
            "WHERE m.company_id=%s AND m.active AND m.auth_user_id IS NOT NULL "
            "ORDER BY m.id FOR UPDATE OF m",
            (runtime.company_id,),
        ).fetchall()
        with psycopg.connect(operator.config["runtime_dsn"], row_factory=dict_row) as own:
            registered = {
                str(row["id"]): row
                for row in own.execute(
                    render(
                        "SELECT id,email,provision_id FROM {analytics}.portal_identity_metadata",
                        runtime,
                    )
                )
            }
        for member in members:
            email = str(member["username"]).strip().casefold()
            if not member["identity_email"] or member["identity_email"].strip().casefold() != email:
                raise _pending()
            existing = registered.get(str(member["auth_user_id"]))
            if existing:
                if (
                    existing["email"].strip().casefold() != email
                    or not member["identity_marker"]
                    or str(existing["provision_id"]) != str(member["identity_marker"])
                ):
                    raise _pending()
                # Never reapply administrator permissions or password flags on replay.
                continue
            marker = member["identity_marker"]
            try:
                UUID(str(marker))
            except (ValueError, TypeError, AttributeError):
                raise _pending() from None
            if (
                member["role"] != "company_admin"
                or member["is_primary_admin"] is not True
                or member["auth_exclusive"] is not True
                or member["platform_identity"]
                or member["journal_state"] != "complete"
                or member["journal_user"] != member["auth_user_id"]
                or str(member["journal_request"]) != str(marker)
                or str(member["journal_email"]).strip().casefold() != email
                or type(member["must_change"]) is not bool
            ):
                raise _pending()
            target = IdentityTarget(runtime, operator.config["identity_dsn"])
            target.provision_primary(
                member["auth_user_id"],
                email,
                str(marker),
                member["display_name"],
                member["must_change"],
            )
            projected += 1
        with psycopg.connect(operator.config["runtime_dsn"], row_factory=dict_row) as own:
            verified = {
                str(row["id"]): row
                for row in own.execute(
                    render("SELECT id,email FROM {analytics}.portal_identities", runtime)
                )
            }
        if any(
            str(member["auth_user_id"]) not in verified
            or verified[str(member["auth_user_id"])]["email"].strip().casefold()
            != member["username"].strip().casefold()
            for member in members
        ):
            raise _pending()
    return {"registered_members": len(members), "projected_primary_admins": projected}
