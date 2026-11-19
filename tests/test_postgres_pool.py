from contextlib import contextmanager

import psycopg
import pytest
from psycopg_pool import ConnectionPool

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
