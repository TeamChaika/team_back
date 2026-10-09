"""Private operator-only first-payment path; never registered as an HTTP route."""

import hmac
import secrets
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from app.saas_admin.entitlements import evaluate_feature
from app.saas_admin.repository import Problem
from app.saas_admin.runtime_registry import RuntimeRegistry
from app.tenant_payments.models import PaymentPrincipal
from app.tenant_payments.store import digest, serial


class OwnerAcceptanceAuthority:
    """Reuses the delegated owner handle and its freshly verified central parent."""

    def __init__(self, operator, *, registry=None):
        self.operator = operator
        self.registry = registry or RuntimeRegistry(operator.repo)

    def verify(self, *, creation=True):
        operator = self.operator
        runtime = operator.runtime
        path = Path(operator.config.get("acceptance_session_file", ""))
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise HTTPException(403, "Требуется закрытый файл сессии приёмки владельца.")
        root = operator.config.get("acceptance_root")
        if root and path != Path(root) / (runtime.key + ".acceptance"):
            raise HTTPException(403, "Файл сессии принадлежит другой компании.")
        token = path.read_text().strip()
        try:
            actor, session = operator.repo.tenant_actor_session(token, str(runtime.company_id))
            company = operator.current(str(runtime.company_id), runtime.configuration_version)
        except Problem as exc:
            raise HTTPException(exc.status, exc.message) from None
        except ValueError:
            raise HTTPException(409, "Конфигурация компании изменилась.") from None
        if (
            actor.kind != "platform_owner"
            or actor.company_id != runtime.company_id
            or session.get("must_change_password")
            or session.get("must_change")
        ):
            raise HTTPException(403, "Приёмка доступна только действующему владельцу платформы.")
        if creation:
            if self.registry.resolve_setup(company) is None:
                raise HTTPException(409, "Предварительные этапы настройки не подтверждены.")
            if not evaluate_feature(
                company,
                "payments.create",
                operation="create",
                staff_allowed=True,
                warehouse_allowed=True,
            ).allowed:
                raise HTTPException(403, "Создание оплаты недоступно по подписке.")
        return PaymentPrincipal(actor, True, (), True), digest(token)


