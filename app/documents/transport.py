"""Fixed iiko endpoints. No retries of document writes, including timeouts."""

import logging
import re
from contextlib import contextmanager
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx
from defusedxml.ElementTree import fromstring

from app.documents.context import local_zone
from app.documents.policy import full_name

log = logging.getLogger(__name__)


class DocumentTransport:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    @contextmanager
    def session(self):
        base = self.settings.iiko_url.rstrip("/")
        if (
            not base.startswith("https://")
            or not self.settings.iiko_login
            or not self.settings.iiko_password_hash.get_secret_value()
        ):
            raise ValueError("Document iiko connection is not configured")
        if not base.endswith("/resto/api"):
            base += "/resto/api"
        with httpx.Client(
            base_url=base + "/",
            timeout=httpx.Timeout(60, connect=10),
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            token = None
            try:
                response = client.get(
                    "auth",
                    params={
                        "login": self.settings.iiko_login,
                        "pass": self.settings.iiko_password_hash.get_secret_value(),
                    },
                )
                if response.status_code != 200 or not re.fullmatch(
                    r"[A-Za-z0-9-]{16,128}", response.text.strip()
                ):
                    raise ValueError("Document iiko authentication failed")
                token = response.text.strip()
                yield client, token
            finally:
                if token:
                    try:
                        client.get("logout", params={"key": token}, timeout=10)
                    except httpx.HTTPError:
                        log.warning("Document iiko logout unavailable")

    @staticmethod
    def payload(kind, doc, rows, creator, processor, source, target, reason, *, timezone=None):
        number = f"DJ{doc['id']:06d}"
        local_time = doc["created_at"].astimezone(timezone or local_zone())
        if kind == "writeoff":
            if not reason or not reason["account_id"]:
                raise ValueError("Writeoff account missing")
            return {
                "documentNumber": number,
                "dateIncoming": local_time.replace(tzinfo=None).isoformat(timespec="seconds"),
                "status": "NEW",
                "storeId": str(doc["store_id"]),
                "accountId": str(reason["account_id"]),
                "comment": f"СПИСАНИЕ\n{doc['reason_id'] or ''}\n{doc['comment'] or ''}",
                "items": [{"productId": str(r["product_id"]), "amount": r["amount"]} for r in rows],
            }
        # Retain the existing outgoingInvoice operation; do not silently replace it
        # with internalTransfer or add an unsupported recipient field.
        root = Element("document")
        values = {
            "documentNumber": number,
            "dateIncoming": local_time.isoformat(timespec="seconds"),
            "useDefaultDocumentTime": "true",
            "defaultStoreId": str(doc["store_id"]),
            "comment": f"Отправил: {full_name(creator)} [{creator['username']}] "
            f"со склада {source}\n"
            f"Принял: {full_name(processor)} [{processor['username']}] на склад {target}",
        }
        for key, value in values.items():
            SubElement(root, key).text = value
        children = SubElement(root, "items")
        for row in rows:
            child = SubElement(children, "item")
            for key, value in {
                "productId": row["product_id"],
                "price": 0.0,
                "amount": row["amount"],
            }.items():
                SubElement(child, key).text = str(value)
        return tostring(root, encoding="utf-8", xml_declaration=True)

    def send(self, kind, payload):
        with self.session() as (client, token):
            return self.send_authenticated(client, token, kind, payload)

    def send_authenticated(self, client, token, kind, payload):
        endpoint = (
            "documents/import/outgoingInvoice" if kind == "waybill" else "v2/documents/writeoff"
        )
        kwargs = (
            {"content": payload, "headers": {"Content-Type": "application/xml;charset=utf-8"}}
            if kind == "waybill"
            else {"json": payload}
        )
        # Any non-success without a parsed validation result remains uncertain.
        response = client.post(endpoint, params={"key": token}, **kwargs)
        if response.status_code != 200 or len(response.content) > 4 * 1024 * 1024:
            return "unknown"
        if kind == "waybill":
            valid = fromstring(response.content).findtext("valid")
            return {"true": "sent", "false": "rejected"}.get(valid, "unknown")
        content = response.json()
        if isinstance(content, dict):
            content = content.get("data", content)
        return (
            "sent"
            if isinstance(content, dict) and content.get("result") == "SUCCESS"
            else "unknown"
        )

    def catalogs(self):
        with self.session() as (client, token):
            responses = []
            for endpoint in ("products", "corporation/stores/"):
                with client.stream("GET", endpoint, params={"key": token}, timeout=120) as response:
                    if response.status_code != 200:
                        raise ValueError("Catalog request failed")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 64 * 1024 * 1024:
                            raise ValueError("Catalog response too large")
                    responses.append(fromstring(bytes(data)))
        products = {
            p.findtext("id"): p.findtext("name")
            for p in responses[0].iter("productDto")
            if p.findtext("productType") in {"GOODS", "PREPARED"}
        }
        stores = [
            {"id": p.findtext("id"), "name": p.findtext("name")}
            for p in responses[1].iter("corporateItemDto")
        ]
        if not products or not stores or any(not k or not v for k, v in products.items()):
            raise ValueError("Incomplete catalogs")
        return products, stores
