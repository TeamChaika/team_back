"""Real PostgreSQL contracts; never contact real iiko or Telegram."""

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.documents.config import DocumentSettings
from app.documents.database import DocumentDatabase
from app.documents.dispatch import claim, complete, deliver_one, recover_uncertain, reserve
from app.documents.reconcile import reconcile
from app.documents.service import DocumentService
from app.documents.telegram import deliver_notification, handle_update
from app.documents.transport import DocumentTransport
from app.documents.worker import poll, recover_bot_jobs
from app.portal import create_portal
from app.web.settings import WebSettings
from tests.test_portal import FakeRepository, login, provider

SENDER, RECEIVER, ADMIN, FOREIGN = (UUID(int=i) for i in range(1, 5))
SOURCE, TARGET, PRODUCT = (UUID(int=i) for i in range(10, 13))


class Provider(DocumentTransport):
    def __init__(self):
        self.auth_error = self.send_error = self.hook = None
        self.outcome = "sent"
        self.sends = []

    @contextmanager
    def session(self):
        if self.auth_error:
            raise self.auth_error
        yield None, "token"

    def send_authenticated(self, client, token, kind, payload):
        self.sends.append((kind, payload))
        if self.hook:
            self.hook()
        if self.send_error:
            raise self.send_error
        return self.outcome


@pytest.fixture(scope="module")
def database():
    url = os.environ.get("CHAIKA_DOCUMENTS_TEST_DSN") or os.environ.get("CHAIKA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL required")
    if conninfo_to_dict(url).get("host") not in {"127.0.0.1", "localhost"}:
        pytest.fail("Only loopback disposable test databases are allowed")
    with psycopg.connect(url, autocommit=True) as db:
        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname='chaika_iiko_app'").fetchone():
            db.execute("CREATE ROLE chaika_iiko_app")
        if not db.execute(
            "SELECT 1 FROM pg_database WHERE datname='documents_native_tests'"
        ).fetchone():
            db.execute("CREATE DATABASE documents_native_tests")
    test_url = make_conninfo(url, dbname="documents_native_tests")
    with psycopg.connect(test_url, autocommit=True) as db:
        db.execute("DROP SCHEMA IF EXISTS chaika_iiko_documents CASCADE")
        db.execute(Path("tests/fixtures/documents_schema.sql").read_text())
        db.execute(Path("migrations/documents/0001_native_runtime.sql").read_text())
    database = DocumentDatabase(test_url)
    yield database
    database.close()


@pytest.fixture
def service(database):
    with database.connection() as db:
        db.execute(
            "TRUNCATE stores,authentication_user,portal_access,writeoffs_reasons,"
            "native_catalog,native_jobs,native_bot_updates RESTART IDENTITY CASCADE"
        )
        db.execute(
            "INSERT INTO stores (id,name) VALUES (%s,'Sender'),(%s,'Receiver')", (SOURCE, TARGET)
        )
        for i, portal_id in enumerate((SENDER, RECEIVER, ADMIN, FOREIGN), 1):
            db.execute(
                "INSERT INTO authentication_user (id,password,is_superuser,username,first_name,"
                "last_name,email,is_staff,is_active,date_joined,telegram_id) "
                "VALUES (%s,'!',false,%s,'Name','',%s,false,true,now(),%s)",
                (i, f"user{i}", f"user{i}@test.invalid", 100 + i),
            )
            db.execute(
                "INSERT INTO portal_access VALUES (%s,true,%s,%s)",
                (portal_id, Jsonb(["transfers", "writeoffs"]), portal_id == ADMIN),
            )
            db.execute(
                "INSERT INTO portal_documents_userlink (user_id,supabase_id,revision) "
                "VALUES (%s,%s,1)",
                (i, portal_id),
            )
        db.execute("SELECT setval('authentication_user_id_seq',4)")
        for uid, kind, store, actions in [
            (1, "waybill", SOURCE, ["view", "create", "edit", "copy", "cancel"]),
            (2, "waybill", TARGET, ["view", "approve"]),
            (1, "writeoff", SOURCE, ["view", "create"]),
            (2, "writeoff", SOURCE, ["view", "approve"]),
        ]:
            db.execute(
                "INSERT INTO portal_documents_grant (user_id,kind,store_id,actions) "
                "VALUES (%s,%s,%s,%s)",
                (uid, kind, store, Jsonb(actions)),
            )
        db.execute(
            "INSERT INTO native_catalog (name,data) VALUES ('products',%s)",
            (Jsonb({str(PRODUCT): "Product"}),),
        )
        db.execute(
            "INSERT INTO writeoffs_reasons (id,name,account_id) VALUES (1,'Reason',%s)", (uuid4(),)
        )
    return DocumentService(DocumentSettings(_env_file=None), database=database, provider=Provider())


