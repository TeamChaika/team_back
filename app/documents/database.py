from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class DocumentDatabase:
    def __init__(self, url):
        if not url:
            raise ValueError("CHAIKA_DOCUMENTS_DATABASE_URL is required")
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
                "application_name": "chaika-documents-fastapi",
            },
            check=ConnectionPool.check_connection,
        )

    @contextmanager
    def connection(self, *, readonly=False):
        self.pool.open()
        with self.pool.connection() as db, db.transaction():
            if readonly:
                db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            db.execute("SET LOCAL search_path=chaika_iiko_documents,pg_catalog")
            db.execute("SET LOCAL statement_timeout='15000ms'")
            yield db

    def close(self):
        self.pool.close()
