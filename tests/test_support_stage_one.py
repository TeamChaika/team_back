"""Support trust boundaries; real PostgreSQL tests enabled with SUPPORT_TEST_DSN."""

import os
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from pydantic import ValidationError

from app.support.capture import diagnostic
from app.support.models import Context, EventCreate, StatusWrite, TicketCreate, clean_text
from app.support.routes import mount_primary
from app.support.store import SupportStore


def test_closed_telemetry_and_redaction():
    for key in ["company_id", "actor_id", "message", "stack", "url", "headers", "body"]:
        with pytest.raises(ValidationError):
            EventCreate(client_request_id=uuid4(), code="api_error", **{key: "secret"})
    assert (
        clean_text("Ошибка https://pay.example/guest/CAPABILITY?token=SECRET и /guest/SECRET")
        == "Ошибка [ссылка удалена] и [путь удалён]"
    )
    assert "SECRET" not in clean_text("token=SECRET password=SECRET Bearer SECRET")
    assert diagnostic(ValueError("SECRET URL https://example/token")) == {
        "exception_class": "ValueError",
        "frames": [],
    }


@pytest.fixture
def pg(monkeypatch):
    dsn = os.environ.get("SUPPORT_TEST_DSN") or os.environ.get("CHAIKA_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("SUPPORT_TEST_DSN not configured")
    monkeypatch.setenv("SUPPORT_TEST_DSN", dsn)
    actor = str(uuid4())
    central = SupportStore(make_conninfo(dsn, user="restcontrol_backend"))
    primary = SupportStore(make_conninfo(dsn, user="chaika_support"))
    other = SupportStore(make_conninfo(dsn, user="other_support"))
    yield central, primary, other, Context("primary-dashboard", "Чайка", actor, "Автор")
    with psycopg.connect(dsn) as db:
        for table in ["tickets", "events", "rate_buckets"]:
            db.execute("DELETE FROM support." + table + " WHERE actor_id=%s", (actor,))


def test_pg_idempotency_isolation_spoof_and_event_link(pg):
    central, primary, other, context = pg
    event = primary.create_event(
        context, EventCreate(client_request_id=uuid4(), code="api_error", status_code=500)
    )
    payload = TicketCreate(
        client_request_id=uuid4(), description="Проверка проблемы", event_id=event["id"]
    )
    first = primary.create_ticket(context, payload)
    assert primary.create_ticket(context, payload)["id"] == first["id"]
    assert primary.detail(first["id"], context)["audit"][0]["actor_name"] == "Автор"
    assert central.detail(first["id"])["company_id"] == "primary-dashboard"
    with pytest.raises(HTTPException) as error:
        central.detail(first["id"], Context("other-company", "Other", context.actor_id))
    assert error.value.status_code == 404
    with other.connect() as db:
        assert not db.execute(
            "SELECT * FROM support.tickets WHERE id=%s", (first["id"],)
        ).fetchone()
    with pytest.raises(HTTPException) as error:
        primary.create_ticket(
            Context("other-company", "Spoof", context.actor_id),
            TicketCreate(client_request_id=uuid4(), description="Spoofed context"),
        )
    assert error.value.status_code == 503  # RLS blocks caller even with direct store misuse.
    with pytest.raises(HTTPException) as error:
        primary.create_ticket(
            context, payload.model_copy(update={"description": "Different payload"})
        )
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        central.create_ticket(
            Context("primary-dashboard", "Чайка", "another"),
            payload.model_copy(update={"client_request_id": uuid4()}),
        )
    assert error.value.status_code == 404


def test_pg_rate_dedup_conflict_audit(pg):
    central, primary, other, context = pg
    e = EventCreate(client_request_id=uuid4(), code="browser_error")
    first = primary.create_event(context, e)
    assert primary.create_event(context, e) == first
    assert (
        primary.create_event(context, e.model_copy(update={"client_request_id": uuid4()})) == first
    )
    assert central.detail(first["id"], table="events")["occurrences"] == 2
    ticket = None
    for _ in range(5):
        ticket = primary.create_ticket(
            context, TicketCreate(client_request_id=uuid4(), description="Новая проблема")
        )
    with pytest.raises(HTTPException) as error:
        primary.create_ticket(
            context, TicketCreate(client_request_id=uuid4(), description="Шестая проблема")
        )
    assert error.value.status_code == 429
    changed = central.transition(
        ticket["id"],
        StatusWrite(expected_revision=1, status="investigating", note="token=SECRET"),
        "owner",
    )
    assert changed["revision"] == 2 and len(changed["audit"]) == 2
    assert "SECRET" not in changed["audit"][1]["note"]
    with pytest.raises(HTTPException) as error:
        central.transition(
            ticket["id"], StatusWrite(expected_revision=1, status="resolved"), "owner"
        )
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        primary.transition(
            ticket["id"], StatusWrite(expected_revision=2, status="resolved"), "intruder"
        )


def test_pg_retention_and_immutable_audit(pg):
    central, primary, other, context = pg
    event = primary.create_event(context, EventCreate(client_request_id=uuid4(), code="api_error"))
    ticket = primary.create_ticket(
        context,
        TicketCreate(
            client_request_id=uuid4(), description="Проверка истории", event_id=event["id"]
        ),
    )
    with psycopg.connect(os.environ["SUPPORT_TEST_DSN"]) as db:
        db.execute(
            "UPDATE support.events SET last_seen=now()-interval '31 days' WHERE id=%s",
            (event["id"],),
        )
    assert central.prune()["events"] >= 1
    assert central.detail(ticket["id"])["event_id"] is None
    with pytest.raises(HTTPException):
        with central.connect() as db:
            db.execute("DELETE FROM support.audit WHERE ticket_id=%s", (ticket["id"],))
    with psycopg.connect(os.environ["SUPPORT_TEST_DSN"]) as db:
        db.execute(
            "UPDATE support.tickets SET updated_at=now()-interval '366 days' WHERE id=%s",
            (ticket["id"],),
        )
    assert central.prune()["tickets"] == 0
    assert central.detail(ticket["id"])["status"] == "new"
    with psycopg.connect(os.environ["SUPPORT_TEST_DSN"]) as db:
        db.execute("UPDATE support.tickets SET status='resolved' WHERE id=%s", (ticket["id"],))
    assert central.prune()["tickets"] >= 1


def test_capture_failure_cannot_break_business_request():
    class BrokenStore:
        def event(self, *args):
            raise RuntimeError("secret failure")

    app = FastAPI()
    mount_primary(app, object(), store=BrokenStore())

    @app.get("/api/business")
    def failure():
        raise ValueError("https://secret/private-token")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/business")
        assert response.status_code == 500
        assert "X-Request-ID" in response.headers
        assert "secret" not in response.text
    app.state.support_capture.queue.join()
    app.state.support_capture.close()


def test_primary_active_identity_without_business_grants(pg):
    _, store, _, context = pg

    class Repo:
        def support_identity(self, user_id):
            return {"id": user_id, "display_name": "Без заведений"}

        def portal_scope(self, user_id):
            raise HTTPException(403, "No restaurant grants")

    class Auth:
        async def user(self, token):
            if token != "valid":
                raise HTTPException(401)
            return context.actor_id

        def check_origin(self, request):
            if request.headers.get("origin") != "https://dashboard.example":
                raise HTTPException(403)

    app = FastAPI()
    app.state.auth = Auth()
    mount_primary(app, Repo(), store=store)
    with TestClient(app, cookies={"chaika_access": "valid"}) as client:
        response = client.post(
            "/api/support/tickets",
            headers={"origin": "https://dashboard.example"},
            json={"client_request_id": str(uuid4()), "description": "Нет доступа к данным"},
        )
        assert response.status_code == 200
        assert response.json()["actor_name"] == "Без заведений"
        assert client.get("/api/support/tickets").status_code == 200
        payload = {"client_request_id": str(uuid4()), "description": "Нет доступа к данным"}
        assert client.post("/api/support/tickets", json=payload).status_code == 403
        assert (
            client.post(
                "/api/support/tickets",
                headers={"origin": "https://dashboard.example"},
                json={**payload, "company_id": "another-company"},
            ).status_code
            == 422
        )
        client.cookies.clear()
        assert client.get("/api/support/tickets").status_code == 401
    app.state.support_capture.close()
