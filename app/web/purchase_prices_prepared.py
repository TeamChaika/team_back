"""Select current, authorized prepared timestamps without reading source invoice lines."""

from app.purchase_prices_precompute import source_revision
from app.tenancy.sql import ANALYTICS_SCHEMA as DB


def prepared_receipts_query(db, scope, kind, excluded_products=None):
    """Return a receipt SELECT and parameters, or None to use the existing reader.

    The repository supplies a repeatable-read transaction, keeping metadata,
    source revision and receipt rows on the same snapshot during publication.
    """
    available = db.execute(
        f"SELECT to_regclass('{DB}.purchase_prices_prepared') IS NOT NULL AS available"
    ).fetchone()["available"]
    if not available:
        return None
    current = db.execute(
        f"SELECT id,revision FROM {DB}.purchase_prices_prepared WHERE source_id='primary'"
    ).fetchone()
    if current is None or current["revision"] != source_revision(db):
        return None
    clauses, params = ["generation_id=%s"], [current["id"]]
    if not scope.unrestricted:
        clauses.extend(["NOT has_unknown_store", "required_store_ids <@ %s::uuid[]"])
        params.append(list(scope.store_ids))
    if kind != "all":
        clauses.append("linked=%s")
        params.append(kind == "linked")
    if excluded_products is not None:
        clauses.append("NOT (product_id=ANY(%s::uuid[]))")
        params.append(excluded_products)
    # Different signatures may contain the same timestamp. Combine those before
    # global ranking, including invalid groups in the existing quality statistics.
    return (
        "SELECT product_id,NULL::uuid AS store_id,unit_id,linked,date,"
        "sum(amount) AS amount,sum(sum) AS sum,bool_and(valid) AS valid "
        f"FROM {DB}.purchase_prices_prepared_receipts WHERE "
        + " AND ".join(clauses)
        + " GROUP BY 1,2,3,4,5",
        params,
    )
