"""Prepare a fresh isolated PostgreSQL for tests; never use a production database."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict


def main():
    url = os.environ.get("CHAIKA_TEST_DATABASE_URL", "")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 15438
        or parsed.path != "/postgres"
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit("Use the isolated test database at 127.0.0.1:15438/postgres")
    migrations = Path(__file__).resolve().parents[1] / "supabase/migrations"
    with psycopg.connect(url, connect_timeout=10) as db:
        prepare_database(db, migrations, conninfo_to_dict(url).get("password"))


def prepare_database(db, migrations, password=None):
    """Populate a disposable database; caller must enforce its environment boundary."""
    if db.execute(
        "SELECT 1 FROM pg_namespace WHERE nspname IN ('auth', 'chaika', 'support')"
    ).fetchone():
        raise SystemExit("Test database is not empty; refusing to modify it")
    db.execute("CREATE ROLE anon NOLOGIN")
    db.execute("CREATE ROLE authenticated NOLOGIN")
    # Test logins exercise FORCE RLS with separate real connections. These roles
    # carry no global/schema-owner powers. Generate SCRAM from the decoded test
    # DSN password; copying MD5 hashes between usernames would not authenticate.
    db.execute("SET LOCAL password_encryption='scram-sha-256'")
    for role in ("restcontrol_backend", "chaika_support", "other_support"):
        db.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE").format(
                sql.Identifier(role)
            )
        )
        if password is not None:
            db.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
    # Only the FK target is needed. Supabase Auth HTTP calls are mocked in tests.
    db.execute("CREATE SCHEMA auth")
    db.execute(
        "CREATE TABLE auth.users (id uuid PRIMARY KEY, email text, raw_app_meta_data "
        "jsonb DEFAULT '{}')"
    )
    for migration in sorted(migrations.glob("*.sql")):
        db.execute(migration.read_text())
        print(migration.name, flush=True)
    for role, company in (
        ("chaika_support", "primary-dashboard"),
        ("other_support", "other-company"),
    ):
        db.execute(
            "INSERT INTO support.principals(role_name,company_id) VALUES (%s,%s)",
            (role, company),
        )
        grants = (
            "GRANT USAGE ON SCHEMA support TO {}",
            "GRANT EXECUTE ON FUNCTION support.can_access(text) TO {}",
            "GRANT SELECT,INSERT ON support.events,support.tickets,support.audit,"
            "support.event_receipts,support.rate_buckets TO {}",
            "GRANT UPDATE(occurrences,last_seen,request_id) ON support.events TO {}",
            "GRANT UPDATE(n) ON support.rate_buckets TO {}",
        )
        for statement in grants:
            db.execute(sql.SQL(statement).format(sql.Identifier(role)))


if __name__ == "__main__":
    main()
