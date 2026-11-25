"""Exercise the digest migration using collector-style upserts on isolated, synthetic invoices."""

import os
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb


def observe(db, side, doc, day, *, status="PROCESSED", link=None, source="primary"):
    source_link = UUID(int=link) if link is not None else None
    if side == "incoming":
        table, column = "incoming_invoices", "details"
        value = Jsonb({"linked_outgoing_invoice_id": str(source_link)} if source_link else {})
    else:
        table, column = "outgoing_invoices", "linked_incoming_invoice_id"
        value = source_link
    db.execute(
        sql.SQL(
            "INSERT INTO {table}(source_id,id,{link},status,first_seen_at,last_seen_at)"
            " VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(source_id,id) DO UPDATE SET"
            " status=EXCLUDED.status,last_seen_at=EXCLUDED.last_seen_at,{link}=EXCLUDED.{link}"
        ).format(table=sql.Identifier(table), link=sql.Identifier(column)),
        (source, UUID(int=doc), value, status, f"2026-01-{day:02}", f"2026-01-{day:02}"),
    )


@pytest.fixture
def invoices():
    dsn = os.environ.get("LOW_MARKUP_TEST_DSN")
    if not dsn:
        pytest.skip("loopback PostgreSQL fixture required")
    assert conninfo_to_dict(dsn).get("host") in {"127.0.0.1", "localhost"}
    name = "digest_availability_test_" + uuid4().hex
    migration = (
        Path(__file__).resolve().parents[1]
        / "supabase/migrations/20261125183225_purchase_digest_receipt_availability.sql"
    ).read_text(encoding="utf-8")
    # The production role names are not required on a fresh standalone test database.
    migration = (
        migration.replace("chaika.", name + ".")
        .replace("PUBLIC, anon, authenticated", "PUBLIC")
        .replace("TO chaika_backend", "TO CURRENT_USER")
    )
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            db.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(name)))
            db.execute("SET TIME ZONE 'UTC'")
            for table, link in [
                ("incoming_invoices", "details jsonb"),
                ("outgoing_invoices", "linked_incoming_invoice_id uuid"),
            ]:
                db.execute(
                    sql.SQL(
                        "CREATE TABLE {}(source_id text,id uuid,status text,"
                        "first_seen_at timestamptz,last_seen_at timestamptz,{},"
                        "PRIMARY KEY(source_id,id))"
                    ).format(sql.Identifier(table), sql.SQL(link))
                )
            observe(db, "incoming", 1, 1, link=11)
            observe(db, "outgoing", 11, 2, link=1)
            observe(db, "incoming", 2, 1, link=99)  # Unpaired legacy receipt.
            db.execute(migration)
            yield db, dsn, name
        finally:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def paired_day(db, incoming, source="primary"):
    row = db.execute(
        "SELECT extract(day from first_paired_at)::int FROM purchase_digest_receipt_pairs "
        "WHERE source_id=%s AND incoming_id=%s",
        (source, UUID(int=incoming)),
    ).fetchone()
    return row[0] if row else None


def test_backfills_only_true_pairs_and_preserves_them_across_replacement(invoices):
    db, _, _ = invoices
    assert paired_day(db, 1) == 2
    assert paired_day(db, 2) is None
    observe(db, "incoming", 1, 6, link=11)  # Routine collector re-import.
    observe(db, "outgoing", 11, 7, status="DELETED", link=1)
    observe(db, "incoming", 1, 8, link=12)
    observe(db, "outgoing", 12, 9, link=1)
    assert paired_day(db, 1) == 2
    assert db.execute("SELECT count(*) FROM purchase_digest_receipt_pairs").fetchone()[0] == 1
    # An invalid, non-empty pointer must not predate the first actual reciprocal pair.
    observe(db, "outgoing", 22, 2, link=2)
    observe(db, "incoming", 2, 8, link=22)
    assert paired_day(db, 2) == 8


def test_outgoing_correction_and_late_posting_use_first_reciprocal_observation(invoices):
    db, _, _ = invoices
    observe(db, "outgoing", 23, 2, link=88)
    observe(db, "incoming", 3, 3, status="NEW", link=23)
    observe(db, "incoming", 3, 5, link=23)
    assert paired_day(db, 3) is None
    observe(db, "outgoing", 23, 7, link=3)
    observe(db, "outgoing", 23, 9, link=3)
    observe(db, "incoming", 3, 11, status="NEW")
    observe(db, "incoming", 3, 12, link=23)
    assert paired_day(db, 3) == 7
    posted, current_link = db.execute(
        "SELECT extract(day from processed_first_seen_at)::int,"
        "extract(day from paired_first_seen_at)::int FROM incoming_invoices "
        "WHERE source_id='primary' AND id=%s",
        (UUID(int=3),),
    ).fetchone()
    assert (posted, current_link) == (5, 12)
    observe(db, "outgoing", 23, 15, source="other", link=3)
    assert paired_day(db, 3, "other") is None


def test_concurrent_first_imports_and_later_correction_preserve_first_pair(invoices):
    db, dsn, name = invoices
    with psycopg.connect(dsn) as writer, psycopg.connect(dsn) as other:
        for connection in (writer, other):
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(name)))
        observe(writer, "incoming", 5, 2, link=25)
        observe(other, "outgoing", 25, 4, link=5)
        writer.commit()
        other.commit()
    # Neither trigger could see the other transaction's uncommitted leg. The report
    # reads both markers until the next publication records their first availability.
    assert paired_day(db, 5) is None
    assert (
        db.execute(
            "SELECT extract(day from greatest(i.paired_first_seen_at,o.paired_first_seen_at))::int "
            "FROM incoming_invoices i JOIN outgoing_invoices o "
            "ON o.linked_incoming_invoice_id=i.id "
            "AND o.id::text=i.details->>'linked_outgoing_invoice_id' AND o.source_id=i.source_id "
            "WHERE i.id=%s",
            (UUID(int=5),),
        ).fetchone()[0]
        == 4
    )
    # Even the first future update being an unpair (not an unchanged re-import)
    # captures the old reciprocal pair and suppresses duplicate publication after relinking.
    observe(db, "outgoing", 25, 7, link=77)
    assert paired_day(db, 5) == 4
    observe(db, "outgoing", 25, 8, link=5)
    assert paired_day(db, 5) == 4
