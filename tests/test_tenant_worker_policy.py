"""Expiry blocks saved unsent queues before reservation, with no provider write."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.commercial_invoices import counterparty_dispatch
from app.commercial_invoices import dispatch as commercial_dispatch
from app.documents import dispatch as native_dispatch
from app.saas_admin.repository import Problem
from app.tenancy import worker_policy
from app.tenancy.config import TenantRuntime
from tests.test_tenant_runtime_wiring import tenant_environment


class Database:
    def __init__(self, kind="purchase"):
        self.runtime = TenantRuntime.from_env(tenant_environment())
        self.kind = kind
        self.statements = []
        self.actor = None

    @contextmanager
    def connection(self, **kwargs):
        yield self

    def execute(self, statement, params=None):
        self.statements.append(statement)
        return self

    def fetchone(self):
        return {"kind": self.kind, "actor": self.actor, "actor_id": None}


class Provider:
    def __init__(self):
        self.writes = 0
        self.preflight = []
        self.outcome = {"state": "accepted"}

    @contextmanager
    def session(self):
        self.preflight.append("authenticated")
        yield object(), "synthetic"

    def send_authenticated(self, *args):
        self.writes += 1
        return self.outcome

    def registry(self, *args):
        self.preflight.append("registry")
        return []

    def exists(self, *args, **kwargs):
        self.preflight.append("exists")
        return False

    def create(self, *args):
        self.writes += 1
        return "unknown"


def denial(mode, observed):
    def authorize(feature):
        observed.append(feature)
        if mode == "false":
            return False
        raise Problem(403 if mode == "expired" else 503, mode, "synthetic")

    return authorize


@pytest.mark.parametrize("mode", ["expired", "unavailable", "false", "missing"])
@pytest.mark.parametrize(
    "kind,feature",
    [
        ("native", "documents.dispatch"),
        ("purchase", "commercial.incoming"),
        ("sale", "commercial.outgoing"),
    ],
)
def test_saved_invoice_is_requeued_before_reservation(monkeypatch, mode, kind, feature):
    provider, calls, retried = Provider(), [], []
    db = Database(kind)
    service = SimpleNamespace(
        database=db,
        provider=provider,
        submit_enabled=True,
        feature_authorizer=None if mode == "missing" else denial(mode, calls),
    )
    dispatch = native_dispatch if kind == "native" else commercial_dispatch
    job = {"document_id": "synthetic", "operation_id": "operation", "version": 1}
    monkeypatch.setattr(dispatch, "claim", lambda database: job)
    monkeypatch.setattr(dispatch, "reserve", lambda *args: pytest.fail("Must remain unsent"))
    monkeypatch.setattr(dispatch, "complete", lambda *args: pytest.fail("No sent outcome"))
    monkeypatch.setattr(dispatch, "retry", lambda database, saved: retried.append(saved))
    assert dispatch.deliver_one(service)
    assert retried == [job]
    assert provider.writes == 0
    assert calls == ([] if mode == "missing" else [feature])


@pytest.mark.parametrize("mode", ["expired", "unavailable", "false", "missing"])
def test_counterparty_expiry_after_get_preflight_requeues(monkeypatch, mode):
    provider, calls = Provider(), []
    db = Database()
    service = SimpleNamespace(
        database=db,
        counterparty_provider=provider,
        counterparty_enabled=True,
        feature_authorizer=None if mode == "missing" else denial(mode, calls),
    )
    job = {
        "id": "synthetic",
        "claim_id": "claim",
        "source_key": "source",
        "payload": {},
        "iiko_id": "iiko",
        "code": "code",
    }
    monkeypatch.setattr(counterparty_dispatch, "claim", lambda database: job)
    monkeypatch.setattr(counterparty_dispatch, "source_key", lambda provider: "source")
    monkeypatch.setattr(counterparty_dispatch, "candidates", lambda *args: [])
    monkeypatch.setattr(
        counterparty_dispatch, "reserve", lambda *args: pytest.fail("Must remain unsent")
    )
    monkeypatch.setattr(
        counterparty_dispatch,
        "finish",
        lambda *args, **kwargs: pytest.fail("No rejected or unknown outcome"),
    )
    assert counterparty_dispatch.deliver_one(service)
    assert provider.preflight == ["authenticated", "registry", "exists", "exists"]
    assert provider.writes == 0
    assert any("SET state='queued'" in sql for sql in db.statements)
    assert calls == ([] if mode == "missing" else ["commercial.counterparties"])


def test_configure_pins_capability_and_checks_fresh_each_call(monkeypatch):
    db, calls = Database(), []
    monkeypatch.setattr(worker_policy, "load_runtime", lambda: db.runtime)
    verifier = SimpleNamespace(
        grant=SimpleNamespace(company_id=str(db.runtime.company_id), role="documents-worker"),
        require_feature=lambda feature, operation: calls.append((feature, operation)) or True,
        authorize_platform_actor=lambda *args: None,
    )
    service = worker_policy.configure(SimpleNamespace(database=db), verifier)
    for _ in range(2):
        worker_policy.require_feature(service, "documents.dispatch")
    assert calls == [("documents.dispatch", "create")] * 2
    assert service.owner_authorizer is verifier.authorize_platform_actor
    verifier.grant.company_id = "different-company"
    with pytest.raises(ValueError):
        worker_policy.configure(SimpleNamespace(database=db), verifier)


@pytest.mark.parametrize("kind", ["native", "purchase", "sale"])
def test_entitlement_is_rechecked_for_next_queued_invoice(monkeypatch, kind):
    provider, steps, retried = Provider(), [], []
    if kind == "native":
        provider.outcome = "sent"
    service = SimpleNamespace(database=Database(kind), provider=provider, submit_enabled=True)
    enabled = True

    def authorize(feature):
        steps.append("authorize")
        return enabled

    service.feature_authorizer = authorize
    dispatch = native_dispatch if kind == "native" else commercial_dispatch
    job = {
        "document_id": "synthetic",
        "operation_id": "operation",
        "version": 1,
        "kind": "waybill",
        "payload": {"xml": "<synthetic/>"},
    }
    monkeypatch.setattr(dispatch, "claim", lambda db: job)

    def reserve(*args):
        steps.append("reserve")
        return True if kind == "native" else kind

    monkeypatch.setattr(dispatch, "reserve", reserve)
    monkeypatch.setattr(dispatch, "complete", lambda *args: steps.append("complete"))
    monkeypatch.setattr(dispatch, "retry", lambda db, row: retried.append(row))
    assert dispatch.deliver_one(service)
    assert provider.writes == 1
    expected = ["authorize"] * (2 if kind == "native" else 1) + ["reserve", "complete"]
    assert steps == expected
    enabled = False
    assert dispatch.deliver_one(service)
    assert provider.writes == 1
    assert retried == [job]
    assert steps == expected + ["authorize"]


def test_legacy_service_needs_no_verifier():
    worker_policy.require_feature(SimpleNamespace(database=object()), "documents.dispatch")


@pytest.mark.parametrize("kind", ["native", "sale"])
@pytest.mark.parametrize("mode", ["revoked", "unavailable", "missing"])
def test_original_queued_owner_revocation_requeues_without_post(monkeypatch, kind, mode):
    provider, retried, calls = Provider(), [], []
    db = Database(kind)
    db.actor = {
        "company_id": str(db.runtime.company_id),
        "auth_user_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "kind": "platform_owner",
    }

    def authorize_owner(company_id, user_id):
        calls.append((str(company_id), str(user_id)))
        if mode == "unavailable":
            raise Problem(503, "unavailable", "synthetic")
        return None

    service = SimpleNamespace(
        database=db,
        provider=provider,
        submit_enabled=True,
        feature_authorizer=lambda feature: True,
        owner_authorizer=None if mode == "missing" else authorize_owner,
    )
    dispatch = native_dispatch if kind == "native" else commercial_dispatch
    job = {"document_id": "synthetic", "operation_id": "submit-operation", "version": 9}
    monkeypatch.setattr(dispatch, "claim", lambda database: job)
    monkeypatch.setattr(dispatch, "reserve", lambda *args: pytest.fail("Must remain unsent"))
    monkeypatch.setattr(dispatch, "complete", lambda *args: pytest.fail("No sent outcome"))
    monkeypatch.setattr(dispatch, "retry", lambda database, saved: retried.append(saved))
    assert dispatch.deliver_one(service)
    assert provider.writes == 0
    assert retried == [job]
    expected_actor = (db.actor["company_id"], db.actor["auth_user_id"])
    assert calls == ([] if mode == "missing" else [expected_actor])
    if kind == "native":
        assert "portal_documents_operation" in db.statements[0]
    else:
        assert "e.action='submit'" in db.statements[0]
        assert "e.version=d.version" in db.statements[0]


@pytest.mark.parametrize(
    "kind,feature", [("waybill", "documents.waybills"), ("writeoff", "documents.writeoffs")]
)
def test_native_kind_disabled_after_queue_never_posts(monkeypatch, kind, feature):
    provider, calls, retried = Provider(), [], []

    def authorize(selected):
        calls.append(selected)
        return selected != feature

    service = SimpleNamespace(
        database=Database("native"), provider=provider, feature_authorizer=authorize
    )
    job = {"kind": kind, "operation_id": "operation"}
    monkeypatch.setattr(native_dispatch, "claim", lambda db: job)
    monkeypatch.setattr(native_dispatch, "reserve", lambda *args: pytest.fail("Must remain unsent"))
    monkeypatch.setattr(native_dispatch, "retry", lambda db, saved: retried.append(saved))
    assert native_dispatch.deliver_one(service)
    assert provider.writes == 0
    assert retried == [job]
    assert calls == ["documents.dispatch", feature]
