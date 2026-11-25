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
), procurement AS MATERIALIZED (
    SELECT s.id, btrim(s.name) AS name FROM {schema}.stores s
    WHERE s.source_id = 'primary' AND s.parent_id = %(procurement_group)s AND s.present_in_latest
      AND btrim(s.name) ~* '^(ООО|ИП|АО|ПАО|ОАО|ЗАО)[[:space:]]+[^[:space:]]'
), external_headers AS NOT MATERIALIZED (
    SELECT t.*, s.name AS supplier,
           coalesce(t.processed_first_seen_at, t.first_seen_at) AS observed,
           CASE WHEN length(t.date_incoming) = 10 THEN t.date_incoming || 'T00:00:00'
                ELSE t.date_incoming END AS stamp
    FROM {schema}.incoming_invoices t
    LEFT JOIN {schema}.counteragents s ON (s.source_id, s.id) = (t.source_id, t.supplier_id)
    WHERE t.source_id = 'primary' AND t.status = 'PROCESSED'
      AND nullif(t.details ->> 'linked_outgoing_invoice_id', '') IS NULL
      AND NOT coalesce(s.represents_store, false) AND s.represented_store_id IS NULL
      AND coalesce(t.processed_first_seen_at, t.first_seen_at) < %(end)s
), pairs AS NOT MATERIALIZED (
    -- Both posted UUID links must agree. The stock counterparty alone could also denote a restaurant.
    SELECT t.id AS doc, t.document_number AS number, t.default_store_id AS receiver_default, o.id AS outgoing,
           o.default_store_id AS outgoing_store, p.id AS via_id, p.name AS via,
           coalesce(ledger.first_paired_at,ready.current_pair_ready) AS observed,
           CASE WHEN length(t.date_incoming) = 10 THEN t.date_incoming || 'T00:00:00'
                ELSE t.date_incoming END AS stamp,
           btrim(regexp_replace(lower(o.details ->> 'comment'), '[[:space:]]+', ' ', 'g')) AS ref
    FROM {schema}.incoming_invoices t
    JOIN {schema}.counteragents c ON (c.source_id, c.id) = (t.source_id, t.supplier_id)
                                  AND c.represents_store
    JOIN procurement p ON p.id = c.represented_store_id
    JOIN {schema}.outgoing_invoices o ON o.source_id = t.source_id AND o.linked_incoming_invoice_id = t.id
                                      AND o.id::text = t.details ->> 'linked_outgoing_invoice_id'
    LEFT JOIN {schema}.purchase_digest_receipt_pairs ledger ON (ledger.source_id,ledger.incoming_id)=(t.source_id,t.id)
    CROSS JOIN LATERAL (
        SELECT greatest(coalesce(t.paired_first_seen_at,t.processed_first_seen_at,t.first_seen_at),
                        coalesce(o.paired_first_seen_at,o.processed_first_seen_at,o.first_seen_at)) AS current_pair_ready
    ) ready
    WHERE t.source_id = 'primary' AND t.status = 'PROCESSED' AND o.status = 'PROCESSED'
      AND ready.current_pair_ready < %(end)s
      AND coalesce(ledger.first_paired_at,ready.current_pair_ready) < %(end)s
      AND EXISTS (
          SELECT 1 FROM {schema}.outgoing_invoice_items oi
          WHERE (oi.source_id, oi.document_id) = (o.source_id, o.id) AND oi.present_in_latest
          HAVING count(*) > 0 AND bool_and(coalesce(oi.store_id, o.default_store_id) IS NOT DISTINCT FROM p.id)
      )
), day_pairs AS MATERIALIZED (
    SELECT p.* FROM pairs p
    WHERE p.observed >= %(start)s AND EXISTS (
        SELECT 1 FROM {schema}.incoming_invoices t
        JOIN {schema}.incoming_invoice_items i ON (i.source_id, i.document_id) = (t.source_id, t.id)
        JOIN warehouse w ON w.id = coalesce(i.store_id, t.default_store_id)
        WHERE t.source_id = 'primary' AND t.id = p.doc AND i.present_in_latest
    )
), origin_headers AS MATERIALIZED (
    -- Look up references only at the purchasing warehouses needed by this venue today.
    SELECT i.source_warehouse AS via_id, e.id AS origin, e.document_number AS origin_number,
           e.supplier, e.stamp, e.details ->> 'incoming_document_number' AS supplier_number,
           e.details ->> 'invoice' AS invoice, e.details ->> 'transport_invoice_number' AS transport_number
    FROM external_headers e
    JOIN (
        SELECT DISTINCT li.document_id, coalesce(li.store_id, h.default_store_id) AS source_warehouse
        FROM {schema}.incoming_invoice_items li
        JOIN {schema}.incoming_invoices h ON (h.source_id,h.id) = (li.source_id,li.document_id)
        JOIN (SELECT DISTINCT via_id FROM day_pairs) needed ON needed.via_id = coalesce(li.store_id,h.default_store_id)
        WHERE li.source_id = 'primary' AND li.present_in_latest
    ) i ON i.document_id = e.id
    WHERE nullif(btrim(e.supplier), '') IS NOT NULL
), origin_keys AS MATERIALIZED (
    -- Normalization preserves digits, leading zeroes and punctuation in the supplier's references.
    SELECT DISTINCT h.via_id, h.origin, h.stamp,
           btrim(regexp_replace(lower(h.supplier || ' ' || btrim(k.number)), '[[:space:]]+', ' ', 'g')) AS ref
    FROM origin_headers h CROSS JOIN LATERAL (
        VALUES (h.supplier_number), (h.invoice), (h.transport_number), (h.origin_number)
    ) k(number) WHERE nullif(btrim(k.number), '') IS NOT NULL
), day_origins AS (
    SELECT DISTINCT k.via_id, k.origin FROM day_pairs p JOIN origin_keys k USING (via_id, ref)
    WHERE left(k.stamp, 10) <= left(p.stamp, 10)
), relevant_refs AS (
    SELECT k.via_id, k.ref FROM origin_keys k JOIN day_origins d USING (via_id, origin)
    UNION SELECT via_id, ref FROM day_pairs
), related_pairs AS MATERIALIZED (
    -- Include receipts at OTHER venues for the same references, for the allocation ceiling only.
    SELECT p.* FROM pairs p JOIN relevant_refs r USING (via_id, ref)
), possible AS MATERIALIZED (
    SELECT p.doc, p.via_id, k.origin FROM related_pairs p JOIN origin_keys k USING (via_id, ref)
    WHERE left(k.stamp, 10) <= left(p.stamp, 10)
), receiver_stores AS (
    -- A gain on one store's lines must not cancel a loss on another store's lines.
    SELECT i.document_id AS doc, i.product_id AS pid, i.amount_unit_id AS uid,
           coalesce(i.store_id,p.receiver_default) AS receiving_store,
           sum(i.amount) AS qty, sum(i.sum) AS cost,
           bool_and(coalesce(i.store_id,p.receiver_default) IS NOT NULL AND i.product_id IS NOT NULL
                    AND i.amount_unit_id IS NOT NULL AND i.amount IS NOT NULL AND i.amount > 0
                    AND i.sum IS NOT NULL AND i.sum >= 0) AS good
    FROM {schema}.incoming_invoice_items i JOIN related_pairs p ON p.doc = i.document_id
    WHERE i.source_id = 'primary' AND i.present_in_latest
      AND coalesce(i.details ->> 'is_additional_expense', 'false') <> 'true'
    GROUP BY 1, 2, 3, 4
), receipt_content AS MATERIALIZED (
    SELECT r.*,sum(qty) OVER item AS doc_qty FROM receiver_stores r
    WINDOW item AS (PARTITION BY doc,pid,uid)
), origin_content AS MATERIALIZED (
    SELECT k.via_id, k.origin, i.product_id AS pid, i.amount_unit_id AS uid,
           sum(i.amount) AS qty, sum(i.sum) AS cost,
           bool_and(i.amount IS NOT NULL AND i.amount > 0 AND i.sum IS NOT NULL AND i.sum >= 0) AS good
    FROM (SELECT DISTINCT via_id, origin FROM possible) k
    JOIN {schema}.incoming_invoices t ON t.source_id = 'primary' AND t.id = k.origin
    JOIN {schema}.incoming_invoice_items i ON (i.source_id, i.document_id) = (t.source_id, t.id)
                                          AND coalesce(i.store_id, t.default_store_id) = k.via_id
    WHERE i.present_in_latest AND coalesce(i.details ->> 'is_additional_expense', 'false') <> 'true'
    GROUP BY 1, 2, 3, 4
), checked AS (
    SELECT p.doc, p.origin,
           bool_and(coalesce(r.good AND e.good AND r.doc_qty <= e.qty, false)) AS quantity_ok,
           bool_and(coalesce(abs(r.cost - e.cost*r.qty/nullif(e.qty,0)) <= 0.01, false)) AS cost_ok
    FROM possible p JOIN receipt_content r USING (doc)
    LEFT JOIN origin_content e ON (e.via_id,e.origin,e.pid,e.uid) = (p.via_id,p.origin,r.pid,r.uid)
    GROUP BY p.doc, p.origin
), match_count AS (
    SELECT doc, bool_or(quantity_ok) AS any_qty_ok,
           count(*) FILTER (WHERE quantity_ok AND cost_ok) AS fits
    FROM checked GROUP BY doc
), allocation AS (
    -- Earlier peers retain their consumed quantity. Ambiguous claims also reserve capacity so
    -- another venue never gets a confirmed allocation based on potentially reused stock.
    SELECT p.doc, p.via_id, c.origin, r.pid, r.uid,
           sum(r.qty) OVER peers AS used_qty, sum(r.cost) OVER peers AS used_cost
    FROM related_pairs p JOIN checked c ON c.doc = p.doc AND c.quantity_ok AND c.cost_ok
    JOIN receipt_content r ON r.doc = p.doc
    WINDOW peers AS (PARTITION BY p.via_id,c.origin,r.pid,r.uid ORDER BY p.observed RANGE UNBOUNDED PRECEDING)
), budget AS (
    -- Preserve the cents in actual receiving invoices. Partial shares may round by one kopek;
    -- a fully allocated product must account for exactly the supplier's total.
    SELECT a.doc, a.origin, bool_and(a.used_qty <= e.qty) AS qty_ok,
           bool_and(a.used_cost <= e.cost AND (a.used_qty <> e.qty OR a.used_cost = e.cost)
                    AND abs(a.used_cost - e.cost*a.used_qty/nullif(e.qty,0)) <= 0.01) AS cost_ok
    FROM allocation a JOIN origin_content e USING (via_id, origin, pid, uid)
    GROUP BY a.doc, a.origin
), routed AS MATERIALIZED (
    SELECT p.*, e.origin, e.origin_number, e.supplier, e.stamp AS origin_stamp,
           CASE WHEN m.doc IS NULL THEN 'Исходная накладная поставщика не найдена'
                WHEN m.fits = 0 AND m.any_qty_ok THEN 'Сумма передачи отличается от закупочной'
                WHEN m.fits = 0 THEN 'Товар, единица или количество расходятся с закупочной накладной'
                WHEN m.fits > 1 THEN 'Подходят несколько накладных поставщика'
                WHEN NOT b.qty_ok THEN 'Количество по передачам превышает объём исходной закупки'
                WHEN NOT b.cost_ok THEN 'Сумма по передачам расходится с исходной закупкой'
           END AS reason
    FROM day_pairs p LEFT JOIN match_count m ON m.doc = p.doc
    LEFT JOIN budget b ON b.doc = p.doc AND m.fits = 1
    LEFT JOIN origin_headers e ON (e.via_id,e.origin) = (p.via_id,b.origin) AND b.qty_ok AND b.cost_ok
), headers AS MATERIALIZED (
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
