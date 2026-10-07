"""Synthetic counterparty commands in disposable PostgreSQL; no live iiko access."""

# Imported pytest fixtures are intentionally injected by their parameter names.
# ruff: noqa: F811

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from test_commercial_database import (  # noqa: F401
    ADMIN,
    PORTAL,
    database,
    service,
)
from test_commercial_database import (
    body as invoice_body,
)

from app.commercial_invoices import counterparties, counterparty_dispatch
from app.commercial_invoices.counterparty_models import normalize


def body(**changes):
    return {
        "request_id": str(uuid4()),
        "entity_type": "organization",
        "name": "Synthetic LLC",
        "inn": "1234567894",
        "kpp": "123401001",
        "address": "Synthetic address",
        "phone": "+79990000001",
        "email": "synthetic@example.invalid",
        **changes,
    }


class Provider:
    def __init__(self):
        self.settings = SimpleNamespace(
            iiko_url="https://iiko.invalid/resto/api", iiko_login="test"
        )
        self.records, self.posts = [], []
        self.remote, self.timeout, self.reject, self.collision = None, False, False, False

    @contextmanager
    def session(self):
        yield None, "synthetic-token"

    def registry(self, *_):
        return self.records

    def exists(self, *_, **__):
        return self.collision

    def create(self, client, token, job):
        self.posts.append(job)
        self.remote = job
        if self.timeout:
            raise TimeoutError("synthetic lost response")
        return "rejected" if self.reject else "unknown"

    def lookup(self, client, token, job):
        if self.remote is None:
            return None
        payload = job["payload"]
        return {
            **{k: payload[k] for k in ("name", "inn", "kpp", "entity_type", "address")},
            "id": str(job["iiko_id"]),
            "source": "iiko_suppliers",
            "local_fields": ["kpp", "entity_type"],
        }


@pytest.fixture
def ready(service):
    service.counterparty_enabled = True
    service.counterparty_provider = Provider()
    counterparties.grants(
        service.database,
        ADMIN,
        user_id=1,
        body={
            "request_id": str(uuid4()),
            "version": 0,
            "can_create": True,
        },
    )
    return service


def command(service, payload=None):
    return counterparties.command(service, PORTAL, "sale", payload or body())["operation"]


def result(service, operation_id):
    return counterparties.operation(service, PORTAL, "sale", operation_id)["operation"]


def test_explicit_global_grant_and_warehouse_both_required(service):
    service.counterparty_enabled = True
    service.counterparty_provider = Provider()
    with pytest.raises(HTTPException) as error:
        command(service)
    assert error.value.status_code == 403
    counterparties.grants(
        service.database,
        ADMIN,
        user_id=1,
        body={
            "request_id": str(uuid4()),
            "version": 0,
            "can_create": True,
        },
    )
    with service.database.connection() as db:
        db.execute("DELETE FROM commercial_invoice_grants")
    with pytest.raises(HTTPException) as error:
        command(service)
    assert error.value.status_code == 403
    assert not service.counterparty_provider.posts


def test_disabled_feature_never_sends(ready):
    ready.counterparty_enabled = False
    with pytest.raises(HTTPException) as error:
        command(ready)
    assert error.value.status_code == 503
    assert not counterparty_dispatch.deliver_one(ready)


def test_confirmed_creation_immediate_catalog_and_pdf_snapshot(ready):
    payload = body()
    queued = command(ready, payload)
    assert queued == {"id": payload["request_id"], "state": "queued"}
    assert command(ready, payload) == queued
    assert counterparty_dispatch.deliver_one(ready)
    confirmed = result(ready, queued["id"])
    assert confirmed["state"] == "confirmed"
    party = confirmed["counterparty"]
    assert party["kpp"] == payload["kpp"]
    listed = ready.dispatch(
        PORTAL, "GET", "sale", action="counterparties", params={"q": payload["inn"]}
    )
    assert listed["items"] == [party]
    document = ready.dispatch(
        PORTAL, "POST", "sale", payload=invoice_body(counterparty_id=party["id"])
    )["document"]
    assert document["counterparty"]["kpp"] == payload["kpp"]
    assert command(ready, payload)["state"] == "confirmed"
    assert not counterparty_dispatch.deliver_one(ready)
    assert len(ready.counterparty_provider.posts) == 1
    options = ready.dispatch(PORTAL, "GET", "sale", action="options")
    assert options["can_create_counterparty"]
    assert options["counterparty_operations"][0] == confirmed


