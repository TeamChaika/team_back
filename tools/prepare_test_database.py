"""Prepare a fresh isolated PostgreSQL for tests; never use a production database."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import psycopg


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
        if db.execute("SELECT 1 FROM pg_namespace WHERE nspname IN ('auth', 'chaika')").fetchone():
            raise SystemExit("Test database is not empty; refusing to modify it")
        db.execute("CREATE ROLE anon NOLOGIN")
        db.execute("CREATE ROLE authenticated NOLOGIN")
        # Only the FK target is needed. Supabase Auth HTTP calls are mocked in tests.
        db.execute("CREATE SCHEMA auth")
        db.execute("CREATE TABLE auth.users (id uuid PRIMARY KEY)")
        for migration in sorted(migrations.glob("*.sql")):
            db.execute(migration.read_text())
            print(migration.name, flush=True)


if __name__ == "__main__":
    main()
