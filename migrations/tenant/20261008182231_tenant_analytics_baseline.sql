-- Private tenant template. Apply only through app.tenancy.migrations.
CREATE TABLE {analytics}.accounts (
    source_id text NOT NULL,
    id uuid NOT NULL,
    root_type text NOT NULL,
    code text,
    name text NOT NULL,
    deleted boolean NOT NULL,
    account_parent_id uuid,
    parent_corporate_id uuid,
    type text NOT NULL,
    system boolean NOT NULL,
    custom_transactions_allowed boolean NOT NULL,
    present_in_latest boolean DEFAULT true NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    last_snapshot_id uuid NOT NULL,
    details jsonb NOT NULL,
    CONSTRAINT accounts_root_type_check CHECK ((root_type = 'Account'::text))
);
COMMENT ON COLUMN {analytics}.accounts.account_parent_id IS 'Original parent account UUID. Missing parents are reported, not discarded or fabricated.';
COMMENT ON COLUMN {analytics}.accounts.type IS 'Original iiko account type, including future codes. Not a rule for interpreting balance signs.';
COMMENT ON COLUMN {analytics}.accounts.present_in_latest IS 'Presence in latest full export. Absence is not evidence of deletion.';
CREATE TABLE {analytics}.assembly_chart_items (
    source_id text NOT NULL,
    chart_id uuid NOT NULL,
    id uuid NOT NULL,
    product_id uuid NOT NULL,
    amount_in numeric NOT NULL,
    amount_middle numeric NOT NULL,
    amount_out numeric NOT NULL,
    sort_weight numeric NOT NULL,
    details jsonb NOT NULL,
    present_in_latest boolean DEFAULT true NOT NULL
);
CREATE TABLE {analytics}.assembly_chart_scopes (
    source_id text NOT NULL,
    business_date date NOT NULL,
    chart_id uuid NOT NULL,
    present_in_latest boolean DEFAULT true NOT NULL,
    last_snapshot_id uuid NOT NULL
);
CREATE TABLE {analytics}.assembly_charts (
    source_id text NOT NULL,
    id uuid NOT NULL,
    product_id uuid NOT NULL,
    date_from date NOT NULL,
    date_to date,
    assembled_amount numeric NOT NULL,
    details jsonb NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    last_snapshot_id uuid NOT NULL,
    CONSTRAINT assembly_charts_check CHECK (((date_to IS NULL) OR (date_to > date_from)))
);
CREATE TABLE {analytics}.assistant_conversations (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    access_hash text NOT NULL,
    context jsonb NOT NULL,
    title text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT assistant_conversations_context_check CHECK ((jsonb_typeof(context) = 'object'::text)),
    CONSTRAINT assistant_conversations_title_check CHECK (((length(title) >= 1) AND (length(title) <= 120)))
);
ALTER TABLE ONLY {analytics}.assistant_conversations FORCE ROW LEVEL SECURITY;
CREATE TABLE {analytics}.assistant_turns (
    id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    question text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    answer text,
    sources jsonb DEFAULT '[]'::jsonb NOT NULL,
    usage jsonb DEFAULT '{{}}'::jsonb NOT NULL,
    model text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    CONSTRAINT assistant_turns_check CHECK (((status <> 'completed'::text) OR ((answer IS NOT NULL) AND ((length(answer) >= 1) AND (length(answer) <= 16000))))),
    CONSTRAINT assistant_turns_question_check CHECK (((length(question) >= 1) AND (length(question) <= 2000))),
    CONSTRAINT assistant_turns_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'completed'::text, 'failed'::text])))
);
ALTER TABLE ONLY {analytics}.assistant_turns FORCE ROW LEVEL SECURITY;
CREATE TABLE {analytics}.cash_shift_days (
    source_id text NOT NULL,
    open_day date NOT NULL,
    last_snapshot_id uuid NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    row_count integer NOT NULL,
    CONSTRAINT cash_shift_days_row_count_check CHECK ((row_count >= 0))
);
CREATE TABLE {analytics}.cash_shift_observations (
    snapshot_id uuid NOT NULL,
    id uuid NOT NULL,
    session_number integer NOT NULL,
    fiscal_number integer,
    cash_reg_number integer NOT NULL,
    cash_reg_serial text,
    open_date timestamp without time zone NOT NULL,
    close_date timestamp without time zone,
    accept_date timestamp without time zone,
    manager_id uuid,
    responsible_user_id uuid,
    session_start_cash numeric,
    pay_orders numeric,
    sum_writeoff_orders numeric,
    sales_cash numeric,
    sales_credit numeric,
    sales_card numeric,
    pay_in numeric,
    pay_out numeric,
    pay_income numeric,
    cash_remain numeric,
    cash_diff numeric,
    session_status text NOT NULL,
    conception_id uuid,
    point_of_sale_id uuid,
    department_id uuid,
    group_id uuid,
    point_of_sale_name text,
    mapping_state text NOT NULL,
    groups_snapshot_id uuid NOT NULL,
    CONSTRAINT cash_shift_observations_check CHECK (((mapping_state = 'matched'::text) = ((department_id IS NOT NULL) AND (group_id IS NOT NULL)))),
    CONSTRAINT cash_shift_observations_mapping_state_check CHECK ((mapping_state = ANY (ARRAY['matched'::text, 'missing'::text, 'ambiguous'::text, 'unknown_department'::text])))
);
CREATE VIEW {analytics}.cash_shifts WITH (security_invoker='true') AS
 SELECT DISTINCT ON (d.source_id, o.id) o.snapshot_id,
    o.id,
    o.session_number,
    o.fiscal_number,
    o.cash_reg_number,
    o.cash_reg_serial,
    o.open_date,
    o.close_date,
    o.accept_date,
    o.manager_id,
    o.responsible_user_id,
    o.session_start_cash,
    o.pay_orders,
    o.sum_writeoff_orders,
    o.sales_cash,
    o.sales_credit,
    o.sales_card,
    o.pay_in,
    o.pay_out,
    o.pay_income,
    o.cash_remain,
    o.cash_diff,
    o.session_status,
    o.conception_id,
    o.point_of_sale_id,
    o.department_id,
    o.group_id,
    o.point_of_sale_name,
    o.mapping_state,
    o.groups_snapshot_id,
    d.source_id,
    d.observed_at AS last_seen_at,
    d.last_snapshot_id
   FROM ({analytics}.cash_shift_days d
     JOIN {analytics}.cash_shift_observations o ON ((o.snapshot_id = d.last_snapshot_id)))
  ORDER BY d.source_id, o.id, d.observed_at DESC;
CREATE TABLE {analytics}.corporate_nodes (
    source_id text NOT NULL,
    id uuid NOT NULL,
    parent_id uuid,
    code text,
    name text,
    type text NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    present_in_latest boolean DEFAULT true NOT NULL,
    last_snapshot_id uuid NOT NULL
);
CREATE TABLE {analytics}.counteragent_balance_items (
    snapshot_id uuid NOT NULL,
    line_num integer NOT NULL,
    account_id uuid NOT NULL,
    counteragent_id uuid,
    department_id uuid,
    sum numeric NOT NULL,
    CONSTRAINT counteragent_balance_items_line_num_check CHECK ((line_num > 0)),
    CONSTRAINT counteragent_balance_items_sum_check CHECK ((sum <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])))
);
COMMENT ON COLUMN {analytics}.counteragent_balance_items.line_num IS '1-based row position in the original array; preserves repeated account/counteragent/department combinations.';
COMMENT ON COLUMN {analytics}.counteragent_balance_items.sum IS 'Exact signed source balance; do not aggregate across accounts or infer debt direction without account semantics.';
CREATE TABLE {analytics}.counteragent_balance_reports (
    source_id text NOT NULL,
    accounting_timestamp timestamp without time zone NOT NULL,
    last_snapshot_id uuid NOT NULL,
    row_count integer NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    CONSTRAINT counteragent_balance_reports_accounting_timestamp_check CHECK ((accounting_timestamp = date_trunc('second'::text, accounting_timestamp))),
    CONSTRAINT counteragent_balance_reports_row_count_check CHECK ((row_count >= 0))
);
COMMENT ON TABLE {analytics}.counteragent_balance_reports IS 'Latest complete unfiltered report per source and accounting timestamp. Join items by last_snapshot_id.';
COMMENT ON COLUMN {analytics}.counteragent_balance_reports.accounting_timestamp IS 'Accounting time sent to iiko verbatim, without UTC conversion.';
CREATE VIEW {analytics}.counteragent_balances_with_accounts WITH (security_invoker='true') AS
 SELECT r.source_id,
    r.accounting_timestamp,
    i.snapshot_id,
    i.line_num,
    i.account_id,
    a.name AS account_name,
    a.code AS account_code,
    a.type AS account_type,
    a.account_parent_id,
    a.parent_corporate_id,
    a.deleted AS account_deleted,
    (a.id IS NOT NULL) AS account_resolved,
    a.present_in_latest AS account_present_in_latest,
    i.counteragent_id,
    i.department_id,
    i.sum
   FROM (({analytics}.counteragent_balance_reports r
     JOIN {analytics}.counteragent_balance_items i ON ((i.snapshot_id = r.last_snapshot_id)))
     LEFT JOIN {analytics}.accounts a ON (((a.source_id = r.source_id) AND (a.id = i.account_id))));
