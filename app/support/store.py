"""Shared durable store. Database principals further restrict company visibility."""

import hashlib
import json
from contextlib import contextmanager
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class SupportStore:
    def __init__(self, dsn):
        self.dsn = dsn

    @contextmanager
    def connect(self):
        if not self.dsn:
            raise HTTPException(503, "Служба поддержки временно недоступна")
        try:
            with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=3) as db:
                db.execute("SET LOCAL statement_timeout='3000ms'")
                db.execute("SET LOCAL lock_timeout='1500ms'")
                yield db
        except psycopg.Error:
            raise HTTPException(503, "Служба поддержки временно недоступна") from None

    @staticmethod
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def rate(db, context, operation, maximum):
        row = db.execute(
            """INSERT INTO support.rate_buckets(company_id,actor_id,operation,bucket,n)
            VALUES (%s,%s,%s,date_trunc('minute',now()),1)
            ON CONFLICT(company_id,actor_id,operation,bucket) DO UPDATE SET n=rate_buckets.n+1
            RETURNING n""",
            (context.company_id, context.actor_id, operation),
        ).fetchone()
        if row["n"] > maximum:
            raise HTTPException(
                429,
                "Слишком много обращений. Повторите через минуту",
                headers={"Retry-After": "60"},
            )

    def create_ticket(self, context, payload):
        values = payload.model_dump(mode="json")
        digest = self.digest(values)
        with self.connect() as db:
            # Serialize a request key before looking it up: retries also remain safe
            # when the first request is committing concurrently.
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (context.company_id + context.actor_id + str(payload.client_request_id),),
            )
            existing = db.execute(
                "SELECT * FROM support.tickets WHERE company_id=%s AND actor_id=%s "
                "AND client_request_id=%s",
                (context.company_id, context.actor_id, payload.client_request_id),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != digest:
                    raise HTTPException(409, "Ключ запроса уже использован с другим содержимым")
                return self.public(existing)
            self.rate(db, context, "ticket", 5)
            if (
                payload.event_id
                and not db.execute(
                    "SELECT id FROM support.events WHERE id=%s AND company_id=%s AND actor_id=%s",
                    (payload.event_id, context.company_id, context.actor_id),
                ).fetchone()
            ):
                raise HTTPException(404, "Событие не найдено")
            row = db.execute(
                """INSERT INTO support.tickets
                (id,company_id,company_name,actor_id,actor_name,client_request_id,payload_hash,description,screen,event_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (
                    uuid4(),
                    context.company_id,
                    context.company_name,
                    context.actor_id,
                    context.actor_name,
                    payload.client_request_id,
                    digest,
                    payload.description,
                    payload.screen,
                    payload.event_id,
                ),
            ).fetchone()
            db.execute(
                """INSERT INTO support.audit
                (id,ticket_id,company_id,actor_id,actor_name,to_status,revision,note)
                VALUES (%s,%s,%s,%s,%s,'new',1,'')""",
                (uuid4(), row["id"], context.company_id, context.actor_id, context.actor_name),
            )
            return self.public(row)

    def create_event(self, context, payload):
        values = payload.model_dump(mode="json")
        return self.event(context, values, "browser", payload.code)

    def event(self, context, values, source, code):
        request_key = values.get("client_request_id") or str(uuid4())
        digest = self.digest(values)
        fingerprint = self.digest(
            [
                context.company_id,
                context.actor_id,
                source,
                code,
                values.get("screen", "other"),
                values.get("status_code"),
                values.get("diagnostic", {}),
            ]
        )
        with self.connect() as db:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (context.company_id + context.actor_id + str(request_key),),
            )
            old = db.execute(
                "SELECT event_id,payload_hash FROM support.event_receipts "
                "WHERE company_id=%s AND actor_id=%s AND client_request_id=%s",
                (context.company_id, context.actor_id, request_key),
            ).fetchone()
            if old:
                if old["payload_hash"] != digest:
                    raise HTTPException(409, "Ключ запроса уже использован")
                return {"id": old["event_id"]}
            self.rate(db, context, "event", 30)
            row = db.execute(
                """INSERT INTO support.events
                (id,company_id,company_name,actor_id,actor_name,source,code,screen,status_code,request_id,diagnostic,fingerprint,bucket)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,date_trunc('hour',now()))
                ON CONFLICT(company_id,fingerprint,bucket) DO UPDATE
                SET occurrences=events.occurrences+1,last_seen=now(),
                    request_id=excluded.request_id RETURNING id""",
                (
                    uuid4(),
                    context.company_id,
                    context.company_name,
                    context.actor_id,
                    context.actor_name,
                    source,
                    code,
                    values.get("screen", "other"),
                    values.get("status_code"),
                    values.get("request_id"),
                    Jsonb(values.get("diagnostic", {})),
                    fingerprint,
                ),
            ).fetchone()
            db.execute(
                """INSERT INTO support.event_receipts
                (company_id,actor_id,client_request_id,event_id,payload_hash)
                VALUES (%s,%s,%s,%s,%s)""",
                (context.company_id, context.actor_id, request_key, row["id"], digest),
            )
            return {"id": row["id"]}

    @staticmethod
    def public(row):
        return {
            k: v
            for k, v in row.items()
            if k not in {"payload_hash", "client_request_id", "fingerprint", "bucket"}
        }

    @staticmethod
    def filters(context, company_id, status, table):
        clauses, args = [], []
        if context is not None:
            clauses += ["company_id=%s", "actor_id=%s"]
            args += [context.company_id, context.actor_id]
        elif company_id:
            clauses += ["company_id=%s"]
            args += [company_id]
        if status and table == "tickets":
            clauses += ["status=%s"]
            args += [status]
        return (" AND ".join(clauses) or "TRUE"), args

    def listing(
        self, context=None, *, table="tickets", company_id=None, status=None, limit=25, offset=0
    ):
        assert table in ("tickets", "events")
        clause, args = self.filters(context, company_id, status, table)
        order = "created_at" if table == "tickets" else "last_seen"
        with self.connect() as db:
            total = db.execute(
                f"SELECT count(*) AS n FROM support.{table} WHERE {clause}", args
            ).fetchone()["n"]
            rows = db.execute(
                f"SELECT * FROM support.{table} WHERE {clause} "
                f"ORDER BY {order} DESC,id DESC LIMIT %s OFFSET %s",
                [*args, limit, offset],
            ).fetchall()
            return {
                "items": [self.public(r) for r in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def detail(self, item_id, context=None, *, table="tickets"):
        assert table in ("tickets", "events")
        clause, args = self.filters(context, None, None, table)
        with self.connect() as db:
            row = db.execute(
                f"SELECT * FROM support.{table} WHERE id=%s AND {clause}", [item_id, *args]
            ).fetchone()
            if not row:
                raise HTTPException(404, "Обращение или событие не найдено")
            result = self.public(row)
            if table == "tickets":
                result["audit"] = db.execute(
                    "SELECT id,actor_id,actor_name,from_status,to_status,note,created_at,revision "
                    "FROM support.audit WHERE ticket_id=%s ORDER BY revision",
                    (item_id,),
                ).fetchall()
                event = (
                    db.execute(
                        "SELECT * FROM support.events WHERE id=%s AND company_id=%s",
                        (row["event_id"], row["company_id"]),
                    ).fetchone()
                    if row["event_id"]
                    else None
                )
                result["event"] = self.public(event) if event else None
            return result

    def transition(self, item_id, payload, actor_id, actor_name="Владелец платформы"):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM support.tickets WHERE id=%s FOR UPDATE", (item_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Обращение не найдено")
            if row["revision"] != payload.expected_revision:
                raise HTTPException(409, "Обращение изменено. Обновите страницу")
            revision = row["revision"] + 1
            db.execute(
                "UPDATE support.tickets SET status=%s,revision=%s,updated_at=now() WHERE id=%s",
                (payload.status, revision, item_id),
            )
            db.execute(
                """INSERT INTO support.audit
                (id,ticket_id,company_id,actor_id,actor_name,from_status,to_status,note,revision)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    uuid4(),
                    item_id,
                    row["company_id"],
                    actor_id,
                    actor_name,
                    row["status"],
                    payload.status,
                    payload.note,
                    revision,
                ),
            )
        return self.detail(item_id)

    def prune(self, batch=1000):
        """Central maintenance, bounded batches; audit is deleted only with ticket."""
        batch = min(max(int(batch), 1), 10000)
        with self.connect() as db:
            events = db.execute(
                """DELETE FROM support.events WHERE id IN
                (SELECT id FROM support.events WHERE last_seen < now()-interval '30 days'
                 ORDER BY last_seen LIMIT %s)""",
                (batch,),
            ).rowcount
            tickets = db.execute(
                """DELETE FROM support.tickets WHERE id IN
                (SELECT id FROM support.tickets WHERE status IN ('resolved','dismissed')
                 AND updated_at < now()-interval '365 days'
                 ORDER BY updated_at LIMIT %s)""",
                (batch,),
            ).rowcount
            buckets = db.execute(
                """DELETE FROM support.rate_buckets WHERE ctid IN
                (SELECT ctid FROM support.rate_buckets
                 WHERE bucket < now()-interval '1 day' LIMIT %s)""",
                (batch,),
            ).rowcount
            receipts = db.execute(
                """DELETE FROM support.event_receipts WHERE ctid IN
                (SELECT ctid FROM support.event_receipts
                 WHERE created_at < now()-interval '30 days' LIMIT %s)""",
                (batch,),
            ).rowcount
        return {"events": events, "tickets": tickets, "rate_buckets": buckets, "receipts": receipts}
