"""The live eligibility and deadline used to display, remind and auto-receive waybills."""

from app.operation_settings import load_settings


def deadlines_query(db):
    selection = load_settings(db)["waybill"]
    query = """
        SELECT w.id,w.version,w.created_at,w.counteragent_id,
               greatest(w.created_at,history.items_changed_at) + interval '72 hours' AS due_at
        FROM waybills w
        JOIN waybill_auto_receipt_policy policy ON policy.singleton
            AND w.created_at >= policy.enabled_at
        CROSS JOIN LATERAL (
            SELECT max(e.created_at) FILTER (
                WHERE e.action='edit' AND e.data->>'items_changed' IS DISTINCT FROM 'false'
            ) AS items_changed_at
            FROM portal_documents_event e WHERE e.kind='waybill' AND e.document_id=w.id
            HAVING count(*) FILTER (
                WHERE e.action NOT IN ('create','copy','edit','edit_previous')
            )=0
        ) history
        WHERE w.status='Created' AND w.submission_state='idle' AND w.receipt_state='none'
          AND (%s OR (w.store_id=ANY(%s::uuid[]) AND w.counteragent_id=ANY(%s::uuid[])))
          AND NOT EXISTS (SELECT 1 FROM native_dispatch d
                          WHERE d.kind='waybill' AND d.document_id=w.id)
          AND EXISTS (SELECT 1 FROM waybills_items i WHERE i.waybill_id=w.id)
          AND NOT EXISTS (SELECT 1 FROM waybills_items i WHERE i.waybill_id=w.id
                          AND i.received_amount IS NOT NULL)
    """
    return query, (selection["mode"] == "all", selection["store_ids"], selection["store_ids"])


def add_waybill_deadlines(db, rows):
    """The input is already restricted to this user's page or individual document."""
    if not rows:
        return
    query, params = deadlines_query(db)
    deadlines = db.execute(
        f"SELECT id,due_at FROM ({query}) eligible WHERE id=ANY(%s)",
        (*params, [row["id"] for row in rows]),
    )
    by_id = {r["id"]: r["due_at"].isoformat() for r in deadlines}
    for row in rows:
        row["auto_receipt_due_at"] = by_id.get(row["id"])