COMMENT ON VIEW {analytics}.counteragent_balances_with_accounts IS 'Latest observation per accounting timestamp, enriched with current account names. Not a P&L report.';
CREATE TABLE {analytics}.counteragents (
    source_id text NOT NULL,
    id uuid NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    deleted boolean,
    supplier boolean,
    employee boolean,
    client boolean,
    represents_store boolean,
    represented_store_id uuid,
    present_in_latest boolean DEFAULT true NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    last_snapshot_id uuid NOT NULL,
    details jsonb NOT NULL
);
COMMENT ON TABLE {analytics}.counteragents IS 'Records returned by /suppliers; not an exhaustive customer directory.';
CREATE TABLE {analytics}.employee_changes (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    is_create boolean NOT NULL,
    request_hash text NOT NULL,
    fields jsonb NOT NULL,
    before_fields jsonb,
    status text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    snapshot_id uuid,
    error_code text,
    pin_accepted boolean DEFAULT false NOT NULL,
    CONSTRAINT employee_changes_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'confirmed'::text, 'rejected'::text, 'reconciled'::text])))
);
CREATE TABLE {analytics}.employee_roles (
    source_id text NOT NULL,
    id uuid NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    payment_per_hour numeric,
    steady_salary numeric,
    schedule_type text,
    deleted boolean,
    present_in_latest boolean DEFAULT true NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    last_snapshot_id uuid NOT NULL,
    details jsonb NOT NULL
);
COMMENT ON COLUMN {analytics}.employee_roles.code IS 'Original code; empty and duplicate codes are valid.';
COMMENT ON COLUMN {analytics}.employee_roles.present_in_latest IS 'Presence in latest full export. Absence is not evidence of deletion.';
CREATE TABLE {analytics}.employees (
    source_id text NOT NULL,
    id uuid NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    first_name text,
    middle_name text,
    last_name text,
    main_role_id uuid,
    role_ids uuid[],
    main_role_code text,
    role_codes text[],
    preferred_department_code text,
    department_codes text[],
    department_codes_state text,
    responsibility_department_codes text[],
    responsibility_department_codes_state text,
    deleted boolean,
    employee boolean,
    supplier boolean,
    client boolean,
    present_in_latest boolean DEFAULT true NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    last_snapshot_id uuid NOT NULL,
    details jsonb NOT NULL,
    phone text,
    cell_phone text,
    email text
);
COMMENT ON COLUMN {analytics}.employees.code IS 'Original iiko code; empty and duplicate values are valid.';
COMMENT ON COLUMN {analytics}.employees.department_codes_state IS 'Original departmentCodesState; NULL/EMPTY strings are retained without inferring access scope.';
COMMENT ON COLUMN {analytics}.employees.present_in_latest IS 'Presence in the last complete includeDeleted=false export; absence does not mean fired or deleted.';
CREATE VIEW {analytics}.employee_role_assignments WITH (security_invoker='true') AS
 SELECT e.source_id,
    e.id AS employee_id,
    e.code AS employee_code,
    e.name AS employee_name,
    e.present_in_latest AS employee_present_in_latest,
    assignment.kind AS assignment_kind,
    assignment.ordinal,
    assignment.role_id,
    r.name AS role_name,
    r.code AS role_code,
    r.deleted AS role_deleted,
    (r.id IS NOT NULL) AS role_resolved,
    r.present_in_latest AS role_present_in_latest
   FROM (({analytics}.employees e
     CROSS JOIN LATERAL ( SELECT 'main'::text AS kind,
            (0)::bigint AS ordinal,
            e.main_role_id AS role_id
          WHERE (e.main_role_id IS NOT NULL)
        UNION ALL
         SELECT 'list'::text AS text,
            a.ordinal,
            a.role_id
           FROM unnest(e.role_ids) WITH ORDINALITY a(role_id, ordinal)
          WHERE (a.role_id IS NOT NULL)) assignment)
     LEFT JOIN {analytics}.employee_roles r ON (((r.source_id = e.source_id) AND (r.id = assignment.role_id))));
