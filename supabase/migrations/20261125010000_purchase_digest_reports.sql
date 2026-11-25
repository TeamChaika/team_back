CREATE TABLE chaika.purchase_digest_destinations (
    department_id uuid PRIMARY KEY,
    chat_id bigint NOT NULL CHECK (chat_id < 0),
    chat_title text NOT NULL,
    bot_id text NOT NULL CHECK (bot_id ~ '^[1-9][0-9]{0,19}$'),
    enabled boolean NOT NULL DEFAULT false,
    start_date date NOT NULL,
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid NOT NULL
);

CREATE TABLE chaika.purchase_digest_deliveries (
    department_id uuid NOT NULL,
    business_date date NOT NULL,
    chat_id bigint NOT NULL CHECK (chat_id < 0),
    bot_id text NOT NULL,
    department_name text NOT NULL,
    state text NOT NULL CHECK (state IN ('sending','sent','unknown','failed')),
    message_id bigint,
    attempts integer NOT NULL CHECK (attempts > 0),
    error_code text,
    next_attempt_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (department_id,business_date),
    CHECK (state <> 'sent' OR (message_id IS NOT NULL AND message_id > 0))
);

ALTER TABLE chaika.purchase_digest_destinations ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.purchase_digest_deliveries ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.purchase_digest_destinations,chaika.purchase_digest_deliveries
    FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE ON chaika.purchase_digest_destinations,chaika.purchase_digest_deliveries
    TO chaika_backend;
CREATE POLICY backend_access ON chaika.purchase_digest_destinations TO chaika_backend
    USING(true) WITH CHECK(true);
CREATE POLICY backend_access ON chaika.purchase_digest_deliveries TO chaika_backend
    USING(true) WITH CHECK(true);
