WITH RECURSIVE venue AS (
    SELECT n.id, n.name
    FROM {schema}.corporate_nodes n CROSS JOIN {schema}.operation_settings o
    WHERE n.source_id = 'primary' AND n.type = 'DEPARTMENT' AND n.id = %(venue)s AND o.singleton
      AND n.id <> %(procurement_group)s
      AND NOT coalesce(o.settings -> 'analytics' -> 'archived_department_ids', '[]'::jsonb) ? n.id::text
      AND (o.settings -> 'sync' ->> 'mode' = 'all'
           OR (o.settings -> 'sync' ->> 'mode' = 'selected'
               AND o.settings -> 'sync' -> 'department_ids' ? n.id::text))
), org AS (
    SELECT id, parent_id FROM {schema}.corporate_nodes WHERE source_id = 'primary'
    UNION
    SELECT id, parent_id FROM {schema}.stores WHERE source_id = 'primary'
), subtree(id) AS (
    SELECT id FROM venue
    UNION
    SELECT org.id FROM org JOIN subtree ON org.parent_id = subtree.id
    WHERE NOT EXISTS (
        SELECT 1 FROM {schema}.corporate_nodes other
        WHERE other.source_id = 'primary' AND other.id = org.id
          AND other.type IN ('DEPARTMENT', 'CENTRALSTORE', 'MANUFACTURE')
    )
), warehouse AS (
    SELECT id, name FROM {schema}.stores WHERE source_id = 'primary' AND id IN (SELECT id FROM subtree)
), household(id) AS (
    SELECT id FROM {schema}.product_groups WHERE source_id = 'primary' AND id = %(household)s
    UNION
    SELECT g.id FROM {schema}.product_groups g JOIN household h ON g.parent_id = h.id
    WHERE g.source_id = 'primary'
), {receipt_routing}, headers AS MATERIALIZED (
    SELECT e.id AS doc, e.document_number AS number, coalesce(e.supplier,'—') AS supplier, e.observed, e.stamp,
           NULL::uuid AS via_id, NULL::text AS via, NULL::uuid AS origin, NULL::text AS origin_number,
           e.stamp AS comparison_stamp, NULL::text AS reason
    FROM external_headers e WHERE e.observed >= %(start)s
    UNION ALL
    SELECT doc,number,supplier,observed,stamp,via_id,via,origin,origin_number,origin_stamp,reason
    FROM routed
), cohort AS MATERIALIZED (
    SELECT c.*, w.id AS store_id,w.name AS store, i.product_id AS pid,i.amount_unit_id AS uid,
           coalesce(p.name,i.product_id::text,'Товар не указан') AS product,
           coalesce(u.name,CASE WHEN i.amount_unit_id IS NULL THEN 'Не указана' ELSE 'ед.' END) AS unit,
           coalesce(i.details->>'is_additional_expense','false') = 'true' AS additional,
           h.id IS NOT NULL AS household, i.amount AS qty,i.sum AS cost,
           i.amount IS NOT NULL AND i.amount>0 AND i.sum IS NOT NULL AND i.sum>=0 AS good
    FROM headers c JOIN {schema}.incoming_invoices t ON t.source_id = 'primary' AND t.id = c.doc
    JOIN {schema}.incoming_invoice_items i ON (i.source_id, i.document_id) = (t.source_id, t.id)
    JOIN warehouse w ON w.id = coalesce(i.store_id,t.default_store_id)
    LEFT JOIN {schema}.products p ON (p.source_id,p.id) = (i.source_id,i.product_id)
    LEFT JOIN household h ON h.id = p.group_id
    LEFT JOIN {schema}.measure_units u ON (u.source_id,u.id) = (i.source_id,i.amount_unit_id)
    WHERE i.present_in_latest
), candidates AS MATERIALIZED (
    SELECT doc,number,supplier,observed,stamp,via_id,via,origin,origin_number,comparison_stamp,reason,
           store_id,store,pid,uid,product,unit,sum(qty) AS qty,sum(cost) AS cost,bool_and(good) AS good
    FROM cohort WHERE NOT additional AND NOT household
      AND (via_id IS NOT NULL OR (pid IS NOT NULL AND uid IS NOT NULL))
    GROUP BY doc,number,supplier,observed,stamp,via_id,via,origin,origin_number,comparison_stamp,reason,
             store_id,store,pid,uid,product,unit
), target_items AS (
    SELECT DISTINCT pid,uid FROM candidates WHERE reason IS NULL
), observations AS MATERIALIZED (
    SELECT t.stamp, i.product_id AS pid,i.amount_unit_id AS uid, sum(i.amount) AS qty,sum(i.sum) AS cost,
           bool_and(i.amount IS NOT NULL AND i.amount>0 AND i.sum IS NOT NULL AND i.sum>=0) AS good
    FROM external_headers t
    JOIN {schema}.incoming_invoice_items i ON (i.source_id, i.document_id) = (t.source_id, t.id)
    JOIN target_items x ON (x.pid,x.uid) = (i.product_id,i.amount_unit_id)
    WHERE t.stamp <= (SELECT max(comparison_stamp) FROM candidates)
      AND i.present_in_latest AND coalesce(i.details->>'is_additional_expense','false') <> 'true'
    GROUP BY 1,2,3
), history AS (
    -- For a transfer, benchmark the supplier purchase date. Every confirmed origin is itself
    -- a network observation, so its preceding six distinct timestamps exclude the purchase.
    SELECT stamp,pid,uid,bool_and(good) OVER prev AS all_valid,
           sum(qty) OVER prev AS prev_qty,sum(cost) OVER prev AS prev_cost
    FROM observations
    WINDOW prev AS (PARTITION BY pid,uid ORDER BY stamp ROWS BETWEEN %(history)s PRECEDING AND 1 PRECEDING)
), results AS MATERIALIZED (
    SELECT c.doc,c.number,c.stamp,c.observed AT TIME ZONE 'Europe/Moscow' AS noticed,c.store,c.product,c.unit,
           c.via,c.origin AS origin_doc,c.origin_number,c.supplier,c.qty::text AS qty,c.cost::text AS cost,
           h.prev_qty::text,h.prev_cost::text,
           coalesce(c.reason,CASE WHEN NOT c.good THEN 'Некорректная сумма или количество'
                                 WHEN h.all_valid IS NOT TRUE OR h.prev_qty IS NULL OR h.prev_qty<=0
                                      OR h.prev_cost IS NULL OR h.prev_cost<=0 THEN 'Недостаточно данных для сравнения' END) AS reason
    FROM candidates c LEFT JOIN history h ON (h.stamp,h.pid,h.uid)=(c.comparison_stamp,c.pid,c.uid)
)
SELECT venue.id AS venue_id,venue.name AS venue,
       (SELECT count(DISTINCT doc) FROM cohort WHERE via_id IS NULL) AS direct_invoices,
       (SELECT count(*) FROM routed) AS routed_invoices,
       coalesce((SELECT json_agg(r ORDER BY stamp DESC,number DESC,doc,product,store)
                 FROM results r WHERE reason IS NULL),'[]'::json) AS rows,
       coalesce((SELECT json_agg(r ORDER BY stamp DESC,number DESC,doc,product,store)
                 FROM results r WHERE via IS NOT NULL AND reason IS NOT NULL),'[]'::json) AS review_rows
FROM venue
