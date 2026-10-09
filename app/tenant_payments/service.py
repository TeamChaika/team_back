"""Reserve -> provider -> verified atomic settlement, with durable reconciliation."""

from urllib.parse import urlencode
from uuid import UUID

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from app.tenant_payments.provider import CreationRejected, CreationUnknown, VerificationUnavailable


class TenantPayments:
    def __init__(self, store, provider_factory, *, feature_authorizer=None):
        self.store = store
        self.feature_authorizer = feature_authorizer
        # Runtime chooses a supported mode explicitly; request data cannot set API hosts.
        self.provider_factory = provider_factory

    async def require_creation(self):
        from app.saas_admin.repository import Problem

        if self.store.runtime.mode != "tenant":
            return
        if self.feature_authorizer is None:
            raise HTTPException(503, "Проверка подписки недоступна.")
        try:
            allowed = await run_in_threadpool(self.feature_authorizer, "payments.create")
        except Problem as exc:
            raise HTTPException(exc.status, exc.message) from None
        if not allowed:
            raise HTTPException(403, "Создание оплаты недоступно по подписке.")

    async def validate_terminal(self, principal, venue_id, terminal_id):
        context = await run_in_threadpool(
            self.store.terminal_context, principal, venue_id, terminal_id
        )
        try:
            verified = await self.provider_factory(context["mode"]).check_terminal(
                api_key=context["api_key"]
            )
        except (VerificationUnavailable, ValueError):
            raise HTTPException(
                503, "Не удалось проверить терминал у платёжного сервиса."
            ) from None
        return await run_in_threadpool(
            self.store.record_terminal_check, context["id"], verified, principal=principal
        )

    async def prepare(self, deposit_id: UUID, token: str, request_id: UUID):
        # Validate the stored company deposit/capability before consulting policy.
        await run_in_threadpool(self.store.guest, deposit_id, token)
        await self.require_creation()
        attempt, context = await run_in_threadpool(
            self.store.reserve, deposit_id, token, request_id
        )
        if context:
            await self.create_reserved(attempt, context, request_id, self.require_creation)
        return await run_in_threadpool(self.store.guest, deposit_id, token)

    async def create_reserved(self, attempt, context, request_id, authorizer):
        """Transport for an already committed attempt, under a fresh scoped authorizer."""
        attempt_id = UUID(attempt["id"])
        callback = (
            f"{self.store.runtime.api_origin}/api/payment-callbacks/{attempt_id}?"
            + urlencode({"token": context["callback"]})
        )
        try:
            await authorizer()
        except HTTPException:
            # No provider bytes have been sent: persist a definite local rejection.
            await run_in_threadpool(self.store.record_creation, attempt_id, rejected=True)
            raise
        try:
            provider = self.provider_factory(context["mode"])
            verified = await provider.check_terminal(api_key=context["api_key"])
            checked = await run_in_threadpool(
                self.store.record_terminal_check,
                context["terminal_version_id"],
                verified,
                attempt_id=attempt_id,
            )
            if not checked["ready"]:
                raise CreationRejected("Terminal subscription or receipt configuration unavailable")
            await authorizer()
            created = await provider.create(
                api_key=context["api_key"],
                amount_minor=context["amount_minor"],
                currency=context["currency"],
                request_id=request_id,
                notification_url=callback,
                redirect_url=context["redirect_url"],
            )
        except HTTPException:
            await run_in_threadpool(self.store.record_creation, attempt_id, rejected=True)
            raise
        except CreationRejected:
            await run_in_threadpool(self.store.record_creation, attempt_id, rejected=True)
        except CreationUnknown:
            await run_in_threadpool(self.store.record_creation, attempt_id)
        except (ValueError, VerificationUnavailable):
            # Terminal validation failed before any creation bytes were sent.
            await run_in_threadpool(self.store.record_creation, attempt_id, rejected=True)
        else:
            await run_in_threadpool(self.store.record_creation, attempt_id, created)

    async def reconcile(self, attempt_id: UUID, *, leased=False):
        if not leased and not await run_in_threadpool(self.store.lease_check, attempt_id):
            return None
        context = await run_in_threadpool(self.store.check_context, attempt_id)
        if context["state"] == "paid":
            return "paid"
        if not context["operation_id"]:
            return await run_in_threadpool(
                self.store.record_check, attempt_id, diagnostic="creation_unknown"
            )
        try:
            provider = self.provider_factory(context["mode"])
            if context["terminal_check_id"] is None:
                verified = await provider.check_terminal(api_key=context["api_key"])
                await run_in_threadpool(
                    self.store.record_terminal_check,
                    context["terminal_version_id"],
                    verified,
                    attempt_id=attempt_id,
                )
            result = await provider.check(
                api_key=context["api_key"], operation_id=context["operation_id"]
            )
        except (VerificationUnavailable, ValueError):
            return await run_in_threadpool(
                self.store.record_check, attempt_id, diagnostic="provider_unavailable"
            )
        return await run_in_threadpool(self.store.record_check, attempt_id, result)

    async def callback(self, attempt_id: UUID, token: str, operation_id: UUID):
        await run_in_threadpool(self.store.callback, attempt_id, token, operation_id)
        state = await self.reconcile(attempt_id)
        # This means receipt accepted, not paid; only independently verified status settles.
        return {"accepted": True, "state": state or "unchanged"}

    async def refresh_guest(self, deposit_id: UUID, token: str):
        await run_in_threadpool(self.store.guest, deposit_id, token)
        attempt_id = await run_in_threadpool(self.store.latest_attempt, deposit_id)
        if attempt_id:
            await self.reconcile(attempt_id)
        return await run_in_threadpool(self.store.guest, deposit_id, token)

    async def reconcile_due(self):
        checked = 0
        for attempt_id in await run_in_threadpool(self.store.due):
            try:
                await self.reconcile(attempt_id, leased=True)
            except HTTPException:
                continue
            checked += 1
        return checked


def documented_provider(mode):
    """Origins are pinned to documented sandbox and the existing production integration."""
    from app.tenant_payments.provider import QRManagerProvider

    return QRManagerProvider(mode=mode)
