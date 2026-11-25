WITH RECURSIVE venue AS (
    SELECT n.id, n.name
    FROM {schema}.corporate_nodes n CROSS JOIN {schema}.operation_settings o
    WHERE n.source_id = 'primary' AND n.type = 'DEPARTMENT' AND n.id = %(venue)s AND o.singleton
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
), cohort AS MATERIALIZED (
    SELECT t.id AS doc, t.document_number AS number, coalesce(s.name, '—') AS supplier,
           t.first_seen_at AT TIME ZONE 'Europe/Moscow' AS noticed,
           CASE WHEN length(t.date_incoming) = 10 THEN t.date_incoming || 'T00:00:00'
                ELSE t.date_incoming END AS stamp,
           w.id AS store_id, w.name AS store, i.product_id AS pid, i.amount_unit_id AS uid,
           coalesce(p.name, i.product_id::text) AS product, coalesce(u.name, 'ед.') AS unit,
           coalesce(i.details ->> 'is_additional_expense', 'false') = 'true' AS additional,
           h.id IS NOT NULL AS household, i.amount AS qty, i.sum AS cost,
           (i.amount IS NOT NULL AND i.amount > 0 AND i.sum IS NOT NULL AND i.sum >= 0) AS good
    FROM {schema}.incoming_invoices t
    JOIN {schema}.incoming_invoice_items i ON (i.source_id, i.document_id) = (t.source_id, t.id)
                                          AND i.present_in_latest
    JOIN warehouse w ON w.id = coalesce(i.store_id, t.default_store_id)
    LEFT JOIN {schema}.products p ON p.source_id = t.source_id AND p.id = i.product_id
    LEFT JOIN household h ON h.id = p.group_id
    LEFT JOIN {schema}.measure_units u ON u.source_id = t.source_id AND u.id = i.amount_unit_id
    LEFT JOIN {schema}.counteragents s ON s.source_id = t.source_id AND s.id = t.supplier_id
    WHERE t.source_id = 'primary' AND t.first_seen_at >= %(start)s AND t.first_seen_at < %(end)s
      AND t.status = 'PROCESSED' AND nullif(t.details ->> 'linked_outgoing_invoice_id', '') IS NULL
), candidates AS MATERIALIZED (
    SELECT doc, number, supplier, noticed, stamp, store_id, store, pid, uid, product, unit,
           sum(qty) AS qty, sum(cost) AS cost, bool_and(good) AS good
    FROM cohort WHERE NOT additional AND NOT household AND pid IS NOT NULL AND uid IS NOT NULL
    GROUP BY doc, number, supplier, noticed, stamp, store_id, store, pid, uid, product, unit
), target_items AS (
    SELECT DISTINCT pid, uid FROM candidates
), observations AS MATERIALIZED (
    -- Pool comparable receipt timestamps across the network before choosing the six prior ones.
    SELECT CASE WHEN length(t.date_incoming) = 10 THEN t.date_incoming || 'T00:00:00'
                ELSE t.date_incoming END AS stamp,
           i.product_id AS pid, i.amount_unit_id AS uid, sum(i.amount) AS qty, sum(i.sum) AS cost,
           bool_and(i.amount IS NOT NULL AND i.amount > 0 AND i.sum IS NOT NULL AND i.sum >= 0) AS good
    FROM {schema}.incoming_invoices t
    JOIN {schema}.incoming_invoice_items i ON (i.source_id, i.document_id) = (t.source_id, t.id)
    JOIN target_items x ON x.pid = i.product_id AND x.uid = i.amount_unit_id
    WHERE t.source_id = 'primary' AND t.first_seen_at < %(end)s AND t.status = 'PROCESSED'
          AND t.date_incoming <= (SELECT max(stamp) FROM candidates)
          AND i.present_in_latest AND coalesce(i.details ->> 'is_additional_expense', 'false') <> 'true'
          AND nullif(t.details ->> 'linked_outgoing_invoice_id', '') IS NULL
    GROUP BY 1, 2, 3
), history AS (
    SELECT stamp, pid, uid, bool_and(good) OVER prev AS all_valid,
           sum(qty) OVER prev AS prev_qty, sum(cost) OVER prev AS prev_cost
    FROM observations
    WINDOW prev AS (PARTITION BY pid, uid ORDER BY stamp ROWS BETWEEN %(history)s PRECEDING AND 1 PRECEDING)
), comparable AS (
    SELECT c.doc, c.number, c.supplier, c.noticed, c.stamp, c.store, c.product, c.unit,
           c.qty::text, c.cost::text, h.prev_qty::text, h.prev_cost::text
    FROM candidates c JOIN history h USING (stamp, pid, uid)
    WHERE c.good AND h.all_valid AND h.prev_qty > 0 AND h.prev_cost > 0
)
SELECT venue.id AS venue_id, venue.name AS venue,
       (SELECT count(DISTINCT doc) FROM cohort) AS direct_invoices,
       coalesce((SELECT json_agg(c ORDER BY stamp DESC, number DESC, doc, product, store)
                 FROM comparable c), '[]'::json) AS rows
FROM venue
