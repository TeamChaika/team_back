from uuid import uuid4

from psycopg import IntegrityError
from psycopg.types.json import Jsonb

from app.documents.policy import ACTIONS, fail, identifier, invalid, profile


def staff(db, portal_id):
    profile(db, portal_id, admin=True)
    rows = db.execute(
        "SELECT u.id,coalesce(nullif(trim(u.first_name || ' ' || u.last_name),''),u.username) "
        "AS name, "
        "u.username,u.is_active AS active,u.telegram_id IS NOT NULL AS telegram_linked, "
        "l.supabase_id,coalesce(l.revision,0) AS revision FROM authentication_user u "
        "LEFT JOIN portal_documents_userlink l ON l.user_id=u.id ORDER BY u.username"
    ).fetchall()
    grants = {}
    for row in db.execute(
        "SELECT g.user_id,g.kind,g.store_id,g.actions FROM portal_documents_grant g "
        "JOIN stores s ON s.id=g.store_id ORDER BY g.kind,s.name"
    ).fetchall():
        grants.setdefault(row.pop("user_id"), []).append(row)
    for row in rows:
        row["grants"] = grants.get(row["id"], [])
    return {
        "rows": rows,
        "stores": db.execute("SELECT id,name FROM stores ORDER BY name").fetchall(),
    }


def save_access(database, admin_id, user_id, payload):
    if not isinstance(payload, dict) or set(payload) - {
        "revision",
        "supabase_id",
        "active",
        "grants",
        "name",
        "telegram_id",
    }:
        invalid()
    revision = payload.get("revision")
    if type(revision) is not int or revision < 0 or type(payload.get("active")) is not bool:
        invalid()
    portal_id = identifier(payload["supabase_id"]) if payload.get("supabase_id") else None
    rows = payload.get("grants")
    if not isinstance(rows, list) or len(rows) > 500:
        invalid()
    grants, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"kind", "store_id", "actions"}:
            invalid()
        kind, actions = row["kind"], row["actions"]
        if (
            not isinstance(kind, str)
            or kind not in ACTIONS
            or not isinstance(actions, list)
            or any(not isinstance(a, str) for a in actions)
            or set(actions) - ACTIONS[kind]
            or (actions and "view" not in actions)
        ):
            invalid("Выберите просмотр и нужные действия для каждого склада.")
        store = identifier(row["store_id"])
        if (kind, store) in seen:
            invalid("Повторное назначение склада.")
        seen.add((kind, store))
        if actions:
            grants.append({"kind": kind, "store_id": store, "actions": sorted(set(actions))})
    telegram_id = payload.get("telegram_id")
    if telegram_id is not None and (type(telegram_id) is not int or not 0 < telegram_id < 2**63):
        invalid("Telegram ID должен быть положительным целым числом.")
    name = payload.get("name", "")
    if not isinstance(name, str) or len(name) > 150:
        invalid()
    try:
        with database.connection() as db:
            profile(db, admin_id, admin=True)
            if (
                portal_id
                and not db.execute(
                    "SELECT 1 FROM portal_access WHERE id=%s", (portal_id,)
                ).fetchone()
            ):
                invalid("Сначала создайте сотрудника в dashboard.")
            for _, store in seen:
                if not db.execute("SELECT 1 FROM stores WHERE id=%s", (store,)).fetchone():
                    invalid("Неизвестный склад.")
            if portal_id:
                db.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("link:" + str(portal_id),),
                )
                if db.execute(
                    "SELECT 1 FROM portal_documents_userlink WHERE supabase_id=%s AND user_id<>%s",
                    (portal_id, user_id),
                ).fetchone():
                    fail(409, "Аккаунт dashboard уже привязан к другому рабочему профилю.")
            if user_id == 0:
                if not portal_id or revision != 0:
                    invalid("Для нового профиля выберите аккаунт dashboard.")
                user_id = db.execute(
                    "INSERT INTO authentication_user "
                    "(password,is_superuser,username,first_name,last_name,email,"
                    "is_staff,is_active,date_joined) VALUES "
                    "(%s,false,%s,%s,'','',false,true,now()) RETURNING id",
                    ("!" + uuid4().hex, "portal_" + portal_id.hex, name),
                ).fetchone()["id"]
            user = db.execute(
                "SELECT id FROM authentication_user WHERE id=%s FOR UPDATE", (user_id,)
            ).fetchone()
            if not user:
                fail(404, "Рабочий профиль не найден.")
            link = db.execute(
                "SELECT revision FROM portal_documents_userlink WHERE user_id=%s FOR UPDATE",
                (user_id,),
            ).fetchone()
            current = link["revision"] if link else 0
            if current != revision:
                fail(409, "Права изменены другим администратором. Обновите список.")
            db.execute(
                "INSERT INTO portal_documents_userlink (user_id,supabase_id,revision) VALUES "
                "(%s,%s,%s) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "supabase_id=excluded.supabase_id,revision=excluded.revision",
                (user_id, portal_id, current + 1),
            )
            db.execute(
                "UPDATE authentication_user SET is_active=%s WHERE id=%s",
                (payload["active"], user_id),
            )
            if "telegram_id" in payload:
                db.execute(
                    "UPDATE authentication_user SET telegram_id=%s WHERE id=%s",
                    (telegram_id, user_id),
                )
            db.execute("DELETE FROM portal_documents_grant WHERE user_id=%s", (user_id,))
            for grant in grants:
                db.execute(
                    "INSERT INTO portal_documents_grant (user_id,kind,store_id,actions) VALUES "
                    "(%s,%s,%s,%s)",
                    (user_id, grant["kind"], grant["store_id"], Jsonb(grant["actions"])),
                )
            db.execute(
                "INSERT INTO portal_documents_accessevent "
                "(actor_supabase_id,user_id,revision,data,created_at) "
                "VALUES (%s,%s,%s,%s,now())",
                (
                    admin_id,
                    user_id,
                    current + 1,
                    Jsonb(
                        {
                            "active": payload["active"],
                            "supabase_id": str(portal_id) if portal_id else None,
                            "grants": [{**g, "store_id": str(g["store_id"])} for g in grants],
                        }
                    ),
                ),
            )
            return {"id": user_id, "revision": current + 1}
    except IntegrityError:
        fail(409, "Этот аккаунт или Telegram ID уже используется.")
