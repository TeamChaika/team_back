"""Authorized external purchase economics, sharing the digest's strict routing matcher.

Origin documents are used internally only. Returned evidence always identifies the
receiving invoice; an unresolved route never falls back to its internal price.
"""

from app.purchase_receipt_matching import EXTERNAL_PREDICATE, PROCUREMENT_GROUP, routing_ctes


def receipt_lines_query(scope, spec):
    """Return an internal SELECT and bound args; apply document AND line access.

    When the procurement warehouse itself is visible, its supplier invoice is
    already a direct observation. Its onward deliveries must not be counted a
    second time. Otherwise only the delivered share enters the receiving scope.
    """
    if scope.unrestricted:
        # Every originating warehouse is accessible, so each external purchase
        # already counts once. No route matching or allocation scan is needed.
        return (
            "SELECT i.*,t.supplier_id,'direct'::text AS receipt_route "
            "FROM chaika.incoming_invoice_items i JOIN chaika.incoming_invoices t "
            "ON (t.source_id,t.id)=(i.source_id,i.document_id) "
            "LEFT JOIN chaika.counteragents s ON (s.source_id,s.id)=(t.source_id,t.supplier_id) "
            "WHERE " + EXTERNAL_PREDICATE + " AND i.present_in_latest",
            [],
        )
    matcher = routing_ctes(
        schema="chaika", day_pair_window="true", destination_guard=spec["guard"],
        receipt_end="'infinity'::timestamptz", procurement_group="%s",
    )
    stores = list(scope.store_ids)
    warehouse = ("SELECT id FROM chaika.stores WHERE source_id='primary'" if scope.unrestricted
                 else "SELECT unnest(%s::uuid[]) AS id")
    params = ([] if scope.unrestricted else [stores]) + [
        PROCUREMENT_GROUP, *spec["params"], *spec["params"]
    ]
    query = "WITH warehouse AS (" + warehouse + "), " + matcher + """,
    external_receipts AS (
        SELECT t.id AS doc,t.supplier_id,'direct'::text AS receipt_route,
               NULL::uuid AS origin,NULL::uuid AS via_id
        FROM external_headers t
        UNION ALL
        SELECT r.doc,e.supplier_id,'via_procurement',r.origin,r.via_id
        FROM routed r JOIN external_headers e ON e.id=r.origin
        WHERE r.reason IS NULL AND r.origin IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM chaika.incoming_invoices t
              JOIN warehouse w ON w.id=r.via_id
              WHERE t.source_id='primary' AND t.id=r.origin AND (""" + spec["guard"] + """)
          )
    )
    SELECT i.source_id,i.document_id,i.num,i.num_occurrence,i.product_id,
           i.store_id,i.amount_unit_id,i.amount,
           i.sum,i.price,
           i.details,i.present_in_latest,r.supplier_id,r.receipt_route
    FROM external_receipts r
    JOIN chaika.incoming_invoices t ON t.source_id='primary' AND t.id=r.doc
    JOIN chaika.incoming_invoice_items i ON (i.source_id,i.document_id)=(t.source_id,t.id)
    JOIN warehouse w ON w.id=coalesce(i.store_id,t.default_store_id)
    WHERE i.present_in_latest
    """
    return query, params
