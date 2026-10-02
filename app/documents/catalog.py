from datetime import UTC, datetime, timedelta

from app.documents.policy import fail


def products(db, *, require_fresh=True):
    row = db.execute("SELECT data,updated_at FROM native_catalog WHERE name='products'").fetchone()
    if (
        not row
        or not row["data"]
        or (require_fresh and row["updated_at"] < datetime.now(UTC) - timedelta(days=30))
    ):
        fail(503, "Справочник товаров временно недоступен.")
    return row["data"]
