BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE chaika.finance_allocations
    DROP CONSTRAINT finance_allocations_method_check,
    ADD CONSTRAINT finance_allocations_method_check
        CHECK (method IN ('equal', 'percent', 'amount', 'weights', 'revenue', 'pending'));

COMMIT;
