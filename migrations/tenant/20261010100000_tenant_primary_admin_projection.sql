-- Only the company-bound central identity role may project a previously proven
-- primary administrator. The operator checks the immutable Auth/journal binding.
-- Existing local profiles are never promoted or reset by a replay.
CREATE FUNCTION {analytics}.provision_company_primary_admin(
 p_user_id uuid,p_email text,p_provision_id text,p_display_name text,p_password_required boolean
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog
AS $primary$
DECLARE existed boolean;
BEGIN
 IF p_password_required IS NULL THEN
  RAISE EXCEPTION 'Password requirement must be explicit' USING ERRCODE='22023';
 END IF;
 -- Match the existing fixed provisioner locks before checking whether this is new.
 PERFORM pg_advisory_xact_lock(hashtextextended('{analytics}:identity:'||p_user_id::text,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('{analytics}:email:'||lower(btrim(p_email)),0));
 SELECT EXISTS(SELECT 1 FROM {analytics}.portal_identity_metadata WHERE id=p_user_id) INTO existed;
 PERFORM {analytics}.provision_company_identity(p_user_id,p_email,p_provision_id,p_display_name);
 IF NOT existed THEN
  UPDATE {analytics}.web_users SET is_portal_admin=true,all_departments=true,
   warehouse_scope_mode='all',password_change_required=p_password_required,
   password_changed_at=CASE WHEN p_password_required THEN NULL ELSE now() END,
   sections=ARRAY['overview','indicators','sales','deposits','cash-shifts','invoices',
   'purchase-prices','outgoing','transfers','writeoffs','products','charts','balances',
   'employees','events','status']::text[]
   WHERE id=p_user_id;
 END IF;
 RETURN p_user_id;
END;
$primary$;
REVOKE ALL ON FUNCTION {analytics}.provision_company_primary_admin(uuid,text,text,text,boolean)
 FROM PUBLIC,{runtime_role};
GRANT EXECUTE ON FUNCTION {analytics}.provision_company_primary_admin(uuid,text,text,text,boolean)
 TO {identity_role};