def test_request_fingerprint_and_concurrent_identity(ready):
    payload = body()
    command(ready, payload)
    with pytest.raises(HTTPException) as error:
        command(ready, {**payload, "name": "Different"})
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        command(ready, body(name="Another name"))
    assert error.value.status_code == 409
    assert not ready.counterparty_provider.posts


def test_concurrent_same_request_returns_one_operation(ready):
    payload = body()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: command(ready, payload), range(2)))
    assert outcomes[0] == outcomes[1]
    with ready.database.connection(readonly=True) as db:
        assert (
            db.execute("SELECT count(*) AS n FROM commercial_counterparty_operations").fetchone()[
                "n"
            ]
            == 1
        )


def test_fresh_duplicate_is_never_merged_or_created(ready):
    existing = {"id": str(uuid4()), "name": "Existing", "inn": body()["inn"], "phone": "private"}
    ready.counterparty_provider.records = [existing]
    with pytest.raises(HTTPException) as error:
        command(ready)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "counterparty_duplicate"
    assert "phone" not in error.value.detail["candidates"][0]
    assert not counterparty_dispatch.deliver_one(ready)


def test_late_duplicate_and_revoked_access_block_post(ready):
    queued = command(ready)
    ready.counterparty_provider.records = [
        {"id": str(uuid4()), "name": "Other", "inn": body()["inn"]}
    ]
    counterparty_dispatch.deliver_one(ready)
    assert result(ready, queued["id"])["error_code"] == "duplicate"
    ready.counterparty_provider.records = []
    queued = command(ready)
    with ready.database.connection() as db:
        db.execute("UPDATE commercial_counterparty_grants SET can_create=false")
    counterparty_dispatch.deliver_one(ready)
    assert result(ready, queued["id"])["error_code"] == "access_revoked"
    assert not ready.counterparty_provider.posts


def test_lost_response_and_crash_only_read_back(ready):
    ready.counterparty_provider.timeout = True
    queued = command(ready)
    counterparty_dispatch.deliver_one(ready)
    assert result(ready, queued["id"])["state"] == "unknown"
    with ready.database.connection() as db:
        db.execute("UPDATE commercial_counterparty_operations SET next_attempt_at=now()")
    counterparty_dispatch.reconcile_one(ready)
    assert result(ready, queued["id"])["state"] == "confirmed"
    assert len(ready.counterparty_provider.posts) == 1
    # A durable sending record without an answer cannot re-enter the POST queue.
    with ready.database.connection() as db:
        db.execute(
            "UPDATE commercial_counterparty_operations SET state='sending', "
            "updated_at=now()-interval '6 minutes'"
        )
    counterparty_dispatch.recover(ready.database)
    assert result(ready, queued["id"])["state"] == "unknown"
    assert not counterparty_dispatch.deliver_one(ready)
    assert len(ready.counterparty_provider.posts) == 1


def test_unknown_not_found_stays_unknown_and_same_id_replay(ready):
    ready.counterparty_provider.timeout = True
    payload = body()
    queued = command(ready, payload)
    counterparty_dispatch.deliver_one(ready)
    ready.counterparty_provider.remote = None
    with ready.database.connection() as db:
        db.execute("UPDATE commercial_counterparty_operations SET next_attempt_at=now()")
    counterparty_dispatch.reconcile_one(ready)
    assert command(ready, payload)["state"] == "unknown"
    assert result(ready, queued["id"])["state"] == "unknown"
    assert len(ready.counterparty_provider.posts) == 1


def test_preflight_collision_and_source_change_never_post(ready):
    queued = command(ready)
    ready.counterparty_provider.collision = True
    counterparty_dispatch.deliver_one(ready)
    assert result(ready, queued["id"])["error_code"] == "uuid_conflict"
    ready.counterparty_provider.collision = False
    queued = command(ready)
    ready.counterparty_provider.settings.iiko_url = "https://another.invalid/resto/api"
    counterparty_dispatch.deliver_one(ready)
    assert result(ready, queued["id"])["error_code"] == "source_changed"
    assert not ready.counterparty_provider.posts


