"""Durable commercial queue. Interrupted POSTs are quarantined, never replayed."""

from uuid import uuid4


def recover_uncertain(database):
    with database.connection() as db:
        jobs = db.execute(
            "SELECT document_id,version FROM commercial_invoice_dispatch "
            "WHERE state='sending' AND updated_at<now()-interval '5 minutes' "
            "FOR UPDATE SKIP LOCKED"
        ).fetchall()
        for job in jobs:
            db.execute(
                (
                    "UPDATE commercial_invoice_dispatch SET "
                    "state='unknown',last_error='interrupted',updated_at=now() WHERE "
                    "document_id=%s"
                ),
                (job["document_id"],),
            )
            db.execute(
                (
                    "UPDATE commercial_invoices SET state='unknown',updated_at=now() "
                    "WHERE id=%s AND version=%s AND state='sending'"
                ),
                (job["document_id"], job["version"]),
            )
            db.execute(
                (
                    "INSERT INTO "
                    "commercial_invoice_events(document_id,version,action) "
                    "VALUES(%s,%s,'iiko_unknown')"
                ),
                (job["document_id"], job["version"]),
            )


def claim(database):
    with database.connection() as db:
        job = db.execute(
            "SELECT * FROM commercial_invoice_dispatch WHERE (state='ready' "
            "AND next_attempt_at<=now()) OR (state='connecting' AND "
            "claimed_at<now()-interval '5 minutes') ORDER BY next_attempt_at "
            "FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()
        if not job:
            return None
        return db.execute(
            (
                "UPDATE commercial_invoice_dispatch SET "
                "state='connecting',claim_id=%s,claimed_at=now(),"
                "attempts=attempts+1,updated_at=now() WHERE "
                "document_id=%s RETURNING *"
            ),
            (uuid4(), job["document_id"]),
        ).fetchone()


def retry(database, job):
    with database.connection() as db:
        db.execute(
            (
                "UPDATE commercial_invoice_dispatch SET "
                "state='ready',last_error='connection',next_attempt_at=now()+(%s "
                "* interval '1 second'),updated_at=now() WHERE document_id=%s AND "
                "claim_id=%s AND state='connecting'"
            ),
            (min(600, 15 * 2 ** min(job["attempts"], 5)), job["document_id"], job["claim_id"]),
        )


def reserve(database, job):
    with database.connection() as db:
        current = db.execute(
            "SELECT * FROM commercial_invoice_dispatch WHERE document_id=%s FOR UPDATE",
            (job["document_id"],),
        ).fetchone()
        if current["state"] != "connecting" or current["claim_id"] != job["claim_id"]:
            return None
        row = db.execute(
            (
                "UPDATE commercial_invoices SET state='sending',updated_at=now() "
                "WHERE id=%s AND version=%s AND state='queued' RETURNING kind"
            ),
            (job["document_id"], job["version"]),
        ).fetchone()
        if not row:
            db.execute(
                (
                    "UPDATE commercial_invoice_dispatch SET "
                    "state='obsolete',updated_at=now() WHERE document_id=%s"
                ),
                (job["document_id"],),
            )
            return None
        db.execute(
            (
                "UPDATE commercial_invoice_dispatch SET "
                "state='sending',updated_at=now() WHERE document_id=%s"
            ),
            (job["document_id"],),
        )
        return row["kind"]


def complete(database, job, outcome):
    state = outcome.get("state", "unknown") if isinstance(outcome, dict) else "unknown"
    if state not in {"accepted", "processed", "rejected", "unknown"}:
        state = "unknown"
    with database.connection() as db:
        current = db.execute(
            "SELECT * FROM commercial_invoice_dispatch WHERE document_id=%s FOR UPDATE",
            (job["document_id"],),
        ).fetchone()
        if current["state"] != "sending" or current["claim_id"] != job["claim_id"]:
            return
        db.execute(
            (
                "UPDATE commercial_invoice_dispatch SET "
                "state=%s,last_error=%s,updated_at=now() WHERE document_id=%s"
            ),
            (state, None if state in {"accepted", "processed"} else state, job["document_id"]),
        )
        db.execute(
            (
                "UPDATE commercial_invoices SET "
                "state=%s,iiko_id=%s,updated_at=now() WHERE id=%s AND version=%s "
                "AND state='sending'"
            ),
            (
                state,
                outcome.get("iiko_id") if isinstance(outcome, dict) else None,
                job["document_id"],
                job["version"],
            ),
        )
        db.execute(
            "INSERT INTO commercial_invoice_events(document_id,version,action) VALUES(%s,%s,%s)",
            (job["document_id"], job["version"], "iiko_" + state),
        )


def deliver_one(service):
    if not service.submit_enabled:
        return False
    job = claim(service.database)
    if not job:
        return False
    reserved = False
    outcome = {"state": "unknown"}
    try:
        with service.provider.session() as (client, token):
            kind = reserve(service.database, job)
            if kind is None:
                return True
            reserved = True
            try:
                outcome = service.provider.send_authenticated(
                    client, token, kind, job["payload"]["xml"].encode()
                )
            except Exception:
                # Even a network exception can follow a committed remote write.
                outcome = {"state": "unknown"}
    except Exception:
        if not reserved:
            retry(service.database, job)
            return True
    complete(service.database, job, outcome)
    return True
