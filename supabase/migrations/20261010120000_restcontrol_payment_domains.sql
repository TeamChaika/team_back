-- Guest domains never grant tenant login/dashboard access.
CREATE TABLE restcontrol.company_payment_domains (
 company_id uuid PRIMARY KEY REFERENCES restcontrol.companies(id),
 domain text UNIQUE,
 status text NOT NULL CHECK (status IN ('unconfigured','pending','active')),
 revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
 verified_at timestamptz,
 updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK ((status='unconfigured') = (domain IS NULL)),
 CHECK (status<>'active' OR verified_at IS NOT NULL)
);
ALTER TABLE restcontrol.company_payment_domains ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON restcontrol.company_payment_domains FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE,DELETE ON restcontrol.company_payment_domains TO restcontrol_backend;
CREATE POLICY payment_domains_control ON restcontrol.company_payment_domains FOR ALL TO restcontrol_backend USING(true) WITH CHECK(true);
-- Serialize both registries to prevent later dashboard assignment stealing a guest host.
CREATE FUNCTION restcontrol.check_payment_domain_collision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('payment-domain-registry',0));
 IF TG_TABLE_NAME='companies' THEN
  IF EXISTS (SELECT 1 FROM restcontrol.company_payment_domains WHERE domain IN (NEW.domain,'api.'||NEW.domain) OR 'api.'||domain IN (NEW.domain,'api.'||NEW.domain)) THEN
   RAISE EXCEPTION 'domain already used by guest registry' USING ERRCODE='23505', CONSTRAINT='company_payment_domain_unique';
  END IF;
 ELSE
  IF EXISTS (SELECT 1 FROM restcontrol.company_payment_domains WHERE company_id<>NEW.company_id AND (domain IN (NEW.domain,'api.'||NEW.domain) OR 'api.'||domain IN (NEW.domain,'api.'||NEW.domain))) THEN
   RAISE EXCEPTION 'domain already used by guest registry' USING ERRCODE='23505', CONSTRAINT='company_payment_domain_unique';
  END IF;
  IF EXISTS (SELECT 1 FROM restcontrol.companies WHERE domain IN (NEW.domain,'api.'||NEW.domain) OR 'api.'||domain IN (NEW.domain,'api.'||NEW.domain)) THEN
   RAISE EXCEPTION 'domain already used by dashboard registry' USING ERRCODE='23505', CONSTRAINT='company_payment_domain_unique';
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER payment_domain_conflict BEFORE INSERT OR UPDATE OF domain ON restcontrol.company_payment_domains FOR EACH ROW EXECUTE FUNCTION restcontrol.check_payment_domain_collision();
CREATE TRIGGER dashboard_domain_conflict BEFORE INSERT OR UPDATE OF domain ON restcontrol.companies FOR EACH ROW EXECUTE FUNCTION restcontrol.check_payment_domain_collision();
