"""Read-only payment invoices from scoped, synchronized iiko sale documents.

Only unambiguous sale prices, base units and explicit VAT are printable. Source
amounts are preserved; missing data is never filled with a current product price.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from app.commercial_invoices.drafts import counterparty_catalog
from app.commercial_invoices.pdf import build_payment_qr_payload, render_invoice_pdf
from app.commercial_invoices.policy import actor, stores_for
from app.documents.context import analytics_schema
from app.documents.policy import fail, identifier
from app.tenancy.actor import actor_from_verified_scope


def _number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        fail(422, "В исходной накладной отсутствуют корректные цены, количества или НДС.")
    if not result.is_finite() or result < 0 or result > Decimal("999999999999999.99"):
        fail(422, "В исходной накладной есть отрицательные или некорректные суммы.")
    return result


def _snapshot(header, rows, buyer, seller):
    if header["status"] != "PROCESSED":
        fail(422, "Счёт на оплату доступен только для проведённой расходной накладной.")
    if (
        not header.get("counteragent_id")
        or header.get("linked_incoming_invoice_id")
        or header.get("linked_outgoing_invoice_id")
        or header.get("represents_store")
        or header.get("represented_store_id")
    ):
        fail(422, "Внутренняя передача или накладная без покупателя не является счётом на оплату.")
    if not buyer or buyer.get("source") != "iiko_suppliers" or not buyer.get("name"):
        fail(422, "Покупатель не подтверждён в справочнике внешних контрагентов iiko.")
    if not header.get("document_number"):
        fail(422, "В исходной накладной отсутствует номер документа.")
    if not 1 <= len(rows) <= 200:
        fail(422, "Для счёта требуется от 1 до 200 строк исходной накладной.")
    try:
        day = datetime.fromisoformat(str(header["date_incoming"])).date().isoformat()
    except (ValueError, TypeError):
        fail(422, "В исходной накладной некорректная дата.")
    items = []
    for row in rows:
        details = row.get("details") or {}
        if not isinstance(details, dict):
            fail(422, "Данные строки накладной недоступны.")
        if row.get("container_id") or not row.get("unit_id") or not row.get("unit"):
            fail(422, "Единица или фасовка товара не подтверждена для печати счёта.")
        if not row.get("product"):
            fail(422, "Наименование товара отсутствует в справочнике iiko.")
        if details.get("discount_sum") is not None and _number(details["discount_sum"]) != 0:
            fail(422, "Печать счёта по накладной со скидкой пока не поддерживается.")
        # A missing rate is unknown, not evidence that this sale is VAT-exempt.
        if details.get("vat_percent") is None or details.get("vat_sum") is None:
            fail(422, "В исходной накладной не указаны ставка или сумма НДС.")
        total, vat = _number(row.get("sum")), _number(details["vat_sum"])
        items.append(
            {
                "name": row["product"],
                "unit": row["unit"],
                "quantity": str(_number(row.get("amount"))),
                "price": str(_number(row.get("price"))),
                "price_includes_vat": True,
                "vat_rate": str(_number(details["vat_percent"])),
                "vat": str(vat),
                "net": str(total - vat),
                "total": str(total),
            }
        )
    return {
        "number": header["document_number"],
        "date": day,
        "state": "processed",
        "seller": dict(seller),
        "counterparty": dict(buyer),
        "items": items,
        "totals": {
            key: str(sum((Decimal(item[key]) for item in items), Decimal(0)))
            for key in ("net", "vat", "total")
        },
    }


def _authorized_snapshot(repo, commercial_service, scope, document_id):
    """Authorize both analytical scope and explicit commercial warehouse grants.

    Header authorization and its lines share one repeatable read transaction, so
    a concurrent collector update cannot switch the warehouses after the check.
    Both connections are read-only; this does not send or create an iiko document.
    """
    if commercial_service is None:
        fail(503, "Печать счетов пока не включена.")
    document_id = identifier(document_id)
    spec = repo.resource_query(scope, "outgoing")
    with commercial_service.database.connection(readonly=True) as commercial_db:
        user = actor(commercial_db, actor_from_verified_scope(scope), kind="sale")
        grants = {str(store) for store in stores_for(commercial_db, user["id"], "sale", "view")}
        with repo.connection(repeatable=True) as db:
            header = db.execute(
                "SELECT t.*,c.represents_store,c.represented_store_id FROM "
                + spec["base"]
                + " WHERE t.source_id='primary' AND ("
                + spec["guard"]
                + ") AND t.id=%s",
                [*spec["params"], document_id],
            ).fetchone()
            if not header:
                fail(404, "Документ не найден или недоступен.")
            rows = db.execute(
                "SELECT i.*,p.name AS product,p.main_unit_id AS unit_id,u.name AS unit "
                f"FROM {analytics_schema(db)}.outgoing_invoice_items i "
                f"LEFT JOIN {analytics_schema(db)}.products p ON "
                "p.source_id=i.source_id AND p.id=i.product_id "
                f"LEFT JOIN {analytics_schema(db)}.measure_units u ON u.source_id=p.source_id "
                "AND u.id=p.main_unit_id WHERE i.source_id='primary' AND i.document_id=%s "
                "AND i.present_in_latest ORDER BY i.line_num LIMIT 201",
                (document_id,),
            ).fetchall()
            # A LIMIT must not hide an inaccessible warehouse on a later row.
            if not 1 <= len(rows) <= 200:
                fail(422, "Для счёта требуется от 1 до 200 строк исходной накладной.")
            warehouses = {
                str(row.get("store_id") or header.get("default_store_id") or "") for row in rows
            }
            if "" in warehouses or not warehouses <= grants:
                fail(403, "Нет права просмотра реализации по всем складам этой накладной.")
        counterparties = counterparty_catalog(commercial_db, "sale")
        buyer = next(
            (c for c in counterparties if c["id"] == str(header.get("counteragent_id"))), None
        )
        return _snapshot(header, rows, buyer, commercial_service.seller)


def historical_pdf_allowed(repo, commercial_service, scope, document_id) -> bool:
    """Detail-page hint only; the PDF request independently repeats all checks."""
    try:
        snapshot = _authorized_snapshot(repo, commercial_service, scope, document_id)
        # QR validation includes precise item/totals arithmetic and bank details.
        # No QR, PDF, document, or payment is generated or persisted here.
        build_payment_qr_payload(snapshot)
        return True
    except (HTTPException, ValueError, InvalidOperation):
        return False


def historical_pdf(repo, commercial_service, scope, document_id) -> bytes:
    """Print only a newly authorized, internally consistent source snapshot."""
    snapshot = _authorized_snapshot(repo, commercial_service, scope, document_id)
    try:
        return render_invoice_pdf(snapshot)
    except (ValueError, InvalidOperation):
        fail(422, "Суммы, цены, НДС или реквизиты накладной не позволяют сформировать верный счёт.")
