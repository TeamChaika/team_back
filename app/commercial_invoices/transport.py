"""Priced invoice XML contracts, separate from legacy transfer/writeoff exports.

Primary specs: ru.iiko.help/article/api-documentations/
zagruzka-i-redaktirovanie-{prikhodnoy,raskhodnoy}-nakladnoy.
Never retry a POST: even a connection failure can follow a committed document.
"""

from datetime import date
from decimal import Decimal
from xml.etree.ElementTree import Element, SubElement, tostring

from defusedxml.ElementTree import fromstring

from app.commercial_invoices.calculation import money
from app.documents.policy import invalid
from app.documents.transport import DocumentTransport

ENDPOINTS = {"purchase": "incomingInvoice", "sale": "outgoingInvoice"}


def fields(element, values):
    for key, value in values.items():
        SubElement(element, key).text = str(value)


class CommercialTransport(DocumentTransport):
    @staticmethod
    def payload(kind, snapshot):
        if kind not in ENDPOINTS:
            raise ValueError("Unsupported invoice kind")
        root = Element("document")
        children = SubElement(root, "items")
        for index, row in enumerate(snapshot["items"], 1):
            item = SubElement(children, "item")
            price = Decimal(row["price"])
            if not row["price_includes_vat"]:
                price *= 1 + Decimal(row["vat_rate"] or "0") / 100
            if money(price * Decimal(row["quantity"])) != row["total"]:
                invalid(
                    "Округление цены с НДС не совпадает с итогом строки. "
                    "Укажите для этой позиции цену, уже включающую НДС."
                )
            if kind == "purchase":
                fields(
                    item,
                    {
                        "amount": row["quantity"],
                        "product": row["product_id"],
                        "num": index,
                        "amountUnit": row["unit_id"],
                        "sum": row["total"],
                        "discountSum": "0",
                        "vatPercent": row["vat_rate"] or "0",
                        "vatSum": row["vat"],
                        "price": format(price, "f"),
                        "store": snapshot["store_id"],
                        "actualAmount": row["quantity"],
                    },
                )
            else:
                fields(
                    item,
                    {
                        "productId": row["product_id"],
                        "price": format(price, "f"),
                        "amount": row["quantity"],
                        "sum": row["total"],
                        "discountSum": "0",
                        "vatPercent": row["vat_rate"] or "0",
                        "vatSum": row["vat"],
                    },
                )
        marker = f"RestControl {snapshot['id']} / {snapshot['version']}"
        comment = (snapshot.get("comment", "") + "\n" + marker).strip()
        if kind == "purchase":
            fields(
                root,
                {
                    "comment": comment,
                    "documentNumber": snapshot["number"],
                    "dateIncoming": date.fromisoformat(snapshot["date"]).strftime("%d.%m.%Y"),
                    "defaultStore": snapshot["store_id"],
                    "supplier": snapshot["counterparty_id"],
                    "incomingDate": snapshot["date"],
                    "useDefaultDocumentTime": "true",
                    "status": "PROCESSED",
                    "incomingDocumentNumber": snapshot["external_number"],
                },
            )
        else:
            root.remove(children)
            fields(
                root,
                {
                    "documentNumber": snapshot["number"],
                    "dateIncoming": snapshot["date"] + "T12:00:00",
                    "useDefaultDocumentTime": "true",
                    "status": "PROCESSED",
                    "defaultStoreId": snapshot["store_id"],
                    "counteragentId": snapshot["counterparty_id"],
                    "comment": comment,
                },
            )
            root.append(children)
        return tostring(root, encoding="utf-8", xml_declaration=True)

    def send_authenticated(self, client, token, kind, payload):
        endpoint = ENDPOINTS[kind]
        response = client.post(
            "documents/import/" + endpoint,
            params={"key": token},
            content=payload,
            headers={"Content-Type": "application/xml;charset=utf-8"},
        )
        if response.status_code != 200 or len(response.content) > 4 * 1024 * 1024:
            return {"state": "unknown"}
        root = fromstring(response.content)
        if root.tag != "documentValidationResult":
            return {"state": "unknown"}
        valid = root.findtext("valid")
        # A validation result has no document status or ID: never call it processed.
        if valid == "false":
            return {"state": "rejected"}
        if valid != "true":
            return {"state": "unknown"}
        sent = fromstring(payload)
        if root.findtext("documentNumber") != sent.findtext("documentNumber"):
            return {"state": "unknown"}
        return {"state": "accepted"}

    def lookup(self, client, token, kind, snapshot):
        """Read back exact number/date, then verify content before reconciling."""
        response = client.get(
            "documents/export/" + ENDPOINTS[kind] + "/byNumber",
            params={
                "key": token,
                "number": snapshot["number"],
                "from": snapshot["date"],
                "to": snapshot["date"],
                "currentYear": "false",
            },
        )
        if response.status_code != 200 or len(response.content) > 4 * 1024 * 1024:
            return None
        root = fromstring(response.content)
        documents = [root] if root.tag == "document" else list(root.iter("document"))
        matched = [d for d in documents if matches(d, kind, snapshot)]
        if len(matched) != 1:
            return None
        document = matched[0]
        from uuid import UUID

        remote_id = str(UUID(document.findtext("id", "")))
        state = document.findtext("status")
        if state not in {"NEW", "PROCESSED"}:
            return None
        return {"state": "processed" if state == "PROCESSED" else "accepted", "iiko_id": remote_id}


def matches(document, kind, snapshot):
    incoming = kind == "purchase"
    store = "defaultStore" if incoming else "defaultStoreId"
    party = "supplier" if incoming else "counteragentId"
    marker = f"RestControl {snapshot['id']} / {snapshot['version']}"
    if (
        document.findtext("documentNumber") != snapshot["number"]
        or document.findtext(store) != snapshot["store_id"]
        or document.findtext(party) != snapshot["counterparty_id"]
        or marker not in document.findtext("comment", "")
        or document.findtext("dateIncoming", "")[:10] != snapshot["date"]
    ):
        return False
    actual = document.findall("items/item")
    if len(actual) != len(snapshot["items"]):
        return False
    if any(
        r.findtext("store" if incoming else "storeId", snapshot["store_id"]) != snapshot["store_id"]
        for r in actual
    ):
        return False

    def signature(row):
        return (
            row.findtext("product" if incoming else "productId"),
            Decimal(row.findtext("amount", "NaN")),
            Decimal(row.findtext("sum", "NaN")) - Decimal(row.findtext("discountSum", "0")),
            Decimal(row.findtext("vatSum", "0")),
            Decimal(row.findtext("vatPercent", "0")),
        )

    expected = [
        (
            r["product_id"],
            Decimal(r["quantity"]),
            Decimal(r["total"]),
            Decimal(r["vat"]),
            Decimal(r["vat_rate"] or "0"),
        )
        for r in snapshot["items"]
    ]
    return sorted(map(signature, actual)) == sorted(expected)
