"""User-private history and durable per-user budgets. Never holds a DB lock during AI calls."""

import hashlib
import json
from contextlib import contextmanager
from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.web.repository import serial


def access_hash(scope):
    identity = {
        "role": scope.user["role"],
        "departments": sorted(str(i) for i in scope.ids),
        "stores": sorted(str(i) for i in scope.store_ids),
        "rms": sorted(scope.rms_ids),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


class AssistantStore:
    def __init__(self, repo):
        self.repo = repo

    @contextmanager
    def connection(self, scope):
        self.repo._pool.open()
        with self.repo._pool.connection() as db, db.transaction():
            db.execute("SET LOCAL statement_timeout='10000ms'")
            db.execute(
                "SELECT set_config('chaika.assistant_user',%s,true)", (str(scope.user["id"]),)
            )
            yield db

    def _conversation(self, db, scope, conversation_id):
        row = db.execute(
            "SELECT * FROM chaika.assistant_conversations WHERE id=%s AND user_id=%s",
            (conversation_id, scope.user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Диалог не найден или недоступен.")
        if row["access_hash"] != access_hash(scope):
            raise HTTPException(409, "Права доступа изменились. Начните новый диалог.")
        return row

    def list(self, scope):
        with self.connection(scope) as db:
            return serial(
                db.execute(
                    "SELECT id,title,context,created_at FROM chaika.assistant_conversations "
                    "WHERE user_id=%s AND access_hash=%s ORDER BY created_at DESC LIMIT 20",
                    (scope.user["id"], access_hash(scope)),
                ).fetchall()
            )

    def history(self, scope, conversation_id):
        with self.connection(scope) as db:
            row = self._conversation(db, scope, conversation_id)
            turns = db.execute(
                "SELECT id,question,answer,sources,model,created_at FROM chaika.assistant_turns "
                "WHERE conversation_id=%s AND user_id=%s AND status='completed' "
                "ORDER BY created_at,id LIMIT 10",
                (conversation_id, scope.user["id"]),
            ).fetchall()
            return serial(dict(id=row["id"], context=row["context"], turns=turns))

    def reserve(self, scope, payload, limit):
        user_id = scope.user["id"]
        context = payload.context.model_dump(mode="json")
        with self.connection(scope) as db:
            # Serializes budget checks across workers; released before any HTTP request.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,71934))", (str(user_id),))
            old = db.execute(
                "SELECT conversation_id,status,question FROM chaika.assistant_turns "
                "WHERE id=%s AND user_id=%s",
                (payload.request_id, user_id),
            ).fetchone()
            if old:
                conv = self._conversation(db, scope, old["conversation_id"])
                if old["question"] != payload.question or conv["context"] != context:
                    raise HTTPException(409, "Идентификатор запроса уже использован.")
                if old["status"] == "completed":
                    return old["conversation_id"], True
                raise HTTPException(409, "Этот запрос уже отправлен. Обновите историю диалога.")
            budget = db.execute(
                "SELECT count(*) AS count, count(*) FILTER (WHERE status='pending' AND "
                "created_at>now()-interval '3 minutes') AS busy FROM chaika.assistant_turns "
                "WHERE user_id=%s AND created_at>now()-interval '1 hour'",
                (user_id,),
            ).fetchone()
            if budget["busy"]:
                raise HTTPException(429, "Предыдущий ответ ещё готовится. Подождите немного.")
            if budget["count"] >= limit:
                raise HTTPException(429, "Достигнут часовой лимит помощника. Попробуйте позже.")
            conversation_id = payload.conversation_id or uuid4()
            if payload.conversation_id:
                row = self._conversation(db, scope, conversation_id)
                if row["context"] != context:
                    raise HTTPException(409, "Контекст изменился. Начните новый диалог.")
                count = db.execute(
                    "SELECT count(*) AS n FROM chaika.assistant_turns "
                    "WHERE conversation_id=%s AND status='completed'",
                    (conversation_id,),
                ).fetchone()["n"]
                if count >= 10:
                    raise HTTPException(409, "В диалоге уже 10 ответов. Начните новый диалог.")
            else:
                db.execute(
                    "INSERT INTO chaika.assistant_conversations "
                    "(id,user_id,access_hash,context,title) VALUES (%s,%s,%s,%s,%s)",
                    (
                        conversation_id,
                        user_id,
                        access_hash(scope),
                        Jsonb(context),
                        payload.question[:120],
                    ),
                )
            db.execute(
                "INSERT INTO chaika.assistant_turns (id,conversation_id,user_id,question) "
                "VALUES (%s,%s,%s,%s)",
                (payload.request_id, conversation_id, user_id, payload.question),
            )
            return conversation_id, False

    def finish(self, scope, request_id, result=None):
        with self.connection(scope) as db:
            db.execute(
                "UPDATE chaika.assistant_turns SET status=%s,answer=%s,sources=%s,usage=%s,"
                "model=%s,finished_at=now() WHERE id=%s AND user_id=%s AND status='pending'",
                (
                    "completed" if result else "failed",
                    result["answer"] if result else None,
                    Jsonb(result["sources"] if result else []),
                    Jsonb(result["usage"] if result else {}),
                    result["model"] if result else None,
                    request_id,
                    scope.user["id"],
                ),
            )
