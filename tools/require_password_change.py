"""Privileged, count-only classification of accounts sharing a temporary password.

Never import this tool into the web runtime. No Auth hashes leave PostgreSQL.
"""

import argparse
import getpass
import json
import os
import sys

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

# Reject malformed/expensive bcrypt salts before crypt(). Normalise compatible
# fixed bcrypt variants to pgcrypto's canonical $2a$ spelling.
BCRYPT = r"^\$2[aby]\$(0[4-9]|1[0-6])\$[./A-Za-z0-9]{53}$"


class BackfillError(Exception):
    """An intentionally secret-free error safe to display to the operator."""


def validate_target(dsn, allowed_targets):
    """Require a pinned, single network destination, without libpq redirects."""
    info = conninfo_to_dict(dsn)
    forbidden = {"service", "servicefile", "hostaddr", "options", "passfile"}
    if forbidden.intersection(info) or any(
        os.environ.get(k)
        for k in (
            "PGSERVICE",
            "PGSERVICEFILE",
            "PGHOSTADDR",
            "PGOPTIONS",
        )
    ):
        raise BackfillError("Indirect connection configuration is not allowed")
    host, port, database = (info.get(k, "") for k in ("host", "port", "dbname"))
    if not all((host, port, database)) or "," in host or "," in port or "/" in host:
        raise BackfillError("An explicit single host, port and database are required")
    if f"{host}:{port}/{database}" not in allowed_targets:
        raise BackfillError("Database target is not in the protected allowlist")


def classify(db, password, crypt_schema):
    query = sql.SQL("""
        WITH classified AS MATERIALIZED (
            SELECT w.password_change_required, a.id IS NULL AS missing_auth,
                COALESCE(a.encrypted_password ~ %(bcrypt)s, false) AS supported,
                CASE WHEN a.encrypted_password ~ %(bcrypt)s THEN
                    {crypt}(%(password)s,
                        '$2a$' || substring(a.encrypted_password FROM 5)) =
                        '$2a$' || substring(a.encrypted_password FROM 5)
                ELSE false END AS matches
            FROM chaika.web_users w LEFT JOIN auth.users a ON a.id = w.id
        )
        SELECT count(*) AS total,
            count(*) FILTER (WHERE missing_auth) AS missing_auth,
            count(*) FILTER (WHERE NOT missing_auth AND NOT supported) AS unsupported,
            count(*) FILTER (WHERE matches) AS matches,
            count(*) FILTER (WHERE matches AND NOT password_change_required) AS to_flag,
            count(*) FILTER (WHERE matches AND password_change_required) AS already_flagged
        FROM classified
    """).format(crypt=sql.Identifier(crypt_schema, "crypt"))
    return dict(db.execute(query, {"bcrypt": BCRYPT, "password": password}).fetchone())


def run_backfill(dsn, password, *, allowed_targets, apply=False, expect_matches=None):
    """Apply under Auth+profile row locks; re-evaluate current password, never old IDs."""
    if not password or "\x00" in password or len(password.encode("utf-8")) > 72:
        raise BackfillError("Temporary password must contain 1 to 72 UTF-8 bytes")
    validate_target(dsn, allowed_targets)
    if apply and (expect_matches is None or expect_matches < 0):
        raise BackfillError("Apply requires --expect-matches from a reviewed dry-run")
    with psycopg.connect(dsn, connect_timeout=10, row_factory=dict_row) as db:
        db.execute("SET LOCAL statement_timeout='120s'")
        db.execute("SET LOCAL lock_timeout='10s'")
        # A privileged operator must also check proxy/audit logs before use.
        # Suppress ordinary PostgreSQL statement/parameter logging for this session.
        db.execute("SET LOCAL log_statement='none'")
        db.execute("SET LOCAL log_min_error_statement='panic'")
        db.execute("SET LOCAL log_parameter_max_length=0")
        db.execute("SET LOCAL log_parameter_max_length_on_error=0")
        if not apply:
            db.execute("SET TRANSACTION READ ONLY")
        extension = db.execute("""
            SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
            WHERE e.extname='pgcrypto'
        """).fetchone()
        if not extension:
            raise BackfillError("pgcrypto must be installed by the database operator")
        if apply:
            # Auth update cannot pass this point until our flag commit. If Auth
            # updated first, these locks wait and classification sees its new hash.
            db.execute("""
                SELECT a.id FROM auth.users a JOIN chaika.web_users w ON w.id=a.id
                ORDER BY a.id FOR UPDATE OF a, w
            """).fetchall()
        counts = classify(db, password, extension["nspname"])
        if apply:
            if counts["matches"] != expect_matches:
                raise BackfillError("Match count changed; repeat and review the dry-run")
            query = sql.SQL("""
                UPDATE chaika.web_users w SET password_change_required=true
                FROM auth.users a
                WHERE a.id=w.id AND NOT w.password_change_required
                    AND CASE WHEN a.encrypted_password ~ %(bcrypt)s THEN
                        {crypt}(%(password)s,
                            '$2a$' || substring(a.encrypted_password FROM 5)) =
                            '$2a$' || substring(a.encrypted_password FROM 5)
                    ELSE false END
            """).format(crypt=sql.Identifier(extension["nspname"], "crypt"))
            counts["updated"] = db.execute(query, {"bcrypt": BCRYPT, "password": password}).rowcount
            if counts["updated"] != counts["to_flag"]:
                raise BackfillError("Accounts changed during apply; transaction rolled back")
        else:
            counts["updated"] = 0
    return {"mode": "apply" if apply else "read_only", **counts}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-matches", type=int)
    parser.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        allowed = json.loads(os.environ.get("CHAIKA_PASSWORD_BACKFILL_ALLOWED_TARGETS", "[]"))
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise BackfillError("Allowlist must be a JSON array of host:port/database strings")
        if not allowed:
            raise BackfillError("Protected database allowlist is required")
        dsn = os.environ.get("CHAIKA_PASSWORD_BACKFILL_DSN") or getpass.getpass("Privileged DSN: ")
        if args.password_stdin:
            password = sys.stdin.readline().removesuffix("\n").removesuffix("\r")
        else:
            password = os.environ.get("CHAIKA_TEMPORARY_PASSWORD") or getpass.getpass(
                "Known temporary password: "
            )
        result = run_backfill(
            dsn,
            password,
            allowed_targets=allowed,
            apply=args.apply,
            expect_matches=args.expect_matches,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except BackfillError as exc:
        print(f"Backfill aborted: {exc}", file=sys.stderr)
    except (Exception, KeyboardInterrupt):
        # PostgreSQL errors can contain statement parameters, connection secrets,
        # or hashes in context. Never stringify, chain or print these exceptions.
        print(
            "Backfill aborted; no successful commit confirmed. Check "
            "configuration and retry dry-run.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
