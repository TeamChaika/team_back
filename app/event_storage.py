"""Transactional event versions, observations, coverage, and reciprocal transfer matching."""

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.schemas.iiko_events import EventsCapture
from app.services.iiko_events import MAX_EVENT_BYTES, event_content_hash, read_events
from app.sync_references import Source, SyncError, append_snapshot

ALGORITHM_VERSION = "reciprocal-v1"
TRANSFER_TYPES = ("dishesMovedFrom", "dishesMovedTo")
MATCH_WINDOW_SECONDS = 1


def capture_snapshot(folder: Path, source: Source, response: dict) -> dict:
    parsed = EventsCapture.model_validate(response)
    key = str(parsed.snapshot_id)
    metadata = json.loads((folder / f"{key}.meta.json").read_text())
    if metadata.pop("source_fingerprint") != source.fingerprint or metadata != response:
        raise SyncError("events_snapshot_identity_mismatch")
    if parsed.source_id != source.id or parsed.received_at.tzinfo is None:
        raise SyncError("events_snapshot_source_mismatch")
    raw_path, type_path = folder / f"{key}.xml", folder / f"{key}.metadata.xml"
    if max(raw_path.stat().st_size, type_path.stat().st_size) > MAX_EVENT_BYTES:
        raise SyncError("events_size_limit")
    raw, types = raw_path.read_bytes(), type_path.read_bytes()
    if (
        hashlib.sha256(raw).hexdigest() != parsed.sha256
        or len(raw) != parsed.source_bytes
        or hashlib.sha256(types).hexdigest() != parsed.metadata_sha256
    ):
        raise SyncError("events_snapshot_hash_mismatch")
    return {
        "id": key,
        "source_id": source.id,
        "observed_at": parsed.received_at,
        "day": parsed.date,
        "raw": raw,
        "metadata_raw": types,
        "expected_total": parsed.total,
    }


def transfer_signature(event: dict):
    fields = event["fields"]
    is_from = event["type"] == "dishesMovedFrom"
    suffix = "нового заказа для блюд" if is_from else "заказа-источника блюд"
    comment = re.fullmatch(r"(\d+) - номер " + suffix, fields.get("comment") or "")
    required = ("orderId", "orderNum", "user", "terminal", "dishes", "sum")
    if not comment or not all(fields.get(k) for k in required):
        return None
    own, other = fields["orderNum"], str(int(comment[1]))
    if own == other:
        return None
    return (
        own if is_from else other,
        other if is_from else own,
        fields["user"],
        fields["terminal"],
        fields["dishes"],
        Decimal(fields["sum"]),
    )


def match_transfers(events: list[dict]) -> list[dict]:
    """Require mutual uniqueness. Equal timestamps alone are never a link."""
    buckets = defaultdict(list)
    candidates = {}
    for event in events:
        signature = transfer_signature(event)
        if signature is not None:
            buckets[signature].append(event)
    for event in events:
        signature = transfer_signature(event)
        if signature is None:
            candidates[event["id"]] = []
            continue
        at = datetime.fromisoformat(event["time"])
        candidates[event["id"]] = [
            other["id"]
            for other in buckets[signature]
            if other["type"] != event["type"]
            and other["fields"]["orderId"] != event["fields"]["orderId"]
            and abs((datetime.fromisoformat(other["time"]) - at).total_seconds())
            <= MATCH_WINDOW_SECONDS
        ]
    results = []
    by_id = {e["id"]: e for e in events}
    for event in events:
        choices = candidates[event["id"]]
        paired = (
            choices[0] if len(choices) == 1 and candidates[choices[0]] == [event["id"]] else None
        )
        status = "matched" if paired else "ambiguous" if choices else "pending"
        if transfer_signature(event) is None:
            status = "invalid"
        evidence = {
            "fields": ["reciprocal_order_numbers", "user", "terminal", "dishes", "sum"],
            "window_seconds": MATCH_WINDOW_SECONDS,
            "line_identity": "unavailable",
        }
        if paired:
            evidence["time_delta_ms"] = int(
                abs(
                    (
                        datetime.fromisoformat(event["time"])
                        - datetime.fromisoformat(by_id[paired]["time"])
                    ).total_seconds()
                )
                * 1000
            )
        results.append(
            {
                "event_id": event["id"],
                "paired_event_id": paired,
                "status": status,
                "candidate_ids": choices,
                "evidence": evidence,
            }
        )
    return results


