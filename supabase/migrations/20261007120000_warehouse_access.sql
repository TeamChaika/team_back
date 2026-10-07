-- Existing users keep their current department/document permissions. Selected
-- warehouses are an additional ceiling, never an action grant.
BEGIN;
SET LOCAL lock_timeout='5s';
ALTER TABLE chaika.web_users ADD COLUMN warehouse_scope_mode text NOT NULL DEFAULT 'all'
    CHECK (warehouse_scope_mode IN ('all','selected'));
CREATE TABLE chaika.web_warehouse_access (
    user_id uuid NOT NULL REFERENCES chaika.web_users(id) ON DELETE CASCADE,
    source_id text NOT NULL CHECK (source_id='primary'),
    store_id uuid NOT NULL,
    PRIMARY KEY(user_id,source_id,store_id),
    FOREIGN KEY(source_id,store_id) REFERENCES chaika.stores(source_id,id)
);
ALTER TABLE chaika.web_warehouse_access ENABLE ROW LEVEL SECURITY;
GRANT SELECT,INSERT,UPDATE,DELETE ON chaika.web_warehouse_access TO chaika_backend;
CREATE POLICY backend_access ON chaika.web_warehouse_access FOR ALL TO chaika_backend
    USING(true) WITH CHECK(true);
-- Only these authorization columns cross into the documents runtime. A selected
-- user with zero grants still has one row with NULL store_id and grants nothing.
CREATE VIEW chaika.portal_warehouse_access WITH (security_barrier=true) AS
SELECT u.id AS user_id,u.warehouse_scope_mode,a.source_id,a.store_id
FROM chaika.web_users u LEFT JOIN chaika.web_warehouse_access a ON a.user_id=u.id
WHERE u.active;
REVOKE ALL ON chaika.portal_warehouse_access FROM PUBLIC;
GRANT SELECT ON chaika.portal_warehouse_access TO chaika_backend;
COMMIT;
