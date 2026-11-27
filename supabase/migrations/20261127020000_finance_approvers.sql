BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE chaika.finance_approvers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
    available_to_all boolean NOT NULL DEFAULT false
);
CREATE TABLE chaika.finance_department_approvers (
    source_id text NOT NULL DEFAULT 'primary' CHECK (source_id='primary'),
    department_id uuid NOT NULL,
    approver_id uuid NOT NULL REFERENCES chaika.finance_approvers(id),
    PRIMARY KEY (source_id,department_id,approver_id),
    FOREIGN KEY (source_id,department_id) REFERENCES chaika.corporate_nodes(source_id,id)
);
ALTER TABLE chaika.finance_approvers ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.finance_department_approvers ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.finance_approvers,chaika.finance_department_approvers
    FROM PUBLIC, anon, authenticated;
GRANT SELECT ON chaika.finance_approvers,chaika.finance_department_approvers TO chaika_backend;
CREATE POLICY backend_read ON chaika.finance_approvers FOR SELECT TO chaika_backend USING (true);
CREATE POLICY backend_read ON chaika.finance_department_approvers FOR SELECT
    TO chaika_backend USING (true);

ALTER TABLE chaika.finance_expenses
    ADD COLUMN approver_id uuid REFERENCES chaika.finance_approvers(id),
    ADD COLUMN approver_name text,
    ADD CHECK ((approver_id IS NULL) = (approver_name IS NULL));
GRANT UPDATE (approver_id,approver_name) ON chaika.finance_expenses TO chaika_backend;
COMMIT;
