-- Confirmed external receipts, generated off the HTTP path from the canonical matcher.
CREATE TABLE chaika.purchase_receipts_prepared (
    id uuid PRIMARY KEY,
    source_id text NOT NULL UNIQUE REFERENCES chaika.sources(id),
    prepared_at timestamptz NOT NULL,
    revision jsonb NOT NULL
);
CREATE TABLE chaika.purchase_receipts_prepared_lines (
    generation_id uuid NOT NULL REFERENCES chaika.purchase_receipts_prepared(id) ON DELETE CASCADE,
    document_id uuid NOT NULL, num integer NOT NULL, num_occurrence integer NOT NULL,
    product_id uuid NOT NULL, amount_unit_id uuid, store_id uuid,
    amount numeric, sum numeric NOT NULL, price numeric, date text,
    supplier_id uuid, receipt_route text NOT NULL,
    required_store_ids uuid[] NOT NULL, has_unknown_store boolean NOT NULL,
    origin_store_ids uuid[] NOT NULL, origin_unknown_store boolean NOT NULL, via_id uuid,
    PRIMARY KEY(generation_id,document_id,num,num_occurrence)
);
CREATE INDEX purchase_receipts_lines_product_idx ON chaika.purchase_receipts_prepared_lines
    (generation_id,product_id,amount_unit_id,date DESC);
CREATE TABLE chaika.purchase_receipts_prepared_observations (
    generation_id uuid NOT NULL REFERENCES chaika.purchase_receipts_prepared(id) ON DELETE CASCADE,
    product_id uuid NOT NULL, unit_id uuid, date text,
    amount numeric, sum numeric, valid boolean NOT NULL,
    receipt_route text NOT NULL,
    required_store_ids uuid[] NOT NULL, has_unknown_store boolean NOT NULL,
    origin_store_ids uuid[] NOT NULL, origin_unknown_store boolean NOT NULL, via_id uuid
);
CREATE INDEX purchase_receipts_observations_generation_idx
    ON chaika.purchase_receipts_prepared_observations(generation_id);
DO $permissions$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['purchase_receipts_prepared','purchase_receipts_prepared_lines',
                            'purchase_receipts_prepared_observations'] LOOP
        EXECUTE format('ALTER TABLE chaika.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('REVOKE ALL ON chaika.%I FROM PUBLIC, anon, authenticated', t);
        EXECUTE format('GRANT SELECT, INSERT, DELETE ON chaika.%I TO chaika_backend', t);
        EXECUTE format('CREATE POLICY backend_access ON chaika.%I TO chaika_backend USING(true) WITH CHECK(true)', t);
    END LOOP;
END
$permissions$;