def test_grant_replay_revision_and_non_admin(ready):
    payload = {"request_id": str(uuid4()), "version": 1, "can_create": False}
    saved = counterparties.grants(ready.database, ADMIN, user_id=1, body=payload)
    assert saved["users"] == [{"id": 1, "can_create": False, "version": 2}]
    assert counterparties.grants(ready.database, ADMIN, user_id=1, body=payload) == saved
    with pytest.raises(HTTPException):
        counterparties.grants(
            ready.database, ADMIN, user_id=1, body={**payload, "request_id": str(uuid4())}
        )
    with pytest.raises(HTTPException) as error:
        counterparties.grants(ready.database, PORTAL)
    assert error.value.status_code == 403


@pytest.mark.parametrize(
    "changes",
    [
        {"supplier": True},
        {"employee": True},
        {"inn": "1234567890"},
        {"name": "\x00bad"},
        {"entity_type": "person", "inn": "", "kpp": "123456789"},
        {"email": "invalid"},
        {"entity_type": "ip", "inn": ""},
        {"name": ""},
        {"name": "a" * 201},
        {"entity_type": []},
        {"phone": "+++++"},
    ],
)
def test_allowlist_and_server_validation(changes):
    with pytest.raises(HTTPException):
        normalize(body(**changes))


def test_physical_person_inn_optional():
    assert normalize(body(entity_type="person", inn="", kpp=""))["inn"] == ""


def test_confirmed_local_identity_blocks_supplier_replication_delay(ready):
    queued = command(ready)
    counterparty_dispatch.deliver_one(ready)
    assert result(ready, queued["id"])["state"] == "confirmed"
    # The fake /suppliers still returns the pre-write snapshot.
    with pytest.raises(HTTPException) as error:
        command(ready)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "counterparty_duplicate"
    assert len(ready.counterparty_provider.posts) == 1


@pytest.mark.parametrize("state", ["queued", "unknown", "confirmed"])
@pytest.mark.parametrize("shared_contact", ["email", "address", "mixed_inn"])
def test_local_contact_duplicate_blocks_different_identity_keys(ready, state, shared_contact):
    first = body(entity_type="person", inn="", kpp="", address="", email="")
    second = {**first, "request_id": str(uuid4()), "phone": "+79990000002"}
    if shared_contact == "address":
        first["address"] = second["address"] = "Shared address"
    else:
        first["email"] = second["email"] = "shared@example.invalid"
    if shared_contact == "mixed_inn":
        first.update(entity_type="organization", inn=body()["inn"], kpp="123401001")
    queued = command(ready, first)
    if state != "queued":
        ready.counterparty_provider.timeout = state == "unknown"
        counterparty_dispatch.deliver_one(ready)
    assert result(ready, queued["id"])["state"] == state
    # /suppliers still returns its pre-write snapshot, even after confirmation.
    with pytest.raises(HTTPException) as error:
        command(ready, second)
    assert error.value.status_code == 409
    if state != "queued":
        assert not counterparty_dispatch.deliver_one(ready)
    assert len(ready.counterparty_provider.posts) == (0 if state == "queued" else 1)


def test_concurrent_shared_contact_with_different_phones_creates_one_operation(ready):
    barrier = Barrier(2)

    def registry(*_):
        barrier.wait(timeout=5)
        return []

    ready.counterparty_provider.registry = registry
    payloads = [
        body(entity_type="person", inn="", kpp="", phone=phone)
        for phone in ("+79990000001", "+79990000002")
    ]

    def submit(payload):
        try:
            return command(ready, payload)["state"]
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, payloads))
    assert outcomes.count("queued") == outcomes.count(409) == 1
    with ready.database.connection(readonly=True) as db:
        assert db.execute(
            "SELECT count(*) AS n FROM commercial_counterparty_operations"
        ).fetchone()["n"] == 1
    assert not ready.counterparty_provider.posts


def test_same_name_with_distinct_contacts_can_create_separate_people(ready):
    for phone in ("+79990000001", "+79990000002"):
        assert command(
            ready, body(entity_type="person", inn="", kpp="", address="", email="", phone=phone)
        )["state"] == "queued"