def rebuild_links(db, source_id: str) -> dict:
    rows = db.execute(
        "SELECT v.payload FROM chaika.rms_events e JOIN chaika.rms_event_versions v "
        "ON v.version_id=e.version_id WHERE e.source_id=%s AND e.event_type=ANY(%s)",
        (source_id, list(TRANSFER_TYPES)),
    ).fetchall()
    links = match_transfers([r[0] for r in rows])
    for link in links:
        db.execute(
            "INSERT INTO chaika.rms_event_links"
            "(source_id,event_id,paired_event_id,status,candidate_ids,algorithm_version,evidence) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source_id,event_id) DO UPDATE SET "
            "paired_event_id=EXCLUDED.paired_event_id,status=EXCLUDED.status,"
            "candidate_ids=EXCLUDED.candidate_ids,algorithm_version=EXCLUDED.algorithm_version,"
            "evidence=EXCLUDED.evidence,updated_at=now()",
            (
                source_id,
                link["event_id"],
                link["paired_event_id"],
                link["status"],
                Jsonb(link["candidate_ids"]),
                ALGORITHM_VERSION,
                Jsonb(link["evidence"]),
            ),
        )
    # A correction can cease being a transfer. Retire the projection, retain event history.
    db.execute(
        "UPDATE chaika.rms_event_links l SET status='invalid',paired_event_id=NULL,"
        "candidate_ids='[]'::jsonb,updated_at=now() FROM chaika.rms_events e "
        "WHERE l.source_id=%s AND e.source_id=l.source_id AND e.id=l.event_id "
        "AND NOT(e.event_type=ANY(%s))",
        (source_id, list(TRANSFER_TYPES)),
    )
    return dict(Counter(link["status"] for link in links))


