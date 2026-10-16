-- Administrative selections are separate from employee authorization.
CREATE TABLE {analytics}.operation_settings (
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
CREATE TABLE {analytics}.operation_settings_audit (
    revision integer PRIMARY KEY,
    actor_id uuid NOT NULL,
    before_settings jsonb NOT NULL,
    after_settings jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE {analytics}.operation_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.operation_settings_audit ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON {analytics}.operation_settings,{analytics}.operation_settings_audit
    FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE ON {analytics}.operation_settings TO {runtime_role};
GRANT SELECT,INSERT ON {analytics}.operation_settings_audit TO {runtime_role};
CREATE POLICY backend_access ON {analytics}.operation_settings TO {runtime_role}
    USING(true) WITH CHECK(true);
CREATE POLICY backend_access ON {analytics}.operation_settings_audit TO {runtime_role}
    USING(true) WITH CHECK(true);

-- Preserve provenance of rows retained while an establishment is paused.
ALTER TABLE {analytics}.sales_report_rows ADD COLUMN origin_report_id uuid
    REFERENCES {analytics}.sales_reports(id);
ALTER TABLE {analytics}.store_balance_items ADD COLUMN origin_snapshot_id uuid
    REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE {analytics}.counteragent_balance_items ADD COLUMN origin_snapshot_id uuid
    REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE {analytics}.cash_shift_observations ADD COLUMN origin_snapshot_id uuid
    REFERENCES {analytics}.raw_snapshots(id);

-- A PostgreSQL view expands o.* only at creation. Keep its existing column order
-- and append the provenance column, so grants and dependants remain intact.
DO $view$
DECLARE observation_columns text;
BEGIN
    SELECT string_agg(format('o.%I', attname), ',' ORDER BY attnum)
      INTO observation_columns
      FROM pg_attribute
     WHERE attrelid = '{analytics}.cash_shifts'::regclass AND attnum > 0 AND NOT attisdropped
       AND attname NOT IN ('source_id','last_seen_at','last_snapshot_id','origin_snapshot_id');
    EXECUTE format('CREATE OR REPLACE VIEW {analytics}.cash_shifts WITH (security_invoker=true) AS
        SELECT DISTINCT ON (d.source_id,o.id) %s,d.source_id,
               COALESCE(origin.observed_at,d.observed_at) AS last_seen_at,
               d.last_snapshot_id,o.origin_snapshot_id
        FROM {analytics}.cash_shift_days d
        JOIN {analytics}.cash_shift_observations o ON o.snapshot_id=d.last_snapshot_id
        LEFT JOIN {analytics}.raw_snapshots origin ON origin.id=o.origin_snapshot_id
        ORDER BY d.source_id,o.id,d.observed_at DESC', observation_columns);
END
$view$;

-- NULL denotes a complete observation, including warehouses with zero stock.
ALTER TABLE {analytics}.store_balance_reports ADD COLUMN covered_store_ids uuid[];

-- Keep the historical RMS mapping while quarantining a changed live source.
ALTER TABLE {analytics}.rms_bindings ADD COLUMN sync_blocked boolean NOT NULL DEFAULT false;
