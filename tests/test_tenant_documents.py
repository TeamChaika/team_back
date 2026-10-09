"""Existing document services on two freshly provisioned restricted tenant databases."""

from dataclasses import replace
from datetime import datetime
from uuid import UUID
from xml.etree.ElementTree import fromstring
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from app.commercial_invoices.service import CommercialInvoiceService
from app.documents.config import DocumentSettings
from app.documents.context import fixed_lock, lock_resource
from app.documents.database import DocumentDatabase
from app.documents.messages import document_messages
from app.documents.service import DocumentService
from app.documents.transport import DocumentTransport
from app.documents.worker import worker_leader
from app.tenancy.migrations import provision_tenant
from app.tenancy.sql import render
from tests.test_tenant_migrations_postgres import empty_database  # noqa: F401

SENDER, RECEIVER, SOURCE, TARGET, PRODUCT, PARTY, UNIT, REQUEST = [UUID(int=i) for i in range(1, 9)]


class NoNetwork(DocumentTransport):
    def __init__(self):
        pass

    def session(self):
        raise AssertionError("No external iiko calls allowed in tenant acceptance")


class CommercialNoNetwork:
    def payload(self, kind, snapshot):
        return b"<synthetic/>"

    def session(self):
        raise AssertionError("No external iiko calls allowed in tenant acceptance")


@pytest.fixture
def companies(empty_database):  # noqa: F811 - imported shared pytest fixture
    operator, dsn, tenant = empty_database
    result = []
    for label in ("Company A", "Company B"):
        runtime = tenant()
        runtime = replace(runtime, timezone="Asia/Tokyo")
        provision_tenant(operator, runtime)
        database = DocumentDatabase(make_conninfo(dsn, user=runtime.database_role), runtime=runtime)
        settings = DocumentSettings(_env_file=None, dashboard_url=runtime.frontend_origin)
        with database.connection() as db:
            for i, portal in enumerate((SENDER, RECEIVER), 1):
                db.execute(
                    render(
                        "INSERT INTO {analytics}.web_users(id,display_name,role,sections) "
                        "VALUES(%s,%s,'manager',ARRAY['transfers','writeoffs','invoices','outgoing'])",
                        runtime,
                    ),
                    (portal, label),
                )
                db.execute(
                    "INSERT INTO authentication_user(id,password,is_superuser,username,"
                    "first_name,last_name,email,is_staff,is_active,date_joined,telegram_id) "
                    "VALUES(%s,'!',false,%s,%s,'','',false,true,now(),%s)",
                    (i, f"person{i}", label, 100 + i),
                )
                db.execute(
                    "INSERT INTO portal_documents_userlink(user_id,supabase_id,revision) "
                    "VALUES(%s,%s,1)",
                    (i, portal),
                )
            db.execute(
                "INSERT INTO stores(id,name) VALUES(%s,'Sender'),(%s,'Receiver')", (SOURCE, TARGET)
            )
            for user, store, actions in (
                (1, SOURCE, ["view", "create", "edit", "copy", "cancel"]),
                (2, TARGET, ["view", "approve"]),
            ):
                db.execute(
                    "INSERT INTO portal_documents_grant(user_id,kind,store_id,actions) "
                    "VALUES(%s,'waybill',%s,%s)",
                    (user, store, Jsonb(actions)),
                )
            db.execute(
                "INSERT INTO writeoffs_reasons(id,name,account_id) VALUES(1,'Reason',%s)", (UNIT,)
            )
            db.execute(
                "INSERT INTO portal_documents_grant(user_id,kind,store_id,actions) "
                "VALUES(1,'writeoff',%s,%s)",
                (SOURCE, Jsonb(["view", "create"])),
            )
            db.execute(
                "INSERT INTO native_catalog(name,data) VALUES('products',%s)",
                (Jsonb({str(PRODUCT): label + " product"}),),
            )
            db.execute(
                "INSERT INTO commercial_invoice_grants VALUES(1,'sale',%s,%s)",
                (SOURCE, Jsonb(["view", "create", "edit", "submit"])),
            )
            for name, rows in {
                "commercial_products": [
                    {
                        "id": str(PRODUCT),
                        "name": label + " product",
                        "unit_id": str(UNIT),
                        "unit": "шт.",
                    }
                ],
                "commercial_sale_counterparties": [
                    {
                        "id": str(PARTY),
                        "name": label + " buyer",
                        "inn": "",
                        "kpp": "",
                        "address": "",
                    }
                ],
            }.items():
                db.execute(
                    "INSERT INTO native_catalog(name,data) VALUES(%s,%s)", (name, Jsonb(rows))
                )
        result.append(
            (
                DocumentService(settings, database=database, provider=NoNetwork()),
                CommercialInvoiceService(
                    database,
                    CommercialNoNetwork(),
                    seller={
                        "name": label,
                        "inn": "1234567890",
                        "bank_name": "Test Bank",
                        "bic": "123456789",
                        "account": "12345678901234567890",
                        "correspondent_account": "12345678901234567890",
                    },
                    submit_enabled=True,
                ),
            )
        )
    try:
        yield result
    finally:
        for service, _ in result:
            service.close()


