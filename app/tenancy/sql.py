"""SQL templates authored by developers; values remain psycopg query parameters."""

from string import Formatter

from psycopg import sql
from psycopg.rows import dict_row

from app.tenancy.config import RuntimeConfigurationError, TenantRuntime, identifier


def render(template: str, runtime: TenantRuntime, **identifiers: str) -> sql.Composed:
    """Compose {analytics}/{documents}/{payments} and explicitly named identifiers.

    Never pass request data as the template. Runtime data belongs in execute(params),
    including JSON; literal template braces must be escaped as {{ and }}.
    """
    areas = {area: runtime.schema(area) for area in ("analytics", "documents", "payments")}
    if areas.keys() & identifiers.keys():
        raise RuntimeConfigurationError("Cannot override runtime schema identifiers")
    values = {**areas, **identifiers}
    for _, field, spec, conversion in Formatter().parse(template):
        if field is not None and (field not in values or spec or conversion):
            raise RuntimeConfigurationError(
                "SQL template contains an unknown or unsafe placeholder"
            )
    return sql.SQL(template).format(
        **{name: sql.Identifier(identifier(value)) for name, value in values.items()}
    )


def validate_database_runtime(connection, runtime: TenantRuntime) -> None:
    """Read-only startup gate on the SAME restricted connection used by this process.

    Does not SET ROLE: a privileged connection masquerading as a restricted role is
    rejected. Run before accepting traffic or starting jobs and on pool configuration.
    Provisioning uses a separate operator connection and must not call this gate.
    """
    if runtime.mode == "legacy":
        return
    own_schemas = [runtime.schema(area) for area in ("analytics", "documents", "payments")]
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("""
            SELECT current_user AS current_role, session_user AS session_role,
                   r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolbypassrls,
                   has_database_privilege(current_user,current_database(),'CREATE')
                       AS database_create,
                   EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid) AS memberships
            FROM pg_roles r WHERE r.rolname = current_user
        """)
        role = cursor.fetchone()
        if (
            not role
            or any(
                role[name]
                for name in (
                    "rolsuper",
                    "rolcreatedb",
                    "rolcreaterole",
                    "rolreplication",
                    "rolbypassrls",
                    "memberships",
                    "database_create",
                )
            )
            or role["current_role"] != runtime.database_role
            or role["session_role"] != runtime.database_role
        ):
            raise RuntimeConfigurationError(
                "Tenant database connection must use its restricted role"
            )
        cursor.execute("""
            SELECT n.nspname AS name,
                   has_schema_privilege(current_user, n.oid, 'USAGE') AS usable,
                   has_schema_privilege(current_user, n.oid, 'CREATE') AS writable
            FROM pg_namespace n
            WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
        """)
        namespaces = {row["name"]: row for row in cursor.fetchall()}
        for name in own_schemas:
            row = namespaces.get(name)
            if not row or not row["usable"] or row["writable"]:
                raise RuntimeConfigurationError(
                    "Tenant schema missing, inaccessible or runtime-owned"
                )
        for name, row in namespaces.items():
            if name not in own_schemas and (
                row["writable"] or (row["usable"] and name not in ("public", "extensions"))
            ):
                raise RuntimeConfigurationError(
                    "Tenant role can access a foreign application schema"
                )
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname <> ALL(%s)
                  AND n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
                  AND CASE WHEN c.relkind = 'S' THEN
                      has_sequence_privilege(current_user, c.oid, 'USAGE,SELECT,UPDATE')
                  WHEN c.relkind IN ('r','v','m','f','p') THEN
                      has_table_privilege(current_user, c.oid,
                          'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                      OR has_any_column_privilege(current_user, c.oid,
                          'SELECT,INSERT,UPDATE,REFERENCES')
                  ELSE false END
            ) AS foreign_access
        """,
            (own_schemas,),
        )
        if cursor.fetchone()["foreign_access"]:
            raise RuntimeConfigurationError(
                "Tenant role has privileges on foreign application data"
            )


def schema_identifier(area: str, runtime: TenantRuntime | None = None) -> str:
    """Only validated SQL identifier text, for existing developer-authored SQL builders.

    Quoting is delegated to psycopg, never to hand-written escaping. Legacy names
    are fixed source literals so historical query text remains unchanged.
    """
    from app.tenancy.config import load_runtime

    runtime = runtime or load_runtime()
    name = identifier(runtime.schema(area))
    return name if runtime.mode == "legacy" else sql.Identifier(name).as_string()


ANALYTICS_SCHEMA = schema_identifier("analytics")
DOCUMENTS_SCHEMA = schema_identifier("documents")
PAYMENTS_SCHEMA = schema_identifier("payments")
