-- Inventory-derived receipt facts. Authorization and household filters remain live.
CREATE TABLE chaika.purchase_prices_prepared (
    id uuid PRIMARY KEY,
    source_id text NOT NULL UNIQUE REFERENCES chaika.sources(id),
    prepared_at timestamptz NOT NULL,
    revision jsonb NOT NULL
);

CREATE TABLE chaika.purchase_prices_prepared_receipts (
    generation_id uuid NOT NULL REFERENCES chaika.purchase_prices_prepared(id) ON DELETE CASCADE,
    required_store_ids uuid[] NOT NULL,
    has_unknown_store boolean NOT NULL,
    product_id uuid NOT NULL,
    unit_id uuid,
    linked boolean NOT NULL,
    date text,
    amount numeric,
    sum numeric,
    valid boolean NOT NULL
);
CREATE INDEX purchase_prices_receipts_generation_idx
    ON chaika.purchase_prices_prepared_receipts(generation_id);

ALTER TABLE chaika.purchase_prices_prepared ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.purchase_prices_prepared_receipts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.purchase_prices_prepared,
    chaika.purchase_prices_prepared_receipts FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, DELETE ON chaika.purchase_prices_prepared,
    chaika.purchase_prices_prepared_receipts TO chaika_backend;
CREATE POLICY backend_access ON chaika.purchase_prices_prepared TO chaika_backend
    USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON chaika.purchase_prices_prepared_receipts TO chaika_backend
    USING (true) WITH CHECK (true);