def test_original_portal_identity_cannot_be_relinked_before_post(ready):
    queued = command(ready)
    with ready.database.connection() as db:
        db.execute("UPDATE portal_documents_userlink SET supabase_id=%s", (ADMIN,))
    counterparty_dispatch.deliver_one(ready)
    with ready.database.connection(readonly=True) as db:
        row = db.execute(
            "SELECT state,error_code FROM commercial_counterparty_operations WHERE id=%s",
            (queued["id"],),
        ).fetchone()
    assert row == {"state": "rejected", "error_code": "access_revoked"}
    assert not ready.counterparty_provider.posts


def test_refresh_race_preserves_confirmation_then_removal_excludes_card(ready, monkeypatch):
    import json

    from test_commercial_database import PRODUCT, UNIT

    from app.commercial_invoices import catalogs

    queued = command(ready)
    ready.provider = ready.counterparty_provider
    with ready.database.connection() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS native_jobs(name text PRIMARY KEY,data jsonb,"
            "updated_at timestamptz DEFAULT now())"
        )

    confirm_during_fetch = True

    def get(client, token, endpoint, params=()):
        nonlocal confirm_during_fetch
        if endpoint.endswith("products/list"):
            return json.dumps(
                [
                    {
                        "id": str(PRODUCT),
                        "name": "Synthetic product",
                        "mainUnit": str(UNIT),
                        "type": "GOODS",
                        "deleted": False,
                    }
                ]
            ).encode()
        if endpoint.endswith("entities/list"):
            return json.dumps(
                [{"id": str(UNIT), "name": "шт.", "rootType": "MeasureUnit", "deleted": False}]
            ).encode()
        if confirm_during_fetch:
            counterparty_dispatch.deliver_one(ready)
            confirm_during_fetch = False
        return b"<employees/>"

    monkeypatch.setattr(catalogs, "_get", get)
    catalogs.refresh(ready)
    confirmed = result(ready, queued["id"])["counterparty"]
    listed = ready.dispatch(
        PORTAL, "GET", "sale", action="counterparties", params={"q": confirmed["inn"]}
    )
    assert listed["items"] == [confirmed]
    catalogs.refresh(ready)
    listed = ready.dispatch(
        PORTAL, "GET", "sale", action="counterparties", params={"q": confirmed["inn"]}
    )
    assert listed["items"] == []


def test_refresh_retains_local_fields_with_write_disabled(ready, monkeypatch):
    import json
    from xml.etree.ElementTree import Element, SubElement, tostring

    from test_commercial_database import PRODUCT, UNIT

    from app.commercial_invoices import catalogs

    queued = command(ready)
    counterparty_dispatch.deliver_one(ready)
    confirmed = result(ready, queued["id"])["counterparty"]
    ready.counterparty_enabled = False
    ready.provider = ready.counterparty_provider
    with ready.database.connection() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS native_jobs(name text PRIMARY KEY,data jsonb,"
            "updated_at timestamptz DEFAULT now())"
        )

    def get(client, token, endpoint, params=()):
        if endpoint.endswith("products/list"):
            return json.dumps(
                [
                    {
                        "id": str(PRODUCT),
                        "name": "Synthetic product",
                        "mainUnit": str(UNIT),
                        "type": "GOODS",
                        "deleted": False,
                    }
                ]
            ).encode()
        if endpoint.endswith("entities/list"):
            return json.dumps(
                [{"id": str(UNIT), "name": "шт.", "rootType": "MeasureUnit", "deleted": False}]
            ).encode()
        root = Element("employees")
        party = SubElement(root, "employee")
        for key, value in {
            "id": confirmed["id"],
            "name": "Renamed in iiko",
            "taxpayerIdNumber": confirmed["inn"],
            "supplier": "true",
            "employee": "false",
            "deleted": "false",
        }.items():
            SubElement(party, key).text = value
        return tostring(root)

    monkeypatch.setattr(catalogs, "_get", get)
    catalogs.refresh(ready)
    party = ready.dispatch(
        PORTAL, "GET", "sale", action="counterparties", params={"q": confirmed["inn"]}
    )["items"][0]
    assert party["name"] == "Renamed in iiko"
    assert party["kpp"] == confirmed["kpp"]
    assert party["entity_type"] == "organization"
