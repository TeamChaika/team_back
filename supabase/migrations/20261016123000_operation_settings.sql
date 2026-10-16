-- Administrative selections are separate from employee authorization.
CREATE TABLE chaika.operation_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision integer NOT NULL CHECK (revision > 0),
    settings jsonb NOT NULL CHECK ((
        jsonb_typeof(settings) = 'object'
        AND settings ?& ARRAY['sync','writeoff','waybill']
        AND settings->'sync'->>'mode' IN ('all','selected')
        AND settings->'writeoff'->>'mode' IN ('all','selected')
        AND settings->'waybill'->>'mode' IN ('all','selected')
        AND jsonb_typeof(settings->'sync'->'department_ids') = 'array'
        AND jsonb_typeof(settings->'writeoff'->'store_ids') = 'array'
        AND jsonb_typeof(settings->'waybill'->'store_ids') = 'array'
    ) IS TRUE),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid NOT NULL
);
CREATE TABLE chaika.operation_settings_audit (
    revision integer PRIMARY KEY,
    actor_id uuid NOT NULL,
    before_settings jsonb NOT NULL,
    after_settings jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE chaika.operation_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.operation_settings_audit ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.operation_settings,chaika.operation_settings_audit
    FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE ON chaika.operation_settings TO chaika_backend;
GRANT SELECT,INSERT ON chaika.operation_settings_audit TO chaika_backend;
CREATE POLICY backend_access ON chaika.operation_settings TO chaika_backend
    USING(true) WITH CHECK(true);
CREATE POLICY backend_access ON chaika.operation_settings_audit TO chaika_backend
    USING(true) WITH CHECK(true);

-- Preserve provenance of rows retained while an establishment is paused.
ALTER TABLE chaika.sales_report_rows ADD COLUMN origin_report_id uuid
    REFERENCES chaika.sales_reports(id);
ALTER TABLE chaika.store_balance_items ADD COLUMN origin_snapshot_id uuid
    REFERENCES chaika.raw_snapshots(id);
ALTER TABLE chaika.counteragent_balance_items ADD COLUMN origin_snapshot_id uuid
    REFERENCES chaika.raw_snapshots(id);
ALTER TABLE chaika.cash_shift_observations ADD COLUMN origin_snapshot_id uuid
    REFERENCES chaika.raw_snapshots(id);

-- A PostgreSQL view expands o.* only at creation. Keep its existing column order
-- and append the provenance column, so grants and dependants remain intact.
DO $view$
DECLARE observation_columns text;
BEGIN
    SELECT string_agg(format('o.%I', attname), ',' ORDER BY attnum)
      INTO observation_columns
      FROM pg_attribute
     WHERE attrelid = 'chaika.cash_shifts'::regclass AND attnum > 0 AND NOT attisdropped
       AND attname NOT IN ('source_id','last_seen_at','last_snapshot_id','origin_snapshot_id');
    EXECUTE format('CREATE OR REPLACE VIEW chaika.cash_shifts WITH (security_invoker=true) AS
        SELECT DISTINCT ON (d.source_id,o.id) %s,d.source_id,
               COALESCE(origin.observed_at,d.observed_at) AS last_seen_at,
               d.last_snapshot_id,o.origin_snapshot_id
        FROM chaika.cash_shift_days d
        JOIN chaika.cash_shift_observations o ON o.snapshot_id=d.last_snapshot_id
        LEFT JOIN chaika.raw_snapshots origin ON origin.id=o.origin_snapshot_id
        ORDER BY d.source_id,o.id,d.observed_at DESC', observation_columns);
END
$view$;

-- NULL denotes a complete observation, including warehouses with zero stock.
ALTER TABLE chaika.store_balance_reports ADD COLUMN covered_store_ids uuid[];

-- Keep the historical RMS mapping while quarantining a changed live source.
ALTER TABLE chaika.rms_bindings ADD COLUMN sync_blocked boolean NOT NULL DEFAULT false;
