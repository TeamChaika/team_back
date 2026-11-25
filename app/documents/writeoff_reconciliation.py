"""Observe writeoffs in iiko independently of the import/approval queue."""

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from app.documents.reads import ZONE
from app.schemas.iiko_writeoffs import WriteoffDocument

log = logging.getLogger(__name__)
DOCUMENTS = TypeAdapter(list[WriteoffDocument])
READ_TIMEOUT = 4


def lookup_writeoffs(client, token, number):
    # A number lookup also finds posted/deleted acts moved to a different accounting day.
    with client.stream(
        "GET",
        "v2/documents/writeoff/byNumber",
        params={"key": token, "documentNumber": number},
        headers={"Accept": "application/json"},
        timeout=READ_TIMEOUT,
    ) as response:
        response.raise_for_status()
        body = bytearray()
        deadline = monotonic() + READ_TIMEOUT
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > 4 * 1024 * 1024 or monotonic() >= deadline:
                raise ValueError("iiko writeoff lookup exceeded its limit")
    rows = DOCUMENTS.validate_python(json.loads(body, parse_float=Decimal))
    if any(row.document_number != number for row in rows):
        raise ValueError("Unexpected iiko writeoff number")
    return rows


def compare_writeoff(created, store, expected, documents, verified_id=None):
    expected_date = created.astimezone(ZONE).date().isoformat()
    matches = [doc for doc in documents if doc.store_id == store]
    result = {
        "state": "not_found",
        "iiko_status": None,
        "iiko_id": verified_id,
        "expected_date": expected_date,
        "actual_date": None,
        "date_differs": False,
        "quantity_differences": [],
    }
    if not matches:
        return result
    if verified_id:
        matches = [row for row in matches if str(row.id) == verified_id]
    if len(matches) != 1:
        result["state"] = "ambiguous"
        return result
    doc = matches[0]
    actual_date = doc.date_incoming.date().isoformat()
    actual = defaultdict(Decimal)
    for item in doc.items:
        actual[str(item.product_id)] += item.amount
    changes = [
        {
            "product_id": key,
            "expected_amount": format(expected[key], "f") if key in expected else None,
            "actual_amount": format(actual[key], "f") if key in actual else None,
        }
        for key in sorted(expected.keys() | actual.keys())
        if expected.get(key) != actual.get(key)
    ]
    return {
        **result,
        "state": "matched",
        "iiko_status": doc.status,
        "iiko_id": str(doc.id),
        "actual_date": actual_date,
        "date_differs": expected_date != actual_date,
        "quantity_differences": changes,
    }


def claim_batch(database):
    claim = uuid4()
    jobs = []
    with database.connection() as db:
        # Both queues get a share even after an extended outage.
        for native, limit in ((True, 4), (False, 1)):
            rows = db.execute(
                "WITH due AS (SELECT document_id,next_check_at FROM writeoff_reconciliation "
                "WHERE (operation_id IS NOT NULL)=%s AND next_check_at<=now() "
                "ORDER BY next_check_at,document_id DESC LIMIT %s FOR UPDATE SKIP LOCKED), "
                "claimed AS (UPDATE writeoff_reconciliation r SET claim_id=%s, "
                "next_check_at=now()+interval '5 minutes' FROM due "
                "WHERE r.document_id=due.document_id RETURNING r.*,due.next_check_at AS due_at) "
                "SELECT c.document_id,c.version,c.claim_id,w.created_at,w.store_id, "
                "coalesce(d.created_at,w.created_at) AS submitted_at, "
                "c.due_at,c.operation_id,c.result->>'iiko_id' AS verified_id, "
                "d.payload->'json' AS payload FROM claimed c "
                "JOIN writeoffs w "
                "ON w.id=c.document_id AND w.version=c.version LEFT JOIN native_dispatch d "
                "ON d.operation_id=c.operation_id",
                (native, limit, claim),
            ).fetchall()
            for row in rows:
                row["number"] = f"DJ{row['document_id']:06d}"
                if not native:
                    row["saved_items"] = db.execute(
                        'SELECT product_id::text AS "productId",amount::text FROM writeoffs_items '
                        "WHERE writeoff_id=%s",
                        (row["document_id"],),
                    ).fetchall()
                jobs.append(row)
    return sorted(jobs, key=lambda job: job["due_at"])


def expected_quantities(job):
    if job["operation_id"] is None:
        items = job["saved_items"]
    else:
        payload = job["payload"]
        if payload["documentNumber"] != job["number"] or payload["storeId"] != str(job["store_id"]):
            raise ValueError("Unexpected writeoff dispatch identity")
        items = payload["items"]
    expected = defaultdict(Decimal)
    for item in items:
        amount = Decimal(str(item["amount"]))
        if not amount.is_finite():
            raise ValueError("Invalid expected writeoff amount")
        expected[item["productId"]] += amount
    return expected


def reconcile_batch(service):
    jobs = claim_batch(service.database)
    if not jobs:
        return False
    first_error = None
    with service.provider.session(timeout=READ_TIMEOUT) as (client, token):
        deadline = monotonic() + READ_TIMEOUT
        for index, job in enumerate(jobs):
            if index and monotonic() >= deadline:
                # Return remaining reads to their previous position in the queue.
                with service.database.connection() as db:
                    for remaining in jobs[index:]:
                        db.execute(
                            "UPDATE writeoff_reconciliation SET next_check_at=%s,claim_id=NULL "
                            "WHERE document_id=%s AND claim_id=%s",
                            (remaining["due_at"], remaining["document_id"], remaining["claim_id"]),
                        )
                break
            try:
                expected = expected_quantities(job)
                documents = lookup_writeoffs(client, token, job["number"])
                result = compare_writeoff(
                    job["created_at"],
                    job["store_id"],
                    expected,
                    documents,
                    job["verified_id"],
                )
            except Exception as exc:
                # Preserve the last successful check. The lease schedules the retry.
                if first_error is None:
                    first_error = exc
                continue
            age = datetime.now(UTC) - job["submitted_at"]
            if age < timedelta(days=7):
                interval = timedelta(minutes=15)
            elif age < timedelta(days=30):
                interval = timedelta(hours=6)
            else:
                interval = timedelta(days=30)
            with service.database.connection() as db:
                db.execute(
                    "UPDATE writeoff_reconciliation SET result=%s,checked_at=now(), "
                    "next_check_at=now()+%s,claim_id=NULL "
                    "WHERE document_id=%s AND version=%s AND claim_id=%s",
                    (Jsonb(result), interval, job["document_id"], job["version"], job["claim_id"]),
                )
    if first_error:
        raise first_error
    return True


def run_reconciliation(service):
    # Called after foreground jobs under their existing cross-replica leader lock.
    try:
        with service.database.connection() as db:
            slot = db.execute(
                "INSERT INTO native_jobs(name,data) VALUES ('writeoff-reconciliation',"
                "jsonb_build_object('next_check_at',now()+interval '5 seconds')) "
                "ON CONFLICT(name) DO UPDATE SET data=excluded.data,updated_at=now() "
                "WHERE (native_jobs.data->>'next_check_at')::timestamptz<=now() RETURNING name"
            ).fetchone()
        if slot:
            reconcile_batch(service)
    except Exception as exc:
        log.warning("Writeoff reconciliation unavailable (%s)", type(exc).__name__)
        with service.database.connection() as db:
            db.execute(
                "UPDATE native_jobs SET data=jsonb_build_object('next_check_at',"
                "now()+interval '1 minute','error',%s::text),updated_at=now() "
                "WHERE name='writeoff-reconciliation'",
                (type(exc).__name__,),
            )
