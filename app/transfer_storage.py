"""Internal-transfer documents; preserve source warehouse direction and numeric values."""

from datetime import date

from psycopg.types.json import Jsonb

from app.sync_inventory import upsert_rows


def publish_transfers(db, snapshot: dict, day: date) -> dict:
    """Publish one internal-transfer day inside the caller's RAW/checkpoint transaction."""
    parents = snapshot["payload"]["items"]
    source_id = snapshot["source_id"]

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
        "internal_transfers",
        [
            "source_id",
            "id",
            "document_number",
            "date_incoming",
            "status",
            "store_from_id",
            "store_to_id",
            "last_export_date",
            "first_seen_at",
            "last_seen_at",
            "last_snapshot_id",
            "details",
        ],
        ["source_id", "id"],
        headers(),
    )
    db.execute(
        "UPDATE chaika.internal_transfer_items SET present_in_latest=false "
        "WHERE source_id=%s AND document_id=ANY(%s::uuid[])",
        (source_id, [p["id"] for p in parents]),
    )
    upsert_rows(
        db,
        "internal_transfer_items",
        [
            "source_id",
            "document_id",
            "num",
            "product_id",
            "amount",
            "cost",
            "measure_unit_id",
            "amount_factor",
            "product_size_id",
            "container_id",
            "details",
            "present_in_latest",
        ],
        ["source_id", "document_id", "num"],
        (
            {
                **item,
                "source_id": source_id,
                "document_id": parent["id"],
                "details": Jsonb(item),
                "present_in_latest": True,
            }
            for parent in parents
            for item in parent["items"]
        ),
    )

    return transfer_link_counts(db, source_id, [p["id"] for p in parents])


def transfer_link_counts(db, source_id: str, ids: list) -> dict:
    row = db.execute(
        "SELECT count(*),count(*) FILTER(WHERE t.status='PROCESSED'),"
        "count(*) FILTER(WHERE f.id IS NOT NULL),count(*) FILTER(WHERE d.id IS NOT NULL) "
        "FROM chaika.internal_transfers t "
        "LEFT JOIN chaika.stores f ON f.source_id=t.source_id AND f.id=t.store_from_id "
        "LEFT JOIN chaika.stores d ON d.source_id=t.source_id AND d.id=t.store_to_id "
        "WHERE t.source_id=%s AND t.id=ANY(%s::uuid[])",
        (source_id, ids),
    ).fetchone()
    counts = dict(
        zip(
            ("documents", "processed", "sender_store_resolved", "receiver_store_resolved"),
            row,
            strict=True,
        )
    )
    row = db.execute(
        "SELECT count(*),count(*) FILTER(WHERE p.id IS NULL),"
        "count(*) FILTER(WHERE i.measure_unit_id IS NOT NULL AND u.id IS NULL) "
        "FROM chaika.internal_transfer_items i "
        "LEFT JOIN chaika.products p ON p.source_id=i.source_id AND p.id=i.product_id "
        "LEFT JOIN chaika.measure_units u ON u.source_id=i.source_id AND u.id=i.measure_unit_id "
        "WHERE i.source_id=%s AND i.document_id=ANY(%s::uuid[]) AND i.present_in_latest",
        (source_id, ids),
    ).fetchone()
    counts.update(zip(("items", "unknown_products", "unknown_measure_units"), row, strict=True))
    return counts
