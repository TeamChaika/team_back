"""Bounded, repeatable-read graph of stored events; no upstream calls."""

from datetime import date
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.core.config import Settings
from app.event_storage import ALGORITHM_VERSION
from app.schemas.iiko_events import OrderTopology, TopologyEdge, TopologyNode
from app.services.iiko_events import day_bounds
from app.services.sync_jobs import SyncJobError

MAX_ORDERS = 50
MAX_EVENTS = 2000


def build_topology(db, source_id: str, root_id: UUID) -> OrderTopology:
    orders, frontier = {root_id}, {root_id}
    while frontier:
        related = db.execute(
            "SELECT DISTINCT p.order_id FROM chaika.rms_events e "
            "JOIN chaika.rms_event_links l ON l.source_id=e.source_id AND l.event_id=e.id "
            "JOIN chaika.rms_events p ON p.source_id=l.source_id AND p.id=l.paired_event_id "
            "WHERE e.source_id=%s AND e.order_id=ANY(%s::uuid[]) AND l.status='matched'",
            (source_id, list(frontier)),
        ).fetchall()
        frontier = {r[0] for r in related if r[0]} - orders
        orders |= frontier
        if len(orders) > MAX_ORDERS:
            raise SyncJobError("topology_too_large", "Связанных заказов больше лимита 50.", 413)
    with db.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT v.payload, e.version_id, t.label FROM chaika.rms_events e "
            "JOIN chaika.rms_event_versions v ON v.version_id=e.version_id "
            "LEFT JOIN chaika.rms_event_types t ON t.source_id=e.source_id AND t.id=e.event_type "
            "WHERE e.source_id=%s AND e.order_id=ANY(%s::uuid[]) "
            "ORDER BY e.occurred_at,e.id LIMIT %s",
            (source_id, list(orders), MAX_EVENTS + 1),
        )
        rows = cursor.fetchall()
    if len(rows) > MAX_EVENTS:
        raise SyncJobError("topology_too_large", "В связанных заказах больше 2000 событий.", 413)
    if not rows:
        raise SyncJobError("order_not_found", "Заказ не найден в загруженных событиях.", 404)
    event_ids = [r["payload"]["id"] for r in rows]
    with db.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT * FROM chaika.rms_event_links WHERE source_id=%s "
            "AND event_id=ANY(%s::uuid[]) ORDER BY event_id",
            (source_id, event_ids),
        )
        links = cursor.fetchall()
    actors = {
        v
        for r in rows
        for k, v in r["payload"]["fields"].items()
        if k in ("user", "auth", "waiter", "cashier") and v
    }
    names = dict(
        db.execute(
            "SELECT e.id::text,e.name FROM chaika.employees e JOIN chaika.rms_bindings b "
            "ON b.chain_source_id=e.source_id WHERE b.source_id=%s AND b.state='matched' "
            "AND e.id=ANY(%s::uuid[])",
            (source_id, list(actors)),
        ).fetchall()
    )
    nodes, edges, last_by_order, by_id = {}, [], {}, {}

    def node(key, kind, label, details):
        nodes[key] = TopologyNode(id=key, kind=kind, label=label, details=details)

    def edge(a, b, kind, evidence):
        edges.append(
            TopologyEdge(
                id=f"{kind}:{a}:{b}", source=a, target=b, kind=kind, evidence_event_ids=evidence
            )
        )

    for row in rows:
        event = row["payload"]
        fields = event["fields"]
        eid, oid = event["id"], fields["orderId"]
        action_key, order_key = f"{source_id}:event:{eid}", f"{source_id}:order:{oid}"
        if order_key not in nodes:
            node(order_key, "order", f"№{fields.get('orderNum', '?')}", {"order_id": oid})
        node(
            action_key,
            "action",
            row["label"] or event["type"],
            {
                "event_id": eid,
                "version_id": str(row["version_id"]),
                "type": event["type"],
                "occurred_at": event["time"],
                "fields": fields,
            },
        )
        edge(order_key, action_key, "has_action", [eid])
        for attr, kind in (
            ("user", "performed_by"),
            ("auth", "authorized_by"),
            ("waiter", "assigned_waiter"),
            ("cashier", "cashier"),
        ):
            actor = fields.get(attr)
            if actor:
                actor_key = f"{source_id}:actor:{actor}"
                node(
                    actor_key,
                    "actor",
                    names.get(actor, actor),
                    {
                        "actor_id": actor,
                        "name_resolved": actor in names,
                        "name_source": "current_chain_dictionary",
                    },
                )
                edge(action_key, actor_key, kind, [eid])
        if oid in last_by_order:
            previous = last_by_order[oid]
            # Tied timestamps have a stable display order, not a known chronological relationship.
            if previous["time"] != event["time"]:
                edge(
                    f"{source_id}:event:{previous['id']}",
                    action_key,
                    "later_in_time",
                    [previous["id"], eid],
                )
        last_by_order[oid] = event
        by_id[eid] = event
    transfers, done = [], set()
    for link in links:
        eid = str(link["event_id"])
        if eid in done:
            continue
        event = by_id[eid]
        counterpart = str(link["paired_event_id"]) if link["paired_event_id"] else None
        fields = event["fields"]
        transfer = {
            "status": link["status"],
            "event_ids": [eid],
            "evidence": link["evidence"],
            "candidate_event_ids": link["candidate_ids"],
            "amount": fields.get("sum"),
            "dishes_text": fields.get("dishes"),
            "algorithm_version": link["algorithm_version"],
            "line_identity": "unavailable",
        }
        if counterpart and counterpart in by_id:
            other = by_id[counterpart]
            sender, receiver = (
                (event, other) if event["type"] == "dishesMovedFrom" else (other, event)
            )
            transfer.update(
                from_order_id=sender["fields"]["orderId"],
                to_order_id=receiver["fields"]["orderId"],
                event_ids=[sender["id"], receiver["id"]],
            )
            key = f"{source_id}:transfer:{sender['id']}"
            node(key, "transfer", "Перенос позиций", transfer)
            edge(
                f"{source_id}:order:{transfer['from_order_id']}",
                key,
                "transferred_out",
                transfer["event_ids"],
            )
            edge(
                key,
                f"{source_id}:order:{transfer['to_order_id']}",
                "transferred_in",
                transfer["event_ids"],
            )
            done.add(counterpart)
        transfers.append(transfer)
        done.add(eid)
    coverage = [
        dict(date=r[0].isoformat(), timezone=r[1], observed_at=r[2].isoformat(), event_count=r[3])
        for r in db.execute(
            "SELECT event_date,timezone,observed_at,event_count FROM chaika.rms_event_days "
            "WHERE source_id=%s ORDER BY event_date",
            (source_id,),
        ).fetchall()
    ]
    return OrderTopology(
        source_id=source_id,
        root_order_id=root_id,
        order_ids=sorted(orders, key=str),
        nodes=list(nodes.values()),
        edges=edges,
        transfers=transfers,
        coverage=coverage,
        event_count=len(rows),
        algorithm_version=ALGORITHM_VERSION,
        limitations=[
            "Полнота ограничена загруженными днями; отсутствие события не доказывает "
            "отсутствие действия.",
            "Переносы сопоставлены по парным событиям. UUID строк позиций не предоставлены; "
            "dishes_text — исходный состав группы.",
            "later_in_time означает порядок по времени, а не причинную связь. "
            "Имена сотрудников взяты из текущего справочника.",
        ],
    )


def read_topology(
    settings: Settings,
    source_id: str,
    day: date,
    order_number: int,
    *,
    order_id: UUID | None = None,
) -> OrderTopology:
    if not settings.database_url.get_secret_value():
        raise SyncJobError("database_not_configured", "База данных не настроена.")
    with psycopg.connect(settings.database_url.get_secret_value(), connect_timeout=10) as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        db.execute("SET LOCAL statement_timeout='15s'")
        if order_id is not None:
            # A UUID from OLAP is an exact root; never substitute a reused order number.
            return build_topology(db, source_id, order_id)
        start, end = day_bounds(day)
        found = db.execute(
            "SELECT DISTINCT order_id FROM chaika.rms_events WHERE source_id=%s "
            "AND order_number=%s AND occurred_at>=%s AND occurred_at<%s AND order_id IS NOT NULL",
            (source_id, str(order_number), start, end),
        ).fetchall()
        if not found:
            raise SyncJobError(
                "order_not_found", "Заказ не найден в загруженных событиях за эту дату.", 404
            )
        if len(found) > 1:
            raise SyncJobError(
                "order_number_ambiguous", "Этому номеру соответствуют несколько UUID заказов.", 409
            )
        return build_topology(db, source_id, found[0][0])
