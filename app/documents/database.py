from contextlib import contextmanager

from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.tenancy.config import load_runtime
from app.tenancy.sql import validate_database_runtime


class DocumentDatabase:
    def __init__(self, url, *, runtime=None):
        self.runtime = runtime or load_runtime()
        if not url:
            raise ValueError("Document database URL is required")
        self.pool = ConnectionPool(
            url,
            min_size=0,
            max_size=4,
            timeout=10,
            max_waiting=20,
            open=False,
            kwargs={
                "row_factory": dict_row,
                "autocommit": True,
                "prepare_threshold": None,
                "connect_timeout": 10,
                "application_name": f"{self.runtime.key}-documents",
            },
            check=ConnectionPool.check_connection,
            configure=self._configure,
        )

    def _configure(self, db):
        db.tenant_runtime = self.runtime
        with db.transaction():
            validate_database_runtime(db, self.runtime)

    @contextmanager
    def connection(self, *, readonly=False):
        self.pool.open()
        with self.pool.connection() as db, db.transaction():
            if readonly:
                db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            db.execute(
                sql.SQL("SET LOCAL search_path={},pg_catalog").format(
                    sql.Identifier(self.runtime.documents_schema)
                )
            )
            db.execute("SET LOCAL statement_timeout='15000ms'")
            yield db

    def close(self):
        self.pool.close()
