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


def product_suggestions(db):
    """Native names remain authoritative; enrich only from a fresh own-schema catalog."""
    names = products(db)
    row = db.execute(
        "SELECT data,updated_at FROM native_catalog WHERE name='commercial_products'"
    ).fetchone()
    metadata = {}
    if row and row["updated_at"] >= datetime.now(UTC) - timedelta(days=2):
        metadata = {str(item["id"]): item for item in row["data"]}
    result = []
    for key, name in names.items():
        item = {"id": key, "name": name}
        source = metadata.get(str(key), {})
        if source.get("article"):
            item["article"] = source["article"]
        if source.get("unit"):
            item["unit_name"] = source["unit"]
        result.append(item)
    return result
