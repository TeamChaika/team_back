-- Recovery proof remains private to this company's execution-only identity channel.
CREATE POLICY identity_owner ON {documents}.native_password_recovery TO {migration_owner_role}
    USING(true) WITH CHECK(true);

CREATE FUNCTION {analytics}.lock_company_password_recovery(p_hash text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog
AS $recovery$
DECLARE
    proof {documents}.native_password_recovery%ROWTYPE;
    binding {documents}.portal_documents_userlink%ROWTYPE;
    employee {documents}.authentication_user%ROWTYPE;
    link_lock text;
BEGIN
    SELECT * INTO proof FROM {documents}.native_password_recovery WHERE token_hash=p_hash;
    IF NOT FOUND OR proof.expires_at<=clock_timestamp() THEN RETURN NULL; END IF;
    link_lock := regexp_replace(trim(both '"' from '{analytics}'), '_analytics$', '')
        || ':link:' || proof.portal_id::text;
    PERFORM pg_advisory_xact_lock(hashtextextended(link_lock,0));
    SELECT * INTO proof FROM {documents}.native_password_recovery WHERE token_hash=p_hash FOR UPDATE;
    IF NOT FOUND OR proof.expires_at<=clock_timestamp() THEN RETURN NULL; END IF;
    SELECT * INTO binding FROM {documents}.portal_documents_userlink
        WHERE supabase_id=proof.portal_id FOR UPDATE;
    IF NOT FOUND OR binding.user_id<>proof.user_id OR binding.revision<>proof.revision
        THEN RETURN NULL; END IF;
    SELECT * INTO employee FROM {documents}.authentication_user WHERE id=proof.user_id FOR UPDATE;
    IF NOT FOUND OR NOT employee.is_active OR employee.telegram_id IS DISTINCT FROM proof.telegram_id
        OR (SELECT count(*) FROM {documents}.authentication_user WHERE telegram_id=proof.telegram_id)<>1
        THEN RETURN NULL; END IF;
    PERFORM 1 FROM {analytics}.web_users WHERE id=proof.portal_id AND active FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN proof.portal_id;
END;
$recovery$;

CREATE FUNCTION {analytics}.consume_company_password_recovery(p_hash text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog
AS $recovery$
DECLARE
    subject uuid;
BEGIN
    subject := {analytics}.lock_company_password_recovery(p_hash);
    IF subject IS NULL THEN RETURN NULL; END IF;
    UPDATE {documents}.native_password_recovery SET claimed_at=clock_timestamp()
        WHERE token_hash=p_hash AND claimed_at IS NULL AND expires_at>clock_timestamp();
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN subject;
END;
$recovery$;
REVOKE ALL ON FUNCTION {analytics}.lock_company_password_recovery(text),
    {analytics}.consume_company_password_recovery(text) FROM PUBLIC,{runtime_role};
GRANT EXECUTE ON FUNCTION {analytics}.lock_company_password_recovery(text),
    {analytics}.consume_company_password_recovery(text) TO {identity_role};
