"""Real restricted tenant SQL, verified owner actors, and synthetic providers only."""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from app.commercial_invoices import counterparties, counterparty_dispatch
from app.commercial_invoices.service import CommercialInvoiceService
from app.documents.database import DocumentDatabase
from app.tenancy.actor import ActorContext
from app.tenancy.migrations import provision_tenant
from tests.test_tenant_migrations_postgres import empty_database as empty_database


class SyntheticProvider:
    def __init__(self):
        self.settings = SimpleNamespace(
            iiko_url="https://iiko.invalid/resto/api", iiko_login="test"
        )
        self.posts = []

    @contextmanager
    def session(self):
        yield None, "synthetic"

    def payload(self, kind, snapshot):
        return b"<synthetic/>"

    def registry(self, *_):
        return []

    def exists(self, *_, **__):
        return False

    def create(self, client, token, job):
        self.posts.append(job["id"])
        return "rejected"


@pytest.fixture
def tenant_owner_commercial(empty_database):
    operator, dsn, tenant = empty_database
    services = []
    for label in ("A", "B"):
        runtime = tenant()
        provision_tenant(operator, runtime)
        db = DocumentDatabase(make_conninfo(dsn, user=runtime.database_role), runtime=runtime)
        store, party, product, unit = [uuid4() for _ in range(4)]
        provider = SyntheticProvider()
        service = CommercialInvoiceService(
            db,
            provider,
            submit_enabled=True,
            counterparty_enabled=True,
            counterparty_provider=provider,
            feature_authorizer=lambda feature: True,
        )
        owner = ActorContext(runtime.company_id, uuid4(), "platform_owner", "Owner " + label)
        service.owner_authorizer = lambda company, user, expected=owner: (
            expected if (company, user) == (expected.company_id, expected.auth_user_id) else None
        )
        with db.connection() as conn:
            conn.execute("INSERT INTO stores(id,name) VALUES(%s,%s)", (store, label))
            for name, catalog in {
                "commercial_products": [
                    {
                        "id": str(product),
                        "name": label + " Product",
                        "unit_id": str(unit),
                        "unit": "шт.",
                    }
                ],
                "commercial_sale_counterparties": [
                    {
                        "id": str(party),
                        "name": label + " Buyer",
                        "inn": "",
                        "kpp": "",
                        "address": "",
                    }
                ],
            }.items():
                conn.execute(
                    "INSERT INTO native_catalog(name,data) VALUES(%s,%s)", (name, Jsonb(catalog))
                )
        body = dict(
            request_id=str(uuid4()),
            store_id=str(store),
            counterparty_id=str(party),
            date="2026-10-08",
            comment=label,
            items=[
                dict(
                    product_id=str(product),
                    quantity="2",
                    price="100",
                    price_includes_vat=True,
                    vat_rate="20",
                )
            ],
        )
        services.append((service, owner, body))
    try:
        yield services
    finally:
        for service, _, _ in services:
            service.database.close()


def counterparty_body():
    return dict(
        request_id=str(uuid4()),
        entity_type="organization",
        name="Synthetic LLC",
        inn="1234567894",
        kpp="123401001",
        address="Synthetic address",
        phone="+79990000001",
        email="synthetic@example.invalid",
    )


def test_owner_create_edit_submit_replay_history_and_isolation(tenant_owner_commercial):
    service, owner, body = tenant_owner_commercial[0]
    other_service, foreign_owner, _ = tenant_owner_commercial[1]
    created = service.mutate(owner, "sale", None, "create", body)
    assert service.mutate(owner, "sale", None, "create", body) == created
    document = created["document"]
    second_owner = replace(owner, auth_user_id=uuid4(), display_name="Second owner")
    with pytest.raises(HTTPException) as conflict:
        service.mutate(second_owner, "sale", None, "create", body)
    assert conflict.value.status_code == 409
    changed = service.mutate(
        second_owner,
        "sale",
        document["id"],
        "edit",
        {**body, "request_id": str(uuid4()), "version": document["version"], "comment": "Changed"},
    )
    queued = service.mutate(
        owner,
        "sale",
        document["id"],
        "submit",
        {"request_id": str(uuid4()), "version": changed["document"]["version"]},
    )
    assert queued["document"]["state"] == "queued"
    detail = service.dispatch(owner, "GET", "sale", document["id"])["document"]
    assert [e["actor_name"] for e in detail["history"]] == ["Owner A", "Second owner", "Owner A"]
    assert all(e["actor_id"] is None for e in detail["history"])
    with pytest.raises(HTTPException) as denied:
        other_service.dispatch(owner, "GET", "sale", document["id"])
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as absent:
        other_service.dispatch(foreign_owner, "GET", "sale", document["id"])
    assert absent.value.status_code == 404
    with service.database.connection(readonly=True) as db:
        assert db.execute("SELECT count(*) AS n FROM authentication_user").fetchone()["n"] == 0
        assert (
            db.execute("SELECT count(*) AS n FROM portal_documents_userlink").fetchone()["n"] == 0
        )
        row = db.execute("SELECT created_by_id,created_actor FROM commercial_invoices").fetchone()
        assert row["created_by_id"] is None and row["created_actor"] == owner.as_dict()
        assert db.execute("SELECT count(*) AS n FROM native_actor_audit").fetchone()["n"] == 3
        assert (
            db.execute("SELECT count(*) AS n FROM commercial_invoice_dispatch").fetchone()["n"] == 1
        )


def test_counterparty_owner_replay_listing_and_revalidated_worker(tenant_owner_commercial):
    service, owner, _ = tenant_owner_commercial[0]
    payload = counterparty_body()
    saved = counterparties.command(service, owner, "sale", payload)
    assert counterparties.command(service, owner, "sale", payload) == saved
    second_owner = replace(owner, auth_user_id=uuid4())
    with pytest.raises(HTTPException) as conflict:
        counterparties.command(service, second_owner, "sale", payload)
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException) as absent:
        counterparties.operation(service, second_owner, "sale", payload["request_id"])
    assert absent.value.status_code == 404
    assert service.dispatch(owner, "GET", "sale", action="options")["counterparty_operations"]
    assert (
        service.dispatch(second_owner, "GET", "sale", action="options")["counterparty_operations"]
        == []
    )
    assert counterparty_dispatch.deliver_one(service)
    assert len(service.counterparty_provider.posts) == 1
    with service.database.connection(readonly=True) as db:
        row = db.execute("SELECT actor_id,actor FROM commercial_counterparty_operations").fetchone()
        assert row["actor_id"] is None and row["actor"] == owner.as_dict()
        assert db.execute("SELECT count(*) AS n FROM authentication_user").fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM native_actor_audit").fetchone()["n"] == 3


@pytest.mark.parametrize(
    "authorization", ["revoked", "missing", "another_owner", "another_company"]
)
def test_counterparty_worker_never_uses_snapshot_as_authority(
    tenant_owner_commercial, authorization
):
    service, owner, _ = tenant_owner_commercial[0]
    saved = counterparties.command(service, owner, "sale", counterparty_body())
    returns = {
        "revoked": None,
        "another_owner": replace(owner, auth_user_id=uuid4()),
        "another_company": replace(owner, company_id=uuid4()),
    }
    service.owner_authorizer = (
        None if authorization == "missing" else lambda *_: returns[authorization]
    )
    assert counterparty_dispatch.deliver_one(service)
    assert service.counterparty_provider.posts == []
    result = counterparties.operation(service, owner, "sale", saved["operation"]["id"])
    assert result["operation"]["error_code"] == "access_revoked"