CREATE TABLE {analytics}.incoming_invoice_items (
    source_id text NOT NULL,
    document_id uuid NOT NULL,
    num integer NOT NULL,
    product_id uuid,
    store_id uuid,
    amount numeric,
    actual_amount numeric,
    price numeric,
    sum numeric NOT NULL,
    amount_unit_id uuid,
    details jsonb NOT NULL,
    present_in_latest boolean DEFAULT true NOT NULL,
    num_occurrence integer DEFAULT 1 NOT NULL,
    CONSTRAINT incoming_invoice_items_num_occurrence_check CHECK ((num_occurrence > 0))
);
COMMENT ON COLUMN {analytics}.incoming_invoice_items.num_occurrence IS '1-based occurrence of the source num in XML order; not an iiko identifier.';
CREATE TABLE {analytics}.incoming_invoices (
    source_id text NOT NULL,
    id uuid NOT NULL,
    document_number text,
    date_incoming text,
    incoming_date text,
    status text,
    supplier_id uuid,
    default_store_id uuid,
    revision bigint,
    last_export_date date NOT NULL,
    details jsonb NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    last_snapshot_id uuid NOT NULL
);
CREATE TABLE {analytics}.indicator_filter_sync (
    source_id text NOT NULL,
    field text NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    synced_at timestamp with time zone DEFAULT now() NOT NULL,
    value_count integer NOT NULL,
    CONSTRAINT indicator_filter_sync_value_count_check CHECK ((value_count >= 0))
);
CREATE TABLE {analytics}.indicator_filter_values (
    source_id text NOT NULL,
    field text NOT NULL,
    department_id uuid NOT NULL,
    value text NOT NULL,
    CONSTRAINT indicator_filter_values_value_check CHECK (((length(value) >= 1) AND (length(value) <= 500)))
);
CREATE TABLE {analytics}.internal_transfer_items (
    source_id text NOT NULL,
    document_id uuid NOT NULL,
    num integer NOT NULL,
    product_id uuid NOT NULL,
    measure_unit_id uuid,
    product_size_id uuid,
    container_id uuid,
    amount numeric NOT NULL,
    cost numeric,
    amount_factor numeric,
    details jsonb NOT NULL,
    present_in_latest boolean DEFAULT true NOT NULL,
    CONSTRAINT internal_transfer_items_amount_check CHECK ((amount <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric]))),
    CONSTRAINT internal_transfer_items_amount_factor_check CHECK ((amount_factor <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric]))),
    CONSTRAINT internal_transfer_items_cost_check CHECK ((cost <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])))
);
COMMENT ON COLUMN {analytics}.internal_transfer_items.cost IS 'Original total line cost, not unit price; null is not zero.';
CREATE TABLE {analytics}.internal_transfers (
    source_id text NOT NULL,
    id uuid NOT NULL,
    document_number text NOT NULL,
    date_incoming timestamp without time zone NOT NULL,
    status text NOT NULL,
    store_from_id uuid NOT NULL,
    store_to_id uuid NOT NULL,
    last_export_date date NOT NULL,
    details jsonb NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    last_snapshot_id uuid NOT NULL,
    CONSTRAINT internal_transfers_status_check CHECK ((status = ANY (ARRAY['NEW'::text, 'PROCESSED'::text, 'DELETED'::text])))
);
COMMENT ON TABLE {analytics}.internal_transfers IS 'Original internalTransfer documents, separate from outgoing/incoming invoice pairs; all source statuses are preserved.';
COMMENT ON COLUMN {analytics}.internal_transfers.date_incoming IS 'Accounting local time, without conversion to UTC. Original JSON remains in RAW.';
CREATE TABLE {analytics}.manual_sync_requests (
    request_id uuid NOT NULL,
    job text NOT NULL,
    requested_by uuid NOT NULL,
    requested_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    error_code text,
    CONSTRAINT manual_sync_requests_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'running'::text, 'succeeded'::text, 'failed'::text])))
);
CREATE TABLE {analytics}.measure_units (
    source_id text NOT NULL,
    id uuid NOT NULL,
    root_type text NOT NULL,
    code text,
    name text NOT NULL,
    deleted boolean NOT NULL,
    present_in_latest boolean DEFAULT true NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    last_snapshot_id uuid NOT NULL,
    details jsonb NOT NULL,
    CONSTRAINT measure_units_root_type_check CHECK ((root_type = 'MeasureUnit'::text))
);
CREATE TABLE {analytics}.outgoing_invoice_items (
    source_id text NOT NULL,
    document_id uuid NOT NULL,
    line_num integer NOT NULL,
    product_id uuid,
    store_id uuid,
    container_id uuid,
    amount numeric,
    price numeric,
    sum numeric NOT NULL,
    details jsonb NOT NULL,
    present_in_latest boolean DEFAULT true NOT NULL,
    CONSTRAINT outgoing_invoice_items_line_num_check CHECK ((line_num > 0)),
    CONSTRAINT outgoing_invoice_items_sum_check CHECK ((sum <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])))
);
COMMENT ON COLUMN {analytics}.outgoing_invoice_items.line_num IS '1-based position in the XML array, not an iiko row identifier; original observations remain in RAW.';
CREATE TABLE {analytics}.outgoing_invoices (
    source_id text NOT NULL,
    id uuid NOT NULL,
    document_number text,
    date_incoming text NOT NULL,
    status text NOT NULL,
    counteragent_id uuid,
    default_store_id uuid,
    linked_incoming_invoice_id uuid,
    linked_outgoing_invoice_id uuid,
    last_export_date date NOT NULL,
    details jsonb NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    last_snapshot_id uuid NOT NULL,
    CONSTRAINT outgoing_invoices_status_check CHECK ((status = ANY (ARRAY['NEW'::text, 'PROCESSED'::text, 'DELETED'::text])))
);
COMMENT ON TABLE {analytics}.outgoing_invoices IS 'All returned statuses and counterparties. Do not treat drafts or unverified recipients as completed internal transfers.';
COMMENT ON COLUMN {analytics}.outgoing_invoices.linked_incoming_invoice_id IS 'Original linkedIncomingInvoiceId from iiko. Missing pairs are reported, never inferred from document numbers.';
CREATE TABLE {analytics}.portal_admin_audit (
    id bigint NOT NULL,
    actor_id uuid NOT NULL,
    action text NOT NULL,
    target_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE {analytics}.portal_admin_audit ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME {analytics}.portal_admin_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE {analytics}.portal_identity_metadata (
    id uuid NOT NULL,
    email text,
    provision_id text
);
CREATE VIEW {analytics}.portal_identities WITH (security_invoker='true', security_barrier='true') AS
 SELECT id,
    email,
    provision_id
   FROM {analytics}.portal_identity_metadata;
CREATE TABLE {analytics}.web_users (
    id uuid NOT NULL,
    display_name text NOT NULL,
    role text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sections text[] DEFAULT '{{}}'::text[] NOT NULL,
    is_portal_admin boolean DEFAULT false NOT NULL,
    all_departments boolean DEFAULT false NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    password_change_required boolean DEFAULT true NOT NULL,
    password_changed_at timestamp with time zone,
    warehouse_scope_mode text DEFAULT 'all'::text NOT NULL,
    CONSTRAINT web_users_display_name_check CHECK (((length(display_name) >= 1) AND (length(display_name) <= 150))),
    CONSTRAINT web_users_role_check CHECK ((role = ANY (ARRAY['owner'::text, 'manager'::text, 'analyst'::text, 'deposits'::text]))),
    CONSTRAINT web_users_warehouse_scope_mode_check CHECK ((warehouse_scope_mode = ANY (ARRAY['all'::text, 'selected'::text])))
);
CREATE TABLE {analytics}.web_warehouse_access (
    user_id uuid NOT NULL,
    source_id text NOT NULL,
    store_id uuid NOT NULL,
    CONSTRAINT web_warehouse_access_source_id_check CHECK ((source_id = 'primary'::text))
);
CREATE VIEW {analytics}.portal_warehouse_access WITH (security_invoker='true', security_barrier='true') AS
 SELECT u.id AS user_id,
    u.warehouse_scope_mode,
    a.source_id,
    a.store_id
   FROM ({analytics}.web_users u
     LEFT JOIN {analytics}.web_warehouse_access a ON ((a.user_id = u.id)))
  WHERE u.active;
CREATE TABLE {analytics}.product_categories (
    source_id text NOT NULL,
    id uuid NOT NULL,
    root_type text,
    code text,
    name text NOT NULL,
    deleted boolean NOT NULL,
    present_in_latest boolean DEFAULT true NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    last_snapshot_id uuid NOT NULL,
    details jsonb NOT NULL,
    CONSTRAINT product_categories_root_type_check CHECK ((root_type = 'ProductCategory'::text))
);
CREATE TABLE {analytics}.product_groups (
    source_id text NOT NULL,
    id uuid NOT NULL,
    name text NOT NULL,
    parent_id uuid,
    code text,
    num text,
    deleted boolean NOT NULL,
    details jsonb NOT NULL,
    present_in_latest boolean DEFAULT true NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    last_snapshot_id uuid NOT NULL
);
CREATE TABLE {analytics}.products (
    source_id text NOT NULL,
    id uuid NOT NULL,
    name text NOT NULL,
    type text NOT NULL,
    group_id uuid,
    main_unit_id uuid NOT NULL,
    category_id uuid,
    code text,
    num text,
    deleted boolean NOT NULL,
    default_sale_price numeric NOT NULL,
    unit_weight numeric NOT NULL,
    unit_capacity numeric NOT NULL,
    details jsonb NOT NULL,
    present_in_latest boolean DEFAULT true NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    last_snapshot_id uuid NOT NULL
);
CREATE TABLE {analytics}.purchase_impact_prepared (
    id uuid NOT NULL,
    source_id text NOT NULL,
    prepared_at timestamp with time zone NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    recipe_day date,
    revision jsonb NOT NULL,
    coverage jsonb NOT NULL,
    selling_department_ids jsonb NOT NULL,
    CONSTRAINT purchase_impact_prepared_check CHECK ((period_end >= period_start))
);
CREATE TABLE {analytics}.purchase_impact_prepared_products (
    generation_id uuid NOT NULL,
    product_id uuid NOT NULL,
    product_exists boolean NOT NULL,
    main_unit_id uuid,
    has_graph boolean NOT NULL,
    departments jsonb NOT NULL
);
CREATE TABLE {analytics}.purchase_prices_prepared (
    id uuid NOT NULL,
    source_id text NOT NULL,
    prepared_at timestamp with time zone NOT NULL,
    revision jsonb NOT NULL
);
CREATE TABLE {analytics}.purchase_prices_prepared_receipts (
    generation_id uuid NOT NULL,
    required_store_ids uuid[] NOT NULL,
    has_unknown_store boolean NOT NULL,
    product_id uuid NOT NULL,
    unit_id uuid,
    linked boolean NOT NULL,
    date text,
    amount numeric,
    sum numeric,
    valid boolean NOT NULL
);
CREATE TABLE {analytics}.raw_snapshots (
    id uuid NOT NULL,
    run_id uuid NOT NULL,
    source_id text NOT NULL,
    resource text NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    sha256 text NOT NULL,
    source_bytes integer NOT NULL,
    raw bytea NOT NULL,
    normalized jsonb NOT NULL,
    CONSTRAINT raw_snapshots_check CHECK ((octet_length(raw) = source_bytes)),
    CONSTRAINT raw_snapshots_check1 CHECK ((encode(sha256(raw), 'hex'::text) = sha256)),
    CONSTRAINT raw_snapshots_resource_check CHECK ((resource = ANY (ARRAY['server_type'::text, 'departments'::text, 'groups'::text, 'stores'::text, 'replication'::text, 'products'::text, 'product_groups'::text, 'incoming_invoices'::text, 'writeoffs'::text, 'assembly_charts'::text, 'employees'::text, 'store_balances'::text, 'counteragents'::text, 'measure_units'::text, 'product_categories'::text, 'counteragent_balances'::text, 'outgoing_invoices'::text, 'transfers'::text, 'events'::text, 'event_types'::text, 'employee_roles'::text, 'accounts'::text, 'cash_shifts'::text]))),
    CONSTRAINT raw_snapshots_sha256_check CHECK ((sha256 ~ '^[0-9a-f]{{64}}$'::text)),
    CONSTRAINT raw_snapshots_source_bytes_check CHECK ((source_bytes >= 0))
);
CREATE TABLE {analytics}.rms_bindings (
    source_id text NOT NULL,
    chain_source_id text NOT NULL,
    department_id uuid,
    state text NOT NULL,
    details jsonb NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    chain_snapshot_id uuid NOT NULL,
    rms_snapshot_id uuid NOT NULL,
    groups_snapshot_id uuid NOT NULL,
    CONSTRAINT rms_bindings_check CHECK ((source_id <> chain_source_id)),
    CONSTRAINT rms_bindings_check1 CHECK (((state <> 'matched'::text) OR (department_id IS NOT NULL)))
);
CREATE TABLE {analytics}.rms_event_days (
    source_id text NOT NULL,
    event_date date NOT NULL,
    timezone text NOT NULL,
    last_snapshot_id uuid NOT NULL,
    event_count integer NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    CONSTRAINT rms_event_days_event_count_check CHECK ((event_count >= 0))
);
CREATE TABLE {analytics}.rms_event_links (
    source_id text NOT NULL,
    event_id uuid NOT NULL,
    paired_event_id uuid,
    status text NOT NULL,
    candidate_ids jsonb NOT NULL,
    algorithm_version text NOT NULL,
    evidence jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT rms_event_links_check CHECK (((status = 'matched'::text) = (paired_event_id IS NOT NULL))),
    CONSTRAINT rms_event_links_check1 CHECK ((event_id <> paired_event_id)),
    CONSTRAINT rms_event_links_status_check CHECK ((status = ANY (ARRAY['matched'::text, 'pending'::text, 'ambiguous'::text, 'invalid'::text])))
);
COMMENT ON TABLE {analytics}.rms_event_links IS 'Rebuildable hypotheses. matched means unique reciprocal event matching, not a source-provided order-line identity.';
CREATE TABLE {analytics}.rms_event_observations (
    snapshot_id uuid NOT NULL,
    source_id text NOT NULL,
    event_id uuid NOT NULL,
    version_id uuid NOT NULL
);
CREATE TABLE {analytics}.rms_event_types (
    source_id text NOT NULL,
    id text NOT NULL,
    label text NOT NULL,
    details jsonb NOT NULL,
    last_snapshot_id uuid NOT NULL
);
CREATE TABLE {analytics}.rms_event_versions (
    source_id text NOT NULL,
    event_id uuid NOT NULL,
    version_id uuid NOT NULL,
    version_no integer NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    snapshot_id uuid NOT NULL,
    content_hash text NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT rms_event_versions_content_hash_check CHECK ((content_hash ~ '^[0-9a-f]{{64}}$'::text)),
    CONSTRAINT rms_event_versions_version_no_check CHECK ((version_no > 0))
);
COMMENT ON TABLE {analytics}.rms_event_versions IS 'Append-only observed business versions, including A-B-A. Credential attributes are redacted; original XML remains private locally.';
CREATE TABLE {analytics}.rms_events (
    source_id text NOT NULL,
    id uuid NOT NULL,
    version_id uuid NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    event_type text NOT NULL,
    order_id uuid,
    order_number text,
    department_code text,
    actor_id uuid,
    authorizer_id uuid,
    waiter_id uuid,
    terminal_id uuid,
    event_sum numeric,
    order_sum_after_discount numeric,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL
);
CREATE TABLE {analytics}.sales_drilldown_captures (
    id uuid NOT NULL,
    report_id uuid NOT NULL,
    ordinal integer NOT NULL,
    order_id uuid,
    request jsonb NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    raw bytea NOT NULL,
    sha256 text NOT NULL,
    rows jsonb NOT NULL,
    CONSTRAINT sales_drilldown_captures_check CHECK ((encode(sha256(raw), 'hex'::text) = sha256)),
    CONSTRAINT sales_drilldown_captures_rows_check CHECK (((jsonb_typeof(rows) = 'array'::text) AND (jsonb_array_length(rows) <= 10000)))
);
CREATE TABLE {analytics}.sales_report_captures (
    id uuid NOT NULL,
    source_id text NOT NULL,
    kind text NOT NULL,
    date_from date NOT NULL,
    date_to date NOT NULL,
    request jsonb NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    raw bytea NOT NULL,
    sha256 text NOT NULL,
    row_count integer NOT NULL,
    CONSTRAINT sales_report_captures_check CHECK (((date_to >= date_from) AND ((date_to - date_from) <= 6))),
    CONSTRAINT sales_report_captures_check1 CHECK ((encode(sha256(raw), 'hex'::text) = sha256)),
    CONSTRAINT sales_report_captures_kind_check CHECK ((kind = ANY (ARRAY['daily'::text, 'dishes'::text, 'payments'::text, 'discounts'::text, 'returns'::text, 'waiters'::text, 'hours'::text]))),
    CONSTRAINT sales_report_captures_row_count_check CHECK ((row_count >= 0))
);
CREATE TABLE {analytics}.sales_report_days (
    source_id text NOT NULL,
    business_date date NOT NULL,
    current_set_id uuid NOT NULL
);
CREATE TABLE {analytics}.sales_report_rows (
    report_id uuid NOT NULL,
    ordinal integer NOT NULL,
    department_id uuid NOT NULL,
    revenue numeric NOT NULL,
    cost numeric,
    checks numeric,
    guests numeric,
    discount numeric,
    return_sum numeric,
    quantity numeric,
    dimensions jsonb NOT NULL,
    CONSTRAINT sales_report_rows_ordinal_check CHECK ((ordinal >= 0))
);
CREATE TABLE {analytics}.sales_report_sets (
    id uuid NOT NULL,
    source_id text NOT NULL,
    business_date date NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    imported_at timestamp with time zone DEFAULT now() NOT NULL,
    reviewed boolean DEFAULT false NOT NULL,
    checks jsonb NOT NULL,
    reviewed_at timestamp with time zone,
    reviewed_by uuid
);
CREATE TABLE {analytics}.sales_reports (
    id uuid NOT NULL,
    set_id uuid NOT NULL,
    kind text NOT NULL,
    request jsonb NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    raw bytea,
    sha256 text NOT NULL,
    row_count integer NOT NULL,
    source_capture_id uuid,
    CONSTRAINT sales_reports_check CHECK ((encode(sha256(raw), 'hex'::text) = sha256)),
    CONSTRAINT sales_reports_kind_check CHECK ((kind = ANY (ARRAY['daily'::text, 'dishes'::text, 'payments'::text, 'discounts'::text, 'returns'::text, 'waiters'::text, 'hours'::text]))),
    CONSTRAINT sales_reports_raw_origin CHECK ((((raw IS NOT NULL) AND (source_capture_id IS NULL)) OR ((raw IS NULL) AND (source_capture_id IS NOT NULL)))),
    CONSTRAINT sales_reports_row_count_check CHECK ((row_count >= 0))
);
CREATE TABLE {analytics}.scheduled_sync_runs (
    job text NOT NULL,
    slot timestamp with time zone NOT NULL,
    status text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    attempts integer DEFAULT 1 NOT NULL,
    error_code text,
    next_retry_at timestamp with time zone,
    CONSTRAINT scheduled_sync_runs_status_check CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'failed'::text])))
);
CREATE TABLE {analytics}.scheduler_runtime (
    singleton boolean DEFAULT true NOT NULL,
    instance_id uuid NOT NULL,
    heartbeat_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    available boolean DEFAULT true NOT NULL,
    CONSTRAINT scheduler_runtime_singleton_check CHECK (singleton)
);
CREATE TABLE {analytics}.sources (
    id text NOT NULL,
    label text NOT NULL,
    base_url text NOT NULL,
    fingerprint text NOT NULL,
    server_type text,
    verified_at timestamp with time zone,
    configured boolean DEFAULT true NOT NULL,
    CONSTRAINT sources_fingerprint_check CHECK ((fingerprint ~ '^[0-9a-f]{{64}}$'::text)),
    CONSTRAINT sources_id_check CHECK ((id ~ '^[a-z][a-z0-9-]{{0,49}}$'::text)),
    CONSTRAINT sources_server_type_check CHECK ((server_type = ANY (ARRAY['CHAIN'::text, 'REPLICATED_RMS'::text, 'STANDALONE_RMS'::text])))
);
CREATE TABLE {analytics}.store_balance_items (
    snapshot_id uuid NOT NULL,
    line_num integer NOT NULL,
    store_id uuid NOT NULL,
    product_id uuid NOT NULL,
    amount numeric NOT NULL,
    sum numeric NOT NULL,
    CONSTRAINT store_balance_items_line_num_check CHECK ((line_num > 0))
);
COMMENT ON COLUMN {analytics}.store_balance_items.line_num IS '1-based row position in the original array; preserves repeated store/product pairs.';
CREATE TABLE {analytics}.store_balance_reports (
    source_id text NOT NULL,
    accounting_timestamp timestamp without time zone NOT NULL,
    last_snapshot_id uuid NOT NULL,
    row_count integer NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    CONSTRAINT store_balance_reports_accounting_timestamp_check CHECK ((accounting_timestamp = date_trunc('second'::text, accounting_timestamp))),
    CONSTRAINT store_balance_reports_row_count_check CHECK ((row_count >= 0))
);
COMMENT ON TABLE {analytics}.store_balance_reports IS 'Latest complete unfiltered report per source and accounting timestamp. Join items by last_snapshot_id.';
COMMENT ON COLUMN {analytics}.store_balance_reports.accounting_timestamp IS 'Accounting time sent to iiko verbatim, without UTC conversion.';
CREATE TABLE {analytics}.stores (
    source_id text NOT NULL,
    id uuid NOT NULL,
    parent_id uuid,
    code text,
    name text,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    present_in_latest boolean DEFAULT true NOT NULL,
    last_snapshot_id uuid NOT NULL
);
CREATE TABLE {analytics}.sync_runs (
    id uuid NOT NULL,
    job text DEFAULT 'references'::text NOT NULL,
    status text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    counts jsonb DEFAULT '{{}}'::jsonb NOT NULL,
    error_code text,
    CONSTRAINT sync_runs_check CHECK (((status = 'running'::text) = (finished_at IS NULL))),
    CONSTRAINT sync_runs_job_check CHECK ((job = ANY (ARRAY['references'::text, 'inventory'::text, 'employees'::text, 'store_balances'::text, 'dictionaries'::text, 'counteragent_balances'::text, 'events'::text, 'employee_roles'::text, 'accounts'::text, 'cash_shifts'::text]))),
    CONSTRAINT sync_runs_status_check CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'failed'::text])))
);
CREATE TABLE {analytics}.web_department_access (
    user_id uuid NOT NULL,
    source_id text NOT NULL,
    department_id uuid NOT NULL
);
CREATE TABLE {analytics}.writeoff_items (
    source_id text NOT NULL,
    document_id uuid NOT NULL,
    num integer NOT NULL,
    product_id uuid NOT NULL,
    amount numeric NOT NULL,
    cost numeric,
    measure_unit_id uuid,
    amount_factor numeric,
    details jsonb NOT NULL,
    present_in_latest boolean DEFAULT true NOT NULL
);
CREATE TABLE {analytics}.writeoffs (
    source_id text NOT NULL,
    id uuid NOT NULL,
    document_number text NOT NULL,
    date_incoming timestamp without time zone NOT NULL,
    status text NOT NULL,
    store_id uuid NOT NULL,
    account_id uuid NOT NULL,
    last_export_date date NOT NULL,
    details jsonb NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    last_snapshot_id uuid NOT NULL
);
ALTER TABLE ONLY {analytics}.accounts
    ADD CONSTRAINT accounts_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.assembly_chart_items
    ADD CONSTRAINT assembly_chart_items_pkey PRIMARY KEY (source_id, chart_id, id);
