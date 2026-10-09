from app.documents import administration, reads
from app.documents.context import runtime_of
from app.documents.database import DocumentDatabase
from app.documents.policy import actor, fail, table
from app.documents.transport import DocumentTransport
from app.documents.workflow import mutate


class DocumentService:
    def __init__(self, settings, *, database=None, provider=None, feature_authorizer=None):
        self.settings = settings
        self.database = database or DocumentDatabase(settings.database_url.get_secret_value())
        runtime = runtime_of(self.database)
        if runtime.mode == "tenant" and settings.dashboard_url != runtime.frontend_origin:
            raise ValueError("Document links must use the tenant frontend origin")
        self.provider = provider or DocumentTransport(settings)
        self.feature_authorizer = feature_authorizer
        self.owner_authorizer = None

    def dispatch(self, portal_id, method, path, *, params=None, payload=None, csv=False):
        parts = path.strip("/").split("/")
        params = params or {}
        if parts[0] == "admin":
            if method == "POST":
                return administration.save_access(self.database, portal_id, int(parts[2]), payload)
            with self.database.connection(readonly=True) as db:
                return administration.staff(db, portal_id)
        kind = parts[0]
        table(kind)
        if method == "POST":
            action = "create" if len(parts) == 1 else parts[2]
            return mutate(
                self.database,
                self.provider,
                portal_id,
                kind,
                action,
                payload,
                int(parts[1]) if len(parts) > 1 else None,
            )
        with self.database.connection(readonly=True) as db:
            user = actor(db, portal_id, kind=kind)
            if len(parts) == 1 or parts[1] == "export":
                return reads.listing(db, user, kind, params, export=csv)
            if parts[1] == "options":
                return reads.options(db, user, kind)
            if parts[1] == "products":
                return reads.suggestions(db, user, kind, params)
            return reads.detail(db, user, kind, int(parts[1]), params)

    def pending(self, telegram_id, kind):
        with self.database.connection(readonly=True) as db:
            user = actor(db, telegram_id=telegram_id, kind=kind)
            stores = reads.stores_for(db, user["id"], kind, "approve")
            field = "counteragent_id" if kind == "waybill" else "store_id"
            condition = f"d.{field}=ANY(%s)"
            values = [stores]
            if kind == "waybill":
                condition = (
                    "((d.counteragent_id=ANY(%s) AND d.receipt_state!='pending_sender') "
                    "OR (d.store_id=ANY(%s) AND d.receipt_state='pending_sender'))"
                )
                values.append(reads.stores_for(db, user["id"], kind, "edit"))
            docs = db.execute(
                reads.joined(kind, db) + f" WHERE {condition} AND d.status='Created' "
                "AND d.submission_state NOT IN ('queued','sending','unknown') ORDER BY d.id "
                "DESC LIMIT 20",
                values,
            ).fetchall()
            return [
                {**reads.serialize(kind, doc), "items": reads.items(db, kind, doc["id"])}
                for doc in docs
            ]

    def bot_action(self, telegram_id, kind, action, document_id, body):
        if action not in {"confirm", "deny", "confirm_receipt", "reject_receipt"}:
            fail(403, "Действие недоступно.")
        return mutate(
            self.database,
            self.provider,
            telegram_id,
            kind,
            action,
            body,
            document_id,
            telegram=True,
        )

    def close(self):
        self.database.close()
