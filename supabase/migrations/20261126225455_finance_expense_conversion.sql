BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE chaika.finance_expenses ADD COLUMN converted_allocation_id uuid UNIQUE
    REFERENCES chaika.finance_allocations(id);
ALTER TABLE chaika.finance_expenses ADD CONSTRAINT finance_converted_source_retired
    CHECK (converted_allocation_id IS NULL OR (cancelled AND allocation_id IS NULL));
ALTER TABLE chaika.finance_allocations ADD COLUMN source_expense_id uuid UNIQUE
    REFERENCES chaika.finance_expenses(id);
ALTER TABLE chaika.finance_expense_history DROP CONSTRAINT finance_expense_history_action_check;
ALTER TABLE chaika.finance_expense_history ADD CONSTRAINT finance_expense_history_action_check
    CHECK (action IN ('create','update','cancel','convert'));
GRANT UPDATE (converted_allocation_id) ON chaika.finance_expenses TO chaika_backend;
CREATE UNIQUE INDEX finance_conversion_request ON chaika.finance_allocations
    ((request_payload->>'conversion_actor_id'),(request_payload->>'conversion_request_id'))
    WHERE source_expense_id IS NOT NULL;
COMMIT;
