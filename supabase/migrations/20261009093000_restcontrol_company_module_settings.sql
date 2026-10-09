-- Encrypted private control plane settings; never granted to tenant roles.
CREATE TABLE restcontrol.company_module_settings (
 company_id uuid PRIMARY KEY REFERENCES restcontrol.companies(id),
 ciphertext text NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE restcontrol.company_module_settings ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON restcontrol.company_module_settings FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE,DELETE ON restcontrol.company_module_settings TO restcontrol_backend;
CREATE POLICY module_settings_control ON restcontrol.company_module_settings FOR ALL TO restcontrol_backend USING(true) WITH CHECK(true);
