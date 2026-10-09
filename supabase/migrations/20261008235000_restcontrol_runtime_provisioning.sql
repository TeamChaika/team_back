-- Private control plane state. No tenant runtime role receives access.
CREATE TABLE restcontrol.runtime_provisioning (
 company_id uuid PRIMARY KEY REFERENCES restcontrol.companies(id),
 configuration_version bigint NOT NULL CHECK(configuration_version > 0),
 socket_path text NOT NULL,
 active_socket_path text,
 active_version bigint,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','failed','ready')),
 step text,
 checks jsonb NOT NULL DEFAULT '{}'::jsonb,
 attempts bigint NOT NULL DEFAULT 0,
 error_code text,
 updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE restcontrol.runtime_provisioning ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON restcontrol.runtime_provisioning FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE,DELETE ON restcontrol.runtime_provisioning TO restcontrol_backend;
CREATE POLICY runtime_control ON restcontrol.runtime_provisioning FOR ALL TO restcontrol_backend USING(true) WITH CHECK(true);
