"""Current outgoing documents and source-UUID links, published inside a daily transaction."""

from datetime import date

from psycopg.types.json import Jsonb

from app.sync_inventory import upsert_rows


def outgoing_link_counts(db, source_id: str, document_ids: list) -> dict:
    row = db.execute(
        "SELECT count(*),"
        "count(*) FILTER(WHERE o.status='PROCESSED'),"
        "count(*) FILTER(WHERE o.linked_incoming_invoice_id IS NOT NULL),"
        "count(*) FILTER(WHERE i.id IS NOT NULL),"
        "count(*) FILTER(WHERE i.details->>'linked_outgoing_invoice_id'=o.id::text),"
        "count(*) FILTER(WHERE i.status='PROCESSED'),"
        "count(*) FILTER(WHERE s.id IS NOT NULL) "
        "FROM chaika.outgoing_invoices o "
        "LEFT JOIN chaika.incoming_invoices i ON i.source_id=o.source_id "
        "AND i.id=o.linked_incoming_invoice_id "
        "LEFT JOIN chaika.counteragents c ON c.source_id=o.source_id AND c.id=o.counteragent_id "
        "AND c.represents_store "
        "LEFT JOIN chaika.stores s ON s.source_id=o.source_id AND s.id=c.represented_store_id "
        "WHERE o.source_id=%s AND o.id=ANY(%s::uuid[])",
        (source_id, document_ids),
    ).fetchone()
    return dict(
        zip(
            (
                "documents",
                "processed",
                "with_incoming_link",
                "incoming_found",
                "reverse_link_matches",
                "incoming_processed",
                "receiver_store_resolved",
            ),
            row,
            strict=True,
        )
    )


def publish_outgoing_invoices(db, snapshot: dict, day: date) -> dict:
    parents = snapshot["payload"]["items"]
    source_id = snapshot["source_id"]
    ids = [r["id"] for r in parents]

    def headers():
        for row in parents:
            details = {k: v for k, v in row.items() if k != "items"}
            yield {
                **details,
                "source_id": source_id,
                "last_export_date": day,
                "first_seen_at": snapshot["observed_at"],
                "last_seen_at": snapshot["observed_at"],
                "last_snapshot_id": snapshot["id"],
                "details": Jsonb(details),
            }

    upsert_rows(
        db,
        "outgoing_invoices",
        [
            "source_id",
            "id",
            "document_number",
            "date_incoming",
            "status",
            "counteragent_id",
            "default_store_id",
            "linked_incoming_invoice_id",
            "linked_outgoing_invoice_id",
            "last_export_date",
            "first_seen_at",
            "last_seen_at",
            "last_snapshot_id",
            "details",
        ],
        ["source_id", "id"],
        headers(),
    )
    # Absence of a document in a later date export is not proof of its deletion.
    db.execute(
        "UPDATE chaika.outgoing_invoice_items SET present_in_latest=false "
        "WHERE source_id=%s AND document_id=ANY(%s::uuid[])",
        (source_id, ids),
    )
    upsert_rows(
        db,
        "outgoing_invoice_items",
        [
            "source_id",
            "document_id",
            "line_num",
            "product_id",
            "store_id",
            "container_id",
            "amount",
            "price",
            "sum",
            "details",
            "present_in_latest",
        ],
        ["source_id", "document_id", "line_num"],
        (
            {
                **item,
                "source_id": source_id,
                "document_id": parent["id"],
                "line_num": n,
                "details": Jsonb(item),
                "present_in_latest": True,
            }
            for parent in parents
            for n, item in enumerate(parent["items"], 1)
        ),
    )
    return outgoing_link_counts(db, source_id, ids)
