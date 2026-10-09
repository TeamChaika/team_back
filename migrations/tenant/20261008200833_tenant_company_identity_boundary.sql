-- Global actor UUIDs are historical attribution, not employee subject references.
ALTER TABLE {analytics}.employee_changes DROP CONSTRAINT employee_changes_user_id_fkey;
ALTER TABLE {analytics}.assistant_conversations DROP CONSTRAINT assistant_conversations_user_id_fkey;
ALTER TABLE {analytics}.sales_report_sets DROP CONSTRAINT sales_report_sets_reviewed_by_fkey;

-- Keep assistant ownership RLS, using an unquoted, company-specific GUC name.
ALTER POLICY assistant_conversation_owner ON {analytics}.assistant_conversations
    USING (user_id = NULLIF(current_setting(trim(both '"' from '{analytics}') || '.assistant_user',true),'')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting(trim(both '"' from '{analytics}') || '.assistant_user',true),'')::uuid);
ALTER POLICY assistant_turn_owner ON {analytics}.assistant_turns
    USING (user_id = NULLIF(current_setting(trim(both '"' from '{analytics}') || '.assistant_user',true),'')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting(trim(both '"' from '{analytics}') || '.assistant_user',true),'')::uuid);

-- Central account service connects as this company-specific, execution-only role.
CREATE ROLE {identity_role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT USAGE ON SCHEMA {analytics} TO {identity_role};

-- The existing schema owner owns the routines. FORCE RLS remains enabled; these
-- policies let that owner execute the fixed provisioning statements without
-- granting identity callers any table privilege or introducing a BYPASSRLS role.
CREATE POLICY identity_owner ON {analytics}.web_users TO {migration_owner_role}
    USING(true) WITH CHECK(true);
CREATE POLICY identity_owner ON {analytics}.portal_identity_metadata TO {migration_owner_role}
    USING(true) WITH CHECK(true);
CREATE POLICY identity_owner ON {documents}.authentication_user TO {migration_owner_role}
    USING(true) WITH CHECK(true);
CREATE POLICY identity_owner ON {documents}.portal_documents_userlink TO {migration_owner_role}
    USING(true) WITH CHECK(true);

CREATE FUNCTION {analytics}.provision_company_identity(
    p_user_id uuid, p_email text, p_provision_id text, p_display_name text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog
AS $identity$
DECLARE
    normalized_email text := lower(btrim(p_email));
    existing {analytics}.portal_identity_metadata%ROWTYPE;
    employee_id bigint;
BEGIN
    IF p_user_id IS NULL OR p_user_id='00000000-0000-0000-0000-000000000000'::uuid
       OR normalized_email IS NULL OR length(normalized_email) NOT BETWEEN 3 AND 254
       OR normalized_email !~ '^[^[:space:]@]+@[^[:space:]@]+$'
       OR p_provision_id IS NULL OR length(btrim(p_provision_id)) NOT BETWEEN 1 AND 128
       OR p_display_name IS NULL OR length(p_display_name) NOT BETWEEN 1 AND 150
       OR p_display_name !~ '[^[:space:]]' THEN
        RAISE EXCEPTION 'Invalid company identity' USING ERRCODE='22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('{analytics}:identity:'||p_user_id::text,0));
    PERFORM pg_advisory_xact_lock(hashtextextended('{analytics}:email:'||normalized_email,0));
    SELECT * INTO existing FROM {analytics}.portal_identity_metadata
        WHERE id=p_user_id OR lower(email)=normalized_email ORDER BY id LIMIT 1 FOR UPDATE;
    IF FOUND THEN
        IF existing.id IS DISTINCT FROM p_user_id
           OR existing.email IS DISTINCT FROM normalized_email
           OR existing.provision_id IS DISTINCT FROM p_provision_id THEN
            RAISE EXCEPTION 'Company identity already belongs to another provision'
                USING ERRCODE='23505';
        END IF;
        -- Replays must preserve profile permissions, disabled state and local links.
        RETURN p_user_id;
    END IF;
    IF EXISTS(SELECT 1 FROM {analytics}.web_users WHERE id=p_user_id)
       OR EXISTS(SELECT 1 FROM {documents}.portal_documents_userlink WHERE supabase_id=p_user_id)
    THEN
        RAISE EXCEPTION 'Existing company profile cannot be adopted' USING ERRCODE='23505';
    END IF;
    INSERT INTO {analytics}.web_users
        (id,display_name,role,active,sections,is_portal_admin,all_departments,
         password_change_required,warehouse_scope_mode)
        VALUES(p_user_id,p_display_name,'manager',true,ARRAY[]::text[],false,false,true,'selected');
    INSERT INTO {documents}.authentication_user
        (password,is_superuser,username,first_name,last_name,email,is_staff,is_active,date_joined)
        VALUES('!',false,'member_'||replace(p_user_id::text,'-',''),p_display_name,'',
               normalized_email,false,true,now()) RETURNING id INTO employee_id;
    INSERT INTO {documents}.portal_documents_userlink(user_id,supabase_id,revision)
        VALUES(employee_id,p_user_id,0);
    INSERT INTO {analytics}.portal_identity_metadata(id,email,provision_id)
        VALUES(p_user_id,normalized_email,p_provision_id);
    RETURN p_user_id;
END;
$identity$;

CREATE FUNCTION {analytics}.set_company_identity_password_required(p_user_id uuid,p_required boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog
AS $identity$
BEGIN
    -- Only verified successful self-password changes may call this central API.
    IF p_required IS DISTINCT FROM false THEN
        RAISE EXCEPTION 'Only successful password completion is supported' USING ERRCODE='22023';
    END IF;
    UPDATE {analytics}.web_users SET password_change_required=false,password_changed_at=now()
        WHERE id=p_user_id AND active
          AND EXISTS(SELECT 1 FROM {analytics}.portal_identity_metadata WHERE id=p_user_id);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Company identity unavailable' USING ERRCODE='22023';
    END IF;
END;
$identity$;

CREATE FUNCTION {analytics}.company_identity_actor_is_admin(p_user_id uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog
AS $identity$
    SELECT EXISTS(SELECT 1 FROM {analytics}.web_users
        WHERE id=p_user_id AND active AND is_portal_admin);
$identity$;

REVOKE ALL ON FUNCTION {analytics}.provision_company_identity(uuid,text,text,text),
    {analytics}.set_company_identity_password_required(uuid,boolean),
    {analytics}.company_identity_actor_is_admin(uuid) FROM PUBLIC,{runtime_role};
GRANT EXECUTE ON FUNCTION {analytics}.provision_company_identity(uuid,text,text,text),
    {analytics}.set_company_identity_password_required(uuid,boolean),
    {analytics}.company_identity_actor_is_admin(uuid) TO {identity_role};