class OwnerPaymentAcceptance:
    def __init__(self, service, authority):
        if service.store.runtime != authority.operator.runtime:
            raise ValueError("Acceptance authority and payment store must share a runtime")
        self.service, self.store, self.authority = service, service.store, authority

    def create_intent(self, *, terminal_id, amount_minor, mode, request_id, confirmed=False):
        if confirmed is not True:
            raise HTTPException(400, "Необходимо явное подтверждение создания оплаты.")
        if (
            mode not in {"sandbox", "live"}
            or type(amount_minor) is not int
            or not (100 <= amount_minor <= 214748364700 and amount_minor % 100 == 0)
        ):
            raise HTTPException(422, "Укажите режим и положительную сумму в целых рублях.")
        terminal_id, request_id = UUID(str(terminal_id)), UUID(str(request_id))
        principal, session_hash = self.authority.verify()
        runtime = self.store.runtime
        with self.store.connection() as db:
            # Serialize the request before allocating the synthetic acceptance deposit.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(request_id),))
            old = self.store.execute(
                db,
                "SELECT * FROM {payments}.acceptance_intents WHERE request_id=%s",
                (request_id,),
            ).fetchone()
            if old:
                self._bound(old, principal, session_hash=session_hash)
                if (old["terminal_id"], old["amount_minor"], old["mode"]) != (
                    terminal_id,
                    amount_minor,
                    mode,
                ):
                    raise HTTPException(409, "Подтверждение с этим ключом уже сохранено.")
                return serial(old)
            terminal = self.store.execute(
                db,
                "SELECT t.id,t.venue_id,t.current_version_id,v.mode,v.currency "
                "FROM {payments}.terminals t JOIN {payments}.terminal_versions v "
                "ON v.id=t.current_version_id JOIN {payments}.venues n ON n.id=t.venue_id "
                "WHERE t.id=%s AND t.active AND n.active FOR SHARE OF t,n",
                (terminal_id,),
            ).fetchone()
            if not terminal or terminal["mode"] != mode:
                raise HTTPException(409, "Терминал недоступен или выбран другой режим.")
            intent_id, deposit_id, guest = uuid4(), uuid4(), secrets.token_urlsafe(32)
            self.store.execute(
                db,
                "INSERT INTO {payments}.deposits(id,request_id,fingerprint,venue_id,"
                "customer_name,phone,amount_minor,currency,created_by,actor_kind,"
                "guest_token_hash,encrypted_guest_token,notes) "
                "VALUES(%s,%s,%s,%s,%s,'0',%s,%s,%s,'platform_owner',%s,%s,%s)",
                (
                    deposit_id,
                    request_id,
                    digest(str(intent_id)),
                    terminal["venue_id"],
                    "Приёмка оплаты владельцем",
                    amount_minor,
                    terminal["currency"],
                    principal.actor.auth_user_id,
                    digest(guest),
                    self.store.vault.encrypt(guest),
                    "Контрольная оплата: " + mode,
                ),
            )
            row = self.store.execute(
                db,
                "INSERT INTO {payments}.acceptance_intents(id,company_id,configuration_version,"
                "actor_id,session_hash,terminal_id,terminal_version_id,deposit_id,request_id,"
                "amount_minor,currency,mode) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    intent_id,
                    runtime.company_id,
                    runtime.configuration_version,
                    principal.actor.auth_user_id,
                    session_hash,
                    terminal_id,
                    terminal["current_version_id"],
                    deposit_id,
                    request_id,
                    amount_minor,
                    terminal["currency"],
                    mode,
                ),
            ).fetchone()
            self.store.audit(
                db,
                principal,
                "payment.acceptance_confirmed",
                intent_id,
                {"mode": mode, "amount_minor": amount_minor},
            )
            return serial(row)

    def _bound(self, row, principal, *, session_hash=None, allow_prior_version=False):
        if not row:
            raise HTTPException(404, "Подтверждение приёмки не найдено.")
        if (
            row["company_id"] != self.store.runtime.company_id
            or (
                not allow_prior_version
                and row["configuration_version"] != self.store.runtime.configuration_version
            )
            or row["actor_id"] != principal.actor.auth_user_id
        ):
            raise HTTPException(403, "Подтверждение другой компании, версии или владельца.")
        if session_hash is not None and not hmac.compare_digest(row["session_hash"], session_hash):
            raise HTTPException(403, "Подтверждение выдано другой сессии владельца.")

    def _load(
        self, db, intent_id, principal, *, lock=False, session_hash=None, allow_prior_version=False
    ):
        row = self.store.execute(
            db,
            "SELECT * FROM {payments}.acceptance_intents WHERE id=%s"
            + (" FOR UPDATE" if lock else ""),
            (intent_id,),
        ).fetchone()
        self._bound(
            row, principal, session_hash=session_hash, allow_prior_version=allow_prior_version
        )
        return row

    def _terminal(self, db, row):
        terminal = self.store.execute(
            db,
            "SELECT v.encrypted_key,v.mode,v.currency,t.venue_id "
            "FROM {payments}.terminals t JOIN {payments}.terminal_versions v "
            "ON v.id=t.current_version_id JOIN {payments}.venues n ON n.id=t.venue_id "
            "WHERE t.id=%s AND t.current_version_id=%s AND t.active AND n.active "
            "FOR SHARE OF t,n",
            (row["terminal_id"], row["terminal_version_id"]),
        ).fetchone()
        if not terminal or (terminal["mode"], terminal["currency"]) != (
            row["mode"],
            row["currency"],
        ):
            raise HTTPException(409, "Версия или состояние терминала изменились.")
        deposit = self.store._deposit(db, row["deposit_id"], lock=True)
        if (deposit["venue_id"], deposit["amount_minor"], deposit["currency"]) != (
            terminal["venue_id"],
            row["amount_minor"],
            row["currency"],
        ):
            raise HTTPException(409, "Сохранённый депозит изменился.")
        return terminal, deposit

    def consume(self, intent_id):
        principal, session_hash = self.authority.verify()
        with self.store.connection() as db:
            row = self._load(db, intent_id, principal, lock=True, session_hash=session_hash)
            if row["consumed_at"]:
                return row, None
            terminal, deposit = self._terminal(db, row)
            if (
                deposit["status"] == "paid"
                or self.store.execute(
                    db,
                    "SELECT 1 FROM {payments}.attempts WHERE deposit_id=%s",
                    (row["deposit_id"],),
                ).fetchone()
            ):
                raise HTTPException(409, "Для депозита уже существует попытка оплаты.")
            attempt_id, callback = uuid4(), secrets.token_urlsafe(32)
            self.store.execute(
                db,
                "INSERT INTO {payments}.attempts(id,deposit_id,request_id,terminal_id,"
                "terminal_version_id,amount_minor,currency,state,"
                "callback_token_hash,next_check_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,'creating',%s,now()+interval '30 seconds')",
                (
                    attempt_id,
                    row["deposit_id"],
                    row["request_id"],
                    row["terminal_id"],
                    row["terminal_version_id"],
                    row["amount_minor"],
                    row["currency"],
                    digest(callback),
                ),
            )
            row = self.store.execute(
                db,
                "UPDATE {payments}.acceptance_intents SET consumed_at=now(),attempt_id=%s "
                "WHERE id=%s RETURNING *",
                (attempt_id, intent_id),
            ).fetchone()
            self.store.audit(
                db,
                principal,
                "payment.acceptance_consumed",
                intent_id,
                {"attempt_id": str(attempt_id)},
            )
            guest = self.store.vault.decrypt(deposit["encrypted_guest_token"])
            context = {
                "api_key": self.store.vault.decrypt(terminal["encrypted_key"]),
                "mode": row["mode"],
                "terminal_version_id": row["terminal_version_id"],
                "amount_minor": row["amount_minor"],
                "currency": row["currency"],
                "callback": callback,
                "redirect_url": f"{self.store.runtime.frontend_origin}/deposit/{row['deposit_id']}?"
                + urlencode({"token": guest}),
            }
        return row, context

    def reauthorize(self, intent_id):
        principal, session_hash = self.authority.verify()
        with self.store.connection() as db:
            row = self._load(db, intent_id, principal, session_hash=session_hash)
            self._terminal(db, row)

    async def execute(self, intent_id):
        intent_id = UUID(str(intent_id))
        row, context = await run_in_threadpool(self.consume, intent_id)
        if context:

            async def authorize():
                await run_in_threadpool(self.reauthorize, intent_id)

            await self.service.create_reserved(
                {"id": str(row["attempt_id"])}, context, row["request_id"], authorize
            )
        return await self.reconcile(intent_id)

    async def reconcile(self, intent_id):
        # Persisted operations remain reconcilable when payments.create is unavailable.
        principal, _ = await run_in_threadpool(self.authority.verify, creation=False)
        with self.store.connection() as db:
            row = self._load(db, UUID(str(intent_id)), principal, allow_prior_version=True)
        if row["attempt_id"]:
            await self.service.reconcile(row["attempt_id"])
        deposit = await run_in_threadpool(self.store.get, principal, row["deposit_id"])
        return {
            "intent_id": str(row["id"]),
            "attempt_id": str(row["attempt_id"]) if row["attempt_id"] else None,
            "mode": row["mode"],
            "company_id": str(row["company_id"]),
            "configuration_version": row["configuration_version"],
            "deposit": {key: deposit[key] for key in ("id", "amount_minor", "currency", "status")},
            "payment": await run_in_threadpool(self._payment_summary, row["attempt_id"]),
        }

    def _payment_summary(self, attempt_id):
        if not attempt_id:
            return None
        with self.store.connection() as db:
            row = self.store.execute(
                db,
                "SELECT state,diagnostic,payment_url,qr_image FROM {payments}.attempts WHERE id=%s",
                (attempt_id,),
            ).fetchone()
        return serial(row)
