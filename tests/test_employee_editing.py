"""Employee writes are tested only against a fake iiko and isolated PostgreSQL."""

import asyncio
import os
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import UUID, uuid4
from xml.etree.ElementTree import Element, SubElement, fromstring, tostring

import httpx
import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_employee_sync import sample as sample
from test_employee_sync import stage
from test_iiko_employees import config, employee
from test_portal import USER, FakeRepository, provider

from app.integrations.iiko.employee_write import EmployeeGateway
from app.integrations.iiko.errors import IikoError
from app.portal import create_portal
from app.sync_employees import publish_employees
from app.web.auth import ACCESS_COOKIE
from app.web.employees import EmployeeCommand, EmployeeEditor, EmployeeFields, parse_card, version
from app.web.settings import WebSettings


class Upstream:
    def __init__(self):
        self.cards = {UUID(int=1): employee().encode()}
        self.posts = []
        self.requests = []
        self.timeout_after_save = False
        self.no_response = False
        self.reject = False
        self.ignore_name = False

    def visible_card(self, key):
        root = fromstring(self.cards[key])
        for node in root.findall("pinCode"):
            root.remove(node)
        return tostring(root)

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, text="employee-test-token")
        if request.url.path.endswith("/logout"):
            return httpx.Response(200, text="Connection released: employee-test-token")
        key = UUID(request.url.path.split("/")[-1])
        if request.method == "GET":
            if self.no_response and self.posts:
                raise httpx.ReadTimeout("test", request=request)
            return (
                httpx.Response(200, content=self.visible_card(key))
                if key in self.cards
                else httpx.Response(404)
            )
        assert request.method == "POST"
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        fields = parse_qs(request.content.decode(), keep_blank_values=True)
        self.posts.append((key, fields))
        if self.reject:
            return httpx.Response(403, text="private upstream content")
        root = fromstring(self.cards[key]) if key in self.cards else Element("employee")
        if root.find("id") is None:
            SubElement(root, "id").text = str(key)
        for name, values in fields.items():
            if self.ignore_name and name == "name":
                continue
            for old in root.findall(name):
                root.remove(old)
            for value in values:
                SubElement(root, name).text = value
        self.cards[key] = tostring(root)
        if self.timeout_after_save:
            raise httpx.ReadTimeout("private timeout", request=request)
        return httpx.Response(200, content=self.visible_card(key))


