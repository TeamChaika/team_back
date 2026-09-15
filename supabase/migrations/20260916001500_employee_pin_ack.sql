SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(7623011091001);

-- The write-only PIN is never stored. Keep only a successful upstream acknowledgement.
ALTER TABLE chaika.employee_changes ADD COLUMN pin_accepted boolean NOT NULL DEFAULT false;
