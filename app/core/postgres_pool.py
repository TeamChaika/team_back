from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool


class RecoveringConnectionPool(ConnectionPool):
    def __init__(self, conninfo: str, **kwargs):
        super().__init__(conninfo, check=self.check_connection, **kwargs)

    @staticmethod
    def check_connection(conn: psycopg.Connection) -> None:
        try:
            # Force a PostgreSQL round trip through the proxy before lending the session.
            conn.execute("SELECT 1").fetchone()
        except psycopg.Error:
            # A rejected check alone can return an idle session to the pool.
            conn.close()
            raise

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[psycopg.Connection]:
        with super().connection(timeout=timeout) as conn:
            try:
                yield conn
            except (psycopg.InterfaceError, psycopg.OperationalError) as exc:
                state = exc.sqlstate
                if state is None or state.startswith("08") or state in {"57P01", "57P02", "57P03"}:
                    conn.close()
                raise
