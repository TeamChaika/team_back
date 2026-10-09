"""Company payments use a separate restricted DSN and freshly verified portal actors."""

import os
from uuid import uuid4

from fastapi import HTTPException

from app.saas_admin.vault import Vault
from app.tenancy.actor import ActorContext
from app.tenancy.config import load_runtime
from app.tenancy.sql import render
from app.tenant_payments.models import PaymentPrincipal, TerminalInput, VenueInput
from app.tenant_payments.service import TenantPayments, documented_provider
from app.tenant_payments.store import PaymentStore


def principal_from_scope(runtime, scope):
    actor = getattr(scope, "actor", None)
    if not isinstance(actor, ActorContext) or actor.company_id != runtime.company_id:
        raise HTTPException(403, "Проверенный субъект этой компании обязателен.")
    owner = actor.kind == "platform_owner"
    if not owner and actor.membership_id is None:
        raise HTTPException(403, "Активное членство компании обязательно.")
    principal = PaymentPrincipal(
        actor=actor,
        active=owner or scope.user.get("active") is True,
        sections=tuple(scope.user.get("sections", ())),
        can_manage=owner or scope.user.get("is_portal_admin") is True,
        warehouse_restricted=scope.warehouse_restricted,
        profile_revision=None if owner else scope.user.get("revision"),
    )
    if not owner and (
        type(principal.profile_revision) is not int or principal.profile_revision < 1
    ):
        raise HTTPException(403, "Редакция доступа сотрудника не подтверждена.")
    principal.require(runtime.company_id)
    return principal


def build_payments(runtime=None, *, environ=None, provider_factory=None):
    runtime = runtime or load_runtime()
    environment = os.environ if environ is None else environ
    dsn = environment.get("RESTCONTROL_TENANT_PAYMENTS_DATABASE_URL", "")
    if not dsn:
        raise ValueError("Explicit dedicated tenant payments database URL is required")
    directory = runtime.runtime_path("payments-vault")
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError("Company payment vault must be a private regular directory")
    return TenantPayments(
        PaymentStore(runtime, dsn, Vault(directory)), provider_factory or documented_provider
    )


class TenantPaymentAdministration:
    """Payment SQL never uses the analytics-role repository connection."""

    def __init__(self, service, repo):
        self.service, self.store, self.repo = service, service.store, repo

    def principal(self, actor):
        if not isinstance(actor, ActorContext):
            raise HTTPException(403, "Проверенный субъект компании обязателен.")
        return principal_from_scope(self.store.runtime, self.repo.actor_scope(actor))

    def catalog(self, actor):
        principal = self.principal(actor)
        configuration = self.store.management(principal)
        return self.store.grants(principal), [
            {"name": row["name"]} for row in configuration["venues"]
        ]

    def configuration(self, actor):
        result = self.store.management(self.principal(actor))
        result["tenant_payments"] = True
        for terminal in result["terminals"]:
            terminal["qrt_uuid"] = None
        return result

    def save_venue(self, actor, venue_id, payload):
        return self.store.save_venue(
            self.principal(actor), venue_id, VenueInput.model_validate(payload.model_dump())
        )

    def save_terminal(self, actor, venue_id, terminal_id, payload):
        if payload.mode is None:
            raise HTTPException(422, "Выберите режим терминала компании.")
        return self.store.save_terminal(
            self.principal(actor),
            venue_id,
            terminal_id,
            TerminalInput.model_validate(payload.model_dump(exclude={"qrt_uuid"})),
        )

    def validate_grants(self, actor, payload):
        _, venues = self.catalog(actor)
        if {grant.venue for grant in payload.deposit_grants} - {venue["name"] for venue in venues}:
            raise HTTPException(422, "Выбрано неизвестное заведение депозитов.")

    def user_snapshot(self, user_id):
        with self.repo.connection() as db:
            return db.execute(
                render(
                    "SELECT id,active,revision,sections FROM {analytics}.web_users WHERE id=%s",
                    self.store.runtime,
                ),
                (user_id,),
            ).fetchone()

    def validate_user(self, user_id):
        row = self.user_snapshot(user_id)
        return row if row and row["active"] else None

    def account_sync_cursor(self, actor, user_id):
        self.principal(actor)
        with self.store.connection() as db:
            return self.store.execute(
                db,
                "SELECT coalesce(max(id),0) AS cursor FROM {payments}.audit WHERE object_id=%s",
                (user_id,),
            ).fetchone()["cursor"]

    def sync_account(self, actor, user_id, payload, *, if_unchanged_since=None):
        """Profile commits first; revision mismatch denies stale grants on partial failure."""
        principal = self.principal(actor)
        snapshot = self.user_snapshot(user_id)
        if not snapshot:
            raise HTTPException(404, "Сотрудник не найден.")
        with self.store.connection() as db:
            if if_unchanged_since is not None:
                # Serialize against every grant/revoke DML, including direct
                # access routes; later administrator changes win over a replay.
                self.store.execute(
                    db, "LOCK TABLE {payments}.deposit_grants IN SHARE ROW EXCLUSIVE MODE"
                )
                cursor = self.store.execute(
                    db,
                    "SELECT coalesce(max(id),0) AS cursor FROM {payments}.audit WHERE object_id=%s",
                    (user_id,),
                ).fetchone()["cursor"]
                if cursor > if_unchanged_since:
                    return False
            self.store.execute(
                db, "DELETE FROM {payments}.deposit_grants WHERE user_id=%s", (user_id,)
            )
            if snapshot["active"] and "deposits" in snapshot["sections"]:
                rows = (
                    [(None, True, payload.deposits_create)]
                    if payload.deposits_all
                    else [
                        (self.store.venue(db, grant.venue)["id"], False, grant.can_create)
                        for grant in payload.deposit_grants
                    ]
                )
                for venue_id, is_all, can_create in rows:
                    self.store.execute(
                        db,
                        "INSERT INTO {payments}.deposit_grants "
                        "(id,user_id,venue_id,is_all,can_create,profile_revision) "
                        "VALUES(%s,%s,%s,%s,%s,%s)",
                        (uuid4(), user_id, venue_id, is_all, can_create, snapshot["revision"]),
                    )
            self.store.audit(
                db,
                principal,
                "deposit_access.replace",
                user_id,
                {"profile_revision": snapshot["revision"]},
            )
