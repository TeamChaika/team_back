"""Prepare confirmed receipt economics once; HTTP reads never run the routing matcher."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.purchase_prices_precompute import check_stop
from app.purchase_receipt_matching import (
    EXTERNAL_PREDICATE,
    PROCUREMENT_GROUP,
    ROUTING,
    routing_ctes,
)

ALGORITHM_VERSION = 1


def source_revision(db):
    """Publication tokens, including partially completed history in both directions.

    Failed/running jobs without committed checkpoints do not invalidate data. Reference,
    dictionary and product publications own the directories consulted by the matcher.
    """
    row = db.execute(
        "SELECT count(*) FILTER (WHERE status='succeeded') AS count,"
        "max(finished_at) FILTER (WHERE status='succeeded') AS finished_at,"
        "md5(string_agg(id::text||':'||(counts->>'last_snapshot_id'),',' ORDER BY id) "
        "FILTER (WHERE counts->>'resource' IN ('incoming_invoices','outgoing_invoices'))) "
        "AS checkpoints FROM chaika.sync_runs "
        "WHERE job IN ('inventory','references','dictionaries','products')"
    ).fetchone()
    return dict(
        version=ALGORITHM_VERSION,
        matcher=hashlib.sha256((ROUTING + EXTERNAL_PREDICATE).encode()).hexdigest(),
        count=row["count"],
        finished_at=row["finished_at"].astimezone(UTC).isoformat() if row["finished_at"] else None,
        checkpoints=row["checkpoints"],
    )


def publish_prepared(db, revision, *, stop=None):
    from app.web.purchase_prices import ELIGIBLE, HISTORY_SIZE, JOIN, STAMP

    check_stop(stop)
    generation, prepared_at = uuid4(), datetime.now(UTC)
    matcher = routing_ctes(
        schema="chaika",
        day_pair_window="true",
        destination_guard="true",
        receipt_end="'infinity'::timestamptz",
        procurement_group="%s",
    )
    with db.transaction():
        db.execute("DELETE FROM chaika.purchase_receipts_prepared WHERE source_id='primary'")
        db.execute(
            "INSERT INTO chaika.purchase_receipts_prepared VALUES(%s,'primary',%s,%s)",
            (generation, prepared_at, Jsonb(revision)),
        )
        planner = db.execute("SELECT current_setting('enable_nestloop') AS value").fetchone()[
            "value"
        ]
        db.execute("SET LOCAL enable_nestloop=off")
        db.execute(
            "INSERT INTO chaika.purchase_receipts_prepared_lines "
            "WITH warehouse AS (SELECT id FROM chaika.stores WHERE source_id='primary'), "
            + matcher
            + ", signatures AS MATERIALIZED ("
            "SELECT t.id,coalesce(array_agg(DISTINCT coalesce(i.store_id,t.default_store_id) "
            "ORDER BY coalesce(i.store_id,t.default_store_id)) FILTER (WHERE "
            "coalesce(i.store_id,t.default_store_id) IS NOT NULL),'{}'::uuid[]) AS stores,"
            "bool_or(coalesce(i.store_id,t.default_store_id) IS NULL) AS unknown "
            "FROM " + JOIN + " WHERE t.source_id='primary' AND i.present_in_latest "
            "GROUP BY t.id), evidence AS ("
            "SELECT id AS doc,supplier_id,'direct'::text AS route,NULL::uuid AS origin,"
            "NULL::uuid AS via_id FROM external_headers UNION ALL "
            "SELECT r.doc,e.supplier_id,'via_procurement',r.origin,r.via_id FROM routed r "
            "JOIN external_headers e ON e.id=r.origin WHERE r.reason IS NULL "
            "AND r.origin IS NOT NULL) "
            "SELECT %s,t.id,i.num,i.num_occurrence,i.product_id,i.amount_unit_id,"
            "coalesce(i.store_id,t.default_store_id),i.amount,i.sum,i.price," + STAMP + ","
            "e.supplier_id,e.route,s.stores,s.unknown,coalesce(o.stores,'{}'::uuid[]),"
            "coalesce(o.unknown,false),e.via_id FROM " + JOIN + " JOIN evidence e ON e.doc=t.id "
            "JOIN signatures s ON s.id=t.id LEFT JOIN signatures o ON o.id=e.origin "
            "WHERE " + ELIGIBLE,
            (PROCUREMENT_GROUP, generation),
        )
        db.execute("SELECT set_config('enable_nestloop',%s,true)", (planner,))
        check_stop(stop)
        db.execute(
            "INSERT INTO chaika.purchase_receipts_prepared_observations "
            "WITH grouped AS (SELECT generation_id,product_id,amount_unit_id AS unit_id,date,"
            "sum(amount) AS amount,sum(sum) AS sum,"
            "bool_and(amount IS NOT NULL AND amount>0 AND sum>=0) AS valid,"
            "receipt_route,required_store_ids,has_unknown_store,origin_store_ids,"
            "origin_unknown_store,via_id FROM chaika.purchase_receipts_prepared_lines "
            "WHERE generation_id=%s GROUP BY 1,2,3,4,8,9,10,11,12,13), ranked AS ("
            "SELECT *,row_number() OVER (PARTITION BY product_id,unit_id,receipt_route,"
            "required_store_ids,has_unknown_store,origin_store_ids,origin_unknown_store,via_id "
            "ORDER BY date DESC) AS position FROM grouped) "
            "SELECT generation_id,product_id,unit_id,date,amount,sum,valid,receipt_route,"
            "required_store_ids,has_unknown_store,origin_store_ids,origin_unknown_store,via_id "
            "FROM ranked WHERE position<=%s",
            (generation, HISTORY_SIZE + 1),
        )
        check_stop(stop)
    return dict(status="ready", generation_id=str(generation), prepared_at=prepared_at)


def ensure_prepared(settings, stop=None):
    check_stop(stop)
    with psycopg.connect(
        settings.database_url.get_secret_value(),
        autocommit=True,
        connect_timeout=10,
        row_factory=dict_row,
        prepare_threshold=None,
    ) as db:
        with db.transaction():
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            try:
                db.execute(
                    "LOCK TABLE chaika.purchase_receipts_prepared "
                    "IN SHARE ROW EXCLUSIVE MODE NOWAIT"
                )
            except psycopg.errors.LockNotAvailable:
                raise psycopg.Rollback() from None
            revision = source_revision(db)
            current = db.execute(
                "SELECT id,revision,prepared_at FROM chaika.purchase_receipts_prepared "
                "WHERE source_id='primary'"
            ).fetchone()
            if current and current["revision"] == revision:
                return dict(
                    status="unchanged",
                    generation_id=str(current["id"]),
                    prepared_at=current["prepared_at"],
                )
            return publish_prepared(db, revision, stop=stop)
        return dict(status="busy")


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps(ensure_prepared(Settings()), default=str))


if __name__ == "__main__":
    main()
