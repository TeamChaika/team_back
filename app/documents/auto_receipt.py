"""Queue the full receipt of unanswered waybills after 72 hours."""

from hashlib import sha256
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.documents.reads import summary
from app.documents.workflow import event, queue_submission
from app.operation_settings import load_settings


def queue_due_one(service):
    """Claim and queue one eligible waybill, without contacting iiko or Telegram."""
    with service.database.connection() as db:
        policy = db.execute(
            "SELECT enabled_at FROM waybill_auto_receipt_policy WHERE singleton"
        ).fetchone()
        if not policy or policy["enabled_at"] is None:
            return False

        selection = load_settings(db)["waybill"]
        # Match both ID and version when taking the lock. A concurrent edit must
        # not inherit the deadline calculated from a snapshot of the older version.
        doc = db.execute(
            """
            WITH eligible AS MATERIALIZED (
                SELECT w.id,w.version FROM waybills w
                WHERE w.status='Created' AND w.submission_state='idle' AND w.receipt_state='none'
                  AND w.created_at >= %s AND w.created_at <= now()-interval '72 hours'
                  AND (%s OR (w.store_id=ANY(%s::uuid[]) AND w.counteragent_id=ANY(%s::uuid[])))
                  AND NOT EXISTS (
                      SELECT 1 FROM portal_documents_event e
                      WHERE e.kind='waybill' AND e.document_id=w.id AND (
                          e.action NOT IN ('create','copy','edit','edit_previous') OR
                          (e.action='edit' AND e.created_at > now()-interval '72 hours'
                           AND e.data->>'items_changed' IS DISTINCT FROM 'false')
                      )
                  )
                  AND NOT EXISTS (SELECT 1 FROM native_dispatch d
                                  WHERE d.kind='waybill' AND d.document_id=w.id)
                  AND EXISTS (SELECT 1 FROM waybills_items i WHERE i.waybill_id=w.id)
                  AND NOT EXISTS (SELECT 1 FROM waybills_items i WHERE i.waybill_id=w.id
                                  AND i.received_amount IS NOT NULL)
            )
            SELECT w.* FROM eligible e JOIN waybills w ON w.id=e.id AND w.version=e.version
            ORDER BY w.created_at,w.id LIMIT 1 FOR UPDATE OF w SKIP LOCKED
            """,
            (
                policy["enabled_at"],
                selection["mode"] == "all",
                selection["store_ids"],
                selection["store_ids"],
            ),
        ).fetchone()
        if doc is None:
            return False

        key = uuid4()
        fingerprint = sha256(f"waybill:{doc['id']}:{doc['version']}:auto_receive".encode())
        doc = db.execute(
            "UPDATE waybills SET version=version+1,receipt_state='accepted',"
            "submission_state='queued',processed_by_id=NULL WHERE id=%s RETURNING *",
            (doc["id"],),
        ).fetchone()
        db.execute(
            "UPDATE waybills_items SET received_amount=amount WHERE waybill_id=%s",
            (doc["id"],),
        )
        # This operation is internal only; the public mutation API does not accept it.
        db.execute(
            "INSERT INTO portal_documents_operation "
            "(id,actor_id,kind,action,document_id,fingerprint,state,result,created_at) "
            "VALUES (%s,NULL,'waybill','auto_receive',%s,%s,'queued',%s,now())",
            (key, doc["id"], fingerprint.hexdigest(), Jsonb(summary("waybill", doc))),
        )
        event(db, "waybill", doc, None, "auto_receive")
        queue_submission(db, service.provider, key, "waybill", doc, receiver=None)
        return True
