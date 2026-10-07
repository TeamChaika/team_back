"""One durable POST, then read-only reconciliation for every uncertain outcome."""

from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.commercial_invoices.counterparties import CATALOG_LOCK, require_creator
from app.commercial_invoices.counterparty_models import candidates
from app.commercial_invoices.counterparty_transport import source_key


def recover(database):
    with database.connection() as db:
        rows = db.execute(
            "UPDATE commercial_counterparty_operations SET state='unknown',error_code='unknown',"
            "updated_at=now() WHERE state='sending' "
            "AND updated_at<now()-interval '5 minutes' RETURNING id"
        ).fetchall()
        for row in rows:
            db.execute(
                "INSERT INTO commercial_counterparty_events(operation_id,state) "
                "VALUES(%s,'unknown')",
                (row["id"],),
            )


def claim(database):
    with database.connection() as db:
        row = db.execute(
            "SELECT * FROM commercial_counterparty_operations WHERE "
            "(state='queued' AND next_attempt_at<=now()) OR "
            "(state='connecting' AND updated_at<now()-interval '5 minutes') "
            "ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return db.execute(
            "UPDATE commercial_counterparty_operations SET state='connecting',claim_id=%s,"
            "attempts=attempts+1,updated_at=now() WHERE id=%s RETURNING *",
            (uuid4(), row["id"]),
        ).fetchone()


def finish(database, job, state, *, error=None, party=None, duplicates=None):
    with database.connection() as db:
        current = db.execute(
            "SELECT * FROM commercial_counterparty_operations WHERE id=%s FOR UPDATE",
            (job["id"],),
        ).fetchone()
        if current["claim_id"] != job["claim_id"] or current["state"] not in {
            "connecting",
            "sending",
            "unknown",
        }:
            return
        if party:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (CATALOG_LOCK,))
            db.execute(
                "INSERT INTO commercial_counterparties(id,source_key,data,operation_id) "
                "VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET data=EXCLUDED.data,verified_at=now()",
                (job["iiko_id"], job["source_key"], Jsonb(party), job["id"]),
            )
            for kind in ("purchase", "sale"):
                name = "commercial_" + kind + "_counterparties"
                cache = db.execute(
                    "SELECT data FROM native_catalog WHERE name=%s FOR UPDATE", (name,)
                ).fetchone()
                if cache:
                    rows = {row["id"]: row for row in cache["data"]}
                    rows[party["id"]] = party
                    # Do not make the entire dictionary appear fresh because one row was read.
                    db.execute(
                        "UPDATE native_catalog SET data=%s WHERE name=%s",
                        (Jsonb(list(rows.values())), name),
                    )
        db.execute(
            "UPDATE commercial_counterparty_operations SET state=%s,error_code=%s,candidates=%s,"
            "next_attempt_at=now()+interval '5 minutes',updated_at=now() WHERE id=%s",
            (state, error, Jsonb(duplicates) if duplicates else None, job["id"]),
        )
        db.execute(
            "INSERT INTO commercial_counterparty_events(operation_id,state) VALUES(%s,%s)",
            (job["id"], state),
        )


def reserve(service, job):
    with service.database.connection() as db:
        current = db.execute(
            "SELECT * FROM commercial_counterparty_operations WHERE id=%s FOR UPDATE", (job["id"],)
        ).fetchone()
        if current["claim_id"] != job["claim_id"] or current["state"] != "connecting":
            return False
        user = require_creator(db, job["portal_id"], job["kind"])
        if user["id"] != job["actor_id"]:
            return False
        db.execute(
            "UPDATE commercial_counterparty_operations SET state='sending',updated_at=now() "
            "WHERE id=%s",
            (job["id"],),
        )
        db.execute(
            "INSERT INTO commercial_counterparty_events(operation_id,state) VALUES(%s,'sending')",
            (job["id"],),
        )
        return True


def deliver_one(service):
    if not service.counterparty_enabled:
        return False
    job = claim(service.database)
    if not job:
        return False
    provider = service.counterparty_provider
    if job["source_key"] != source_key(provider):
        finish(service.database, job, "rejected", error="source_changed")
        return True
    sent = False
    try:
        with provider.session() as (client, token):
            duplicates = candidates(job["payload"], provider.registry(client, token))
            if duplicates:
                finish(service.database, job, "rejected", error="duplicate", duplicates=duplicates)
                return True
            for field, error in (("identifier", "uuid_conflict"), ("code", "code_conflict")):
                value = job["iiko_id"] if field == "identifier" else job["code"]
                if provider.exists(client, token, **{field: value}):
                    finish(service.database, job, "rejected", error=error)
                    return True
            try:
                reserved = reserve(service, job)
            except HTTPException:
                reserved = False
            if not reserved:
                finish(service.database, job, "rejected", error="access_revoked")
                return True
            sent = True  # Committed before POST, so process death can only lead to GET.
            state = provider.create(client, token, job)
            if state == "rejected":
                finish(service.database, job, "rejected", error="rejected")
                return True
            party = provider.lookup(client, token, job)
            finish(
                service.database,
                job,
                "confirmed" if party else "unknown",
                party=party,
                error=None if party else "unknown",
            )
    except Exception:
        if sent:
            finish(service.database, job, "unknown", error="unknown")
        else:
            # Auth, registry and preflight are GET-only; retrying before reservation is safe.
            with service.database.connection() as db:
                db.execute(
                    "UPDATE commercial_counterparty_operations SET state='queued',"
                    "next_attempt_at=now()+interval '1 minute',updated_at=now() "
                    "WHERE id=%s AND claim_id=%s AND state='connecting'",
                    (job["id"], job["claim_id"]),
                )
    return True


def reconcile_one(service):
    with service.database.connection() as db:
        job = db.execute(
            "SELECT * FROM commercial_counterparty_operations WHERE state='unknown' AND "
            "next_attempt_at<=now() ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()
        if not job:
            return False
        db.execute(
            "UPDATE commercial_counterparty_operations "
            "SET next_attempt_at=now()+interval '5 minutes' WHERE id=%s",
            (job["id"],),
        )
    provider = service.counterparty_provider
    if job["source_key"] != source_key(provider):
        # An old target may still hold a committed record. Keep it unknown, never retry elsewhere.
        return True
    with provider.session() as (client, token):
        party = provider.lookup(client, token, job)
    if party:
        finish(service.database, job, "confirmed", party=party)
    return True
