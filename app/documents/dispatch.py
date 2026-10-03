"""Durable queue: retry connectivity, never blindly repeat an ambiguous POST."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from app.documents.policy import table
from app.documents.reads import summary
from app.documents.workflow import event, finish


def retry_delay(attempts):
    return min(600, 15 * 2 ** min(max(attempts - 1, 0), 6))


def claim(database):
    with database.connection() as db:
        job = db.execute(
            "SELECT * FROM native_dispatch WHERE "
            "(state='ready' AND next_attempt_at<=now()) OR "
            "(state='connecting' AND claimed_at<now()-interval '5 minutes') "
            "ORDER BY next_attempt_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()
        if not job:
            return None
        claim_id = uuid4()
        return db.execute(
            "UPDATE native_dispatch SET state='connecting',claim_id=%s,claimed_at=now(),"
            "attempts=attempts+1,updated_at=now() WHERE operation_id=%s RETURNING *",
            (claim_id, job["operation_id"]),
        ).fetchone()


def retry(database, job):
    with database.connection() as db:
        db.execute(
            "UPDATE native_dispatch SET state='ready',next_attempt_at=%s,last_error='connection',"
            "updated_at=now() WHERE operation_id=%s AND claim_id=%s AND state='connecting'",
            (
                datetime.now(UTC) + timedelta(seconds=retry_delay(job["attempts"])),
                job["operation_id"],
                job["claim_id"],
            ),
        )


def reserve(database, job):
    parent, _ = table(job["kind"])
    with database.connection() as db:
        current = db.execute(
            "SELECT * FROM native_dispatch WHERE operation_id=%s FOR UPDATE", (job["operation_id"],)
        ).fetchone()
        if current["claim_id"] != job["claim_id"] or current["state"] != "connecting":
            return False
        doc = db.execute(
            f"SELECT * FROM {parent} WHERE id=%s FOR UPDATE", (job["document_id"],)
        ).fetchone()
        if (
            not doc
            or doc["version"] != job["version"]
            or doc["submission_state"] != "queued"
            or doc["status"] != "Created"
            or doc.get("receipt_state") == "pending_sender"
        ):
            db.execute(
                "UPDATE native_dispatch SET state='obsolete',updated_at=now() WHERE "
                "operation_id=%s",
                (job["operation_id"],),
            )
            return False
        doc = db.execute(
            f"UPDATE {parent} SET submission_state='sending' WHERE id=%s RETURNING *", (doc["id"],)
        ).fetchone()
        db.execute(
            "UPDATE native_dispatch SET state='sending',updated_at=now() WHERE operation_id=%s",
            (job["operation_id"],),
        )
        finish(db, job["operation_id"], summary(job["kind"], doc), "pending")
        return True


def complete(database, job, outcome):
    parent, _ = table(job["kind"])
    with database.connection() as db:
        # Use the same lock order as reserve and reconciliation.
        current = db.execute(
            "SELECT * FROM native_dispatch WHERE operation_id=%s FOR UPDATE", (job["operation_id"],)
        ).fetchone()
        doc = db.execute(
            f"SELECT * FROM {parent} WHERE id=%s FOR UPDATE", (job["document_id"],)
        ).fetchone()
        if (
            current["claim_id"] != job["claim_id"]
            or current["state"] != "sending"
            or not doc
            or doc["version"] != job["version"]
            or doc["submission_state"] != "sending"
        ):
            return
        if outcome == "not_sent":
            doc = db.execute(
                f"UPDATE {parent} SET submission_state='queued' WHERE id=%s RETURNING *",
                (doc["id"],),
            ).fetchone()
            db.execute(
                "UPDATE native_dispatch SET "
                "state='ready',next_attempt_at=%s,last_error='connection',updated_at=now() "
                "WHERE operation_id=%s",
                (
                    datetime.now(UTC) + timedelta(seconds=retry_delay(job["attempts"])),
                    job["operation_id"],
                ),
            )
            finish(db, job["operation_id"], summary(job["kind"], doc), "queued")
            return
        doc = db.execute(
            f"UPDATE {parent} SET submission_state=%s,status=%s,processed_at=%s "
            "WHERE id=%s RETURNING *",
            (
                outcome,
                "Sent" if outcome == "sent" else doc["status"],
                datetime.now(UTC) if outcome == "sent" else doc["processed_at"],
                doc["id"],
            ),
        ).fetchone()
        db.execute(
            "UPDATE native_dispatch SET state=%s,last_error=%s,updated_at=now() WHERE "
            "operation_id=%s",
            (outcome, None if outcome == "sent" else outcome, job["operation_id"]),
        )
        op = db.execute(
            "SELECT actor_id FROM portal_documents_operation WHERE id=%s", (job["operation_id"],)
        ).fetchone()
        finish(
            db,
            job["operation_id"],
            summary(job["kind"], doc),
            "unknown" if outcome == "unknown" else "done",
        )
        event(db, job["kind"], doc, op["actor_id"], "iiko_" + outcome)


def deliver_one(service):
    job = claim(service.database)
    if not job:
        return False
    reserved = False
    outcome = "unknown"
    try:
        # Failed authentication/connectivity is provably before the document POST.
        with service.provider.session() as (client, token):
            reserved = reserve(service.database, job)
            if not reserved:
                return True
            payload = job["payload"]
            value = payload["xml"].encode() if "xml" in payload else payload["json"]
            try:
                outcome = service.provider.send_authenticated(client, token, job["kind"], value)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                outcome = "not_sent"  # Connection establishment failed before HTTP bytes.
            except Exception:
                outcome = "unknown"
    except Exception:
        if not reserved:
            retry(service.database, job)
            return True
        # A logout failure must not erase a successful document response.
    if outcome not in {"sent", "rejected", "not_sent"}:
        outcome = "unknown"
    complete(service.database, job, outcome)
    return True


def recover_uncertain(database):
    """A killed process may have posted; mark for review, never put it back in the queue."""
    with database.connection() as db:
        jobs = db.execute(
            "SELECT * FROM native_dispatch WHERE state='sending' "
            "AND updated_at<now()-interval '5 minutes' FOR UPDATE SKIP LOCKED"
        ).fetchall()
        for job in jobs:
            parent, _ = table(job["kind"])
            doc = db.execute(
                f"SELECT * FROM {parent} WHERE id=%s FOR UPDATE", (job["document_id"],)
            ).fetchone()
            if not doc or doc["version"] != job["version"] or doc["submission_state"] != "sending":
                continue
            doc = db.execute(
                f"UPDATE {parent} SET submission_state='unknown' WHERE id=%s RETURNING *",
                (doc["id"],),
            ).fetchone()
            op = db.execute(
                "SELECT actor_id FROM portal_documents_operation WHERE id=%s",
                (job["operation_id"],),
            ).fetchone()
            db.execute(
                "UPDATE native_dispatch SET state='unknown',last_error='interrupted',"
                "updated_at=now() WHERE operation_id=%s",
                (job["operation_id"],),
            )
            finish(db, job["operation_id"], summary(job["kind"], doc), "unknown")
            event(db, job["kind"], doc, op["actor_id"], "iiko_unknown")