def body(**changes):
    return {
        "request_id": str(uuid4()),
        "store_id": str(SOURCE),
        "counteragent_id": str(TARGET),
        "comment": "Test",
        "items": [{"product_id": str(PRODUCT), "amount": 1.25}],
        **changes,
    }


def create(service, kind="waybill", **changes):
    return service.dispatch(SENDER, "POST", kind, payload=body(**changes))


def act(service, doc, action="confirm", user=RECEIVER, payload=None):
    return service.dispatch(
        user,
        "POST",
        f"{doc['kind']}/{doc['id']}/{action}",
        payload=payload or {"request_id": str(uuid4()), "version": doc["version"]},
    )


def detail(service, doc):
    return service.dispatch(RECEIVER, "GET", f"{doc['kind']}/{doc['id']}")


def make_due(service):
    with service.database.connection() as db:
        db.execute("UPDATE native_dispatch SET next_attempt_at=now()-interval '1 minute'")


def test_confirm_returns_durable_queue_without_contacting_iiko(service):
    doc = create(service)
    result = act(service, doc)
    assert result["submission_state"] == "queued"
    assert service.provider.sends == []
    assert detail(service, doc)["actions"] == []
    with service.database.connection(readonly=True) as db:
        job = db.execute("SELECT * FROM native_dispatch").fetchone()
        assert job["version"] == result["version"]
        assert job["payload"]["xml"].startswith("<?xml")


def test_queue_sends_once_then_idempotent_http_replay_returns_result(service):
    doc = create(service)
    payload = {"request_id": str(uuid4()), "version": 1}
    queued = act(service, doc, payload=payload)
    assert act(service, doc, payload=payload) == queued
    assert deliver_one(service)
    assert not deliver_one(service)
    assert act(service, doc, payload=payload)["status"] == "Sent"
    assert len(service.provider.sends) == 1


def test_offline_auth_retries_without_document_post(service):
    doc = create(service)
    act(service, doc)
    service.provider.auth_error = httpx.ConnectTimeout("offline")
    assert deliver_one(service)
    assert detail(service, doc)["submission_state"] == "queued"
    assert not deliver_one(service)
    assert service.provider.sends == []
    service.provider.auth_error = None
    make_due(service)
    deliver_one(service)
    assert detail(service, doc)["status"] == "Sent"


@pytest.mark.parametrize("error", [httpx.ReadTimeout("lost"), httpx.WriteError("partial")])
def test_lost_response_never_automatically_resends(service, error):
    doc = create(service)
    act(service, doc)
    service.provider.send_error = error
    deliver_one(service)
    assert detail(service, doc)["submission_state"] == "unknown"
    make_due(service)
    assert not deliver_one(service)
    with pytest.raises(HTTPException) as exc:
        act(service, detail(service, doc))
    assert exc.value.status_code == 409
    assert len(service.provider.sends) == 1


def test_connection_failure_before_post_can_retry(service):
    doc = create(service)
    act(service, doc)
    service.provider.send_error = httpx.ConnectError("no TCP connection")
    deliver_one(service)
    assert detail(service, doc)["submission_state"] == "queued"
    service.provider.send_error = None
    make_due(service)
    deliver_one(service)
    assert detail(service, doc)["status"] == "Sent"


