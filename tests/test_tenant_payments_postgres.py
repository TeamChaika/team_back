"""Disposable loopback PostgreSQL and fake provider; never production/payment calls."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import SecretStr

from app.tenancy.actor import ActorContext
from app.tenancy.migrations import load_migrations
from app.tenancy.sql import render
from app.tenant_payments.models import (
    DepositFilters,
    NewDeposit,
    PaymentPrincipal,
    TerminalInput,
    VenueInput,
)
from app.tenant_payments.provider import CreatedOperation, CreationUnknown, VerifiedOperation
from app.tenant_payments.routes import create_router
from app.tenant_payments.service import TenantPayments
from app.tenant_payments.store import PaymentStore, payment_role
from tests.test_tenant_migrations import runtime_for


class TestVault:
    __test__ = False

    def __init__(self):
        self.key = Fernet(Fernet.generate_key())

    def encrypt(self, value):
        return self.key.encrypt(value.encode()).decode()

    def decrypt(self, value):
        return self.key.decrypt(value.encode()).decode()


@pytest.fixture
def companies():
    dsn = os.environ.get("RESTCONTROL_PAYMENTS_TEST_DSN")
    if not dsn:
        pytest.skip("Set RESTCONTROL_PAYMENTS_TEST_DSN to a disposable local PostgreSQL")
    if conninfo_to_dict(dsn).get("host") not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail("Payment tests require explicit loopback DB")
    name = "tenant_payments_test_" + uuid4().hex
    admin = psycopg.connect(dsn, autocommit=True)
    admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    isolated = make_conninfo(dsn, dbname=name)
    operator = psycopg.connect(isolated, autocommit=True)
    stores = []
    roles = []
    try:
        for _ in range(2):
            runtime = runtime_for(uuid4())
            role = payment_role(runtime)
            roles.append(role)
            operator.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(runtime.payments_schema))
            )
            # Baseline is deliberately standalone: payment runtime never needs analytics tables.
            for migration in load_migrations():
                if migration.area == "payments":
                    operator.execute(render(migration.template, runtime, payments_role=role))
            for table in (
                "venues",
                "terminals",
                "terminal_versions",
                "terminal_checks",
                "deposit_grants",
                "deposits",
                "attempts",
                "audit",
                "webhook_receipts",
            ):
                operator.execute(
                    sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(
                        sql.Identifier(runtime.payments_schema, table)
                    )
                )
            stores.append(PaymentStore(runtime, make_conninfo(isolated, user=role), TestVault()))
        yield operator, stores
    finally:
        operator.close()
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        for role in roles:
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        admin.close()


def owner(store, user=None):
    return PaymentPrincipal(
        ActorContext(store.runtime.company_id, user or uuid4(), "platform_owner", "Owner"),
        True,
        (),
        True,
    )


def member(store, user, **kwargs):
    return PaymentPrincipal(
        ActorContext(store.runtime.company_id, user, "company_member", "Staff", uuid4()),
        True,
        ("deposits",),
        **kwargs,
    )


def setup(store, actor, venue_id=None, terminal_id=None):
    venue, terminal = venue_id or uuid4(), terminal_id or uuid4()
    store.save_venue(actor, venue, VenueInput(name="Same venue"))
    store.save_terminal(
        actor,
        venue,
        terminal,
        TerminalInput(
            name="Same terminal",
            api_key=SecretStr("synthetic-key-1"),
            mode="sandbox",
            make_default=True,
        ),
    )
    return venue, terminal


def payload(venue, request=None, **fields):
    return NewDeposit(
        request_id=request or uuid4(),
        customer_name="Гость",
        phone="79991234567",
        amount=500,
        restaurant=str(venue),
        reservation_date=datetime(2026, 10, 9, 20, tzinfo=UTC),
        notes="note",
        **fields,
    )


def reserved(store, deposit_id, capability, request_id):
    attempt, context = store.reserve(deposit_id, capability, request_id)
    if context:
        verified = asyncio.run(Provider().check_terminal())
        store.record_terminal_check(
            context["terminal_version_id"], verified, attempt_id=UUID(attempt["id"])
        )
    return attempt, context


def token(deposit):
    return parse_qs(urlsplit(deposit["guest_url"]).query)["token"][0]


class Provider:
    def __init__(self):
        self.calls = 0
        self.checks = 0
        self.operation_id = uuid4()
        self.callback = None
        self.status = "pending"
        self.currency = "RUB"
        self.amount = 50000
        self.error = False

    async def check_terminal(self, **kwargs):
        from datetime import date

        from app.tenant_payments.provider import TerminalContext

        return TerminalContext(
            "synthetic-merchant",
            "synthetic-terminal",
            date(2099, 1, 1),
            True,
            "sandbox",
            False,
            False,
            False,
        )

    async def create(self, **kwargs):
        self.calls += 1
        self.callback = kwargs["notification_url"]
        self.redirect = kwargs["redirect_url"]
        if self.error:
            raise CreationUnknown("Unknown synthetic POST")
        return CreatedOperation(self.operation_id, "https://provider.example/pay")

    async def check(self, **kwargs):
        self.checks += 1
        return VerifiedOperation(kwargs["operation_id"], self.amount, self.currency, self.status)


def test_two_companies_same_ids_user_names_and_requests_are_separate(companies):
    operator, stores = companies
    user, venue, terminal, request = uuid4(), uuid4(), uuid4(), uuid4()
    ids = []
    for store in stores:
        actor = owner(store, user)
        setup(store, actor, venue, terminal)
        deposit = store.create(actor, payload(venue, request))
        ids.append(UUID(deposit["id"]))
        assert store.listing(actor, DepositFilters())["total"] == 1
        assert store.get(actor, UUID(deposit["id"]))["restaurant"] == "Same venue"
        assert "synthetic-key" not in str(store.management(actor))
        assert store.create(actor, payload(venue, request))["id"] == deposit["id"]
    assert ids[0] != ids[1]
    for own, foreign in ((stores[0], stores[1]), (stores[1], stores[0])):
        with pytest.raises(HTTPException) as error:
            own.get(owner(foreign, user), ids[0])
        assert error.value.status_code == 403
        with own.connection() as db:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute(render("SELECT * FROM {payments}.deposits", foreign.runtime))
    with pytest.raises(ValueError, match="restricted"):
        PaymentStore(stores[0].runtime, operator.info.dsn, TestVault())


def test_rights_disabled_warehouse_and_booking_filters_export(companies):
    _, (store, _) = companies
    admin = owner(store)
    venue, _ = setup(store, admin)
    staff = member(store, uuid4())
    with pytest.raises(HTTPException):
        store.create(staff, payload(venue))
    store.grant(admin, staff.actor.auth_user_id, str(venue), can_create=False)
    assert len(store.venues(staff)) == 1
    assert not store.venues(staff, create=True)
    with pytest.raises(HTTPException):
        store.create(staff, payload(venue))
    store.grant(admin, staff.actor.auth_user_id, str(venue), can_create=True)
    assert store.grant(admin, staff.actor.auth_user_id, str(venue), can_create=None)["can_create"]
    item = store.create(staff, payload(venue))
    assert (
        store.listing(
            staff, DepositFilters(reservation_from="2026-10-09", reservation_to="2026-10-09")
        )["total"]
        == 1
    )
    assert store.listing(staff, DepositFilters(reservation_from="2026-10-10"))["total"] == 0
    assert store.listing(staff, DepositFilters(query="note"), export=True)["total"] == 1
    assert store.listing(staff, DepositFilters(query="%"))["total"] == 0
    for principal in (
        replace(staff, active=False),
        replace(staff, warehouse_restricted=True),
        replace(staff, sections=()),
    ):
        with pytest.raises(HTTPException):
            store.get(principal, UUID(item["id"]))
    store.revoke(admin, staff.actor.auth_user_id, str(venue))
    assert store.listing(staff, DepositFilters())["total"] == 0


def test_concurrent_intent_reserves_once_unknown_never_recreates(companies):
    _, (store, _) = companies
    admin = owner(store)
    venue, _ = setup(store, admin)
    deposit = store.create(admin, payload(venue))
    deposit_id, guest = UUID(deposit["id"]), token(deposit)
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda _: reserved(store, deposit_id, guest, uuid4()), range(6)))
    assert len({row[0]["id"] for row in rows}) == 1
    assert sum(context is not None for _, context in rows) == 1
    attempt_id = UUID(rows[0][0]["id"])
    store.record_creation(attempt_id)
    provider = Provider()
    result = asyncio.run(
        TenantPayments(store, lambda _: provider, feature_authorizer=lambda _: True).prepare(
            deposit_id, guest, uuid4()
        )
    )
    assert provider.calls == 0
    assert result["payment"]["state"] == "unknown"


def test_attempt_persisted_before_unknown_provider_post_and_worker_restart(companies):
    _, (store, _) = companies
    admin = owner(store)
    venue, _ = setup(store, admin)
    deposit = store.create(admin, payload(venue))
    provider = Provider()
    provider.error = True
    service = TenantPayments(store, lambda _: provider, feature_authorizer=lambda _: True)
    result = asyncio.run(service.prepare(UUID(deposit["id"]), token(deposit), uuid4()))
    assert result["payment"]["state"] == "unknown"
    assert provider.calls == 1
    restarted = TenantPayments(
        PaymentStore(store.runtime, store.dsn, store.vault),
        lambda _: provider,
        feature_authorizer=lambda _: True,
    )
    asyncio.run(restarted.prepare(UUID(deposit["id"]), token(deposit), uuid4()))
    assert provider.calls == 1
    attempt = store.latest_attempt(UUID(deposit["id"]))
    with store.connection() as db:
        store.execute(
            db, "UPDATE {payments}.attempts SET next_check_at=now() WHERE id=%s", (attempt,)
        )
    assert asyncio.run(restarted.reconcile_due()) == 1
    assert (
        store.guest(UUID(deposit["id"]), token(deposit))["payment"]["diagnostic"]
        == "creation_unknown"
    )


def test_webhook_requires_token_and_verified_money_atomic_monotonic(companies):
    _, (store, _) = companies
    admin = owner(store)
    venue, _ = setup(store, admin)
    deposit = store.create(admin, payload(venue))
    provider = Provider()
    service = TenantPayments(store, lambda _: provider, feature_authorizer=lambda _: True)
    deposit_id, guest = UUID(deposit["id"]), token(deposit)
    asyncio.run(service.prepare(deposit_id, guest, uuid4()))
    callback = urlsplit(provider.callback)
    assert f"{callback.scheme}://{callback.netloc}" == store.runtime.frontend_origin
    attempt_id = UUID(callback.path.rsplit("/", 1)[1])
    callback_token = parse_qs(callback.query)["token"][0]
    with pytest.raises(HTTPException):
        store.callback(attempt_id, "wrong-token", provider.operation_id)
    provider.status, provider.currency = "paid", None
    asyncio.run(service.callback(attempt_id, callback_token, provider.operation_id))
    assert store.get(admin, deposit_id)["status"] == "pending"
    assert store.guest(deposit_id, guest)["payment"]["diagnostic"] == "currency_unverified"
    provider.currency, provider.amount = "RUB", 50100
    assert (
        store.record_check(
            attempt_id, asyncio.run(provider.check(operation_id=provider.operation_id))
        )
        == "pending"
    )
    assert store.get(admin, deposit_id)["status"] == "pending"
    provider.amount = 50000
    verified = asyncio.run(provider.check(operation_id=provider.operation_id))
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(
            state == "paid"
            for state in pool.map(lambda _: store.record_check(attempt_id, verified), range(4))
        )
    assert store.get(admin, deposit_id)["status"] == "paid"
    provider.status = "pending"
    assert (
        store.record_check(
            attempt_id, asyncio.run(provider.check(operation_id=provider.operation_id))
        )
        == "paid"
    )
    with store.connection() as db:
        assert (
            store.execute(
                db, "SELECT count(*) AS n FROM {payments}.audit WHERE action='payment.paid'"
            ).fetchone()["n"]
            == 1
        )


def test_immutable_terminal_versions_and_early_callback_race(companies):
    _, (store, _) = companies
    admin = owner(store)
    venue, terminal = setup(store, admin)
    deposit = store.create(admin, payload(venue))
    attempt, context = reserved(store, UUID(deposit["id"]), token(deposit), uuid4())
    attempt_id = UUID(attempt["id"])
    version = UUID(attempt["terminal_version_id"])
    store.save_terminal(
        admin,
        venue,
        terminal,
        TerminalInput(
            name="Changed",
            revision=1,
            api_key=SecretStr("synthetic-key-2"),
            mode="sandbox",
            make_default=True,
        ),
    )
    assert store.check_context(attempt_id)["api_key"] == "synthetic-key-1"
    with store.connection() as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            store.execute(
                db,
                "UPDATE {payments}.terminal_versions SET encrypted_key='bad' WHERE id=%s",
                (version,),
            )
    operation = uuid4()
    store.callback(attempt_id, context["callback"], operation)
    store.record_check(attempt_id, VerifiedOperation(operation, 50000, "RUB", "failed"))
    assert store.check_context(attempt_id)["state"] == "unknown"
    store.record_creation(attempt_id, CreatedOperation(operation, "https://provider.example/early"))
    store.record_check(attempt_id, VerifiedOperation(operation, 50000, "RUB", "failed"))
    second, _ = reserved(store, UUID(deposit["id"]), token(deposit), uuid4())
    assert second["id"] != attempt["id"]
    assert store.get(admin, UUID(deposit["id"]))["status"] == "pending"
    assert (
        store.record_check(attempt_id, VerifiedOperation(operation, 50000, "RUB", "pending"))
        == "failed"
    )
    assert store.get(admin, UUID(deposit["id"]))["status"] == "pending"
    store.record_creation(attempt_id, CreatedOperation(operation, "https://provider.example/pay"))
    assert store.check_context(attempt_id)["state"] == "failed"
    assert store.check_context(UUID(second["id"]))["state"] == "creating"


def test_shared_route_contract_and_capability_guest_never_reports_callback_paid(companies):
    _, (store, _) = companies
    actor = owner(store)
    venue, _ = setup(store, actor)
    provider = Provider()
    service = TenantPayments(store, lambda _: provider, feature_authorizer=lambda _: True)
    app = FastAPI()

    def origin(request):
        if request.headers.get("origin") != store.runtime.frontend_origin:
            raise HTTPException(403, "Origin")

    app.include_router(create_router(service, lambda: actor, origin))
    client = TestClient(app)
    assert client.get("/api/deposits/venues").json() == ["Same venue"]
    bad_key = client.post(
        f"/api/payment-settings/venues/{venue}/terminals/{uuid4()}",
        json={"name": "Bad", "mode": "sandbox", "api_key": "private"},
        headers={"Origin": store.runtime.frontend_origin},
    )
    assert bad_key.status_code == 422 and "private" not in bad_key.text
    created = client.post(
        "/api/deposits",
        json=payload(venue).model_dump(mode="json"),
        headers={"Origin": store.runtime.frontend_origin},
    )
    assert created.status_code == 201
    deposit = created.json()
    export = client.get("/api/deposits/export")
    assert export.status_code == 200 and export.content.startswith(b"PK")
    assert client.get(f"/api/guest-deposits/{deposit['id']}?token={'x' * 32}").status_code == 404
    body = {"token": token(deposit), "request_id": str(uuid4())}
    assert client.post(f"/api/guest-deposits/{deposit['id']}/prepare", json=body).status_code == 403
    prepared = client.post(
        f"/api/guest-deposits/{deposit['id']}/prepare",
        json=body,
        headers={"Origin": store.runtime.frontend_origin},
    )
    assert prepared.status_code == 200 and prepared.json()["status"] == "pending"
    callback = urlsplit(provider.callback)
    assert f"{callback.scheme}://{callback.netloc}" == store.runtime.frontend_origin
    notified = client.post(
        callback.path + "?" + callback.query,
        json={"operation_id": str(provider.operation_id), "status": "paid"},
    )
    assert notified.status_code == 200
    assert client.get(f"/api/deposits/{deposit['id']}").json()["status"] == "pending"


def test_fresh_actor_revision_denies_stale_or_cross_company_grants(companies):
    from types import SimpleNamespace

    from app.tenancy.payment_bootstrap import principal_from_scope

    _, stores = companies
    store = stores[0]
    admin = owner(store)
    venue, _ = setup(store, admin)
    actor = ActorContext(store.runtime.company_id, uuid4(), "company_member", "Staff", uuid4())
    scope = SimpleNamespace(
        actor=actor,
        user={"active": True, "revision": 1, "sections": ["deposits"]},
        warehouse_restricted=False,
    )
    staff = principal_from_scope(store.runtime, scope)
    store.grant(admin, actor.auth_user_id, str(venue), can_create=True, profile_revision=1)
    deposit = store.create(staff, payload(venue))
    scope.user["revision"] = 2
    updated = principal_from_scope(store.runtime, scope)
    with pytest.raises(HTTPException) as stale:
        store.get(updated, UUID(deposit["id"]))
    assert stale.value.status_code == 403
    store.grant(admin, actor.auth_user_id, str(venue), can_create=True, profile_revision=2)
    assert store.get(updated, UUID(deposit["id"]))["id"] == deposit["id"]
    scope.user["active"] = False
    with pytest.raises(HTTPException):
        principal_from_scope(store.runtime, scope)
    scope.user["active"] = True
    with pytest.raises(HTTPException):
        principal_from_scope(stores[1].runtime, scope)
    scope.actor = replace(actor, membership_id=None)
    with pytest.raises(HTTPException):
        principal_from_scope(store.runtime, scope)
    scope.actor = {"kind": "platform_owner"}
    with pytest.raises(HTTPException):
        principal_from_scope(store.runtime, scope)
    scope.actor = admin.actor
    scope.user = {}
    assert principal_from_scope(store.runtime, scope).can_manage


def test_common_management_adapter_uses_only_dedicated_payment_connection(companies):
    from types import SimpleNamespace

    from app.tenancy.payment_bootstrap import TenantPaymentAdministration
    from app.web.administration import Terminal, Venue

    _, stores = companies
    store = stores[0]
    admin = owner(store)
    scope = SimpleNamespace(actor=admin.actor, user={}, warehouse_restricted=False)

    class Repository:
        def actor_scope(self, actor):
            assert actor == admin.actor
            return scope

        def connection(self):
            pytest.fail("Management configuration must not query analytics")

    adapter = TenantPaymentAdministration(TenantPayments(store, lambda _: Provider()), Repository())
    venue, terminal = uuid4(), uuid4()
    adapter.save_venue(admin.actor, venue, Venue(name="Own company venue"))
    adapter.save_terminal(
        admin.actor,
        venue,
        terminal,
        Terminal(
            name="Own terminal",
            api_key="synthetic-config-key",
            mode="sandbox",
            merchant_id="ownmerchant",
            make_default=True,
        ),
    )
    config = adapter.configuration(admin.actor)
    assert config["tenant_payments"] is True
    assert config["terminals"][0]["mode"] == "sandbox"
    assert config["terminals"][0]["merchant_id"] == "ownmerchant"
    assert config["terminals"][0]["qrt_uuid"] is None
    assert "synthetic-config-key" not in str(config)
    assert adapter.catalog(admin.actor) == ([], [{"name": "Own company venue"}])
    with pytest.raises(HTTPException):
        adapter.configuration(admin.actor.auth_user_id)


def test_provider_creation_currency_and_validated_merchant_settle_documented_sse(companies):
    import httpx

    from app.tenant_payments.provider import QRManagerProvider

    _, (store, foreign) = companies
    admin = owner(store)
    venue, terminal = setup(store, admin)
    operation = uuid4()
    expanded = "https://qr.nspk.ru/AD10102T192IVSCK87ERR4FDK14GNOR7?type=02&bank=100000000241&sum=50000&cur=RUB&crc=09E8"
    calls = []
    link = [expanded]

    def transport(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/users/check-api-key/":
            return httpx.Response(
                200,
                json={
                    "merchant_id": "synthetic-merchant",
                    "qrt_name": "Own checked terminal",
                    "subscription_end_date": "2099-01-01",
                    "qrt_is_b2c": True,
                    "requires_receipt": False,
                    "is_nomenclature": False,
                    "is_cash_link": False,
                },
            )
        if request.url.path == "/operations/qr-code/":
            return httpx.Response(
                200,
                json={
                    "results": {
                        "operation_id": str(operation),
                        "payment_page_link": "https://provider.example/pay",
                        "qr_link": link[0],
                    }
                },
            )
        return httpx.Response(
            200, json={"results": {"operation_status_code": 5, "operation_sum": 50000}}
        )

    provider = QRManagerProvider(transport=httpx.MockTransport(transport))
    service = TenantPayments(store, lambda _: provider, feature_authorizer=lambda _: True)
    validated = asyncio.run(service.validate_terminal(admin, venue, terminal))
    assert validated["ready"] and validated["merchant_id"] == "synthetic-merchant"
    for expected, qr_link in (
        ("paid", expanded),
        ("pending", "https://qr.nspk.ru/AD10002ML1STTEO98KC8LRS9BO5FU9P5"),
    ):
        operation = uuid4()
        link[0] = qr_link
        deposit = store.create(admin, payload(venue))
        asyncio.run(service.prepare(UUID(deposit["id"]), token(deposit), uuid4()))
        result = asyncio.run(service.refresh_guest(UUID(deposit["id"]), token(deposit)))
        assert result["status"] == expected
        attempt_id = store.latest_attempt(UUID(deposit["id"]))
        attempt = store.check_context(attempt_id)
        assert attempt["terminal_check_id"] and attempt["creation_confirmed"]
        if expected == "paid":
            assert attempt["creation_currency"] == "RUB"
            assert attempt["provider_amount_minor"] == 50000
        else:
            assert attempt["creation_currency"] is None
            assert attempt["diagnostic"] == "currency_unverified"
        with pytest.raises(HTTPException):
            foreign.guest(UUID(deposit["id"]), token(deposit))
    assert calls[0] == ("GET", "/users/check-api-key/")


def test_invalid_terminal_context_never_creates_provider_operation(companies):
    from datetime import date

    _, (store, _) = companies
    admin = owner(store)
    venue, _ = setup(store, admin)
    provider = Provider()
    verified = asyncio.run(provider.check_terminal())
    for context in (
        replace(verified, subscription_end_date=date(2000, 1, 1)),
        replace(verified, qrt_is_b2c=False),
        replace(verified, requires_receipt=True),
        replace(verified, requires_receipt=None),
    ):

        async def check_terminal(verified_context=context, **kwargs):
            return verified_context

        provider.check_terminal = check_terminal
        service = TenantPayments(store, lambda _: provider, feature_authorizer=lambda _: True)
        deposit = store.create(admin, payload(venue))
        result = asyncio.run(service.prepare(UUID(deposit["id"]), token(deposit), uuid4()))
        assert result["payment"]["state"] == "failed"
        assert provider.calls == 0
    # Checks are immutable and cannot be borrowed from another terminal version.
    with store.connection() as db:
        check = store.execute(db, "SELECT id FROM {payments}.terminal_checks LIMIT 1").fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            store.execute(
                db,
                "UPDATE {payments}.terminal_checks SET merchant_id='other' WHERE id=%s",
                (check["id"],),
            )


def test_short_guest_capability_existing_link_revocation_and_tenant_isolation(companies):
    _, (store, other) = companies
    actor = owner(store)
    venue, _ = setup(store, actor)
    legacy = store.create(actor, payload(venue))
    legacy_token = token(legacy)
    store.runtime = replace(store.runtime, payment_origin="https://pay.customer.example")
    short = store.get(actor, UUID(legacy["id"]))
    code = short["guest_url"].rsplit("/", 1)[1]
    assert short["guest_origin"] == store.runtime.payment_origin and len(code) == 32
    assert store.resolve_guest_link(code) == (UUID(legacy["id"]), legacy_token)
    assert store.guest(UUID(legacy["id"]), legacy_token)["id"] == legacy["id"]
    with pytest.raises(HTTPException):
        other.resolve_guest_link(code)
    provider = Provider()
    service = TenantPayments(store, lambda _: provider, feature_authorizer=lambda _: True)
    app = FastAPI()
    app.include_router(create_router(service, lambda: actor, lambda _: None))
    client = TestClient(app)
    assert client.get(f"/api/guest-links/{code}").status_code == 200
    assert (
        client.post(
            f"/api/guest-links/{code}/prepare",
            json={"request_id": str(uuid4())},
            headers={"Origin": "https://untrusted.example"},
        ).status_code
        == 403
    )
    assert provider.calls == 0
    response = client.post(
        f"/api/guest-links/{code}/prepare",
        json={"request_id": str(uuid4())},
        headers={"Origin": store.runtime.payment_origin},
    )
    assert response.status_code == 200
    assert provider.calls == 1
    assert provider.redirect == short["guest_url"]
    with store.connection() as db:
        store.execute(
            db,
            "UPDATE {payments}.deposits SET guest_token_hash=%s WHERE id=%s",
            ("revoked", UUID(legacy["id"])),
        )
    with pytest.raises(HTTPException):
        store.resolve_guest_link(code)
    assert client.get(f"/api/guest-links/{code}").status_code == 404
