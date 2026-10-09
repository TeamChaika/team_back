"""Transactional drafts, immutable submission snapshots and scoped reads."""

import hashlib
import json
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.commercial_invoices.calculation import VAT_RATES
from app.commercial_invoices.drafts import build_snapshot, counterparty_catalog, product_catalog
from app.commercial_invoices.policy import actor, require, stores_for
from app.documents.policy import fail, full_name, identifier, invalid
from app.documents.product_search import search_products

KINDS = {"purchase", "sale"}
EDITABLE = {"draft", "rejected"}


def check_kind(kind):
    if kind not in KINDS:
        fail(404, "Тип документа не найден.")


class CommercialInvoiceService:
    def __init__(
        self,
        database,
        provider,
        *,
        seller=None,
        submit_enabled=False,
        counterparty_enabled=False,
        counterparty_provider=None,
    ):
        self.database, self.provider = database, provider
        self.seller = seller or {}
        self.submit_enabled = submit_enabled
        self.counterparty_enabled = counterparty_enabled
        self.counterparty_provider = counterparty_provider

    def _actor(self, db, portal_id, kind):
        check_kind(kind)
        return actor(db, portal_id, kind=kind)

    def _serialize(self, db, user, row):
        snapshot = row["snapshot"]
        editable = row["state"] in EDITABLE
        can_edit = editable and row["store_id"] in stores_for(db, user["id"], row["kind"], "edit")
        can_submit = editable and row["store_id"] in stores_for(
            db, user["id"], row["kind"], "submit"
        )
        return {
            **snapshot,
            "id": str(row["id"]),
            "kind": row["kind"],
            "number": row["number"],
            "version": row["version"],
            "state": row["state"],
            "iiko_id": str(row["iiko_id"]) if row.get("iiko_id") else None,
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
            "can_edit": can_edit,
            "can_submit": can_submit and self.submit_enabled,
            "can_pdf": row["kind"] == "sale",
        }

    def _get(self, db, user, kind, document_id, *, lock=False):
        row = db.execute(
            "SELECT * FROM commercial_invoices WHERE id=%s AND kind=%s"
            + (" FOR UPDATE" if lock else ""),
            (identifier(document_id), kind),
        ).fetchone()
        if not row or row["store_id"] not in stores_for(db, user["id"], kind, "view"):
            fail(404, "Документ не найден или недоступен.")
        return row

    def options(self, db, user, kind):
        from app.commercial_invoices.counterparties import permitted, serialize

        visible = stores_for(db, user["id"], kind, "view")
        create = stores_for(db, user["id"], kind, "create")
        edit = stores_for(db, user["id"], kind, "edit")
        submit = stores_for(db, user["id"], kind, "submit")
        stores = db.execute(
            "SELECT id,name FROM stores WHERE id=ANY(%s) ORDER BY name", (visible,)
        ).fetchall()
        return {
            "can_create_counterparty": self.counterparty_enabled
            and permitted(db, user["id"], kind),
            "counterparty_operations": [
                serialize(db, row)["operation"]
                for row in db.execute(
                    "SELECT * FROM commercial_counterparty_operations "
                    "WHERE actor_id=%s AND portal_id=%s AND kind=%s "
                    "AND (state NOT IN ('confirmed','rejected') "
                    "OR created_at>now()-interval '1 day') "
                    "ORDER BY created_at DESC LIMIT 10",
                    (user["id"], user["supabase_id"], kind),
                ).fetchall()
            ]
            if self.counterparty_enabled
            else [],
            "stores": [
                {
                    "id": str(s["id"]),
                    "name": s["name"],
                    "can_create": s["id"] in create,
                    "can_edit": s["id"] in edit,
                    "can_submit": self.submit_enabled and s["id"] in submit,
                }
                for s in stores
            ],
            "can_create": bool(set(visible) & set(create)),
            "can_submit": self.submit_enabled and bool(set(visible) & set(submit)),
            "seller": self.seller,
            "vat_rates": list(VAT_RATES),
            "submit_enabled": self.submit_enabled,
        }

    def dispatch(
        self, portal_id, method, kind, document_id=None, action=None, *, payload=None, params=None
    ):
        check_kind(kind)
        if method == "POST":
            return self.mutate(portal_id, kind, document_id, action or "create", payload)
        params = params or {}
        with self.database.connection(readonly=True) as db:
            user = self._actor(db, portal_id, kind)
            if action == "options":
                return self.options(db, user, kind)
            if action in {"products", "counterparties"}:
                if not stores_for(db, user["id"], kind, "view"):
                    fail(403, "Сначала получите доступ к складу.")
                if action == "products":
                    result = search_products(
                        product_catalog(db), params.get("q", params.get("query", ""))
                    )
                    return {"items": result["rows"], "total": result["total"]}
                query = params.get("q", params.get("query", "")).strip().casefold()
                if len(query) > 200:
                    invalid()
                catalog = (
                    product_catalog(db) if action == "products" else counterparty_catalog(db, kind)
                )
                matches = [
                    r
                    for r in catalog
                    if query in r["name"].casefold() or query in str(r.get("inn", ""))
                ]
                return {"items": sorted(matches, key=lambda r: r["name"])[:50]}
            if document_id is not None:
                row = self._get(db, user, kind, document_id)
                document = self._serialize(db, user, row)
                document["history"] = [
                    {
                        "action": e["action"],
                        "version": e["version"],
                        "actor_id": e["actor_id"],
                        "actor_name": full_name(e) if e["actor_id"] is not None else "Система",
                        "created_at": e["created_at"].isoformat(),
                    }
                    for e in db.execute(
                        (
                            "SELECT e.action,e.version,e.created_at,e.actor_id,"
                            "u.first_name,u.last_name,u.username FROM commercial_invoice_events e "
                            "LEFT JOIN authentication_user u ON u.id=e.actor_id "
                            "WHERE e.document_id=%s ORDER BY e.id"
                        ),
                        (row["id"],),
                    ).fetchall()
                ]
                if action == "pdf":
                    if kind != "sale":
                        fail(404, "Счёт доступен для реализации.")
                    from app.commercial_invoices.pdf import render_invoice_pdf

                    try:
                        return render_invoice_pdf(document)
                    except ValueError:
                        fail(503, "Проверьте сохранённые реквизиты и суммы счёта перед печатью.")
                return {"document": document}
            try:
                limit = min(100, max(1, int(params.get("limit", 50))))
                offset = max(0, int(params.get("offset", 0)))
            except (ValueError, TypeError):
                invalid()
            stores = stores_for(db, user["id"], kind, "view")
            rows = db.execute(
                (
                    "SELECT * FROM commercial_invoices WHERE kind=%s AND "
                    "store_id=ANY(%s) ORDER BY created_at DESC,id DESC LIMIT %s "
                    "OFFSET %s"
                ),
                (kind, stores, limit, offset),
            ).fetchall()
            total = db.execute(
                "SELECT count(*) AS n FROM commercial_invoices WHERE kind=%s AND store_id=ANY(%s)",
                (kind, stores),
            ).fetchone()["n"]
            return {"items": [self._serialize(db, user, row) for row in rows], "total": total}

    def mutate(self, portal_id, kind, document_id, action, body):
        if action not in {"create", "edit", "submit"} or not isinstance(body, dict):
            invalid()
        request_id = identifier(body.get("request_id"))
        fingerprint = hashlib.sha256(
            json.dumps(
                {"kind": kind, "id": str(document_id), "action": action, "body": body},
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        with self.database.connection() as db:
            user = self._actor(db, portal_id, kind)
            # Serialize only this request key; replay cannot race a concurrent insert.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(request_id),))
            previous = db.execute(
                "SELECT * FROM commercial_invoice_operations WHERE request_id=%s", (request_id,)
            ).fetchone()
            if previous:
                if previous["actor_id"] != user["id"] or previous["fingerprint"] != fingerprint:
                    fail(409, "Этот request_id уже использован другой командой.")
                self._get(db, user, kind, previous["document_id"])
                return previous["result"]
            if action == "create":
                snapshot = build_snapshot(db, user, kind, body, self.seller, action=action)
                number = db.execute(
                    "SELECT nextval('commercial_invoice_number_seq') AS n"
                ).fetchone()["n"]
                document_id = uuid4()
                number = ("CI-P-" if kind == "purchase" else "CI-S-") + f"{number:08d}"
                row = db.execute(
                    (
                        "INSERT INTO "
                        "commercial_invoices(id,kind,number,store_id,"
                        "created_by_id,snapshot) VALUES(%s,%s,%s,%s,%s,%s) "
                        "RETURNING *"
                    ),
                    (
                        document_id,
                        kind,
                        number,
                        identifier(snapshot["store_id"]),
                        user["id"],
                        Jsonb(snapshot),
                    ),
                ).fetchone()
            else:
                row = self._get(db, user, kind, document_id, lock=True)
                if type(body.get("version")) is not int or body["version"] != row["version"]:
                    fail(409, "Документ изменён. Откройте актуальную версию.")
                require(
                    db, user["id"], kind, "edit" if action == "edit" else "submit", row["store_id"]
                )
                if row["state"] not in EDITABLE:
                    fail(
                        409,
                        "Документ уже отправляется или отправлен. Повторная отправка запрещена.",
                    )
                if action == "edit":
                    snapshot = build_snapshot(db, user, kind, body, self.seller, action=action)
                    require(db, user["id"], kind, "edit", identifier(snapshot["store_id"]))
                    row = db.execute(
                        (
                            "UPDATE commercial_invoices SET "
                            "snapshot=%s,store_id=%s,version=version+1,"
                            "state='draft',updated_at=now() WHERE id=%s RETURNING *"
                        ),
                        (Jsonb(snapshot), identifier(snapshot["store_id"]), row["id"]),
                    ).fetchone()
                else:
                    if set(body) != {"request_id", "version"}:
                        invalid()
                    if not self.submit_enabled:
                        fail(503, "Отправка в iiko ещё не включена: требуется проверка интеграции.")
                    snapshot = self._serialize(db, user, row)
                    snapshot.update(version=row["version"] + 1, state="queued")
                    payload = self.provider.payload(kind, snapshot)
                    row = db.execute(
                        (
                            "UPDATE commercial_invoices SET "
                            "state='queued',version=version+1,updated_at=now() WHERE id=%s "
                            "RETURNING *"
                        ),
                        (row["id"],),
                    ).fetchone()
                    db.execute(
                        (
                            "INSERT INTO "
                            "commercial_invoice_dispatch(document_id,version,payload) "
                            "VALUES(%s,%s,%s) ON CONFLICT(document_id) DO UPDATE SET "
                            "version=EXCLUDED.version,payload=EXCLUDED.payload,"
                            "state='ready',attempts=0,claim_id=NULL,claimed_at=NULL,"
                            "next_attempt_at=now(),last_error=NULL,updated_at=now() "
                            "WHERE commercial_invoice_dispatch.state='rejected'"
                        ),
                        (row["id"], row["version"], Jsonb({"xml": payload.decode("utf-8")})),
                    )
            db.execute(
                (
                    "INSERT INTO "
                    "commercial_invoice_revisions(document_id,version,snapshot) "
                    "VALUES(%s,%s,%s)"
                ),
                (row["id"], row["version"], Jsonb(self._serialize(db, user, row))),
            )
            db.execute(
                (
                    "INSERT INTO "
                    "commercial_invoice_events(document_id,version,actor_id,action) "
                    "VALUES(%s,%s,%s,%s)"
                ),
                (row["id"], row["version"], user["id"], action),
            )
            result = {"document": self._serialize(db, user, row)}
            db.execute(
                (
                    "INSERT INTO "
                    "commercial_invoice_operations(request_id,actor_id,"
                    "fingerprint,document_id,result) VALUES(%s,%s,%s,%s,%s)"
                ),
                (request_id, user["id"], fingerprint, row["id"], Jsonb(result)),
            )
            return result
