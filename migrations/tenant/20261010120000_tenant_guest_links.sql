-- Capabilities remain secret: only a SHA-256 lookup hash is persisted.
CREATE TABLE {payments}.guest_links (
 code_hash text PRIMARY KEY CHECK(length(code_hash)=64),
 deposit_id uuid NOT NULL REFERENCES {payments}.deposits(id) ON DELETE CASCADE,
 guest_token_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX guest_links_deposit ON {payments}.guest_links(deposit_id);
GRANT SELECT,INSERT ON {payments}.guest_links TO {payments_role};
ALTER TABLE {payments}.guest_links ENABLE ROW LEVEL SECURITY;
CREATE POLICY payment_runtime ON {payments}.guest_links TO {payments_role}
 USING(true) WITH CHECK(true);
