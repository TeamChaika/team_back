import csv
import io
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.documents.catalog import products
from app.documents.policy import fail, identifier, invalid, stores_for, table

ZONE = ZoneInfo("Europe/Simferopol")


def summary(kind, doc):
    return {
        "kind": kind,
        "id": doc["id"],
        "number": f"DJ{doc['id']:06d}",
        **{key: doc[key] for key in ("version", "status", "submission_state")},
    }


def integer(value, default, maximum):
    try:
        result = int(value) if value is not None else default
        if not 1 <= result <= maximum:
            raise ValueError
        return result
    except (ValueError, TypeError):
        invalid()


def filters(db, user, kind, params):
    allowed = stores_for(db, user["id"], kind)
    conditions = [
        "(d.store_id=ANY(%s)" + (" OR d.counteragent_id=ANY(%s))" if kind == "waybill" else ")")
    ]
    values = [allowed, allowed] if kind == "waybill" else [allowed]
    status = params.get("status")
    if status:
        if status not in {"Created", "Sent", "Denied", "Cancelled"}:
            invalid()
        conditions.append("d.status=%s")
        values.append(status)
        if status == "Created":
            # Approval completes before the asynchronous delivery to iiko does.
            conditions.append("d.submission_state NOT IN ('queued','sending','unknown')")
    direction = params.get("direction", "all")
    if direction not in {"all", "incoming", "outgoing"}:
        invalid()
    store = identifier(params["store_id"]) if params.get("store_id") else None
    if store is not None and store not in allowed:
        fail(403, "Нет доступа к выбранному складу.")
    if kind == "waybill" and direction != "all":
        field = "counteragent_id" if direction == "incoming" else "store_id"
        conditions.append(f"d.{field}=ANY(%s)")
        values.append([store] if store is not None else allowed)
    elif store is not None:
        conditions.append(
            "(d.store_id=%s" + (" OR d.counteragent_id=%s)" if kind == "waybill" else ")")
        )
        values.extend([store, store] if kind == "waybill" else [store])
    dates = {}
    for key in ("date_from", "date_to"):
        if params.get(key):
            try:
                dates[key] = datetime.strptime(params[key], "%Y-%m-%d").date()
                day = dates[key] + (timedelta(days=1) if key == "date_to" else timedelta())
                cutoff = datetime.combine(day, time.min, ZONE)
            except (ValueError, TypeError, OverflowError):
                invalid("Выберите корректную дату.")
            conditions.append("d.created_at" + ("<%s" if key == "date_to" else ">=%s"))
            values.append(cutoff)
    if len(dates) == 2 and dates["date_from"] > dates["date_to"]:
        invalid("Начало периода не может быть позже окончания.")
    search = params.get("query", "").strip()
    if len(search) > 200:
        invalid()
    if search:
        number = search.removeprefix("DJ")
        if number.isascii() and number.isdigit() and len(number) <= 18:
            conditions.append("d.id=%s")
            values.append(int(number))
        else:
            # Match literal substrings, including user-entered SQL wildcard characters.
            conditions.append("strpos(lower(coalesce(d.comment,'')),lower(%s))>0")
            values.append(search)
    return " AND ".join(conditions), values


def joined(kind):
    parent, _ = table(kind)
    extra = ", t.name AS counteragent" if kind == "waybill" else ", r.id AS reason_pk"
    relation = (
        "JOIN stores t ON t.id=d.counteragent_id"
        if kind == "waybill"
        else "LEFT JOIN writeoffs_reasons r ON r.name=d.reason_id"
    )
    return (
        f"SELECT d.*, s.name AS store, "
        "coalesce(nullif(trim(c.first_name || ' ' || c.last_name),''),c.username) AS creator, "
        "coalesce(nullif(trim(p.first_name || ' ' || p.last_name),''),p.username) AS processor"
        f"{extra} FROM {parent} d JOIN stores s ON s.id=d.store_id "
        "JOIN authentication_user c ON c.id=d.created_by_id "
        f"LEFT JOIN authentication_user p ON p.id=d.processed_by_id {relation}"
    )


def serialize(kind, doc):
    result = {
        **summary(kind, doc),
        "store_id": str(doc["store_id"]),
        "store": doc["store"],
        "comment": doc["comment"] or "",
        "created_by": doc["creator"],
        "created_at": doc["created_at"].isoformat(),
        "processed_by": doc["processor"],
        "processed_at": doc["processed_at"].isoformat() if doc["processed_at"] else None,
    }
    if kind == "waybill":
        result.update(
            counteragent_id=str(doc["counteragent_id"]),
            counteragent=doc["counteragent"],
            iiko_document_type="outgoingInvoice",
        )
    else:
        result.update(
            reason=doc["reason_id"], reason_id=doc["reason_pk"], iiko_document_type="writeoff"
        )
    return result