ALTER TABLE ONLY {analytics}.assembly_chart_scopes
    ADD CONSTRAINT assembly_chart_scopes_pkey PRIMARY KEY (source_id, business_date, chart_id);
ALTER TABLE ONLY {analytics}.assembly_charts
    ADD CONSTRAINT assembly_charts_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.assistant_conversations
    ADD CONSTRAINT assistant_conversations_id_user_id_key UNIQUE (id, user_id);
ALTER TABLE ONLY {analytics}.assistant_conversations
    ADD CONSTRAINT assistant_conversations_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.assistant_turns
    ADD CONSTRAINT assistant_turns_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.cash_shift_days
    ADD CONSTRAINT cash_shift_days_pkey PRIMARY KEY (source_id, open_day);
ALTER TABLE ONLY {analytics}.cash_shift_observations
    ADD CONSTRAINT cash_shift_observations_pkey PRIMARY KEY (snapshot_id, id);
ALTER TABLE ONLY {analytics}.corporate_nodes
    ADD CONSTRAINT corporate_nodes_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.counteragent_balance_items
    ADD CONSTRAINT counteragent_balance_items_pkey PRIMARY KEY (snapshot_id, line_num);
ALTER TABLE ONLY {analytics}.counteragent_balance_reports
    ADD CONSTRAINT counteragent_balance_reports_pkey PRIMARY KEY (source_id, accounting_timestamp);
ALTER TABLE ONLY {analytics}.counteragents
    ADD CONSTRAINT counteragents_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.employee_changes
    ADD CONSTRAINT employee_changes_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.employee_roles
    ADD CONSTRAINT employee_roles_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.employees
    ADD CONSTRAINT employees_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.incoming_invoice_items
    ADD CONSTRAINT incoming_invoice_items_pkey PRIMARY KEY (source_id, document_id, num, num_occurrence);
ALTER TABLE ONLY {analytics}.incoming_invoices
    ADD CONSTRAINT incoming_invoices_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.indicator_filter_sync
    ADD CONSTRAINT indicator_filter_sync_pkey PRIMARY KEY (source_id, field);
