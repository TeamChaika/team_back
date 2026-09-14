"""Strict RMS event parsing; original downloads remain private local files."""

import hashlib
import json
import os
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from app.core.config import BACKEND_DIR
from app.integrations.iiko.errors import IikoError
from app.schemas.iiko_events import EventsCapture
from app.services.iiko_connections import IikoConnectionsService
from app.sync_references import SyncError

MAX_EVENT_BYTES = 32 * 1024 * 1024
EVENT_ZONE = ZoneInfo("Europe/Simferopol")
SECRET_NAMES = {
    "pin",
    "pincode",
    "userpin",
    "password",
    "pass",
    "token",
    "key",
    "apikey",
    "accesstoken",
}
UUID_FIELDS = ("orderId", "user", "waiter", "cashier", "auth", "terminal", "sessionId")
NUMBER_FIELDS = (
    "sum",
    "orderSumAfterDiscount",
    "rowCount",
    "sumCash",
    "sumCard",
    "sumCredit",
    "sumPrepay",
    "percent",
    "withWriteoff",
    "penalty",
    "receiptsSum",
)
PUBLIC_FIELDS = (
    set(UUID_FIELDS)
    | set(NUMBER_FIELDS)
    | {
        "orderNum",
        "openTime",
        "tableNum",
        "dishes",
        "reason",
        "comment",
        "session",
        "discountTypeId",
        "productId",
        "orderItemId",
        "itemId",
        "positionId",
    }
)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, EVENT_ZONE)
    return start, start + timedelta(days=1)


def xml_root(raw: bytes, expected: str):
    if len(raw) > MAX_EVENT_BYTES:
        raise SyncError("events_size_limit")
    try:
        root = ET.fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ET.ParseError, DefusedXmlException, ValueError):
        raise SyncError("events_invalid_xml") from None
    if root.tag != expected:
        raise SyncError("events_invalid_root")
    return root


def decimal_text(value: str) -> str:
    try:
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError
        return str(number)
    except (InvalidOperation, ValueError):
        raise SyncError("events_invalid_number") from None


def event_content_hash(record: dict) -> str:
    """Attribute order varies between exports; preserve it in RAW, ignore it for equality."""
    canonical = {k: v for k, v in record.items() if k not in {"hash", "hash_method"}}
    canonical["attributes"] = sorted(
        canonical["attributes"], key=lambda a: json.dumps(a, ensure_ascii=False, sort_keys=True)
    )
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def read_events(raw: bytes, day: date) -> tuple[list[dict], bytes, int]:
    """Keep typed attributes as a list; redact credentials in the persisted business copy."""
    root = xml_root(raw, "eventsList")
    start, end = day_bounds(day)
    events, seen, redacted = [], set(), 0
    for element in root:
        if element.tag == "revision":
            continue
        if element.tag != "event":
            raise SyncError("events_unexpected_element")
        try:
            event_id = str(UUID(element.findtext("id", "")))
            occurred = datetime.fromisoformat(element.findtext("date", ""))
            if occurred.tzinfo is None or not start <= occurred < end:
                raise ValueError
            event_type = element.findtext("type")
            if not event_type:
                raise ValueError
        except ValueError:
            raise SyncError("events_invalid_identity_or_time") from None
        if event_id in seen:
            # Do not silently choose among repeated UUIDs within an export.
            raise SyncError("events_duplicate_uuid_in_export")
        seen.add(event_id)
        attributes, core = [], {}
        for a in element.findall("attribute"):
            name, value = a.findtext("name"), a.findtext("value")
            if not name:
                raise SyncError("events_missing_attribute_name")
            if re.sub(r"[^a-z]", "", name.lower()) in SECRET_NAMES:
                value = "[REDACTED]"
                value_node = a.find("value")
                if value_node is not None:
                    value_node.clear()
                    value_node.text = value
                redacted += 1
            attributes.append({"name": name, "value": value, "type": a.findtext("type")})
            if name in PUBLIC_FIELDS:
                if name in core:
                    raise SyncError("events_duplicate_core_attribute")
                core[name] = value
        for name in UUID_FIELDS:
            if core.get(name):
                try:
                    core[name] = str(UUID(core[name]))
                except ValueError:
                    raise SyncError("events_invalid_reference_uuid") from None
            elif name in core:
                core[name] = None
        for name in NUMBER_FIELDS:
            if core.get(name) is not None:
                core[name] = decimal_text(core[name])
        if core.get("orderNum") is not None:
            number = Decimal(decimal_text(core["orderNum"]))
            if number != number.to_integral_value():
                raise SyncError("events_invalid_order_number")
            core["orderNum"] = str(int(number))
        record = {
            "id": event_id,
            "time": occurred.isoformat(),
            "type": event_type,
            "department_code": element.findtext("departmentId"),
            "attributes": attributes,
            "fields": core,
        }
        record["hash_method"] = "unordered_attributes_v2"
        record["hash"] = event_content_hash(record)
        events.append(record)
    return events, ET.tostring(root, encoding="utf-8"), redacted


def read_event_types(raw: bytes) -> dict[str, dict]:
    root = xml_root(raw, "groupsList")
    types: dict[str, dict] = {}

    def walk(group, parents):
        path = parents + [{"id": group.findtext("id"), "name": group.findtext("name")}]
        for t in group.findall("type"):
            key = t.findtext("id")
            if not key:
                raise SyncError("events_metadata_missing_type")
            item = types.setdefault(key, {"label": t.findtext("name") or key, "groups": []})
            membership = {
                "path": path,
                "attributes": [
                    {"id": a.findtext("id"), "name": a.findtext("name")}
                    for a in t.findall("attribute")
                ],
            }
            if membership not in item["groups"]:
                item["groups"].append(membership)
        for child in group.findall("group"):
            walk(child, path)

    for group in root.findall("group"):
        walk(group, [])
    return types


async def capture_events(
    connections: IikoConnectionsService,
    source_id: str,
    day: date,
    directory: Path | None = None,
) -> EventsCapture:
    connection = connections.get_connection(source_id)
    if source_id == "primary":
        raise IikoError("events_rms_required", "Для событий выберите RMS.", status_code=422)
    folder = (directory or BACKEND_DIR / ".local/events") / source_id
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = uuid4()
    raw_path, meta_path = folder / f"{key}.xml", folder / f"{key}.metadata.xml"
    try:
        metadata = await connection.auth.download_event_types(meta_path)
        read_event_types(meta_path.read_bytes())
        download = await connection.auth.download_events(raw_path, day=day)
        rows, _, _ = read_events(raw_path.read_bytes(), day)
        result = EventsCapture(
            snapshot_id=key,
            source_id=source_id,
            date=day,
            received_at=datetime.now(UTC),
            total=len(rows),
            sha256=download.sha256,
            source_bytes=download.size_bytes,
            metadata_sha256=metadata.sha256,
        )
        base = str(connection.settings.iiko_base_url).rstrip("/")
        fingerprint = hashlib.sha256(
            f"{base}\n{connection.settings.iiko_login}".encode()
        ).hexdigest()
        metadata_json = result.model_dump(mode="json") | {"source_fingerprint": fingerprint}
        fd = os.open(folder / f"{key}.meta.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(metadata_json, stream)
        return result
    except SyncError as error:
        raise IikoError(str(error), "Некорректная выгрузка событий iiko.") from None
