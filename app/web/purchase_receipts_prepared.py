"""Fast scoped reads of the current canonical receipt generation."""

from app.purchase_receipts_precompute import source_revision


def prepared_queries(db, scope, excluded_products=None):
    available = db.execute(
        "SELECT to_regclass('chaika.purchase_receipts_prepared') IS NOT NULL AS available"
    ).fetchone()["available"]
    if not available:
        return None
    current = db.execute(
        "SELECT id,revision FROM chaika.purchase_receipts_prepared WHERE source_id='primary'"
    ).fetchone()
    if current is None or current["revision"] != source_revision(db):
        return None
    clauses, params = ["generation_id=%s"], [current["id"]]
    if scope.unrestricted:
        clauses.append("receipt_route='direct'")
    else:
        clauses.extend(
            [
                "NOT has_unknown_store",
                "required_store_ids <@ %s::uuid[]",
                "(receipt_route='direct' OR NOT (via_id=ANY(%s::uuid[]) "
                "AND NOT origin_unknown_store AND origin_store_ids <@ %s::uuid[]))",
            ]
        )
        params.extend([list(scope.store_ids)] * 3)
    if excluded_products is not None:
        clauses.append("NOT (product_id=ANY(%s::uuid[]))")
        params.append(excluded_products)
    where = " AND ".join(clauses)
    lines = (
        "SELECT 'primary'::text AS source_id,document_id,num,num_occurrence,product_id,"
        "amount_unit_id,store_id,amount,sum,price,'{}'::jsonb AS details,"
        "true AS present_in_latest,supplier_id,receipt_route "
        "FROM chaika.purchase_receipts_prepared_lines WHERE " + where
    )
    observations = (
        "SELECT product_id,NULL::uuid AS store_id,unit_id,false AS linked,date,"
        "sum(amount) AS amount,sum(sum) AS sum,bool_and(valid) AS valid "
        "FROM chaika.purchase_receipts_prepared_observations WHERE " + where + " GROUP BY 1,2,3,4,5"
    )
    return (lines, params), (observations, params)