def test_existing_document_workflow_and_commercial_drafts_are_isolated(companies):
    documents = []
    for index, (service, commercial) in enumerate(companies):
        body = {
            "request_id": str(REQUEST),
            "store_id": str(SOURCE),
            "counteragent_id": str(TARGET),
            "comment": f"tenant-{index}",
            "items": [{"product_id": str(PRODUCT), "amount": 1.25}],
        }
        doc = service.dispatch(SENDER, "POST", "waybill", payload=body)
        assert doc["id"] == 1
        assert service.dispatch(SENDER, "POST", "waybill", payload=body) == doc
        detail = service.dispatch(RECEIVER, "GET", f"waybill/{doc['id']}")
        assert detail["comment"] == f"tenant-{index}"
        assert ("Company A" if index == 0 else "Company B") in detail["items"][0]["name"]
        texts = "".join(page[0] for page in document_messages("waybill", detail, service.database))
        assert "CHAIKA" not in texts
        approved = service.dispatch(
            RECEIVER,
            "POST",
            "waybill/1/confirm",
            payload={"request_id": str(UUID(int=90)), "version": doc["version"]},
        )
        assert approved["submission_state"] == "queued"
        with service.database.connection(readonly=True) as db:
            queue = db.execute("SELECT * FROM native_dispatch").fetchall()
            assert len(queue) == 1 and queue[0]["document_id"] == 1
            assert "DJ000001" in queue[0]["payload"]["xml"]
            assert fromstring(queue[0]["payload"]["xml"]).findtext("dateIncoming") == (
                datetime.fromisoformat(detail["created_at"])
                .astimezone(ZoneInfo("Asia/Tokyo"))
                .isoformat(timespec="seconds")
            )
        with pytest.raises(HTTPException) as denied:
            service.dispatch(UUID(int=999), "GET", "waybill/1")
        assert denied.value.status_code == 403
        commercial_body = {
            "request_id": str(UUID(int=100)),
            "store_id": str(SOURCE),
            "counterparty_id": str(PARTY),
            "date": "2026-10-08",
            "comment": f"tenant-{index}",
            "items": [
                {
                    "product_id": str(PRODUCT),
                    "quantity": "2",
                    "price": "100.01",
                    "price_includes_vat": True,
                    "vat_rate": "20",
                }
            ],
        }
        created = commercial.mutate(SENDER, "sale", None, "create", commercial_body)
        assert commercial.mutate(SENDER, "sale", None, "create", commercial_body) == created
        assert created["document"]["number"] == "CI-S-00000001"
        assert created["document"]["seller"]["name"] == ("Company A" if index == 0 else "Company B")
        assert commercial.dispatch(
            SENDER, "GET", "sale", created["document"]["id"], action="pdf"
        ).startswith(b"%PDF-")
        exported = service.dispatch(SENDER, "GET", "waybill/export", csv=True).decode("utf-8-sig")
        assert f"tenant-{index}" in exported
        assert f"tenant-{1 - index}" not in exported
        documents.append(created["document"]["id"])
    first, second = companies
    assert second[1].dispatch(SENDER, "GET", "sale", documents[1])["document"]["id"] == documents[1]
    with pytest.raises(HTTPException) as denied:
        first[1].dispatch(SENDER, "GET", "sale", documents[1], action="pdf")
    assert denied.value.status_code == 404


