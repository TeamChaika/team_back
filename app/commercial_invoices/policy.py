"Commercial grants for members and verified global owners in their company."

import hashlib
import json

from psycopg.types.json import Jsonb

from app.documents.actors import actor_user, audit, owner_actor, principal_uuid
from app.documents.context import lock_resource
from app.documents.policy import fail, identifier, invalid, profile, warehouse_grant_guard

SECTIONS = {"purchase": "invoices", "sale": "outgoing"}
ACTIONS = {"view", "create", "edit", "submit"}


def actor(db, portal_id, *, kind):
    owner = owner_actor(db, portal_id)
    if owner is not None:
        return actor_user(owner)
    current = profile(db, portal_id)
    if SECTIONS[kind] not in current["sections"]:
        fail(403, "Нет доступа к разделу накладных.")
    user = db.execute(
        (
            "SELECT u.*,l.supabase_id FROM authentication_user u JOIN "
            "portal_documents_userlink l ON l.user_id=u.id WHERE "
            "l.supabase_id=%s"
        ),
        (principal_uuid(portal_id),),
    ).fetchone()
    if not user or not user["is_active"]:
        fail(403, "Администратор должен подключить рабочий профиль и склады.")
    return user


def stores_for(db, user_id, kind, action="view"):
    if action not in ACTIONS:
        return []
    if owner_actor(db, user_id) is not None:
        return [r["id"] for r in db.execute("SELECT id FROM stores").fetchall()]
    return [
        r["store_id"]
        for r in db.execute(
            (
                "SELECT store_id FROM commercial_invoice_grants WHERE user_id=%s "
                "AND kind=%s AND actions @> %s"
                + warehouse_grant_guard("commercial_invoice_grants", db=db)
            ),
            (user_id, kind, Jsonb([action])),
        ).fetchall()
    ]


def require(db, user_id, kind, action, store_id):
    if store_id not in stores_for(db, user_id, kind, action):
        fail(403, "Нет права на это действие для выбранного склада.")


def administration(database, portal_id, *, user_id=None, body=None):
    with database.connection(readonly=body is None) as db:
        profile(db, portal_id, admin=True)
        if body is not None:
            if (
                not isinstance(body, dict)
                or set(body) != {"grants", "request_id", "version"}
                or type(body["version"]) is not int
                or body["version"] < 0
                or not isinstance(body["grants"], list)
                or len(body["grants"]) > 500
            ):
                invalid()
            request_id = identifier(body["request_id"])
            fingerprint = hashlib.sha256(
                json.dumps(
                    {"user_id": user_id, "body": body}, sort_keys=True, allow_nan=False
                ).encode()
            ).hexdigest()
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (lock_resource(db, str(request_id)),),
            )
            previous = db.execute(
                "SELECT portal_id,fingerprint FROM commercial_grant_operations WHERE request_id=%s",
                (request_id,),
            ).fetchone()
            if previous:
                if (
                    str(previous["portal_id"]) != str(principal_uuid(portal_id))
                    or previous["fingerprint"] != fingerprint
                ):
                    fail(409, "Этот request_id уже использован другой командой.")
                return _listing(db)
            # Lock the profile so two complete grant replacements cannot interleave.
            user = db.execute(
                "SELECT id FROM authentication_user WHERE id=%s FOR UPDATE", (user_id,)
            ).fetchone()
            if not user:
                fail(404, "Рабочий профиль не найден.")
            revision = db.execute(
                "SELECT revision FROM commercial_grant_revisions WHERE user_id=%s", (user_id,)
            ).fetchone()
            revision = revision["revision"] if revision else 0
            if body["version"] != revision:
                fail(409, "Права изменены другим администратором. Обновите список.")
            grants, seen = [], set()
            for grant in body["grants"]:
                if not isinstance(grant, dict) or set(grant) != {"kind", "store_id", "actions"}:
                    invalid()
                kind, actions = grant["kind"], grant["actions"]
                store = identifier(grant["store_id"])
                if (
                    not isinstance(kind, str)
                    or kind not in SECTIONS
                    or not isinstance(actions, list)
                    or any(not isinstance(a, str) or a not in ACTIONS for a in actions)
                    or len(set(actions)) != len(actions)
                ):
                    invalid()
                if actions and "view" not in actions:
                    invalid("Любое действие требует просмотра склада.")
                if (kind, store) in seen or not db.execute(
                    "SELECT id FROM stores WHERE id=%s", (store,)
                ).fetchone():
                    invalid()
                seen.add((kind, store))
                grants.append((user_id, kind, store, Jsonb(actions)))
            db.execute("DELETE FROM commercial_invoice_grants WHERE user_id=%s", (user_id,))
            for grant in grants:
                db.execute(
                    (
                        "INSERT INTO "
                        "commercial_invoice_grants(user_id,kind,store_id,actions) "
                        "VALUES(%s,%s,%s,%s)"
                    ),
                    grant,
                )
            db.execute(
                "INSERT INTO commercial_grant_revisions(user_id,revision) VALUES(%s,1) "
                "ON CONFLICT(user_id) DO UPDATE SET revision=commercial_grant_revisions.revision+1",
                (user_id,),
            )
            db.execute(
                "INSERT INTO commercial_grant_operations(request_id,portal_id,fingerprint,user_id) "
                "VALUES(%s,%s,%s,%s)",
                (request_id, principal_uuid(portal_id), fingerprint, user_id),
            )
        if body is not None:
            audit(db, portal_id, "commercial_grants", "user", user_id)
        return _listing(db)


def _listing(db):
    users = db.execute(
        "SELECT "
        "u.id,u.username,u.first_name,u.last_name,u.is_active,"
        "l.supabase_id,coalesce(r.revision,0) AS revision FROM authentication_user u LEFT JOIN "
        "portal_documents_userlink l ON l.user_id=u.id LEFT JOIN "
        "commercial_grant_revisions r ON r.user_id=u.id ORDER BY "
        "u.username"
    ).fetchall()
    grants = db.execute(
        "SELECT user_id,kind,store_id,actions FROM "
        "commercial_invoice_grants ORDER BY user_id,kind,store_id"
    ).fetchall()
    return {
        "users": [
            {
                **u,
                "supabase_id": str(u["supabase_id"]) if u["supabase_id"] else None,
                "grants": [
                    {"kind": g["kind"], "store_id": str(g["store_id"]), "actions": g["actions"]}
                    for g in grants
                    if g["user_id"] == u["id"]
                ],
            }
            for u in users
        ],
        "stores": [
            {"id": str(s["id"]), "name": s["name"]}
            for s in db.execute("SELECT id,name FROM stores ORDER BY name").fetchall()
        ],
    }