def test_parallel_web_and_bot_approval_create_one_queue_entry(service):
    doc = create(service)
    barrier = Barrier(2)

    def approve(telegram):
        barrier.wait(timeout=5)
        try:
            if telegram:
                return service.bot_action(
                    102, "waybill", "confirm", doc["id"], {"request_id": str(uuid4()), "version": 1}
                )
            return act(service, doc)
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(approve, [True, False]))
    assert sum(isinstance(x, dict) for x in results) == 1
    assert 409 in results
    with service.database.connection(readonly=True) as db:
        assert db.execute("SELECT count(*) AS n FROM native_dispatch").fetchone()["n"] == 1


def test_two_workers_do_not_send_same_queued_document(service):
    doc = create(service)
    act(service, doc)
    sending, release = Event(), Event()

    def network():
        assert detail(service, doc)["submission_state"] == "sending"
        sending.set()
        assert release.wait(5)

    service.provider.hook = network
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(deliver_one, service)
        try:
            assert sending.wait(5)
            assert pool.submit(deliver_one, service).result(timeout=5) is False
        finally:
            release.set()
        assert first.result(timeout=5)
    assert len(service.provider.sends) == 1


def test_crashed_connecting_claim_can_recover_but_old_claim_cannot_send(service):
    act(service, create(service))
    old = claim(service.database)
    with service.database.connection() as db:
        db.execute("UPDATE native_dispatch SET claimed_at=now()-interval '6 minutes'")
    new = claim(service.database)
    assert old["claim_id"] != new["claim_id"]
    assert reserve(service.database, old) is False
    assert reserve(service.database, new) is True
    assert claim(service.database) is None


def test_writeoff_uses_same_queue_and_preserves_reason_and_quantities(service):
    doc = create(service, "writeoff", reason="Reason", reason_id=1)
    act(service, doc)
    deliver_one(service)
    kind, payload = service.provider.sends[0]
    assert kind == "writeoff"
    assert payload["items"] == [{"productId": str(PRODUCT), "amount": 1.25}]
    assert detail(service, doc)["status"] == "Sent"


def test_xml_rejection_remains_unsent_and_requires_new_review(service):
    doc = create(service)
    act(service, doc)
    service.provider.outcome = "rejected"
    deliver_one(service)
    assert detail(service, doc)["status"] == "Created"
    assert detail(service, doc)["submission_state"] == "rejected"
    assert not deliver_one(service)


@pytest.mark.parametrize("amount", [0, -1, True, float("inf"), float("nan"), 1e10])
def test_invalid_items_never_partially_create(service, amount):
    with pytest.raises(HTTPException):
        create(service, items=[{"product_id": str(PRODUCT), "amount": amount}])
    assert service.dispatch(SENDER, "GET", "waybill")["total"] == 0


def test_foreign_warehouse_and_sender_approval_denied(service):
    with pytest.raises(HTTPException) as exc:
        create(service, store_id=str(TARGET), counteragent_id=str(SOURCE))
    assert exc.value.status_code == 403
    doc = create(service)
    with pytest.raises(HTTPException) as exc:
        act(service, doc, user=SENDER)
    assert exc.value.status_code == 403
    assert service.dispatch(FOREIGN, "GET", "waybill")["total"] == 0


def test_revoked_disabled_and_menu_access_checked_fresh(service):
    doc = create(service)
    with service.database.connection() as db:
        db.execute("UPDATE portal_access SET sections='[]' WHERE id=%s", (RECEIVER,))
    with pytest.raises(HTTPException):
        act(service, doc)
    with service.database.connection() as db:
        db.execute("UPDATE authentication_user SET is_active=false WHERE id=1")
    with pytest.raises(HTTPException):
        create(service)


def test_repeated_create_and_stale_edit(service):
    payload = body()
    doc = service.dispatch(SENDER, "POST", "waybill", payload=payload)
    assert service.dispatch(SENDER, "POST", "waybill", payload=payload) == doc
    assert act(service, doc, "edit", SENDER, body(version=1, comment="Changed"))["version"] == 2
    with pytest.raises(HTTPException) as exc:
        act(service, doc, "edit", SENDER, body(version=1))
    assert exc.value.status_code == 409


