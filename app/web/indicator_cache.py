"""Bounded, tenant-schema-local last-good KPI cache and cross-process leases."""

import json
from contextlib import contextmanager
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

CACHE_LOCK = 7623011091063
MAX_ENTRIES = 256


class PostgresIndicatorCache:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def connection(self):
        with psycopg.connect(
            self.settings.database_url.get_secret_value(),
            connect_timeout=3,
            application_name="chaika-indicator-cache",
            row_factory=dict_row,
        ) as db:
            db.execute("SET LOCAL statement_timeout='3000ms'")
            db.execute("SET LOCAL lock_timeout='1000ms'")
            yield db

    def register(self, key, spec, user_id, selection):
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (CACHE_LOCK,))
            db.execute(
                "DELETE FROM chaika.indicator_cache WHERE expires_at < now() "
                "AND (lease_until IS NULL OR lease_until < now())"
            )
            existing = db.execute(
                "SELECT key FROM chaika.indicator_cache WHERE key=%s", (key,)
            ).fetchone()
            if not existing:
                count = db.execute("SELECT count(*) AS n FROM chaika.indicator_cache").fetchone()[
                    "n"
                ]
                if count >= MAX_ENTRIES:
                    deleted = db.execute(
                        "DELETE FROM chaika.indicator_cache WHERE key=(SELECT key FROM "
                        "chaika.indicator_cache WHERE lease_until IS NULL OR lease_until<now() "
                        "ORDER BY requested_at LIMIT 1) RETURNING key"
                    ).fetchone()
                    if not deleted:
                        return False
            db.execute(
                "INSERT INTO chaika.indicator_cache(key,spec,user_id,selection) "
                "VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(key) DO UPDATE SET user_id=EXCLUDED.user_id,"
                "selection=EXCLUDED.selection,"
                "requested_at=now(),expires_at=now()+interval '24 hours'",
                (key, Jsonb(spec), user_id, selection),
            )
            return True

    def read(self, key):
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM chaika.indicator_cache WHERE key=%s", (key,)
            ).fetchone()

    def due(self):
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM chaika.indicator_cache WHERE expires_at>now() AND due_at<=now() "
                "AND (lease_until IS NULL OR lease_until<now()) ORDER BY due_at LIMIT 48"
            ).fetchall()

    def claim(self, key):
        token = uuid4()
        with self.connection() as db:
            row = db.execute(
                "UPDATE chaika.indicator_cache SET lease_token=%s,"
                "lease_until=now()+interval '15 minutes' "
                "WHERE key=%s AND expires_at>now() AND due_at<=now() "
                "AND (lease_until IS NULL OR lease_until<now()) RETURNING key",
                (token, key),
            ).fetchone()
            return token if row else None

    def success(self, key, token, data):
        payload = json.loads(json.dumps(data, default=str))
        with self.connection() as db:
            db.execute(
                "UPDATE chaika.indicator_cache SET payload=%s,observed_at=%s,"
                "due_at=now()+interval '5 minutes',last_error=NULL,"
                "lease_token=NULL,lease_until=NULL "
                "WHERE key=%s AND lease_token=%s AND lease_until>now()",
                (Jsonb(payload), data["observed_at"], key, token),
            )

    def failure(self, key, token, code):
        # Never overwrite the last successful payload or its observation time.
        seconds = 5 if code == "sync_already_running" else 300
        with self.connection() as db:
            db.execute(
                "UPDATE chaika.indicator_cache SET last_error=%s,"
                "due_at=now()+%s*interval '1 second',"
                "lease_token=NULL,lease_until=NULL WHERE key=%s AND lease_token=%s",
                (code, seconds, key, token),
            )

    def discard(self, key, token):
        with self.connection() as db:
            db.execute(
                "DELETE FROM chaika.indicator_cache WHERE key=%s AND lease_token=%s", (key, token)
            )
