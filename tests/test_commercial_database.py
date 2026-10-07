"""Disposable embedded PostgreSQL only; every invoice and provider is synthetic."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.commercial_invoices import dispatch, policy
from app.commercial_invoices.service import CommercialInvoiceService
from app.documents.database import DocumentDatabase

PORTAL, ADMIN, STORE, FOREIGN, PRODUCT, PARTY, UNIT = [UUID(int=i) for i in range(1, 8)]


@pytest.fixture(scope="module")
def database():
    pgserver = pytest.importorskip(
        "pgserver", reason="Optional embedded disposable PostgreSQL required"
    )
    with TemporaryDirectory(prefix="chaika-commercial-tests-") as directory:
        server = pgserver.get_server(Path(directory) / "postgres", cleanup_mode="stop")
        try:
            url = server.get_uri()
            with psycopg.connect(url, autocommit=True) as db:
                db.execute("CREATE ROLE chaika_iiko_app")
                db.execute("CREATE SCHEMA chaika_iiko_documents")
                db.execute("SET search_path=chaika_iiko_documents,pg_catalog")
                db.execute("CREATE TABLE stores(id uuid PRIMARY KEY,name text)")
                db.execute(
                    "CREATE TABLE authentication_user(id bigint PRIMARY KEY,username text,"
                    "first_name text,last_name text,is_active boolean)"
                )
                db.execute(
                    "CREATE TABLE portal_access(id uuid PRIMARY KEY,active boolean,"
                    "is_portal_admin boolean,sections jsonb)"
                )
                db.execute(
                    "CREATE TABLE portal_documents_userlink(user_id bigint PRIMARY KEY,"
                    "supabase_id uuid)"
                )
                db.execute(
                    "CREATE TABLE native_catalog(name text PRIMARY KEY,data jsonb,"
                    "updated_at timestamptz DEFAULT now())"
                )
                db.execute(Path("migrations/documents/0008_commercial_invoices.sql").read_text())
            database = DocumentDatabase(url)
            yield database
            database.close()
        finally:
            server.cleanup()


class Provider:
    payload_calls = 0

    def payload(self, kind, snapshot):
        self.payload_calls += 1
        return ('<synthetic id="' + snapshot["id"] + '"/>').encode()


@pytest.fixture
def service(database):
    with database.connection() as db:
        db.execute(
            "TRUNCATE stores,authentication_user,portal_access,"
            "portal_documents_userlink,native_catalog CASCADE"
        )
        db.execute("INSERT INTO stores VALUES(%s,'Allowed'),(%s,'Foreign')", (STORE, FOREIGN))
        db.execute("INSERT INTO authentication_user VALUES(1,'operator','Test','Operator',true)")
        db.execute(
            "INSERT INTO portal_access VALUES(%s,true,false,%s),(%s,true,true,%s)",
            (PORTAL, Jsonb(["invoices", "outgoing"]), ADMIN, Jsonb([])),
        )
        db.execute("INSERT INTO portal_documents_userlink VALUES(1,%s)", (PORTAL,))
        for kind in ["purchase", "sale"]:
            db.execute(
                "INSERT INTO commercial_invoice_grants VALUES(1,%s,%s,%s)",
                (kind, STORE, Jsonb(["view", "create", "edit", "submit"])),
            )
        for name, rows in {
            "commercial_products": [
                {
                    "id": str(PRODUCT),
                    "name": "Synthetic product",
                    "unit_id": str(UNIT),
                    "unit": "шт.",
                }
            ],
            "commercial_purchase_counterparties": [
                {
                    "id": str(PARTY),
                    "name": "Synthetic supplier",
                    "inn": "",
                    "kpp": "",
                    "address": "",
                }
            ],
            "commercial_sale_counterparties": [
                {"id": str(PARTY), "name": "Synthetic buyer", "inn": "", "kpp": "", "address": ""}
            ],
        }.items():
            db.execute("INSERT INTO native_catalog(name,data) VALUES(%s,%s)", (name, Jsonb(rows)))
    return CommercialInvoiceService(
        database, Provider(), seller={"name": "Synthetic seller"}, submit_enabled=True
    )


def body(**changes):
    return {
        "request_id": str(uuid4()),
        "store_id": str(STORE),
        "counterparty_id": str(PARTY),
        "date": "2026-10-07",
        "external_number": "SUP-1",
        "comment": "Synthetic test",
        "items": [
            {
                "product_id": str(PRODUCT),
                "quantity": "2",
                "price": "100.01",
                "vat_rate": "20",
                "price_includes_vat": True,
            }
        ],
        **changes,
    }


def create(service, **changes):
    return service.dispatch(PORTAL, "POST", "sale", payload=body(**changes))["document"]


def test_create_replay_edit_stale_version_and_snapshot(service):
    original = body()
    first = service.dispatch(PORTAL, "POST", "sale", payload=original)["document"]
    assert first["totals"]["total"] == "200.02"
    assert service.dispatch(PORTAL, "POST", "sale", payload=original)["document"] == first
    with pytest.raises(HTTPException) as collision:
        service.dispatch(PORTAL, "POST", "sale", payload={**original, "comment": "changed"})
    assert collision.value.status_code == 409
    edited = service.dispatch(PORTAL, "POST", "sale", first["id"], "edit", payload=body(version=1))[
        "document"
    ]
    assert edited["version"] == 2
    with pytest.raises(HTTPException) as stale:
        service.dispatch(PORTAL, "POST", "sale", first["id"], "edit", payload=body(version=1))
    assert stale.value.status_code == 409
    with service.database.connection() as db:
        assert (
            db.execute("SELECT count(*) AS n FROM commercial_invoice_revisions").fetchone()["n"]
            == 2
        )


def test_other_store_denied_on_create_list_detail_pdf(service):
    saved = create(service)
    with pytest.raises(HTTPException) as denied:
        create(service, store_id=str(FOREIGN))
    assert denied.value.status_code == 403
    with service.database.connection() as db:
        db.execute("DELETE FROM commercial_invoice_grants")
    assert service.dispatch(PORTAL, "GET", "sale")["items"] == []
    for action in [None, "pdf"]:
        with pytest.raises(HTTPException) as denied:
            service.dispatch(PORTAL, "GET", "sale", saved["id"], action)
        assert denied.value.status_code == 404


def test_concurrent_identical_create_has_one_document_and_number(service):
    request = body()
    with ThreadPoolExecutor(max_workers=4) as pool:
        docs = list(
            pool.map(
                lambda _: service.dispatch(PORTAL, "POST", "sale", payload=request)["document"],
                range(4),
            )
        )
    assert len({d["id"] for d in docs}) == 1
    assert service.dispatch(PORTAL, "GET", "sale")["total"] == 1


def test_submit_atomic_idempotent_and_recovery_never_requeues(service):
    saved = create(service)
    request = {"request_id": str(uuid4()), "version": saved["version"]}
    with ThreadPoolExecutor(max_workers=3) as pool:
        docs = list(
            pool.map(
                lambda _: service.dispatch(
                    PORTAL, "POST", "sale", saved["id"], "submit", payload=request
                )["document"],
                range(3),
            )
        )
    assert {d["state"] for d in docs} == {"queued"}
    job = dispatch.claim(service.database)
    assert dispatch.reserve(service.database, job) == "sale"
    with service.database.connection() as db:
        db.execute("UPDATE commercial_invoice_dispatch SET updated_at=now()-interval '10 minutes'")
    dispatch.recover_uncertain(service.database)
    assert dispatch.claim(service.database) is None
    current = service.dispatch(PORTAL, "GET", "sale", saved["id"])["document"]
    assert current["state"] == "unknown"
    with pytest.raises(HTTPException) as blocked:
        service.dispatch(
            PORTAL,
            "POST",
            "sale",
            saved["id"],
            "submit",
            payload={"request_id": str(uuid4()), "version": current["version"]},
        )
    assert blocked.value.status_code == 409


def test_explicit_rejection_allows_corrected_new_revision(service):
    saved = create(service)
    queued = service.dispatch(
        PORTAL,
        "POST",
        "sale",
        saved["id"],
        "submit",
        payload={"request_id": str(uuid4()), "version": 1},
    )["document"]
    job = dispatch.claim(service.database)
    dispatch.reserve(service.database, job)
    dispatch.complete(service.database, job, {"state": "rejected"})
    history = service.dispatch(PORTAL, "GET", "sale", saved["id"])["document"]["history"]
    assert [(event["actor_id"], event["actor_name"]) for event in history] == [
        (1, "Test Operator"),
        (1, "Test Operator"),
        (None, "Система"),
    ]
    edited = service.dispatch(
        PORTAL, "POST", "sale", saved["id"], "edit", payload=body(version=queued["version"])
    )["document"]
    assert edited["state"] == "draft"
    service.dispatch(
        PORTAL,
        "POST",
        "sale",
        saved["id"],
        "submit",
        payload={"request_id": str(uuid4()), "version": edited["version"]},
    )
    assert dispatch.claim(service.database)["version"] == edited["version"] + 1


def test_admin_revision_and_same_request_retry(service):
    initial = policy.administration(service.database, ADMIN)
    version = initial["users"][0]["revision"]
    request = {
        "request_id": str(uuid4()),
        "version": version,
        "grants": [{"kind": "sale", "store_id": str(STORE), "actions": ["view"]}],
    }
    result = policy.administration(service.database, ADMIN, user_id=1, body=request)
    assert result["users"][0]["revision"] == version + 1
    assert policy.administration(service.database, ADMIN, user_id=1, body=request) == result
    with pytest.raises(HTTPException) as stale:
        policy.administration(
            service.database, ADMIN, user_id=1, body={**request, "request_id": str(uuid4())}
        )
    assert stale.value.status_code == 409
    assert not service.dispatch(PORTAL, "GET", "sale", action="options")["can_create"]
