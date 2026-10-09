"""Prepare receipt observations after inventory publication for fast scoped price lists."""

import argparse
import json
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.tenancy.connection import tenant_connect
from app.tenancy.sql import ANALYTICS_SCHEMA as DB

ALGORITHM_VERSION = 1
LOCK_ID = 7623011071900


def check_stop(stop):
    if stop is not None and stop.is_set():
        raise RuntimeError("purchase_prices_preparation_interrupted")


def source_revision(db):
    """Track both inventory success and committed incoming-history checkpoints.

    A multi-day history run publishes each checkpoint while still running, and
    retains those receipts even if a later day fails. Its last_snapshot_id changes
    in the same transaction as receipt rows. Full inventory sync publishes its
    success marker atomically with rows instead. Neither token includes sales.
    """
    row = db.execute(
        "SELECT count(*) FILTER (WHERE status='succeeded') AS count,"
        "max(finished_at) FILTER (WHERE status='succeeded') AS finished_at,"
        "md5(string_agg(id::text||':'||(counts->>'last_snapshot_id'),',' ORDER BY id) "
        "FILTER (WHERE counts->>'resource'='incoming_invoices')) AS invoice_checkpoints "
        f"FROM {DB}.sync_runs WHERE job='inventory'"
    ).fetchone()
    return dict(
        version=ALGORITHM_VERSION,
        count=row["count"],
        finished_at=row["finished_at"].isoformat() if row["finished_at"] else None,
        invoice_checkpoints=row["invoice_checkpoints"],
    )


def publish_prepared(db, revision, *, stop=None):
    """Build and replace one generation atomically inside the caller's source snapshot."""
    from app.web.purchase_prices import ELIGIBLE, HISTORY_SIZE, JOIN, LINKED, STAMP

    check_stop(stop)
    generation_id, prepared_at = uuid4(), datetime.now(UTC)
    with db.transaction():
        db.execute(f"DELETE FROM {DB}.purchase_prices_prepared WHERE source_id='primary'")
        db.execute(
            f"INSERT INTO {DB}.purchase_prices_prepared(id,source_id,prepared_at,revision) "
            "VALUES (%s,'primary',%s,%s)",
            (generation_id, prepared_at, Jsonb(revision)),
        )
        # Authorization considers ALL present invoice lines, even non-product and
        # additional-expense lines which are excluded from price observations.
        # A priced invoice has at least one present line: if all its effective
        # stores are known/allowed, an explicit store or its default also satisfies
        # resource_query's anchor. No independent default-store check is necessary.
        db.execute(
            f"INSERT INTO {DB}.purchase_prices_prepared_receipts "
            "(generation_id,required_store_ids,has_unknown_store,product_id,unit_id,linked,"
            "date,amount,sum,valid) "
            "WITH signatures AS MATERIALIZED ("
            "SELECT t.id,coalesce(array_agg(DISTINCT coalesce(i.store_id,t.default_store_id) "
            "ORDER BY coalesce(i.store_id,t.default_store_id)) "
            "FILTER (WHERE coalesce(i.store_id,t.default_store_id) IS NOT NULL),"
            "'{}'::uuid[]) AS required_store_ids,"
            "bool_or(coalesce(i.store_id,t.default_store_id) IS NULL) AS has_unknown_store "
            "FROM " + JOIN + " WHERE t.source_id='primary' AND t.status='PROCESSED' "
            "AND i.present_in_latest GROUP BY t.id), receipts AS ("
            "SELECT s.required_store_ids,s.has_unknown_store,i.product_id,"
            "i.amount_unit_id AS unit_id," + LINKED + " AS linked," + STAMP + " AS date,"
            "sum(i.amount) AS amount,sum(i.sum) AS sum,"
            "bool_and(i.amount IS NOT NULL AND i.amount>0 AND i.sum>=0) AS valid "
            "FROM "
            + JOIN
            + " JOIN signatures s ON s.id=t.id WHERE "
            + ELIGIBLE
            + " GROUP BY 1,2,3,4,5,6), ranked AS ("
            "SELECT *,row_number() OVER (PARTITION BY required_store_ids,has_unknown_store,"
            "product_id,unit_id,linked ORDER BY date DESC) AS position FROM receipts) "
            "SELECT %s,required_store_ids,has_unknown_store,product_id,unit_id,linked,"
            "date,amount,sum,valid FROM ranked WHERE position<=%s",
            (generation_id, HISTORY_SIZE + 1),
        )
        # The cap is per authorization signature, including invalid observations.
        # Any discarded timestamp has seven newer accepted timestamps in that same
        # signature, so cannot enter the newest seven of any authorized union.
        check_stop(stop)
    return dict(status="ready", generation_id=str(generation_id), prepared_at=prepared_at)


def ensure_prepared(settings, stop=None):
    """Background entry point. Skip heavy work until published inventory changes."""
    check_stop(stop)
    with tenant_connect(
        settings.database_url.get_secret_value(),
        connector=psycopg.connect,
        autocommit=True,
        connect_timeout=10,
        row_factory=dict_row,
        prepare_threshold=None,
    ) as db:
        with db.transaction():
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            locked = db.execute(
                "SELECT pg_try_advisory_xact_lock(%s) AS locked", (LOCK_ID,)
            ).fetchone()["locked"]
            if not locked:
                return dict(status="busy")
            revision = source_revision(db)
            current = db.execute(
                f"SELECT id,revision,prepared_at FROM {DB}.purchase_prices_prepared "
                "WHERE source_id='primary'"
            ).fetchone()
            if current and current["revision"] == revision:
                return dict(
                    status="unchanged",
                    generation_id=str(current["id"]),
                    prepared_at=current["prepared_at"],
                )
            return publish_prepared(db, revision, stop=stop)


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps(ensure_prepared(Settings()), default=str))


if __name__ == "__main__":
    main()
