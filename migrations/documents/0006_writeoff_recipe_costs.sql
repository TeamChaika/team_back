-- Apply before portal/worker deployment. Read-only recipe/reference access, primary only.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['products','stores','corporate_nodes',
        'assembly_charts','assembly_chart_items','assembly_chart_scopes']
    LOOP
        IF to_regclass('chaika.' || t) IS NOT NULL THEN
            GRANT USAGE ON SCHEMA chaika TO chaika_iiko_app;
            EXECUTE format('GRANT SELECT ON chaika.%I TO chaika_iiko_app', t);
            EXECUTE format('DROP POLICY IF EXISTS documents_recipe_cost ON chaika.%I', t);
            EXECUTE format('CREATE POLICY documents_recipe_cost ON chaika.%I '
                'FOR SELECT TO chaika_iiko_app USING (source_id=''primary'')', t);
        END IF;
    END LOOP;
END $$;
COMMIT;
