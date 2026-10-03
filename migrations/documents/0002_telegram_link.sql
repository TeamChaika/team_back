-- Run as chaika_iiko_migrator before deploying self-service profile endpoints.
-- No existing identity, document, warehouse grant or history is changed.
BEGIN;
SET LOCAL search_path=chaika_iiko_documents,pg_catalog;
CREATE TABLE IF NOT EXISTS native_telegram_link (
    token_hash text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    portal_id uuid NOT NULL UNIQUE,
    user_id bigint NOT NULL REFERENCES authentication_user(id) ON DELETE CASCADE,
    revision integer NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- UPDATE is required by SELECT ... FOR UPDATE during atomic consumption.
GRANT SELECT,INSERT,UPDATE,DELETE ON native_telegram_link TO chaika_iiko_app;
COMMIT;
