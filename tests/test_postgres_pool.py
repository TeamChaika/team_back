from contextlib import contextmanager
from threading import Event

import psycopg
import pytest
from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests
from psycopg_pool.pool import WaitingClient

from app.core.postgres_pool import RecoveringConnectionPool
from app.documents.database import DocumentDatabase


class UnusableSession:
    autocommit = True

    def __init__(self):
        self.closed = False

    def execute(self, query):
        if query:
            raise psycopg.OperationalError("session cannot execute SQL")

    def close(self):
        self.closed = True


def test_checkout_closes_session_that_only_accepts_empty_queries():
    pool = DocumentDatabase("host=127.0.0.1 dbname=postgres").pool
    session = UnusableSession()

    with pytest.raises(psycopg.OperationalError, match="session cannot execute SQL"):
        pool._check(session)

    assert session.closed


def test_waiting_limit_preserves_live_waiters_and_their_order():
    pool = RecoveringConnectionPool("", min_size=0, max_size=1, max_waiting=3, open=False)
    first, expired, last = WaitingClient(), WaitingClient(), WaitingClient()
    expired.fail(PoolTimeout("connection request expired"))

    with pool._lock:
        pool._waiting.extend([first, expired, last])
        assert pool._get_ready_connection(timeout=1) is None
        assert list(pool._waiting) == [first, last]
        pool._waiting.append(WaitingClient())

        with pytest.raises(TooManyRequests):
            pool._get_ready_connection(timeout=1)
        with pytest.raises(PoolTimeout):
            pool._get_ready_connection(timeout=0)


def test_expired_waiters_do_not_prevent_new_connection_attempts():
    retries_exhausted = Event()

    class OfflineConnection(psycopg.Connection):
        @classmethod
        def connect(cls, *args, **kwargs):
            raise psycopg.OperationalError("test database is unavailable")

    pool = RecoveringConnectionPool(
        "",
        connection_class=OfflineConnection,
        min_size=0,
        max_size=1,
        max_waiting=2,
        reconnect_timeout=0,
        reconnect_failed=lambda _: retries_exhausted.set(),
        open=False,
    )
    with pool:
        for _ in range(3):
            retries_exhausted.clear()
            with pytest.raises(PoolTimeout):
                pool.getconn(timeout=0.05)
            assert retries_exhausted.wait(2)


@pytest.mark.parametrize(
    ("error", "closed"),
    [
        (psycopg.OperationalError("connection went away"), True),
        (psycopg.errors.AdminShutdown("database restarted"), True),
        (psycopg.errors.QueryCanceled("statement timeout"), False),
    ],
)
def test_session_handling_after_interrupted_operation(monkeypatch, error, closed):
    session = UnusableSession()
    pool = DocumentDatabase("host=127.0.0.1 dbname=postgres").pool
    checkouts = []

    @contextmanager
    def checkout(self, timeout=None):
        checkouts.append(timeout)
        yield session

    monkeypatch.setattr(ConnectionPool, "connection", checkout)

    with pytest.raises(type(error)) as raised, pool.connection(timeout=1):
        raise error

    assert raised.value is error
    assert session.closed == closed
    assert checkouts == [1]
