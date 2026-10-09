-- Owner-confirmed first payment, independent of ordinary full-dashboard readiness.
CREATE TABLE {payments}.acceptance_intents (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    configuration_version bigint NOT NULL CHECK(configuration_version>0),
    actor_id uuid NOT NULL,
    session_hash text NOT NULL,
    terminal_id uuid NOT NULL,
    terminal_version_id uuid NOT NULL,
    deposit_id uuid NOT NULL UNIQUE REFERENCES {payments}.deposits(id),
    request_id uuid NOT NULL UNIQUE,
    amount_minor bigint NOT NULL CHECK(amount_minor BETWEEN 100 AND 214748364700 AND amount_minor%100=0),
    currency text NOT NULL CHECK(currency='RUB'),
    mode text NOT NULL CHECK(mode IN ('sandbox','live')),
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    consumed_at timestamptz,
    attempt_id uuid UNIQUE REFERENCES {payments}.attempts(id),
    CHECK((consumed_at IS NULL)=(attempt_id IS NULL)),
    FOREIGN KEY(terminal_version_id,terminal_id) REFERENCES {payments}.terminal_versions(id,terminal_id)
);
GRANT SELECT,INSERT ON {payments}.acceptance_intents TO {payments_role};
GRANT UPDATE(consumed_at,attempt_id) ON {payments}.acceptance_intents TO {payments_role};
ALTER TABLE {payments}.acceptance_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE {payments}.acceptance_intents FORCE ROW LEVEL SECURITY;
CREATE POLICY payment_runtime ON {payments}.acceptance_intents TO {payments_role} USING(true) WITH CHECK(true);
