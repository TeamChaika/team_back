-- A local receipt separates profile commit from payment-grant synchronization.
CREATE TABLE {analytics}.portal_account_initializations (
 request_id uuid PRIMARY KEY,
 user_id uuid NOT NULL UNIQUE REFERENCES {analytics}.web_users(id),
 actor_id uuid NOT NULL,
 fingerprint text NOT NULL,
 expected_revision bigint NOT NULL DEFAULT 2 CHECK(expected_revision=2),
 payment_cursor bigint,
 profile_applied boolean NOT NULL DEFAULT false,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done','superseded')),
 created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE {analytics}.portal_account_initializations ENABLE ROW LEVEL SECURITY;
CREATE POLICY runtime_access ON {analytics}.portal_account_initializations TO {runtime_role}
 USING(true) WITH CHECK(true);
GRANT SELECT,INSERT,UPDATE ON {analytics}.portal_account_initializations TO {runtime_role};
