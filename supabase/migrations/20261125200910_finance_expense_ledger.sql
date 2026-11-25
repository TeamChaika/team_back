BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE chaika.finance_expenses (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id text NOT NULL DEFAULT 'primary' CHECK (source_id = 'primary'),
    department_id uuid NOT NULL,
    author_id uuid NOT NULL REFERENCES chaika.web_users(id),
    request_id uuid NOT NULL,
    expense_date date NOT NULL,
    paid_date date,
    category text NOT NULL CHECK (category IN ('rent','payroll','utilities','repairs',
                                               'marketing','services','other')),
    amount numeric(14,2) NOT NULL CHECK (amount > 0 AND amount <> 'NaN'::numeric),
    payment_method text NOT NULL CHECK (payment_method IN ('cash','bank')),
    comment text NOT NULL DEFAULT '' CHECK (length(comment) <= 500),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    cancelled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (source_id,department_id) REFERENCES chaika.corporate_nodes(source_id,id),
    UNIQUE (author_id,request_id)
);
CREATE INDEX finance_expenses_venue_date ON chaika.finance_expenses(department_id,expense_date DESC);
CREATE INDEX finance_expenses_author_date ON chaika.finance_expenses(author_id,expense_date DESC);

CREATE TABLE chaika.finance_expense_history (
    expense_id uuid NOT NULL REFERENCES chaika.finance_expenses(id),
    revision integer NOT NULL CHECK (revision > 0),
    actor_id uuid NOT NULL REFERENCES chaika.web_users(id),
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    action text NOT NULL CHECK (action IN ('create','update','cancel')),
    before_state jsonb,
    after_state jsonb NOT NULL,
    CHECK ((before_state IS NULL) = (action = 'create')),
    PRIMARY KEY (expense_id,revision)
);

ALTER TABLE chaika.finance_expenses ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.finance_expense_history ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.finance_expenses,chaika.finance_expense_history FROM PUBLIC, anon, authenticated;
GRANT SELECT,INSERT ON chaika.finance_expenses,chaika.finance_expense_history TO chaika_backend;
GRANT UPDATE (expense_date,paid_date,category,amount,payment_method,comment,revision,cancelled)
    ON chaika.finance_expenses TO chaika_backend;
CREATE POLICY backend_access ON chaika.finance_expenses TO chaika_backend USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON chaika.finance_expense_history TO chaika_backend USING (true) WITH CHECK (true);
COMMIT;