def safe_cell(value):
    value = str(value or "")
    return (
        "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value
    )


def listing(db, user, kind, params, *, export=False):
    where, values = filters(db, user, kind, params)
    parent, _ = table(kind)
    total = db.execute(f"SELECT count(*) AS n FROM {parent} d WHERE {where}", values).fetchone()[
        "n"
    ]
    if export:
        if total > 10000:
            invalid("Сузьте период экспорта до 10 000 документов.")
        rows = db.execute(joined(kind) + f" WHERE {where} ORDER BY d.id DESC", values).fetchall()
        stream = io.StringIO()
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(
            [
                "Номер",
                "Статус",
                "Отправка iiko",
                "Дата",
                "Склад",
                "Получатель / причина",
                "Автор",
                "Комментарий",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    safe_cell(v)
                    for v in [
                        f"DJ{row['id']:06d}",
                        row["status"],
                        row["submission_state"],
                        row["created_at"].isoformat(),
                        row["store"],
                        row.get("counteragent", row.get("reason_id")),
                        row["creator"],
                        row["comment"],
                    ]
                ]
            )
        return ("\ufeff" + stream.getvalue()).encode()
    page = integer(params.get("page"), 1, 10000000)
    size = integer(params.get("page_size"), 30, 100)
    rows = db.execute(
        joined(kind) + f" WHERE {where} ORDER BY d.id DESC LIMIT %s OFFSET %s",
        [*values, size, (page - 1) * size],
    ).fetchall()
    return {
        "rows": [serialize(kind, row) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
    }


def items(db, kind, document_id, names=None):
    names = products(db, require_fresh=False) if names is None else names
    _, child = table(kind)
    return [
        {
            "product_id": str(row["product_id"]),
            "name": names.get(str(row["product_id"]), str(row["product_id"])),
            "amount": row["amount"],
        }
        for row in db.execute(
            f"SELECT * FROM {child} WHERE {kind}_id=%s ORDER BY id", (document_id,)
        ).fetchall()
    ]


def available_actions(db, user, kind, doc):
    if doc["submission_state"] in {"queued", "sending", "unknown"}:
        return []
    result = []
    if kind == "waybill" and doc["store_id"] in stores_for(db, user["id"], kind, "copy"):
        result.append("copy")
    if doc["status"] == "Created":
        for action in ("edit", "cancel", "approve"):
            store = (
                doc["counteragent_id"]
                if kind == "waybill" and action == "approve"
                else doc["store_id"]
            )
            if store in stores_for(db, user["id"], kind, action):
                result.extend(["confirm", "deny"] if action == "approve" else [action])
    return result


def detail(db, user, kind, document_id, params):
    where, values = filters(db, user, kind, params)
    doc = db.execute(
        joined(kind) + f" WHERE {where} AND d.id=%s", [*values, document_id]
    ).fetchone()
    if not doc:
        fail(404, "Документ не найден.")
    history = db.execute(
        "SELECT e.action,e.version,e.created_at, "
        "coalesce(nullif(trim(u.first_name || ' ' || u.last_name),''),u.username) AS actor "
        "FROM portal_documents_event e JOIN authentication_user u ON u.id=e.actor_id "
        "WHERE kind=%s AND document_id=%s ORDER BY e.id DESC LIMIT 100",
        (kind, document_id),
    ).fetchall()
    return {
        **serialize(kind, doc),
        "items": items(db, kind, document_id),
        "actions": available_actions(db, user, kind, doc),
        "history": history,
    }


def options(db, user, kind):
    grants = db.execute(
        "SELECT store_id,actions FROM portal_documents_grant WHERE user_id=%s AND kind=%s",
        (user["id"], kind),
    ).fetchall()
    return {
        "stores": db.execute(
            "SELECT id,name FROM stores WHERE id=ANY(%s) ORDER BY name",
            ([r["store_id"] for r in grants],),
        ).fetchall(),
        "recipients": db.execute("SELECT id,name FROM stores ORDER BY name").fetchall()
        if kind == "waybill"
        else [],
        "reasons": db.execute(
            "SELECT id,name FROM writeoffs_reasons WHERE account_id IS NOT NULL ORDER BY name"
        ).fetchall()
        if kind == "writeoff"
        else [],
        "grants": grants,
    }


def suggestions(db, user, kind, params):
    if not stores_for(db, user["id"], kind):
        fail(403, "Сначала получите доступ к складу.")
    search = params.get("query", "").strip().lower()
    if len(search) > 200:
        invalid()
    rows = sorted(
        [{"id": key, "name": name} for key, name in products(db).items() if search in name.lower()],
        key=lambda r: r["name"],
    )
    return {"rows": rows[:50], "total": len(rows)}