ALTER TABLE ONLY {analytics}.indicator_filter_values
    ADD CONSTRAINT indicator_filter_values_pkey PRIMARY KEY (source_id, field, department_id, value);
ALTER TABLE ONLY {analytics}.internal_transfer_items
    ADD CONSTRAINT internal_transfer_items_pkey PRIMARY KEY (source_id, document_id, num);
ALTER TABLE ONLY {analytics}.internal_transfers
    ADD CONSTRAINT internal_transfers_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.manual_sync_requests
    ADD CONSTRAINT manual_sync_requests_pkey PRIMARY KEY (request_id);
ALTER TABLE ONLY {analytics}.measure_units
    ADD CONSTRAINT measure_units_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.outgoing_invoice_items
    ADD CONSTRAINT outgoing_invoice_items_pkey PRIMARY KEY (source_id, document_id, line_num);
ALTER TABLE ONLY {analytics}.outgoing_invoices
    ADD CONSTRAINT outgoing_invoices_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.portal_admin_audit
    ADD CONSTRAINT portal_admin_audit_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.portal_identity_metadata
    ADD CONSTRAINT portal_identity_metadata_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.product_categories
    ADD CONSTRAINT product_categories_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.product_groups
    ADD CONSTRAINT product_groups_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.products
    ADD CONSTRAINT products_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.purchase_impact_prepared
    ADD CONSTRAINT purchase_impact_prepared_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.purchase_impact_prepared_products
    ADD CONSTRAINT purchase_impact_prepared_products_pkey PRIMARY KEY (generation_id, product_id);
ALTER TABLE ONLY {analytics}.purchase_impact_prepared
    ADD CONSTRAINT purchase_impact_prepared_source_id_key UNIQUE (source_id);
ALTER TABLE ONLY {analytics}.purchase_prices_prepared
    ADD CONSTRAINT purchase_prices_prepared_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.purchase_prices_prepared
    ADD CONSTRAINT purchase_prices_prepared_source_id_key UNIQUE (source_id);
ALTER TABLE ONLY {analytics}.raw_snapshots
    ADD CONSTRAINT raw_snapshots_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.rms_bindings
    ADD CONSTRAINT rms_bindings_pkey PRIMARY KEY (source_id);
ALTER TABLE ONLY {analytics}.rms_event_days
    ADD CONSTRAINT rms_event_days_pkey PRIMARY KEY (source_id, event_date);
ALTER TABLE ONLY {analytics}.rms_event_links
    ADD CONSTRAINT rms_event_links_pkey PRIMARY KEY (source_id, event_id);
ALTER TABLE ONLY {analytics}.rms_event_observations
    ADD CONSTRAINT rms_event_observations_pkey PRIMARY KEY (snapshot_id, event_id);
ALTER TABLE ONLY {analytics}.rms_event_types
    ADD CONSTRAINT rms_event_types_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.rms_event_versions
    ADD CONSTRAINT rms_event_versions_pkey PRIMARY KEY (source_id, event_id, version_no);
ALTER TABLE ONLY {analytics}.rms_event_versions
    ADD CONSTRAINT rms_event_versions_source_id_event_id_version_id_key UNIQUE (source_id, event_id, version_id);
ALTER TABLE ONLY {analytics}.rms_event_versions
    ADD CONSTRAINT rms_event_versions_version_id_key UNIQUE (version_id);
ALTER TABLE ONLY {analytics}.rms_events
    ADD CONSTRAINT rms_events_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.sales_drilldown_captures
    ADD CONSTRAINT sales_drilldown_captures_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.sales_drilldown_captures
    ADD CONSTRAINT sales_drilldown_captures_report_id_ordinal_order_id_key UNIQUE NULLS NOT DISTINCT (report_id, ordinal, order_id);
ALTER TABLE ONLY {analytics}.sales_report_captures
    ADD CONSTRAINT sales_report_captures_id_kind_sha256_key UNIQUE (id, kind, sha256);
ALTER TABLE ONLY {analytics}.sales_report_captures
    ADD CONSTRAINT sales_report_captures_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.sales_report_days
    ADD CONSTRAINT sales_report_days_pkey PRIMARY KEY (source_id, business_date);
ALTER TABLE ONLY {analytics}.sales_report_rows
    ADD CONSTRAINT sales_report_rows_pkey PRIMARY KEY (report_id, ordinal);
ALTER TABLE ONLY {analytics}.sales_report_sets
    ADD CONSTRAINT sales_report_sets_id_source_id_business_date_key UNIQUE (id, source_id, business_date);
ALTER TABLE ONLY {analytics}.sales_report_sets
    ADD CONSTRAINT sales_report_sets_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.sales_reports
    ADD CONSTRAINT sales_reports_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.sales_reports
    ADD CONSTRAINT sales_reports_set_id_kind_key UNIQUE (set_id, kind);
ALTER TABLE ONLY {analytics}.scheduled_sync_runs
    ADD CONSTRAINT scheduled_sync_runs_pkey PRIMARY KEY (job, slot);
ALTER TABLE ONLY {analytics}.scheduler_runtime
    ADD CONSTRAINT scheduler_runtime_pkey PRIMARY KEY (singleton);
ALTER TABLE ONLY {analytics}.sources
    ADD CONSTRAINT sources_base_url_key UNIQUE (base_url);
ALTER TABLE ONLY {analytics}.sources
    ADD CONSTRAINT sources_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.store_balance_items
    ADD CONSTRAINT store_balance_items_pkey PRIMARY KEY (snapshot_id, line_num);
ALTER TABLE ONLY {analytics}.store_balance_reports
    ADD CONSTRAINT store_balance_reports_pkey PRIMARY KEY (source_id, accounting_timestamp);
ALTER TABLE ONLY {analytics}.stores
    ADD CONSTRAINT stores_pkey PRIMARY KEY (source_id, id);
ALTER TABLE ONLY {analytics}.sync_runs
    ADD CONSTRAINT sync_runs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.web_department_access
    ADD CONSTRAINT web_department_access_pkey PRIMARY KEY (user_id, source_id, department_id);
ALTER TABLE ONLY {analytics}.web_users
    ADD CONSTRAINT web_users_pkey PRIMARY KEY (id);
ALTER TABLE ONLY {analytics}.web_warehouse_access
    ADD CONSTRAINT web_warehouse_access_pkey PRIMARY KEY (user_id, source_id, store_id);
ALTER TABLE ONLY {analytics}.writeoff_items
    ADD CONSTRAINT writeoff_items_pkey PRIMARY KEY (source_id, document_id, num);
ALTER TABLE ONLY {analytics}.writeoffs
    ADD CONSTRAINT writeoffs_pkey PRIMARY KEY (source_id, id);