@pytest.fixture
def editing(sample):
    url = os.environ.get("CHAIKA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("Isolated PostgreSQL required")
    assert "127.0.0.1:15438/" in url
    with psycopg.connect(url, autocommit=True) as db, db.transaction():
        uid = uuid4()
        db.execute("INSERT INTO auth.users(id) VALUES(%s)", (uid,))
        db.execute(
            "INSERT INTO chaika.web_users(id,display_name,role) VALUES(%s,'Test','owner')", (uid,)
        )
        db.execute("SET LOCAL ROLE chaika_backend")
        _, snapshot = stage(db, sample)
        publish_employees(db, snapshot)
        db.execute(
            "INSERT INTO chaika.employee_roles(source_id,id,code,name,first_seen_at,"
            "last_seen_at,last_snapshot_id,details) "
            "VALUES('primary',%s,'001','Test',now(),now(),%s,'{}')",
            (UUID(int=99), snapshot["id"]),
        )
        db.execute(
            "INSERT INTO chaika.corporate_nodes(source_id,id,code,name,type,first_seen_at,"
            "last_seen_at,last_snapshot_id) "
            "VALUES('primary',%s,'0001','Test','DEPARTMENT',now(),now(),%s)",
            (uuid4(), snapshot["id"]),
        )
        scope = replace(FakeRepository().scope(USER), user={"id": uid, "role": "owner"})
        upstream = Upstream()

        class Editor(EmployeeEditor):
            @contextmanager
            def connection(self, scope):
                yield db

        editor = Editor(
            config(),
            SimpleNamespace(detail=lambda *a: None),
            gateway=lambda s: EmployeeGateway(s, transport=httpx.MockTransport(upstream)),
        )
        yield db, scope, upstream, editor
        raise psycopg.Rollback()


def command(upstream, **fields):
    return EmployeeCommand(
        request_id=uuid4(),
        version=version(parse_card(upstream.cards[UUID(int=1)], UUID(int=1))),
        fields=fields,
    )


def test_partial_update_readback_preserves_unedited_fields_and_other_employees(editing):
    db, scope, upstream, editor = editing
    result = editor.save(
        scope, command(upstream, name="Новое & имя", email="staff@example.com"), UUID(int=1)
    )
    assert result["status"] == "confirmed"
    assert len(upstream.posts) == 1
    assert upstream.posts[0][1] == {"name": ["Новое & имя"], "email": ["staff@example.com"]}
    assert b"private-password" in upstream.cards[UUID(int=1)]
    assert db.execute(
        "SELECT name,email FROM chaika.employees WHERE id=%s", (UUID(int=1),)
    ).fetchone() == ("Новое & имя", "staff@example.com")
    assert (
        db.execute("SELECT count(*) FROM chaika.employees WHERE present_in_latest").fetchone()[0]
        == 2
    )
    assert upstream.requests[-1].url.path.endswith("/logout")


def test_create_has_stable_identity_and_repeat_never_posts_twice(editing):
    db, scope, upstream, editor = editing
    payload = EmployeeCommand(
        request_id=uuid4(),
        fields=dict(
            name="Новый",
            code="12345",
            main_role_code="001",
            role_codes=["001"],
            department_codes=["0001"],
        ),
    )
    first = editor.save(scope, payload)
    assert editor.save(scope, payload) == first
    assert len(upstream.posts) == 1
    assert (
        db.execute("SELECT count(*) FROM chaika.employees WHERE present_in_latest").fetchone()[0]
        == 3
    )


def test_timeout_after_write_is_reconciled_without_resend(editing):
    _, scope, upstream, editor = editing
    upstream.timeout_after_save = True
    result = editor.save(scope, command(upstream, name="Updated"), UUID(int=1))
    assert result["status"] == "confirmed" and len(upstream.posts) == 1


def test_unknown_response_retry_only_reads_and_publishes(editing):
    db, scope, upstream, editor = editing
    payload = command(upstream, name="Updated")
    upstream.no_response = True
    with pytest.raises(IikoError):
        editor.save(scope, payload, UUID(int=1))
    assert (
        db.execute(
            "SELECT status FROM chaika.employee_changes WHERE id=%s", (payload.request_id,)
        ).fetchone()[0]
        == "pending"
    )
    upstream.no_response = False
    editor.save(scope, payload, UUID(int=1))
    assert len(upstream.posts) == 1


def test_stale_version_duplicate_code_and_unsupported_fields_do_not_write(editing):
    _, scope, upstream, editor = editing
    payload = command(upstream, name="Updated")
    upstream.cards[UUID(int=1)] = upstream.cards[UUID(int=1)].replace(
        b"<firstName>", b"<firstName>Changed "
    )
    with pytest.raises(HTTPException, match="Карточка изменилась"):
        editor.save(scope, payload, UUID(int=1))
    with pytest.raises(ValidationError):
        EmployeeFields(name="A", password="not allowed")
    with pytest.raises(ValidationError):
        EmployeeFields(department_codes=[])
    assert not upstream.posts


def test_mismatching_readback_stays_pending_and_explicit_refresh_imports_actual(editing):
    db, scope, upstream, editor = editing
    upstream.ignore_name = True
    payload = command(upstream, name="Ignored", email="accepted@example.com")
    with pytest.raises(HTTPException) as err:
        editor.save(scope, payload, UUID(int=1))
    assert err.value.detail["employee_pending"]
    assert db.execute("SELECT status FROM chaika.employee_changes").fetchone()[0] == "pending"
    result = editor.reconcile(scope, payload.request_id)
    assert result["status"] == "reconciled"
    assert len(upstream.posts) == 1
    assert (
        db.execute("SELECT email FROM chaika.employees WHERE id=%s", (UUID(int=1),)).fetchone()[0]
        == "accepted@example.com"
    )


def test_rejected_write_no_secrets_no_blind_retry(editing):
    db, scope, upstream, editor = editing
    upstream.reject = True
    payload = command(upstream, name="Updated")
    with pytest.raises(IikoError) as err:
        editor.save(scope, payload, UUID(int=1))
    assert "private" not in str(err.value)
    with pytest.raises(HTTPException):
        editor.save(scope, payload, UUID(int=1))
    assert len(upstream.posts) == 1
    assert db.execute("SELECT status FROM chaika.employee_changes").fetchone()[0] == "rejected"


def test_gateway_login_failure_closes_and_never_writes():
    async def run():
        with pytest.raises(IikoError):
            async with EmployeeGateway(
                config(), transport=httpx.MockTransport(lambda r: httpx.Response(403))
            ):
                pytest.fail("Should not enter")

    asyncio.run(run())


def test_card_and_write_only_pin_are_sent_exactly_and_not_published(editing):
    db, scope, upstream, editor = editing
    payload = command(upstream, pin_code="001237", card_number="000987654")
    result = editor.save(scope, payload, UUID(int=1))
    assert result["status"] == "confirmed"
    assert upstream.posts[0][1] == {"pinCode": ["001237"], "cardNumber": ["000987654"]}
    assert editor.save(scope, payload, UUID(int=1)) == result
    assert len(upstream.posts) == 1
    card = editor.read(scope, UUID(int=1))
    assert card["fields"]["card_number"] == "000987654"
    assert "pin_code" not in card["fields"]
    row = db.execute(
        "SELECT fields,pin_accepted,request_hash FROM chaika.employee_changes WHERE id=%s",
        (payload.request_id,),
    ).fetchone()
    assert row[0]["pin_code"] is True and row[1]
    assert "001237" not in str(row) and "001237" not in repr(payload)
    assert (
        "card_number"
        not in db.execute(
            "SELECT details FROM chaika.employees WHERE id=%s", (UUID(int=1),)
        ).fetchone()[0]
    )
    raw = db.execute(
        "SELECT raw FROM chaika.raw_snapshots WHERE id=(SELECT snapshot_id "
        "FROM chaika.employee_changes WHERE id=%s)",
        (payload.request_id,),
    ).fetchone()[0]
    assert b"pinCode" not in bytes(raw) and b"cardNumber" not in bytes(raw)


def test_pin_timeout_cannot_be_confirmed_by_readback_and_never_resends(editing):
    db, scope, upstream, editor = editing
    upstream.timeout_after_save = True
    payload = command(upstream, pin_code="001237")
    for _ in range(2):
        with pytest.raises(HTTPException) as err:
            editor.save(scope, payload, UUID(int=1))
        assert err.value.detail["employee_pending"]
    assert len(upstream.posts) == 1
    assert not db.execute("SELECT pin_accepted FROM chaika.employee_changes").fetchone()[0]
    result = editor.reconcile(scope, payload.request_id)
    assert result["status"] == "reconciled" and result["pin_unknown"]


def test_pin_ack_survives_read_failure_and_retries_reject_different_pin(editing):
    _, scope, upstream, editor = editing
    upstream.no_response = True
    payload = command(upstream, pin_code="001237")
    with pytest.raises(IikoError):
        editor.save(scope, payload, UUID(int=1))
    different = payload.model_copy(update={"fields": EmployeeFields(pin_code="001238")})
    with pytest.raises(HTTPException, match="уже использован"):
        editor.save(scope, different, UUID(int=1))
    upstream.no_response = False
    assert editor.save(scope, payload, UUID(int=1))["status"] == "confirmed"
    assert len(upstream.posts) == 1


def test_card_clear_and_external_card_change_are_checked(editing):
    _, scope, upstream, editor = editing
    original = command(upstream, name="Updated")
    root = fromstring(upstream.cards[UUID(int=1)])
    SubElement(root, "cardNumber").text = "000123"
    upstream.cards[UUID(int=1)] = tostring(root)
    with pytest.raises(HTTPException, match="Карточка изменилась"):
        editor.save(scope, original, UUID(int=1))
    editor.save(scope, command(upstream, card_number=""), UUID(int=1))
    assert upstream.posts[0][1] == {"cardNumber": [""]}


@pytest.mark.parametrize("pin", ["", "12a4", "１２３４", "1234\n", "1" * 33, None])
def test_invalid_pin_rejected_without_exposing_input(pin):
    with pytest.raises(ValidationError) as err:
        EmployeeFields(pin_code=pin)
    assert "input_value" not in str(err.value)


@pytest.mark.parametrize(
    "role,origin,status",
    [
        ("manager", "http://127.0.0.1:8013", 403),
        ("owner", "https://evil.example", 403),
        ("owner", "http://127.0.0.1:8013", 200),
    ],
)
def test_public_routes_require_owner_and_origin(role, origin, status):
    class Repo(FakeRepository):
        def scope(self, *args):
            scope = super().scope(*args)
            return replace(scope, user={**scope.user, "role": role})

    saved = []
    editor = SimpleNamespace(save=lambda *a: saved.append(a) or {"status": "confirmed"})
    app = create_portal(
        config(),
        WebSettings(_env_file=None, anon_key="test"),
        repository=Repo(),
        auth_transport=httpx.MockTransport(provider),
        employee_editor=editor,
    )
    with TestClient(app) as c:
        c.cookies.set(ACCESS_COOKIE, "verified")
        r = c.post(
            "/api/employees",
            headers={"Origin": origin},
            json={"request_id": str(uuid4()), "fields": {"name": "Test"}},
        )
        assert r.status_code == status
        assert bool(saved) == (status == 200)
