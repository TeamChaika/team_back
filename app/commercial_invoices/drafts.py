"""Validate and freeze the selected product, counterparty and money data."""

from datetime import UTC, date, datetime, timedelta

from app.commercial_invoices.calculation import calculate
from app.commercial_invoices.policy import require
from app.documents.policy import fail, identifier, invalid


def text_field(body, key, maximum):
    value = body.get(key, "")
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        invalid("Проверьте текстовые поля документа.")
    return value.strip()


def _catalog(db, name):
    row = db.execute("SELECT data,updated_at FROM native_catalog WHERE name=%s", (name,)).fetchone()
    if not row or row["updated_at"] < datetime.now(UTC) - timedelta(days=2):
        fail(503, "Справочник iiko недоступен или устарел. Обновите справочники.")
    return row["data"]


def product_catalog(db):
    return _catalog(db, "commercial_products")


def counterparty_catalog(db, kind):
    return _catalog(db, "commercial_" + kind + "_counterparties")


def build_snapshot(db, user, kind, body, seller, *, action):
    if set(body) - {
        "request_id",
        "version",
        "store_id",
        "counterparty_id",
        "date",
        "external_number",
        "comment",
        "items",
    }:
        invalid("Неизвестные поля документа.")
    store_id = identifier(body.get("store_id"))
    require(db, user["id"], kind, "view", store_id)
    require(db, user["id"], kind, action, store_id)
    store = db.execute("SELECT name FROM stores WHERE id=%s", (store_id,)).fetchone()
    if not store:
        invalid("Выберите склад.")
    try:
        document_date = date.fromisoformat(body.get("date", ""))
    except (ValueError, TypeError):
        invalid("Выберите дату документа.")
    if str(document_date) != body.get("date"):
        invalid("Используйте дату YYYY-MM-DD.")
    external_number = text_field(body, "external_number", 100)
    if kind == "purchase" and not external_number:
        invalid("Укажите номер накладной поставщика.")
    counterparty_id = identifier(body.get("counterparty_id"))
    counterparties = counterparty_catalog(db, kind)
    counterparty = next((c for c in counterparties if str(counterparty_id) == c["id"]), None)
    if not counterparty:
        invalid("Выберите действующего контрагента iiko.")
    items, totals = calculate(body.get("items"))
    products = {p["id"]: p for p in product_catalog(db)}
    for item in items:
        product_id = str(identifier(item["product_id"]))
        product = products.get(product_id)
        if not product or not product.get("unit_id") or not product.get("unit"):
            invalid("Товар или его основная единица недоступны в справочнике iiko.")
        item.update(
            product_id=product_id,
            name=product["name"],
            unit=product["unit"],
            unit_id=product["unit_id"],
        )
    return {
        "date": str(document_date),
        "store_id": str(store_id),
        "store": store["name"],
        "counterparty_id": str(counterparty_id),
        "counterparty": counterparty,
        "seller": seller,
        "external_number": external_number,
        "comment": text_field(body, "comment", 2000),
        "items": items,
        "totals": totals,
    }