CREATE INDEX accounts_corporate_idx ON {analytics}.accounts USING btree (source_id, parent_corporate_id);
CREATE INDEX accounts_parent_idx ON {analytics}.accounts USING btree (source_id, account_parent_id);
CREATE INDEX accounts_snapshot_idx ON {analytics}.accounts USING btree (last_snapshot_id);
CREATE INDEX assembly_charts_product_idx ON {analytics}.assembly_charts USING btree (source_id, product_id, date_from);
CREATE INDEX assembly_charts_snapshot_idx ON {analytics}.assembly_charts USING btree (last_snapshot_id);
CREATE INDEX assembly_items_product_idx ON {analytics}.assembly_chart_items USING btree (source_id, product_id);
CREATE INDEX assembly_scopes_chart_idx ON {analytics}.assembly_chart_scopes USING btree (source_id, chart_id);
CREATE INDEX assembly_scopes_snapshot_idx ON {analytics}.assembly_chart_scopes USING btree (last_snapshot_id);
CREATE INDEX assistant_conversations_user_idx ON {analytics}.assistant_conversations USING btree (user_id, created_at DESC);
CREATE INDEX assistant_turns_conversation_idx ON {analytics}.assistant_turns USING btree (conversation_id, created_at);
CREATE INDEX assistant_turns_user_idx ON {analytics}.assistant_turns USING btree (user_id, created_at DESC);
CREATE INDEX cash_shift_day_snapshot_idx ON {analytics}.cash_shift_days USING btree (last_snapshot_id);
CREATE INDEX cash_shift_department_idx ON {analytics}.cash_shift_observations USING btree (department_id, open_date);
CREATE INDEX cash_shift_group_snapshot_idx ON {analytics}.cash_shift_observations USING btree (groups_snapshot_id);
CREATE INDEX corporate_nodes_parent_idx ON {analytics}.corporate_nodes USING btree (source_id, parent_id);
CREATE INDEX corporate_nodes_snapshot_idx ON {analytics}.corporate_nodes USING btree (last_snapshot_id);
CREATE INDEX counteragent_balance_items_dimensions_idx ON {analytics}.counteragent_balance_items USING btree (account_id, counteragent_id, department_id);
CREATE INDEX counteragent_balance_reports_snapshot_idx ON {analytics}.counteragent_balance_reports USING btree (last_snapshot_id);
CREATE INDEX counteragents_code_idx ON {analytics}.counteragents USING btree (source_id, code);
CREATE INDEX counteragents_snapshot_idx ON {analytics}.counteragents USING btree (last_snapshot_id);
CREATE INDEX counteragents_store_idx ON {analytics}.counteragents USING btree (source_id, represented_store_id);
CREATE UNIQUE INDEX employee_changes_pending_idx ON {analytics}.employee_changes USING btree (employee_id) WHERE (status = 'pending'::text);
CREATE INDEX employee_roles_code_idx ON {analytics}.employee_roles USING btree (source_id, code);
CREATE INDEX employee_roles_snapshot_idx ON {analytics}.employee_roles USING btree (last_snapshot_id);
CREATE INDEX employees_code_idx ON {analytics}.employees USING btree (source_id, code);
CREATE INDEX employees_main_role_idx ON {analytics}.employees USING btree (source_id, main_role_id);
CREATE INDEX employees_snapshot_idx ON {analytics}.employees USING btree (last_snapshot_id);
CREATE INDEX incoming_invoices_export_idx ON {analytics}.incoming_invoices USING btree (source_id, last_export_date);
CREATE INDEX incoming_invoices_snapshot_idx ON {analytics}.incoming_invoices USING btree (last_snapshot_id);
CREATE INDEX incoming_items_product_idx ON {analytics}.incoming_invoice_items USING btree (source_id, product_id);
CREATE INDEX indicator_filters_department ON {analytics}.indicator_filter_values USING btree (department_id, field);
CREATE INDEX internal_transfer_items_product_idx ON {analytics}.internal_transfer_items USING btree (source_id, product_id);
CREATE INDEX internal_transfers_export_idx ON {analytics}.internal_transfers USING btree (source_id, last_export_date);
CREATE INDEX internal_transfers_from_idx ON {analytics}.internal_transfers USING btree (source_id, store_from_id, date_incoming);
CREATE INDEX internal_transfers_snapshot_idx ON {analytics}.internal_transfers USING btree (last_snapshot_id);
CREATE INDEX internal_transfers_to_idx ON {analytics}.internal_transfers USING btree (source_id, store_to_id, date_incoming);
CREATE UNIQUE INDEX manual_sync_active ON {analytics}.manual_sync_requests USING btree (job) WHERE (state = ANY (ARRAY['pending'::text, 'running'::text]));
CREATE INDEX manual_sync_latest ON {analytics}.manual_sync_requests USING btree (job, requested_at DESC);
CREATE INDEX measure_units_snapshot_idx ON {analytics}.measure_units USING btree (last_snapshot_id);
CREATE INDEX outgoing_invoices_export_idx ON {analytics}.outgoing_invoices USING btree (source_id, last_export_date);
CREATE INDEX outgoing_invoices_link_idx ON {analytics}.outgoing_invoices USING btree (source_id, linked_incoming_invoice_id);
CREATE INDEX outgoing_invoices_snapshot_idx ON {analytics}.outgoing_invoices USING btree (last_snapshot_id);
CREATE INDEX outgoing_items_product_idx ON {analytics}.outgoing_invoice_items USING btree (source_id, product_id);
CREATE INDEX product_categories_snapshot_idx ON {analytics}.product_categories USING btree (last_snapshot_id);
CREATE INDEX product_groups_parent_idx ON {analytics}.product_groups USING btree (source_id, parent_id);
CREATE INDEX product_groups_snapshot_idx ON {analytics}.product_groups USING btree (last_snapshot_id);
CREATE INDEX products_group_idx ON {analytics}.products USING btree (source_id, group_id);
CREATE INDEX products_snapshot_idx ON {analytics}.products USING btree (last_snapshot_id);
CREATE INDEX purchase_prices_receipts_generation_idx ON {analytics}.purchase_prices_prepared_receipts USING btree (generation_id);
CREATE INDEX raw_snapshots_run_idx ON {analytics}.raw_snapshots USING btree (run_id);
CREATE INDEX raw_snapshots_source_resource_time_idx ON {analytics}.raw_snapshots USING btree (source_id, resource, observed_at DESC);
CREATE INDEX rms_bindings_chain_snapshot_idx ON {analytics}.rms_bindings USING btree (chain_snapshot_id);
CREATE INDEX rms_bindings_department_idx ON {analytics}.rms_bindings USING btree (chain_source_id, department_id);
CREATE INDEX rms_bindings_groups_snapshot_idx ON {analytics}.rms_bindings USING btree (groups_snapshot_id);
CREATE UNIQUE INDEX rms_bindings_matched_department_idx ON {analytics}.rms_bindings USING btree (chain_source_id, department_id) WHERE (state = 'matched'::text);
CREATE INDEX rms_bindings_rms_snapshot_idx ON {analytics}.rms_bindings USING btree (rms_snapshot_id);
CREATE INDEX rms_event_days_snapshot_idx ON {analytics}.rms_event_days USING btree (last_snapshot_id);
CREATE INDEX rms_event_links_paired_idx ON {analytics}.rms_event_links USING btree (source_id, paired_event_id);
CREATE INDEX rms_event_observations_version_idx ON {analytics}.rms_event_observations USING btree (version_id);
CREATE INDEX rms_event_types_snapshot_idx ON {analytics}.rms_event_types USING btree (last_snapshot_id);
CREATE INDEX rms_event_versions_snapshot_idx ON {analytics}.rms_event_versions USING btree (snapshot_id);
CREATE INDEX rms_events_number_idx ON {analytics}.rms_events USING btree (source_id, order_number, occurred_at);
CREATE INDEX rms_events_order_idx ON {analytics}.rms_events USING btree (source_id, order_id, occurred_at, id);
CREATE INDEX rms_events_transfer_idx ON {analytics}.rms_events USING btree (source_id, occurred_at) WHERE (event_type = ANY (ARRAY['dishesMovedFrom'::text, 'dishesMovedTo'::text]));
CREATE INDEX rms_events_version_idx ON {analytics}.rms_events USING btree (version_id);
CREATE INDEX sales_reports_set_idx ON {analytics}.sales_reports USING btree (set_id);
CREATE INDEX sales_rows_department_idx ON {analytics}.sales_report_rows USING btree (department_id, report_id);
CREATE INDEX sales_sets_date_idx ON {analytics}.sales_report_sets USING btree (source_id, business_date);
CREATE INDEX store_balance_items_store_product_idx ON {analytics}.store_balance_items USING btree (store_id, product_id);
CREATE INDEX store_balance_reports_snapshot_idx ON {analytics}.store_balance_reports USING btree (last_snapshot_id);
CREATE INDEX stores_parent_idx ON {analytics}.stores USING btree (source_id, parent_id);
CREATE INDEX stores_snapshot_idx ON {analytics}.stores USING btree (last_snapshot_id);
CREATE INDEX writeoff_items_product_idx ON {analytics}.writeoff_items USING btree (source_id, product_id);
CREATE INDEX writeoffs_date_idx ON {analytics}.writeoffs USING btree (source_id, date_incoming);
CREATE INDEX writeoffs_snapshot_idx ON {analytics}.writeoffs USING btree (last_snapshot_id);
ALTER TABLE ONLY {analytics}.accounts
    ADD CONSTRAINT accounts_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.accounts
    ADD CONSTRAINT accounts_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.assembly_chart_items
    ADD CONSTRAINT assembly_chart_items_source_id_chart_id_fkey FOREIGN KEY (source_id, chart_id) REFERENCES {analytics}.assembly_charts(source_id, id);
ALTER TABLE ONLY {analytics}.assembly_chart_scopes
    ADD CONSTRAINT assembly_chart_scopes_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.assembly_chart_scopes
    ADD CONSTRAINT assembly_chart_scopes_source_id_chart_id_fkey FOREIGN KEY (source_id, chart_id) REFERENCES {analytics}.assembly_charts(source_id, id);
ALTER TABLE ONLY {analytics}.assembly_charts
    ADD CONSTRAINT assembly_charts_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.assembly_charts
    ADD CONSTRAINT assembly_charts_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.assistant_conversations
    ADD CONSTRAINT assistant_conversations_user_id_fkey FOREIGN KEY (user_id) REFERENCES {analytics}.web_users(id) ON DELETE CASCADE;
ALTER TABLE ONLY {analytics}.assistant_turns
    ADD CONSTRAINT assistant_turns_conversation_id_user_id_fkey FOREIGN KEY (conversation_id, user_id) REFERENCES {analytics}.assistant_conversations(id, user_id) ON DELETE CASCADE;
