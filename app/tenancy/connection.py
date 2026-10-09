"""Restricted connection factory used by tenant collectors and portal helpers."""

from contextlib import contextmanager

import psycopg

from app.tenancy.config import load_runtime
from app.tenancy.sql import validate_database_runtime


def configure_connection(connection) -> None:
    runtime = load_runtime()
    if runtime.mode == "tenant":
        # A pool configure callback must leave its connection idle.
        with connection.transaction():
            validate_database_runtime(connection, runtime)


@contextmanager
def tenant_connect(dsn: str, *, connector=None, **kwargs):
    """Preserve psycopg transaction semantics and reject unsafe tenant logins."""
    with (connector or psycopg.connect)(dsn, **kwargs) as connection:
        configure_connection(connection)
        yield connection
