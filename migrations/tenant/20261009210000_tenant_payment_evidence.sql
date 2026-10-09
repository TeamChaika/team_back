-- Provider-returned evidence, pinned to the immutable terminal credential version.
CREATE TABLE {payments}.terminal_checks (
 id uuid PRIMARY KEY,
 terminal_version_id uuid NOT NULL REFERENCES {payments}.terminal_versions(id),
 merchant_id varchar(200) NOT NULL,
 qrt_name varchar(200) NOT NULL,
 mode text NOT NULL CHECK(mode IN ('sandbox','live')),
 subscription_end_date date NOT NULL,
 qrt_is_b2c boolean NOT NULL,
 requires_receipt boolean,
 is_nomenclature boolean,
 is_cash_link boolean,
 checked_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(id,terminal_version_id)
);
GRANT SELECT,INSERT ON {payments}.terminal_checks TO {payments_role};
ALTER TABLE {payments}.terminal_checks ENABLE ROW LEVEL SECURITY;
CREATE POLICY payment_runtime ON {payments}.terminal_checks TO {payments_role}
 USING(true) WITH CHECK(true);
ALTER TABLE {payments}.attempts
 ADD COLUMN terminal_check_id uuid,
 ADD COLUMN creation_confirmed boolean NOT NULL DEFAULT false,
 ADD COLUMN creation_currency text CHECK(creation_currency ~ '^[A-Z]{{3}}$'),
 ADD COLUMN provider_amount_minor bigint CHECK(provider_amount_minor>0),
 ADD COLUMN provider_qr_payload text,
 ADD CONSTRAINT attempt_check_same_version FOREIGN KEY(terminal_check_id,terminal_version_id)
 REFERENCES {payments}.terminal_checks(id,terminal_version_id);
