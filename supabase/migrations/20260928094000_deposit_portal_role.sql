-- Deposit staff may use the dashboard shell without receiving iiko privileges.
-- Account assignments are a separate, reviewed operational step.
ALTER TABLE chaika.web_users DROP CONSTRAINT web_users_role_check;
ALTER TABLE chaika.web_users ADD CONSTRAINT web_users_role_check
    CHECK (role IN ('owner', 'manager', 'analyst', 'deposits'));