ALTER TABLE ONLY {analytics}.cash_shift_days
    ADD CONSTRAINT cash_shift_days_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.cash_shift_days
    ADD CONSTRAINT cash_shift_days_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.cash_shift_observations
    ADD CONSTRAINT cash_shift_observations_groups_snapshot_id_fkey FOREIGN KEY (groups_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.cash_shift_observations
    ADD CONSTRAINT cash_shift_observations_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.corporate_nodes
    ADD CONSTRAINT corporate_nodes_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.corporate_nodes
    ADD CONSTRAINT corporate_nodes_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.corporate_nodes
    ADD CONSTRAINT corporate_nodes_source_id_parent_id_fkey FOREIGN KEY (source_id, parent_id) REFERENCES {analytics}.corporate_nodes(source_id, id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE ONLY {analytics}.counteragent_balance_items
    ADD CONSTRAINT counteragent_balance_items_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.counteragent_balance_reports
    ADD CONSTRAINT counteragent_balance_reports_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.counteragent_balance_reports
    ADD CONSTRAINT counteragent_balance_reports_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.counteragents
    ADD CONSTRAINT counteragents_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.counteragents
    ADD CONSTRAINT counteragents_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.employee_changes
    ADD CONSTRAINT employee_changes_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.employee_changes
    ADD CONSTRAINT employee_changes_user_id_fkey FOREIGN KEY (user_id) REFERENCES {analytics}.web_users(id);
ALTER TABLE ONLY {analytics}.employee_roles
    ADD CONSTRAINT employee_roles_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.employee_roles
    ADD CONSTRAINT employee_roles_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.employees
    ADD CONSTRAINT employees_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.employees
    ADD CONSTRAINT employees_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.incoming_invoice_items
    ADD CONSTRAINT incoming_invoice_items_source_id_document_id_fkey FOREIGN KEY (source_id, document_id) REFERENCES {analytics}.incoming_invoices(source_id, id);
ALTER TABLE ONLY {analytics}.incoming_invoices
    ADD CONSTRAINT incoming_invoices_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.incoming_invoices
    ADD CONSTRAINT incoming_invoices_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.internal_transfer_items
    ADD CONSTRAINT internal_transfer_items_source_id_document_id_fkey FOREIGN KEY (source_id, document_id) REFERENCES {analytics}.internal_transfers(source_id, id);
ALTER TABLE ONLY {analytics}.internal_transfers
    ADD CONSTRAINT internal_transfers_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.internal_transfers
    ADD CONSTRAINT internal_transfers_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.measure_units
    ADD CONSTRAINT measure_units_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.measure_units
    ADD CONSTRAINT measure_units_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.outgoing_invoice_items
    ADD CONSTRAINT outgoing_invoice_items_source_id_document_id_fkey FOREIGN KEY (source_id, document_id) REFERENCES {analytics}.outgoing_invoices(source_id, id);
ALTER TABLE ONLY {analytics}.outgoing_invoices
    ADD CONSTRAINT outgoing_invoices_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.outgoing_invoices
    ADD CONSTRAINT outgoing_invoices_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.portal_identity_metadata
    ADD CONSTRAINT portal_identity_metadata_id_fkey FOREIGN KEY (id) REFERENCES {analytics}.web_users(id) ON DELETE CASCADE;
ALTER TABLE ONLY {analytics}.product_categories
    ADD CONSTRAINT product_categories_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.product_categories
    ADD CONSTRAINT product_categories_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.product_groups
    ADD CONSTRAINT product_groups_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.product_groups
    ADD CONSTRAINT product_groups_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.products
    ADD CONSTRAINT products_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.products
    ADD CONSTRAINT products_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.purchase_impact_prepared_products
    ADD CONSTRAINT purchase_impact_prepared_products_generation_id_fkey FOREIGN KEY (generation_id) REFERENCES {analytics}.purchase_impact_prepared(id) ON DELETE CASCADE;
ALTER TABLE ONLY {analytics}.purchase_impact_prepared
    ADD CONSTRAINT purchase_impact_prepared_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.purchase_prices_prepared_receipts
    ADD CONSTRAINT purchase_prices_prepared_receipts_generation_id_fkey FOREIGN KEY (generation_id) REFERENCES {analytics}.purchase_prices_prepared(id) ON DELETE CASCADE;
ALTER TABLE ONLY {analytics}.purchase_prices_prepared
    ADD CONSTRAINT purchase_prices_prepared_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.raw_snapshots
    ADD CONSTRAINT raw_snapshots_run_id_fkey FOREIGN KEY (run_id) REFERENCES {analytics}.sync_runs(id);
ALTER TABLE ONLY {analytics}.raw_snapshots
    ADD CONSTRAINT raw_snapshots_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.rms_bindings
    ADD CONSTRAINT rms_bindings_chain_snapshot_id_fkey FOREIGN KEY (chain_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.rms_bindings
    ADD CONSTRAINT rms_bindings_chain_source_id_department_id_fkey FOREIGN KEY (chain_source_id, department_id) REFERENCES {analytics}.corporate_nodes(source_id, id);
ALTER TABLE ONLY {analytics}.rms_bindings
    ADD CONSTRAINT rms_bindings_chain_source_id_fkey FOREIGN KEY (chain_source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.rms_bindings
    ADD CONSTRAINT rms_bindings_groups_snapshot_id_fkey FOREIGN KEY (groups_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.rms_bindings
    ADD CONSTRAINT rms_bindings_rms_snapshot_id_fkey FOREIGN KEY (rms_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.rms_bindings
    ADD CONSTRAINT rms_bindings_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.rms_event_days
    ADD CONSTRAINT rms_event_days_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.rms_event_days
    ADD CONSTRAINT rms_event_days_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.rms_event_links
    ADD CONSTRAINT rms_event_links_source_id_event_id_fkey FOREIGN KEY (source_id, event_id) REFERENCES {analytics}.rms_events(source_id, id);
ALTER TABLE ONLY {analytics}.rms_event_links
    ADD CONSTRAINT rms_event_links_source_id_paired_event_id_fkey FOREIGN KEY (source_id, paired_event_id) REFERENCES {analytics}.rms_events(source_id, id);
ALTER TABLE ONLY {analytics}.rms_event_observations
    ADD CONSTRAINT rms_event_observations_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.rms_event_observations
    ADD CONSTRAINT rms_event_observations_source_id_event_id_version_id_fkey FOREIGN KEY (source_id, event_id, version_id) REFERENCES {analytics}.rms_event_versions(source_id, event_id, version_id);
ALTER TABLE ONLY {analytics}.rms_event_types
    ADD CONSTRAINT rms_event_types_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.rms_event_types
    ADD CONSTRAINT rms_event_types_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.rms_event_versions
    ADD CONSTRAINT rms_event_versions_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.rms_event_versions
    ADD CONSTRAINT rms_event_versions_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.rms_events
    ADD CONSTRAINT rms_events_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.rms_events
    ADD CONSTRAINT rms_events_source_id_id_version_id_fkey FOREIGN KEY (source_id, id, version_id) REFERENCES {analytics}.rms_event_versions(source_id, event_id, version_id);
ALTER TABLE ONLY {analytics}.sales_drilldown_captures
    ADD CONSTRAINT sales_drilldown_captures_report_id_ordinal_fkey FOREIGN KEY (report_id, ordinal) REFERENCES {analytics}.sales_report_rows(report_id, ordinal);
ALTER TABLE ONLY {analytics}.sales_report_captures
    ADD CONSTRAINT sales_report_captures_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.sales_report_days
    ADD CONSTRAINT sales_report_days_current_set_id_source_id_business_date_fkey FOREIGN KEY (current_set_id, source_id, business_date) REFERENCES {analytics}.sales_report_sets(id, source_id, business_date);
ALTER TABLE ONLY {analytics}.sales_report_rows
    ADD CONSTRAINT sales_report_rows_report_id_fkey FOREIGN KEY (report_id) REFERENCES {analytics}.sales_reports(id);
ALTER TABLE ONLY {analytics}.sales_report_sets
    ADD CONSTRAINT sales_report_sets_reviewed_by_fkey FOREIGN KEY (reviewed_by) REFERENCES {analytics}.web_users(id);
ALTER TABLE ONLY {analytics}.sales_report_sets
    ADD CONSTRAINT sales_report_sets_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.sales_reports
    ADD CONSTRAINT sales_reports_capture_fk FOREIGN KEY (source_capture_id, kind, sha256) REFERENCES {analytics}.sales_report_captures(id, kind, sha256);
ALTER TABLE ONLY {analytics}.sales_reports
    ADD CONSTRAINT sales_reports_set_id_fkey FOREIGN KEY (set_id) REFERENCES {analytics}.sales_report_sets(id);
ALTER TABLE ONLY {analytics}.store_balance_items
    ADD CONSTRAINT store_balance_items_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.store_balance_reports
    ADD CONSTRAINT store_balance_reports_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.store_balance_reports
    ADD CONSTRAINT store_balance_reports_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.stores
    ADD CONSTRAINT stores_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.stores
    ADD CONSTRAINT stores_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE ONLY {analytics}.stores
    ADD CONSTRAINT stores_source_id_parent_id_fkey FOREIGN KEY (source_id, parent_id) REFERENCES {analytics}.corporate_nodes(source_id, id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE ONLY {analytics}.web_department_access
    ADD CONSTRAINT web_department_access_source_id_department_id_fkey FOREIGN KEY (source_id, department_id) REFERENCES {analytics}.corporate_nodes(source_id, id);
ALTER TABLE ONLY {analytics}.web_department_access
    ADD CONSTRAINT web_department_access_user_id_fkey FOREIGN KEY (user_id) REFERENCES {analytics}.web_users(id) ON DELETE CASCADE;
ALTER TABLE ONLY {analytics}.web_warehouse_access
    ADD CONSTRAINT web_warehouse_access_source_id_store_id_fkey FOREIGN KEY (source_id, store_id) REFERENCES {analytics}.stores(source_id, id);
ALTER TABLE ONLY {analytics}.web_warehouse_access
    ADD CONSTRAINT web_warehouse_access_user_id_fkey FOREIGN KEY (user_id) REFERENCES {analytics}.web_users(id) ON DELETE CASCADE;
ALTER TABLE ONLY {analytics}.writeoff_items
    ADD CONSTRAINT writeoff_items_source_id_document_id_fkey FOREIGN KEY (source_id, document_id) REFERENCES {analytics}.writeoffs(source_id, id);
ALTER TABLE ONLY {analytics}.writeoffs
    ADD CONSTRAINT writeoffs_last_snapshot_id_fkey FOREIGN KEY (last_snapshot_id) REFERENCES {analytics}.raw_snapshots(id);
ALTER TABLE ONLY {analytics}.writeoffs
    ADD CONSTRAINT writeoffs_source_id_fkey FOREIGN KEY (source_id) REFERENCES {analytics}.sources(id);
ALTER TABLE {analytics}.accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.assembly_chart_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.assembly_chart_scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.assembly_charts ENABLE ROW LEVEL SECURITY;
CREATE POLICY assistant_conversation_owner ON {analytics}.assistant_conversations TO {runtime_role} USING ((user_id = (NULLIF(current_setting('{analytics}.assistant_user'::text, true), ''::text))::uuid)) WITH CHECK ((user_id = (NULLIF(current_setting('{analytics}.assistant_user'::text, true), ''::text))::uuid));
ALTER TABLE {analytics}.assistant_conversations ENABLE ROW LEVEL SECURITY;
CREATE POLICY assistant_turn_owner ON {analytics}.assistant_turns TO {runtime_role} USING ((user_id = (NULLIF(current_setting('{analytics}.assistant_user'::text, true), ''::text))::uuid)) WITH CHECK ((user_id = (NULLIF(current_setting('{analytics}.assistant_user'::text, true), ''::text))::uuid));
ALTER TABLE {analytics}.assistant_turns ENABLE ROW LEVEL SECURITY;
CREATE POLICY backend_access ON {analytics}.accounts TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.assembly_chart_items TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.assembly_chart_scopes TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.assembly_charts TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.cash_shift_days TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.corporate_nodes TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.counteragent_balance_reports TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.counteragents TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.employee_changes TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.employee_roles TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.employees TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.incoming_invoice_items TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.incoming_invoices TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.indicator_filter_sync TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.indicator_filter_values TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.internal_transfer_items TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.internal_transfers TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.manual_sync_requests TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.measure_units TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.outgoing_invoice_items TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.outgoing_invoices TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.portal_admin_audit TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.product_categories TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.product_groups TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.products TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.purchase_impact_prepared TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.purchase_impact_prepared_products TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.purchase_prices_prepared TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.purchase_prices_prepared_receipts TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.rms_bindings TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.sales_report_days TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.scheduled_sync_runs TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.scheduler_runtime TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.sources TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.store_balance_reports TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.stores TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.sync_runs TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.web_warehouse_access TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.writeoff_items TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON {analytics}.writeoffs TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_append ON {analytics}.cash_shift_observations TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_append ON {analytics}.counteragent_balance_items FOR INSERT TO {runtime_role} WITH CHECK (true);
CREATE POLICY backend_append ON {analytics}.raw_snapshots FOR INSERT TO {runtime_role} WITH CHECK (true);
CREATE POLICY backend_append ON {analytics}.rms_event_days FOR INSERT TO {runtime_role} WITH CHECK (true);
CREATE POLICY backend_append ON {analytics}.rms_event_links FOR INSERT TO {runtime_role} WITH CHECK (true);
CREATE POLICY backend_append ON {analytics}.rms_event_observations FOR INSERT TO {runtime_role} WITH CHECK (true);
CREATE POLICY backend_append ON {analytics}.rms_event_types FOR INSERT TO {runtime_role} WITH CHECK (true);
CREATE POLICY backend_append ON {analytics}.rms_event_versions FOR INSERT TO {runtime_role} WITH CHECK (true);
CREATE POLICY backend_append ON {analytics}.rms_events FOR INSERT TO {runtime_role} WITH CHECK (true);
CREATE POLICY backend_append ON {analytics}.sales_report_rows TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_append ON {analytics}.sales_report_sets TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_append ON {analytics}.sales_reports TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_append ON {analytics}.store_balance_items FOR INSERT TO {runtime_role} WITH CHECK (true);
CREATE POLICY backend_insert ON {analytics}.sales_drilldown_captures FOR INSERT TO {runtime_role} WITH CHECK (true);
CREATE POLICY backend_insert ON {analytics}.sales_report_captures FOR INSERT TO {runtime_role} WITH CHECK (true);
CREATE POLICY backend_read ON {analytics}.counteragent_balance_items FOR SELECT TO {runtime_role} USING (true);
CREATE POLICY backend_read ON {analytics}.raw_snapshots FOR SELECT TO {runtime_role} USING (true);
CREATE POLICY backend_read ON {analytics}.rms_event_days FOR SELECT TO {runtime_role} USING (true);
CREATE POLICY backend_read ON {analytics}.rms_event_links FOR SELECT TO {runtime_role} USING (true);
CREATE POLICY backend_read ON {analytics}.rms_event_observations FOR SELECT TO {runtime_role} USING (true);
CREATE POLICY backend_read ON {analytics}.rms_event_types FOR SELECT TO {runtime_role} USING (true);
CREATE POLICY backend_read ON {analytics}.rms_event_versions FOR SELECT TO {runtime_role} USING (true);
CREATE POLICY backend_read ON {analytics}.rms_events FOR SELECT TO {runtime_role} USING (true);
CREATE POLICY backend_read ON {analytics}.sales_drilldown_captures FOR SELECT TO {runtime_role} USING (true);
CREATE POLICY backend_read ON {analytics}.sales_report_captures FOR SELECT TO {runtime_role} USING (true);
CREATE POLICY backend_read ON {analytics}.store_balance_items FOR SELECT TO {runtime_role} USING (true);
CREATE POLICY backend_read ON {analytics}.web_department_access FOR SELECT TO {runtime_role} USING (true);
CREATE POLICY backend_read ON {analytics}.web_users FOR SELECT TO {runtime_role} USING (true);
CREATE POLICY backend_update ON {analytics}.rms_event_days FOR UPDATE TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_update ON {analytics}.rms_event_links FOR UPDATE TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_update ON {analytics}.rms_event_types FOR UPDATE TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_update ON {analytics}.rms_events FOR UPDATE TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_write ON {analytics}.web_department_access TO {runtime_role} USING (true) WITH CHECK (true);
CREATE POLICY backend_write ON {analytics}.web_users TO {runtime_role} USING (true) WITH CHECK (true);
ALTER TABLE {analytics}.cash_shift_days ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.cash_shift_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.corporate_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.counteragent_balance_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.counteragent_balance_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.counteragents ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.employee_changes ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.employee_roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.employees ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.incoming_invoice_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.incoming_invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.indicator_filter_sync ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.indicator_filter_values ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.internal_transfer_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.internal_transfers ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.manual_sync_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.measure_units ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.outgoing_invoice_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.outgoing_invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.portal_admin_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.product_categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.product_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.products ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.purchase_impact_prepared ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.purchase_impact_prepared_products ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.purchase_prices_prepared ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.purchase_prices_prepared_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.raw_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.rms_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.rms_event_days ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.rms_event_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.rms_event_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.rms_event_types ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.rms_event_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.rms_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.sales_drilldown_captures ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.sales_report_captures ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.sales_report_days ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.sales_report_rows ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.sales_report_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.sales_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.scheduled_sync_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.scheduler_runtime ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.store_balance_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.store_balance_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.stores ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.sync_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.web_department_access ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.web_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.web_warehouse_access ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.writeoff_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE {analytics}.writeoffs ENABLE ROW LEVEL SECURITY;
GRANT USAGE ON SCHEMA {analytics} TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.accounts TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.assembly_chart_items TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.assembly_chart_scopes TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.assembly_charts TO {runtime_role};
GRANT SELECT,INSERT ON TABLE {analytics}.assistant_conversations TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.assistant_turns TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.cash_shift_days TO {runtime_role};
GRANT SELECT,INSERT ON TABLE {analytics}.cash_shift_observations TO {runtime_role};
GRANT SELECT ON TABLE {analytics}.cash_shifts TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.corporate_nodes TO {runtime_role};
GRANT SELECT,INSERT ON TABLE {analytics}.counteragent_balance_items TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.counteragent_balance_reports TO {runtime_role};
GRANT SELECT ON TABLE {analytics}.counteragent_balances_with_accounts TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.counteragents TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.employee_changes TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.employee_roles TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.employees TO {runtime_role};
GRANT SELECT ON TABLE {analytics}.employee_role_assignments TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.incoming_invoice_items TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.incoming_invoices TO {runtime_role};
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE {analytics}.indicator_filter_sync TO {runtime_role};
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE {analytics}.indicator_filter_values TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.internal_transfer_items TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.internal_transfers TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.manual_sync_requests TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.measure_units TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.outgoing_invoice_items TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.outgoing_invoices TO {runtime_role};
GRANT SELECT,INSERT ON TABLE {analytics}.portal_admin_audit TO {runtime_role};
GRANT USAGE ON SEQUENCE {analytics}.portal_admin_audit_id_seq TO {runtime_role};
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE {analytics}.web_users TO {runtime_role};
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE {analytics}.web_warehouse_access TO {runtime_role};
GRANT SELECT ON TABLE {analytics}.portal_warehouse_access TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.product_categories TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.product_groups TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.products TO {runtime_role};
GRANT SELECT,INSERT,DELETE ON TABLE {analytics}.purchase_impact_prepared TO {runtime_role};
GRANT SELECT,INSERT,DELETE ON TABLE {analytics}.purchase_impact_prepared_products TO {runtime_role};
GRANT SELECT,INSERT,DELETE ON TABLE {analytics}.purchase_prices_prepared TO {runtime_role};
GRANT SELECT,INSERT,DELETE ON TABLE {analytics}.purchase_prices_prepared_receipts TO {runtime_role};
GRANT SELECT,INSERT ON TABLE {analytics}.raw_snapshots TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.rms_bindings TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.rms_event_days TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.rms_event_links TO {runtime_role};
GRANT SELECT,INSERT ON TABLE {analytics}.rms_event_observations TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.rms_event_types TO {runtime_role};
GRANT SELECT,INSERT ON TABLE {analytics}.rms_event_versions TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.rms_events TO {runtime_role};
GRANT SELECT,INSERT ON TABLE {analytics}.sales_drilldown_captures TO {runtime_role};
GRANT SELECT,INSERT ON TABLE {analytics}.sales_report_captures TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.sales_report_days TO {runtime_role};
GRANT SELECT,INSERT ON TABLE {analytics}.sales_report_rows TO {runtime_role};
GRANT SELECT,INSERT ON TABLE {analytics}.sales_report_sets TO {runtime_role};
GRANT SELECT,INSERT ON TABLE {analytics}.sales_reports TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.scheduled_sync_runs TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.scheduler_runtime TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.sources TO {runtime_role};
GRANT SELECT,INSERT ON TABLE {analytics}.store_balance_items TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.store_balance_reports TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.stores TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.sync_runs TO {runtime_role};
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE {analytics}.web_department_access TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.writeoff_items TO {runtime_role};
GRANT SELECT,INSERT,UPDATE ON TABLE {analytics}.writeoffs TO {runtime_role};

ALTER TABLE {analytics}.portal_identity_metadata ENABLE ROW LEVEL SECURITY;
CREATE POLICY runtime_read ON {analytics}.portal_identity_metadata FOR SELECT TO {runtime_role} USING(true);
GRANT SELECT ON {analytics}.portal_identity_metadata,{analytics}.portal_identities TO {runtime_role};
