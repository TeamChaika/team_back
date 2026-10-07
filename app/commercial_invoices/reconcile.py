"""Resolve accepted/uncertain imports by read-only export, never resubmission."""


def reconcile_one(service):
    with service.database.connection() as db:
        row = db.execute(
            "SELECT d.* FROM commercial_invoice_dispatch q JOIN commercial_invoices d "
            "ON d.id=q.document_id AND d.version=q.version WHERE "
            "q.state IN ('accepted','unknown') AND d.state IN ('accepted','unknown') "
            "AND q.next_attempt_at<=now() ORDER BY q.next_attempt_at "
            "FOR UPDATE OF q SKIP LOCKED LIMIT 1"
        ).fetchone()
        if not row:
            return False
        db.execute(
            "UPDATE commercial_invoice_dispatch SET next_attempt_at=now()+interval '5 minutes' "
            "WHERE document_id=%s",
            (row["id"],),
        )
    snapshot = {
        **row["snapshot"],
        "id": str(row["id"]),
        "number": row["number"],
        "version": row["version"],
    }
    with service.provider.session() as (client, token):
        outcome = service.provider.lookup(client, token, row["kind"], snapshot)
    if not outcome:
        return True
    with service.database.connection() as db:
        changed = db.execute(
            "UPDATE commercial_invoices SET state=%s,iiko_id=%s,updated_at=now() "
            "WHERE id=%s AND version=%s AND state IN ('accepted','unknown') "
            "AND (state<>%s OR iiko_id IS DISTINCT FROM %s::uuid) RETURNING id",
            (
                outcome["state"],
                outcome["iiko_id"],
                row["id"],
                row["version"],
                outcome["state"],
                outcome["iiko_id"],
            ),
        ).fetchone()
        if changed:
            db.execute(
                "UPDATE commercial_invoice_dispatch SET state=%s,updated_at=now(),last_error=NULL "
                "WHERE document_id=%s AND version=%s",
                (outcome["state"], row["id"], row["version"]),
            )
            db.execute(
                "INSERT INTO commercial_invoice_events(document_id,version,action) "
                "VALUES(%s,%s,%s)",
                (row["id"], row["version"], "iiko_" + outcome["state"]),
            )
    return True
