-- Empty company payment baseline. Runtime cannot read analytical/documents schemas.
CREATE ROLE {payments_role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT USAGE ON SCHEMA {payments} TO {payments_role};

CREATE TABLE {payments}.venues (
    id uuid PRIMARY KEY,
    name varchar(100) NOT NULL UNIQUE CHECK(length(trim(name))>0),
    active boolean NOT NULL DEFAULT true,
    revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
    default_terminal_id uuid,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE {payments}.terminals (
    id uuid PRIMARY KEY,
    venue_id uuid NOT NULL REFERENCES {payments}.venues(id),
    name varchar(100) NOT NULL,
    active boolean NOT NULL DEFAULT true,
    revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
    current_version_id uuid,
    UNIQUE(id,venue_id)
);
CREATE TABLE {payments}.terminal_versions (
    id uuid PRIMARY KEY,
    terminal_id uuid NOT NULL REFERENCES {payments}.terminals(id),
    revision integer NOT NULL CHECK(revision>0),
    encrypted_key text NOT NULL,
    merchant_id varchar(200),
    mode text NOT NULL CHECK(mode IN ('sandbox','live')),
    currency text NOT NULL DEFAULT 'RUB' CHECK(currency='RUB'),
    created_by uuid NOT NULL,
    actor_kind text NOT NULL CHECK(actor_kind IN ('platform_owner','company_member')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(terminal_id,revision), UNIQUE(id,terminal_id)
);
ALTER TABLE {payments}.venues ADD CONSTRAINT default_terminal_belongs_to_venue
    FOREIGN KEY(default_terminal_id,id) REFERENCES {payments}.terminals(id,venue_id);
ALTER TABLE {payments}.terminals ADD CONSTRAINT current_version_belongs_to_terminal
    FOREIGN KEY(current_version_id,id) REFERENCES {payments}.terminal_versions(id,terminal_id);
CREATE TABLE {payments}.deposit_grants (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    venue_id uuid REFERENCES {payments}.venues(id),
    is_all boolean NOT NULL DEFAULT false,
    can_create boolean NOT NULL DEFAULT false,
    profile_revision integer CHECK(profile_revision>0),
    CHECK(is_all=(venue_id IS NULL)),
    UNIQUE NULLS NOT DISTINCT(user_id,venue_id)
);
CREATE TABLE {payments}.deposits (
    id uuid PRIMARY KEY,
    request_id uuid NOT NULL UNIQUE,
    fingerprint text NOT NULL,
    venue_id uuid NOT NULL REFERENCES {payments}.venues(id),
    customer_name varchar(100) NOT NULL,
    phone varchar(15) NOT NULL CHECK(phone ~ '^[0-9]+$'),
    amount_minor bigint NOT NULL CHECK(amount_minor BETWEEN 100 AND 214748364700 AND amount_minor%100=0),
    currency text NOT NULL DEFAULT 'RUB' CHECK(currency='RUB'),
    reservation_date timestamptz,
    reservation_day date,
    notes varchar(500),
    status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid','failed')),
    paid_at timestamptz,
    created_by uuid NOT NULL,
    actor_kind text NOT NULL CHECK(actor_kind IN ('platform_owner','company_member')),
    guest_token_hash text NOT NULL,
    encrypted_guest_token text NOT NULL,
    revision integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX deposit_dates ON {payments}.deposits(reservation_day,created_at);
CREATE TABLE {payments}.attempts (
    id uuid PRIMARY KEY,
    deposit_id uuid NOT NULL REFERENCES {payments}.deposits(id),
    request_id uuid NOT NULL UNIQUE,
    terminal_id uuid NOT NULL REFERENCES {payments}.terminals(id),
    terminal_version_id uuid NOT NULL,
    amount_minor bigint NOT NULL CHECK(amount_minor>0),
    currency text NOT NULL CHECK(currency='RUB'),
    operation_id uuid UNIQUE,
    state text NOT NULL CHECK(state IN ('creating','unknown','pending','paid','failed','expired')),
    payment_url text,
    qr_image text,
    callback_token_hash text NOT NULL,
    diagnostic text,
    check_count integer NOT NULL DEFAULT 0,
    checked_at timestamptz,
    next_check_at timestamptz NOT NULL DEFAULT now(),
    valid_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(terminal_version_id,terminal_id) REFERENCES {payments}.terminal_versions(id,terminal_id)
);
CREATE UNIQUE INDEX one_unsettled_attempt ON {payments}.attempts(deposit_id)
    WHERE state IN ('creating','unknown','pending');
CREATE TABLE {payments}.audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_id uuid,
    actor_kind text NOT NULL CHECK(actor_kind IN ('platform_owner','company_member','provider_reconcile')),
    action text NOT NULL,
    object_id uuid NOT NULL,
    data jsonb NOT NULL DEFAULT '{{}}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE {payments}.webhook_receipts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_id uuid NOT NULL REFERENCES {payments}.attempts(id),
    fingerprint text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(attempt_id,fingerprint)
);
GRANT SELECT,INSERT,UPDATE,DELETE ON {payments}.venues,{payments}.terminals,
    {payments}.deposit_grants TO {payments_role};
GRANT SELECT,INSERT,UPDATE ON {payments}.deposits,{payments}.attempts TO {payments_role};
GRANT SELECT,INSERT ON {payments}.terminal_versions,{payments}.audit,
    {payments}.webhook_receipts TO {payments_role};
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {payments} TO {payments_role};
ALTER TABLE {payments}.venues ENABLE ROW LEVEL SECURITY;
ALTER TABLE {payments}.terminals ENABLE ROW LEVEL SECURITY;
ALTER TABLE {payments}.terminal_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE {payments}.deposit_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE {payments}.deposits ENABLE ROW LEVEL SECURITY;
ALTER TABLE {payments}.attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE {payments}.audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE {payments}.webhook_receipts ENABLE ROW LEVEL SECURITY;
CREATE POLICY payment_runtime ON {payments}.venues TO {payments_role} USING(true) WITH CHECK(true);
CREATE POLICY payment_runtime ON {payments}.terminals TO {payments_role} USING(true) WITH CHECK(true);
CREATE POLICY payment_runtime ON {payments}.terminal_versions TO {payments_role} USING(true) WITH CHECK(true);
CREATE POLICY payment_runtime ON {payments}.deposit_grants TO {payments_role} USING(true) WITH CHECK(true);
CREATE POLICY payment_runtime ON {payments}.deposits TO {payments_role} USING(true) WITH CHECK(true);
CREATE POLICY payment_runtime ON {payments}.attempts TO {payments_role} USING(true) WITH CHECK(true);
CREATE POLICY payment_runtime ON {payments}.audit TO {payments_role} USING(true) WITH CHECK(true);
CREATE POLICY payment_runtime ON {payments}.webhook_receipts TO {payments_role} USING(true) WITH CHECK(true);
