-- Derived recipe/sales facts. Prices and user authorizations remain request scoped.
CREATE TABLE chaika.purchase_impact_prepared (
    id uuid PRIMARY KEY,
    source_id text NOT NULL UNIQUE REFERENCES chaika.sources(id),
    prepared_at timestamptz NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    recipe_day date,
    revision jsonb NOT NULL,
    coverage jsonb NOT NULL,
    selling_department_ids jsonb NOT NULL,
    CHECK (period_end >= period_start)
);

CREATE TABLE chaika.purchase_impact_prepared_products (
    generation_id uuid NOT NULL REFERENCES chaika.purchase_impact_prepared(id) ON DELETE CASCADE,
    product_id uuid NOT NULL,
    product_exists boolean NOT NULL,
    main_unit_id uuid,
    has_graph boolean NOT NULL,
    departments jsonb NOT NULL,
    PRIMARY KEY (generation_id, product_id)
);

ALTER TABLE chaika.purchase_impact_prepared ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.purchase_impact_prepared_products ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.purchase_impact_prepared,
    chaika.purchase_impact_prepared_products FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, DELETE ON chaika.purchase_impact_prepared,
    chaika.purchase_impact_prepared_products TO chaika_backend;
CREATE POLICY backend_access ON chaika.purchase_impact_prepared TO chaika_backend
    USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON chaika.purchase_impact_prepared_products TO chaika_backend
    USING (true) WITH CHECK (true);
