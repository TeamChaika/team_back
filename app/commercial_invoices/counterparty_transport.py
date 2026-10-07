"""Official employee DTO used as an external supplier/buyer, never a staff account."""

import hashlib
from urllib.parse import quote
from uuid import UUID

from defusedxml.ElementTree import fromstring

from app.commercial_invoices.counterparty_models import wire_fields
from app.documents.transport import DocumentTransport

LIMIT = 1024 * 1024


def source_key(provider):
    settings = provider.settings
    source = settings.iiko_url.rstrip("/") + "\n" + settings.iiko_login
    return hashlib.sha256(source.encode()).hexdigest()


def request(client, token, method, endpoint, *, data=None, limit=LIMIT):
    with client.stream(
        method,
        endpoint,
        params={"key": token},
        data=data,
        timeout=30,
        headers={"Accept": "application/xml"},
    ) as response:
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > limit:
                raise ValueError("Counterparty response exceeds limit")
        return response.status_code, bytes(content)


def parse_external(element):
    if element.tag != "employee":
        raise ValueError("Unexpected counterparty XML")
    if (
        element.findtext("supplier") not in {"true", "1"}
        or element.findtext("employee") not in {"false", "0"}
        or element.findtext("deleted") not in {"false", "0"}
        or element.findtext("representsStore") in {"true", "1"}
        or element.findtext("representedStoreId")
    ):
        return None
    name = element.findtext("name", "").strip()
    if not name:
        raise ValueError("Unnamed counterparty")
    return {
        "id": str(UUID(element.findtext("id"))),
        "name": name,
        "code": element.findtext("code", "").strip(),
        "inn": element.findtext("taxpayerIdNumber", "").strip(),
        "address": element.findtext("address", "").strip(),
        "phone": element.findtext("phone", "").strip(),
        "email": element.findtext("email", "").strip(),
        "client": element.findtext("client", ""),
    }


class CounterpartyTransport(DocumentTransport):
    def registry(self, client, token):
        status, content = request(client, token, "GET", "suppliers", limit=64 * LIMIT)
        if status != 200:
            raise ValueError("Counterparty registry unavailable")
        root = fromstring(content, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        if root.tag != "employees":
            raise ValueError("Invalid counterparty registry")
        rows, seen = [], set()
        for node in root:
            row = parse_external(node)
            if row:
                if row["id"] in seen:
                    raise ValueError("Duplicate counterparty UUID")
                rows.append(row)
                seen.add(row["id"])
        return rows

    def exists(self, client, token, *, identifier=None, code=None):
        endpoint = (
            "employees/byId/" + str(UUID(str(identifier)))
            if identifier is not None
            else "employees/byCode/" + quote(code, safe="")
        )
        status, _ = request(client, token, "GET", endpoint)
        if status == 404:
            return False
        if status != 200:
            raise ValueError("Counterparty lookup unavailable")
        return True

    def lookup(self, client, token, job):
        status, content = request(client, token, "GET", "employees/byId/" + str(job["iiko_id"]))
        if status == 404:
            return None
        if status != 200:
            raise ValueError("Counterparty readback unavailable")
        root = fromstring(content, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        row = parse_external(root)
        if not row or row["id"] != str(job["iiko_id"]) or row["code"] != job["code"]:
            return None
        if row["client"] not in {"false", "0"}:
            return None
        if any(
            row[key] != job["payload"][key]
            for key in (
                "name",
                "inn",
                "address",
                "phone",
                "email",
            )
        ):
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "inn": row["inn"],
            "address": row["address"],
            "kpp": job["payload"]["kpp"],
            "entity_type": job["payload"]["entity_type"],
            "source": "iiko_suppliers",
            "local_fields": ["kpp", "entity_type"],
        }

    def create(self, client, token, job):
        status, _ = request(
            client,
            token,
            "POST",
            "employees/byId/" + str(job["iiko_id"]),
            data=wire_fields(job["payload"], job["code"]),
        )
        # A received rejection is determinate; arbitrary upstream text is not exposed.
        if status in {400, 401, 403, 404, 409, 422}:
            return "rejected"
        # Even a 200/201 requires independent GET; never trust the POST body alone.
        return "unknown"