def test_read_filter_export_and_formula_escaping(service):
    doc = create(service, comment="=test")
    assert (
        service.dispatch(
            RECEIVER, "GET", "waybill", params={"query": doc["number"], "direction": "incoming"}
        )["total"]
        == 1
    )
    assert service.dispatch(RECEIVER, "GET", "waybill", params={"query": "%"})["total"] == 0
    exported = service.dispatch(SENDER, "GET", "waybill/export", csv=True).decode("utf-8-sig")
    assert "'=test" in exported
    assert (
        service.dispatch(SENDER, "GET", "waybill/products", params={"query": "rod"})["total"] == 1
    )


def test_admin_optimistic_revision_and_no_implicit_warehouse_access(service):
    with pytest.raises(HTTPException):
        service.dispatch(SENDER, "GET", "admin/staff")
    assert len(service.dispatch(ADMIN, "GET", "admin/staff")["rows"]) == 4
    assert service.dispatch(ADMIN, "GET", "waybill/options")["grants"] == []
    payload = {"revision": 1, "active": True, "supabase_id": str(SENDER), "grants": []}
    assert service.dispatch(ADMIN, "POST", "admin/staff/1", payload=payload)["revision"] == 2
    with pytest.raises(HTTPException) as exc:
        service.dispatch(ADMIN, "POST", "admin/staff/1", payload=payload)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException):
        create(service)


