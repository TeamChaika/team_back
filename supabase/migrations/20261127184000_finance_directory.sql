BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE chaika.finance_categories (
    code text PRIMARY KEY CHECK (length(code) BETWEEN 1 AND 64),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
    sort_order integer NOT NULL DEFAULT 100,
    archived boolean NOT NULL DEFAULT false,
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0)
);
CREATE UNIQUE INDEX finance_categories_active_name
    ON chaika.finance_categories (lower(btrim(name))) WHERE NOT archived;
INSERT INTO chaika.finance_categories (code, name, sort_order) VALUES
    ('rent','Аренда',1), ('payroll','Оплата труда',2), ('utilities','Коммунальные услуги',3),
    ('repairs','Ремонт',4), ('marketing','Маркетинг',5), ('services','Услуги',6),
    ('other','Прочее',7);

ALTER TABLE chaika.finance_expenses DROP CONSTRAINT finance_expenses_category_check,
    ADD FOREIGN KEY (category) REFERENCES chaika.finance_categories(code),
    ADD COLUMN category_name text;
ALTER TABLE chaika.finance_allocations DROP CONSTRAINT finance_allocations_category_check,
    ADD FOREIGN KEY (category) REFERENCES chaika.finance_categories(code),
    ADD COLUMN category_name text;
UPDATE chaika.finance_expenses e SET category_name=c.name FROM chaika.finance_categories c
    WHERE e.category=c.code;
UPDATE chaika.finance_allocations a SET category_name=c.name FROM chaika.finance_categories c
    WHERE a.category=c.code;
ALTER TABLE chaika.finance_expenses ALTER COLUMN category_name SET NOT NULL;
ALTER TABLE chaika.finance_allocations ALTER COLUMN category_name SET NOT NULL;

-- A running previous version still writes the seven original codes without a label.
CREATE FUNCTION chaika.finance_fill_category_name() RETURNS trigger
LANGUAGE plpgsql SET search_path='' AS $fn$
BEGIN
    IF NEW.category_name IS NULL OR (TG_OP='UPDATE' AND NEW.category<>OLD.category
            AND NEW.category_name IS NOT DISTINCT FROM OLD.category_name) THEN
        SELECT name INTO NEW.category_name FROM chaika.finance_categories WHERE code=NEW.category;
    END IF;
    RETURN NEW;
END
$fn$;
REVOKE ALL ON FUNCTION chaika.finance_fill_category_name() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION chaika.finance_fill_category_name() TO chaika_backend;
CREATE TRIGGER finance_expense_category_name BEFORE INSERT OR UPDATE OF category
    ON chaika.finance_expenses FOR EACH ROW EXECUTE FUNCTION chaika.finance_fill_category_name();
CREATE TRIGGER finance_allocation_category_name BEFORE INSERT OR UPDATE OF category
    ON chaika.finance_allocations FOR EACH ROW EXECUTE FUNCTION chaika.finance_fill_category_name();
GRANT UPDATE(category_name) ON chaika.finance_expenses,chaika.finance_allocations TO chaika_backend;

ALTER TABLE chaika.finance_approvers ADD COLUMN archived boolean NOT NULL DEFAULT false,
    ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK (revision > 0);
ALTER TABLE chaika.finance_categories ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.finance_categories FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON chaika.finance_categories TO chaika_backend;
GRANT UPDATE(name, archived, revision) ON chaika.finance_categories TO chaika_backend;
CREATE POLICY backend_categories ON chaika.finance_categories TO chaika_backend
    USING (true) WITH CHECK (true);

GRANT INSERT ON chaika.finance_approvers TO chaika_backend;
GRANT UPDATE(name, available_to_all, archived, revision) ON chaika.finance_approvers TO chaika_backend;
CREATE POLICY backend_insert ON chaika.finance_approvers FOR INSERT TO chaika_backend WITH CHECK (true);
CREATE POLICY backend_update ON chaika.finance_approvers FOR UPDATE TO chaika_backend
    USING (true) WITH CHECK (true);
GRANT INSERT, DELETE ON chaika.finance_department_approvers TO chaika_backend;
CREATE POLICY backend_insert ON chaika.finance_department_approvers FOR INSERT
    TO chaika_backend WITH CHECK (true);
CREATE POLICY backend_delete ON chaika.finance_department_approvers FOR DELETE
    TO chaika_backend USING (true);
COMMIT;
