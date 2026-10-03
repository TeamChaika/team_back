-- Existing accounts are classified separately by a privileged, read-only-first tool.
-- Never reset flags on a repeated migration and never expose Auth hashes to runtime.
ALTER TABLE chaika.web_users
    ADD COLUMN IF NOT EXISTS password_change_required boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS password_changed_at timestamptz;
ALTER TABLE chaika.web_users
    ALTER COLUMN password_change_required SET DEFAULT true;