def test_notifications_do_not_resend_unknown_or_already_queued(service):
    create(service)

    class Bot:
        calls = 0

        def call(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError

    bot = Bot()
    assert deliver_notification(service, bot)
    assert not deliver_notification(service, bot)
    assert bot.calls == 1
    act(service, create(service))
    assert deliver_notification(service, bot)
    assert bot.calls == 1


def test_old_telegram_buttons_do_not_approve_and_new_buttons_enqueue(service):
    doc = create(service)

    class Bot:
        calls = []

        def call(self, method, **payload):
            self.calls.append((method, payload))

    bot = Bot()
    callback = {"id": "callback-id", "from": {"id": 102}, "data": f"confirmWaybill:{doc['id']}"}
    handle_update(service, bot, {"callback_query": callback})
    assert detail(service, doc)["submission_state"] == "idle"
    callback["data"] += ":1"
    handle_update(service, bot, {"callback_query": callback})
    assert detail(service, doc)["submission_state"] == "queued"
    assert "очереди" in bot.calls[-1][1]["text"]


def test_worker_restart_does_not_repeat_a_reserved_post(service):
    doc = create(service)
    act(service, doc)
    job = claim(service.database)
    assert reserve(service.database, job)
    with service.database.connection() as db:
        db.execute("UPDATE native_dispatch SET updated_at=now()-interval '6 minutes'")
    recover_uncertain(service.database)
    assert detail(service, doc)["submission_state"] == "unknown"
    assert not deliver_one(service)
    # A late result cannot overwrite the reconciler's state.
    complete(service.database, job, "sent")
    assert detail(service, doc)["submission_state"] == "unknown"


@pytest.mark.parametrize("outcome", ["sent", "absent"])
def test_reconciliation_requires_proof_and_new_approval_for_absent_document(service, outcome):
    doc = create(service)
    act(service, doc)
    service.provider.send_error = httpx.ReadTimeout("lost")
    deliver_one(service)
    kwargs = {
        "version": 2,
        "result": outcome,
        "operator": "Test operator",
        "evidence": "Checked document identity and all lines in iiko",
        "apply": True,
    }
    with pytest.raises(HTTPException):
        reconcile(service.database, "waybill", doc["id"], **kwargs)
    result = reconcile(service.database, "waybill", doc["id"], **kwargs, writers_stopped=True)
    assert result["version"] == 3
    assert not deliver_one(service)
    if outcome == "absent":
        assert result["submission_state"] == "idle"
        act(service, result)
        service.provider.send_error = None
        assert deliver_one(service)
    assert detail(service, doc)["status"] == "Sent"


def test_catalog_failure_does_not_destroy_existing_products(service):
    from app.documents.worker import refresh_catalogs

    def invalid_catalog():
        return {str(PRODUCT): "Replacement"}, [{"id": "invalid", "name": "Bad"}]

    service.provider.catalogs = invalid_catalog
    with pytest.raises(HTTPException):
        refresh_catalogs(service)
    assert (
        service.dispatch(SENDER, "GET", "waybill/products", params={"query": "Product"})["total"]
        == 1
    )


def test_native_http_routes_use_session_identity_and_enforce_origin_and_menu(service, monkeypatch):
    class Repository(FakeRepository):
        sections = ["transfers"]

        def scope(self, *args):
            scope = super().scope(*args)
            return replace(scope, user={**scope.user, "sections": self.sections})

    repo = Repository()
    monkeypatch.setattr(service, "close", lambda: None)  # Shared disposable fixture pool.
    app = create_portal(
        Settings(_env_file=None),
        WebSettings(_env_file=None, anon_key="test", documents_enabled=True),
        repository=repo,
        auth_transport=httpx.MockTransport(provider),
        document_service=service,
        document_settings=DocumentSettings(_env_file=None),
    )
    with TestClient(app, base_url="http://127.0.0.1:8013") as client:
        assert client.get("/api/documents/waybill").status_code == 401
        login(client)
        response = client.post(
            "/api/documents/waybill", json=body(), headers={"Origin": "https://foreign.invalid"}
        )
        assert response.status_code == 403
        assert client.get("/api/documents/waybill").json()["total"] == 0
        response = client.post(
            "/api/documents/waybill", json=body(), headers={"Origin": "http://127.0.0.1:8013"}
        )
        assert response.status_code == 201
        path = f"/api/documents/waybill/{response.json()['id']}"
        assert client.get(path).json()["items"][0]["name"] == "Product"
        assert (
            client.get("/api/documents/waybill/export")
            .headers["content-type"]
            .startswith("text/csv")
        )
        assert client.get("/api/documents/writeoff").status_code == 403
        repo.sections = []
        assert client.get(path).status_code == 403
        assert service.provider.sends == []


def test_stale_catalog_still_displays_history_but_blocks_new_items(service):
    doc = create(service)
    with service.database.connection() as db:
        db.execute("UPDATE native_catalog SET updated_at=now()-interval '31 days'")
    assert detail(service, doc)["items"][0]["name"] == "Product"
    with pytest.raises(HTTPException) as error:
        create(service)
    assert error.value.status_code == 503


def test_bot_crash_recovers_callback_idempotently_but_never_repeats_messages(service):
    doc = create(service)
    callback = {
        "callback_query": {
            "id": "crash-callback",
            "from": {"id": 102},
            "data": f"confirmWaybill:{doc['id']}:1",
        }
    }

    class Bot:
        def call(self, method, **payload):
            return [] if method == "getUpdates" else True

    bot = Bot()
    handle_update(service, bot, callback)  # Committed, then process died before marking done.
    with service.database.connection() as db:
        for number, data in [(1, callback), (2, {"message": {"text": "/pending"}})]:
            db.execute(
                "INSERT INTO native_bot_updates (id,data,state,updated_at) "
                "VALUES (%s,%s,'processing',now()-interval '6 minutes')",
                (number, Jsonb(data)),
            )
        db.execute(
            "UPDATE portal_documents_notification SET state='sending',"
            "updated_at=now()-interval '6 minutes'"
        )
    recover_bot_jobs(service)
    poll(service, bot)
    with service.database.connection(readonly=True) as db:
        assert db.execute("SELECT count(*) AS n FROM native_dispatch").fetchone()["n"] == 1
        states = db.execute("SELECT state FROM native_bot_updates ORDER BY id").fetchall()
        assert [r["state"] for r in states] == ["done", "unknown"]
        assert (
            db.execute("SELECT state FROM portal_documents_notification").fetchone()["state"]
            == "unknown"
        )