def test_worker_locks_for_two_companies_do_not_block_each_other(companies):
    first, second = [pair[0].database for pair in companies]
    with worker_leader(first) as a:
        assert a is not None
        with worker_leader(second) as b:
            assert b is not None
            assert fixed_lock(a, "documents-worker", 1) != fixed_lock(b, "documents-worker", 1)
            assert lock_resource(a, "same-request") != lock_resource(b, "same-request")
        with worker_leader(first) as blocked:
            assert blocked is None


def test_tenant_document_settings_never_inherit_chaika_credentials(monkeypatch, tmp_path):
    from tests.test_tenant_migrations import runtime_for

    runtime = runtime_for()
    monkeypatch.setattr("app.documents.config.load_runtime", lambda: runtime)
    monkeypatch.setenv("CHAIKA_DOCUMENTS_DATABASE_URL", "legacy-database")
    monkeypatch.setenv("CHAIKA_DOCUMENTS_IIKO_LOGIN", "legacy-login")
    monkeypatch.setenv("CHAIKA_DOCUMENTS_BOT_TOKEN", "legacy-bot")
    dotenv = tmp_path / ".env"
    dotenv.write_text("CHAIKA_DOCUMENTS_IIKO_PASSWORD_HASH=legacy-password\n")
    settings = DocumentSettings(_env_file=dotenv)
    assert settings.dashboard_url == runtime.frontend_origin
    assert settings.database_url.get_secret_value() == ""
    assert settings.iiko_login == ""
    assert settings.iiko_password_hash.get_secret_value() == ""
    assert settings.bot_token.get_secret_value() == ""
    with pytest.raises(ValueError, match="database URL"):
        DocumentSettings(_env_file=None, native_enabled=True)
    monkeypatch.setenv("RESTCONTROL_TENANT_DOCUMENTS_DATABASE_URL", "own-database")
    monkeypatch.setenv(
        "RESTCONTROL_TENANT_DOCUMENTS_IIKO_URL", "https://customer-iiko.example/resto/api"
    )
    monkeypatch.setenv("RESTCONTROL_TENANT_DOCUMENTS_IIKO_LOGIN", "own-login")
    monkeypatch.setenv("RESTCONTROL_TENANT_DOCUMENTS_IIKO_PASSWORD_HASH", "own-password")
    configured = DocumentSettings(_env_file=dotenv, native_enabled=True)
    assert configured.iiko_login == "own-login"
    assert configured.database_url.get_secret_value() == "own-database"
    with pytest.raises(ValueError, match="frontend origin"):
        DocumentSettings(_env_file=None, dashboard_url="https://dashboard.chaika.team")
    with pytest.raises(ValueError, match="credentials"):
        DocumentSettings(_env_file=None, native_enabled=True, iiko_url="https://iiko.chaika.team")


def test_writeoff_cost_queries_use_each_tenant_analytics(companies):
    for index, (service, _) in enumerate(companies):
        payload = {
            "request_id": str(UUID(int=200)),
            "store_id": str(SOURCE),
            "reason": "Reason",
            "reason_id": 1,
            "comment": f"writeoff-{index}",
            "items": [{"product_id": str(PRODUCT), "amount": 1}],
        }
        saved = service.dispatch(SENDER, "POST", "writeoff", payload=payload)
        detail = service.dispatch(SENDER, "GET", f"writeoff/{saved['id']}")
        assert detail["comment"] == f"writeoff-{index}"
        assert detail["cost_estimate"]["unpriced_count"] == 1
        assert detail["cost_estimate"]["total"] is None
