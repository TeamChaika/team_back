"""Synthetic capability and strict payment-origin path contracts."""

from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.tenancy.config import RuntimeConfigurationError
from app.tenant_payments.guest_boundary import guest_method_allowed
from app.tenant_payments.store import PaymentStore, digest
from tests.test_tenant_migrations import runtime_for


class Vault:
    def decrypt(self, value):
        return value


class RecordingStore(PaymentStore):
    def __init__(self, runtime):
        self.runtime = runtime
        self.vault = Vault()
        self.writes = []

    def execute(self, db, query, values):
        self.writes.append((query, values))


def test_short_link_is_stable_tenant_bound_hashed_and_not_original_secret():
    runtime = replace(runtime_for(), payment_origin="https://pay.customer.example")
    store = RecordingStore(runtime)
    row = {
        "id": uuid4(),
        "encrypted_guest_token": "synthetic-capability-for-this-test-only",
        "guest_token_hash": "synthetic-hash",
    }
    url = store.guest_url(row, db=object())
    code = url.rsplit("/", 1)[1]
    assert len(code) == 32 and "?" not in url and str(row["id"]) not in url
    assert store.guest_url(row, db=object()) == url
    assert store.writes[0][1] == (digest(code), row["id"], row["guest_token_hash"])
    assert row["encrypted_guest_token"] not in str(store.writes)
    other = RecordingStore(replace(runtime_for(uuid4()), payment_origin=runtime.payment_origin))
    assert other.guest_url(row, db=object()) != url
    with pytest.raises(ValueError):
        store.guest_url(row)


@pytest.mark.parametrize("code", ["", "a" * 31, "a" * 33, "/" * 32, "é" * 32])
def test_invalid_code_rejected_before_database_access(code):
    store = RecordingStore(runtime_for())
    with pytest.raises(HTTPException) as error:
        store.resolve_guest_link(code)
    assert error.value.status_code == 404


@pytest.mark.parametrize(
    "path,method,allowed",
    [
        ("/api/guest-links/" + "a" * 32, "GET", True),
        ("/api/guest-links/" + "a" * 32 + "/prepare", "POST", True),
        ("/api/guest-links/" + "a" * 32 + "/reconcile", "POST", True),
        ("/api/guest-deposits/" + str(uuid4()), "GET", True),
        ("/api/me", "GET", False),
        ("/api/auth/logout", "POST", False),
        ("/api/guest-links/" + "a" * 32 + "/prepare", "GET", False),
        ("/api/guest-links/" + "a" * 32 + "/anything", "POST", False),
    ],
)
def test_payment_origin_only_allows_guest_routes(path, method, allowed):
    assert guest_method_allowed(path, method) is allowed


def test_payment_origin_requires_separate_exact_https_origin():
    for origin in (
        "https://customer.example",
        "https://api.customer.example",
        "http://pay.example",
        "https://pay.example/path",
    ):
        with pytest.raises(RuntimeConfigurationError):
            replace(runtime_for(), payment_origin=origin)
