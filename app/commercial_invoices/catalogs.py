"""Bounded read-only iiko dictionaries, published atomically without private HR fields."""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from defusedxml.ElementTree import fromstring
from psycopg.types.json import Jsonb

LIMIT = 64 * 1024 * 1024
ZONE = ZoneInfo("Europe/Simferopol")


def _get(client, token, endpoint, params=()):
    with client.stream("GET", endpoint, params=[("key", token), *params], timeout=120) as response:
        if response.status_code != 200:
            raise ValueError("Commercial dictionary request failed")
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > LIMIT:
                raise ValueError("Commercial dictionary exceeds size limit")
        return bytes(data)


def parse_products(raw_products, raw_units):
    import json

    products, units = json.loads(raw_products), json.loads(raw_units)
    if not isinstance(products, list) or not isinstance(units, list):
        raise ValueError("Invalid commercial product dictionary")
    unit_map = {
        str(UUID(u["id"])): u["name"]
        for u in units
        if u.get("rootType") == "MeasureUnit" and u.get("deleted") is False
    }
    result, seen = [], set()
    for product in products:
        if product.get("deleted") is not False or product.get("type") not in {"GOODS", "PREPARED"}:
            continue
        product_id = str(UUID(product["id"]))
        unit_id = str(UUID(product["mainUnit"]))
        if product_id in seen:
            raise ValueError("Duplicate commercial product")
        seen.add(product_id)
        unit = unit_map.get(unit_id)
        name = product.get("name")
        if not isinstance(name, str) or not name.strip() or not isinstance(unit, str) or not unit:
            raise ValueError("Incomplete commercial product or unit")
        result.append({"id": product_id, "name": name, "unit_id": unit_id, "unit": unit})
    if not result:
        raise ValueError("Empty commercial product dictionary")
    return result


def parse_counterparties(content):
    root = fromstring(content, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    if root.tag != "employees":
        raise ValueError("Invalid commercial counterparty dictionary")
    result, seen = [], set()
    for row in root:
        if row.tag != "employee":
            raise ValueError("Invalid commercial counterparty record")
        if (
            row.findtext("supplier") not in {"true", "1"}
            or row.findtext("employee") not in {"false", "0"}
            or row.findtext("deleted") not in {"false", "0"}
            or row.findtext("representsStore") in {"true", "1"}
            or row.findtext("representedStoreId")
        ):
            continue
        identifier = str(UUID(row.findtext("id")))
        if identifier in seen:
            raise ValueError("Duplicate commercial counterparty")
        seen.add(identifier)
        name = row.findtext("name", "").strip()
        if not name:
            raise ValueError("Unnamed commercial counterparty")
        result.append(
            {
                "id": identifier,
                "name": name,
                "inn": row.findtext("taxpayerIdNumber", "").strip(),
                "kpp": "",  # The official employee DTO does not expose KPP.
                "address": row.findtext("address", "").strip(),
                "source": "iiko_suppliers",
            }
        )
    return result


def refresh(service):
    """One license session, GET dictionaries only; a failed read preserves all old caches."""
    timestamp = datetime.now(UTC)
    if getattr(service, "counterparty_provider", None) is not None:
        # Use the same clock as verified_at, including deployments on another host.
        with service.database.connection(readonly=True) as db:
            timestamp = db.execute("SELECT clock_timestamp() AS started_at").fetchone()[
                "started_at"
            ]
    with service.provider.session() as (client, token):
        products = _get(
            client,
            token,
            "v2/entities/products/list",
            (("includeDeleted", "false"), ("types", "GOODS"), ("types", "PREPARED")),
        )
        units = _get(
            client,
            token,
            "v2/entities/list",
            (("rootType", "MeasureUnit"), ("includeDeleted", "false")),
        )
        counterparties = _get(client, token, "suppliers", (("includeDeleted", "false"),))
    product_rows = parse_products(products, units)
    counterparty_rows = parse_counterparties(counterparties)
    with service.database.connection() as db:
        if getattr(service, "counterparty_provider", None) is not None:
            from app.commercial_invoices.counterparties import CATALOG_LOCK
            from app.commercial_invoices.counterparty_transport import source_key

            db.execute("SELECT pg_advisory_xact_lock(%s)", (CATALOG_LOCK,))
            local = db.execute(
                "SELECT data,verified_at FROM commercial_counterparties WHERE source_key=%s",
                (source_key(service.counterparty_provider),),
            ).fetchall()
            by_id = {row["id"]: row for row in counterparty_rows}
            for row in local:
                data = row["data"]
                if data["id"] in by_id:
                    # Keep current iiko core fields; KPP/type were entered on the site.
                    if data["inn"] == by_id[data["id"]]["inn"]:
                        by_id[data["id"]].update(
                            {key: data[key] for key in ("kpp", "entity_type", "local_fields")}
                        )
                elif row["verified_at"] >= timestamp:
                    # A confirmation concurrent with this fetch must survive publication.
                    by_id[data["id"]] = data
            counterparty_rows = list(by_id.values())
        for name, rows in {
            "commercial_products": product_rows,
            "commercial_purchase_counterparties": counterparty_rows,
            "commercial_sale_counterparties": counterparty_rows,
        }.items():
            db.execute(
                "INSERT INTO native_catalog(name,data,updated_at) VALUES(%s,%s,%s) "
                "ON CONFLICT(name) DO UPDATE SET data=EXCLUDED.data,updated_at=EXCLUDED.updated_at",
                (name, Jsonb(rows), timestamp),
            )
        db.execute(
            "INSERT INTO native_jobs(name,data) VALUES('commercial_catalogs',%s) "
            "ON CONFLICT(name) DO UPDATE SET data=EXCLUDED.data,updated_at=now()",
            (Jsonb({"last_success_at": timestamp.isoformat()}),),
        )
    return {"products": len(product_rows), "counterparties": len(counterparty_rows)}


def refresh_if_due(service, now):
    """After 06:00 local daily, at most one attempt every ten minutes per worker."""
    if now.tzinfo is None:
        raise ValueError("Catalog refresh requires an aware datetime")
    previous = getattr(service, "_catalog_check_at", None)
    if previous and now - previous < timedelta(minutes=10):
        return False
    service._catalog_check_at = now
    local = now.astimezone(ZONE)
    if local.hour < 6:
        return False
    with service.database.connection(readonly=True) as db:
        row = db.execute("SELECT data FROM native_jobs WHERE name='commercial_catalogs'").fetchone()
    if row:
        last = datetime.fromisoformat(row["data"]["last_success_at"])
        if last.astimezone(ZONE).date() >= local.date():
            return False
    refresh(service)
    return True
