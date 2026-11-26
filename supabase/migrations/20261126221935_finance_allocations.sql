BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE chaika.finance_allocations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    author_id uuid NOT NULL REFERENCES chaika.web_users(id),
    request_id uuid NOT NULL,
    request_payload jsonb NOT NULL,
    expense_date date NOT NULL,
    paid_date date,
    category text NOT NULL CHECK (category IN ('rent','payroll','utilities','repairs',
                                               'marketing','services','other')),
    amount numeric(14,2) NOT NULL CHECK (amount > 0 AND amount <> 'NaN'::numeric),
    payment_method text NOT NULL CHECK (payment_method IN ('cash','bank')),
    comment text NOT NULL DEFAULT '' CHECK (length(comment) <= 500),
    method text NOT NULL CHECK (method IN ('equal','weights','revenue','pending')),
    parts jsonb NOT NULL CHECK (jsonb_typeof(parts)='array' AND jsonb_array_length(parts)>=2
                               AND jsonb_array_length(parts)<=100),
    -- Monotonic union protects snapshots after a participant is removed.
    access_department_ids uuid[] NOT NULL CHECK (cardinality(access_department_ids)>=2),
    revision integer NOT NULL DEFAULT 1 CHECK (revision>0),
    cancelled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (author_id,request_id)
);
CREATE INDEX finance_allocations_pending_date ON chaika.finance_allocations(expense_date DESC)
    WHERE method='pending' AND NOT cancelled;

ALTER TABLE chaika.finance_expenses ADD COLUMN allocation_id uuid
    REFERENCES chaika.finance_allocations(id);
CREATE UNIQUE INDEX finance_expenses_allocation_venue
    ON chaika.finance_expenses(allocation_id,department_id) WHERE allocation_id IS NOT NULL;

CREATE TABLE chaika.finance_allocation_history (
    allocation_id uuid NOT NULL REFERENCES chaika.finance_allocations(id),
    revision integer NOT NULL CHECK (revision>0),
    actor_id uuid NOT NULL REFERENCES chaika.web_users(id),
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    action text NOT NULL CHECK (action IN ('create','update','cancel')),
    before_state jsonb,
    after_state jsonb NOT NULL,
    CHECK ((before_state IS NULL) = (action='create')),
    PRIMARY KEY (allocation_id,revision)
);
ALTER TABLE chaika.finance_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.finance_allocation_history ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.finance_allocations,chaika.finance_allocation_history
    FROM PUBLIC, anon, authenticated;
GRANT SELECT,INSERT ON chaika.finance_allocations,chaika.finance_allocation_history
    TO chaika_backend;
GRANT UPDATE (expense_date,paid_date,category,amount,payment_method,comment,method,parts,
              access_department_ids,revision,cancelled)
    ON chaika.finance_allocations TO chaika_backend;
CREATE POLICY backend_access ON chaika.finance_allocations TO chaika_backend
    USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON chaika.finance_allocation_history TO chaika_backend
    USING (true) WITH CHECK (true);

-- Check after the complete transaction, so a half-written group cannot commit.
CREATE FUNCTION chaika.check_finance_allocation_balance() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE
    target_id uuid;
    allocation chaika.finance_allocations%ROWTYPE;
    expected numeric;
    posted numeric;
BEGIN
    IF TG_TABLE_NAME='finance_allocations' THEN
        target_id := NEW.id;
    ELSE
        target_id := NEW.allocation_id;
    END IF;
    IF target_id IS NULL THEN RETURN NULL; END IF;
    SELECT * INTO STRICT allocation FROM chaika.finance_allocations
        WHERE id=target_id FOR UPDATE;
    expected := CASE WHEN allocation.cancelled OR allocation.method='pending'
                     THEN 0 ELSE allocation.amount END;
    SELECT coalesce(sum(amount),0) INTO posted FROM chaika.finance_expenses
        WHERE allocation_id=target_id AND NOT cancelled;
    IF expected<>posted THEN
        RAISE EXCEPTION 'Unbalanced finance allocation' USING ERRCODE='23514';
    END IF;
    IF EXISTS (
        SELECT FROM chaika.finance_expenses e WHERE e.allocation_id=target_id AND NOT e.cancelled
        AND (e.expense_date<>allocation.expense_date
             OR e.paid_date IS DISTINCT FROM allocation.paid_date
             OR e.category<>allocation.category OR e.payment_method<>allocation.payment_method
             OR e.comment<>allocation.comment OR e.author_id<>allocation.author_id
             OR NOT EXISTS (SELECT FROM jsonb_array_elements(allocation.parts) part
                            WHERE (part->>'department_id')::uuid=e.department_id
                            AND (part->>'amount')::numeric=e.amount))
    ) THEN
        RAISE EXCEPTION 'Mismatched finance allocation share' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION chaika.check_finance_allocation_balance() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION chaika.check_finance_allocation_balance() TO chaika_backend;
CREATE CONSTRAINT TRIGGER finance_allocation_balanced
    AFTER INSERT OR UPDATE ON chaika.finance_allocations DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION chaika.check_finance_allocation_balance();
CREATE CONSTRAINT TRIGGER finance_allocation_share_balanced
    AFTER INSERT OR UPDATE ON chaika.finance_expenses DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION chaika.check_finance_allocation_balance();
COMMIT;