def publish_events(db, run_id: UUID, snapshot: dict, types: dict) -> dict:
    source, observed, day = snapshot["source_id"], snapshot["observed_at"], snapshot["day"]
    records, business_raw, redacted = read_events(snapshot["raw"], day)
    if len(records) != snapshot["expected_total"]:
        raise SyncError("events_snapshot_count_mismatch")
    with db.transaction(), db.pipeline():
        existing_day = db.execute(
            "SELECT observed_at FROM chaika.rms_event_days WHERE source_id=%s AND event_date=%s",
            (source, day),
        ).fetchone()
        if existing_day and existing_day[0] > observed:
            raise SyncError("events_stale_observation")
        original_sha = hashlib.sha256(snapshot["raw"]).hexdigest()
        base = {"source_id": source, "observed_at": observed}
        append_snapshot(
            db,
            run_id,
            base
            | {
                "id": snapshot["id"],
                "resource": "events",
                "raw": business_raw,
                "sha256": hashlib.sha256(business_raw).hexdigest(),
                "payload": {
                    "date": day.isoformat(),
                    "original_sha256": original_sha,
                    "credential_attributes_redacted": redacted,
                    "total": len(records),
                },
            },
        )
        type_snapshot = str(uuid4())
        append_snapshot(
            db,
            run_id,
            base
            | {
                "id": type_snapshot,
                "resource": "event_types",
                "raw": snapshot["metadata_raw"],
                "sha256": hashlib.sha256(snapshot["metadata_raw"]).hexdigest(),
                "payload": types,
            },
        )
        for key, item in types.items():
            db.execute(
                "INSERT INTO chaika.rms_event_types(source_id,id,label,details,last_snapshot_id) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(source_id,id) DO UPDATE SET "
                "label=EXCLUDED.label,details=EXCLUDED.details,last_snapshot_id=EXCLUDED.last_snapshot_id",
                (source, key, item["label"], Jsonb(item), type_snapshot),
            )
        ids = [r["id"] for r in records]
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT e.id,e.version_id,e.last_seen_at,v.version_no,v.content_hash,v.payload "
                "FROM chaika.rms_events e JOIN chaika.rms_event_versions v "
                "ON v.version_id=e.version_id "
                "WHERE e.source_id=%s AND e.id=ANY(%s::uuid[])",
                (source, ids),
            )
            current = {str(r["id"]): r for r in cursor}
        created, changed, unchanged = 0, 0, 0
        for event in records:
            previous = current.get(event["id"])
            if previous and previous["last_seen_at"] > observed:
                raise SyncError("events_stale_observation")
            version_id = previous["version_id"] if previous else None
            previous_hash = None
            if previous:
                previous_hash = (
                    previous["content_hash"]
                    if previous["payload"].get("hash_method") == "unordered_attributes_v2"
                    else event_content_hash(previous["payload"])
                )
            if previous is None or previous_hash != event["hash"]:
                version_id = uuid4()
                number = previous["version_no"] + 1 if previous else 1
                db.execute(
                    "INSERT INTO chaika.rms_event_versions"
                    "(source_id,event_id,version_id,version_no,observed_at,snapshot_id,content_hash,payload)"
                    " VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        source,
                        event["id"],
                        version_id,
                        number,
                        observed,
                        snapshot["id"],
                        event["hash"],
                        Jsonb(event),
                    ),
                )
                created += previous is None
                changed += previous is not None
            else:
                unchanged += 1
            fields = event["fields"]
            db.execute(
                "INSERT INTO chaika.rms_events"
                "(source_id,id,version_id,occurred_at,event_type,order_id,"
                "order_number,department_code,actor_id,authorizer_id,waiter_id,terminal_id,"
                "event_sum,order_sum_after_discount,first_seen_at,last_seen_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(source_id,id) DO UPDATE SET version_id=EXCLUDED.version_id,"
                "occurred_at=EXCLUDED.occurred_at,event_type=EXCLUDED.event_type,"
                "order_id=EXCLUDED.order_id,order_number=EXCLUDED.order_number,"
                "department_code=EXCLUDED.department_code,actor_id=EXCLUDED.actor_id,"
                "authorizer_id=EXCLUDED.authorizer_id,waiter_id=EXCLUDED.waiter_id,"
                "terminal_id=EXCLUDED.terminal_id,event_sum=EXCLUDED.event_sum,"
                "order_sum_after_discount=EXCLUDED.order_sum_after_discount,last_seen_at=EXCLUDED.last_seen_at",
                (
                    source,
                    event["id"],
                    version_id,
                    event["time"],
                    event["type"],
                    fields.get("orderId"),
                    fields.get("orderNum"),
                    event["department_code"],
                    fields.get("user"),
                    fields.get("auth"),
                    fields.get("waiter"),
                    fields.get("terminal"),
                    fields.get("sum"),
                    fields.get("orderSumAfterDiscount"),
                    observed,
                    observed,
                ),
            )
            db.execute(
                "INSERT INTO chaika.rms_event_observations"
                "(snapshot_id,source_id,event_id,version_id) "
                "VALUES(%s,%s,%s,%s)",
                (snapshot["id"], source, event["id"], version_id),
            )
        links = rebuild_links(db, source)
        db.execute(
            "INSERT INTO chaika.rms_event_days"
            "(source_id,event_date,timezone,last_snapshot_id,event_count,observed_at) "
            "VALUES(%s,%s,'Europe/Simferopol',%s,%s,%s) "
            "ON CONFLICT(source_id,event_date) DO UPDATE SET "
            "last_snapshot_id=EXCLUDED.last_snapshot_id,event_count=EXCLUDED.event_count,"
            "observed_at=EXCLUDED.observed_at",
            (source, day, snapshot["id"], len(records), observed),
        )
    return {
        "events": len(records),
        "new_events": created,
        "changed_events": changed,
        "unchanged_events": unchanged,
        "credential_attributes_redacted": redacted,
        **{f"transfer_events_{k}": v for k, v in links.items()},
    }
