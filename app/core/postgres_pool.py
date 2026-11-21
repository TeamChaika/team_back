from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool


class RecoveringConnectionPool(ConnectionPool):
    def __init__(self, conninfo: str, **kwargs):
        super().__init__(conninfo, check=self.check_connection, **kwargs)

    def _get_ready_connection(self, timeout: float | None) -> psycopg.Connection | None:
        # Psycopg calls this under the pool lock. After reconnect attempts run out,
        # expired waiters can otherwise fill the queue and prevent the next attempt.
        if self.max_waiting and len(self._waiting) >= self.max_waiting:
            self._waiting = deque(waiter for waiter in self._waiting if waiter.error is None)
        return super()._get_ready_connection(timeout)

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
