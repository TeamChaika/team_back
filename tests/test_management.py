"""No menu grant, disabled profile or ordinary user can reach management/data APIs."""

from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.portal import create_portal
from app.web.administration import EditAccount, NewAccount, Terminal
from app.web.permissions import SECTIONS
from app.web.settings import WebSettings
from tests.test_portal import FakeRepository, login, provider


class RestrictedRepository(FakeRepository):
    sections = ["deposits"]
    admin = False

    def scope(self, *args):
        original = super().scope(*args)
        return replace(
            original,
            user={**original.user, "sections": self.sections, "is_portal_admin": self.admin},
        )


@pytest.fixture
def restricted():
    repo = RestrictedRepository()
    app = create_portal(
        Settings(_env_file=None),
        WebSettings(_env_file=None, anon_key="test"),
        repository=repo,
        auth_transport=httpx.MockTransport(provider),
    )
    with TestClient(app, base_url="http://127.0.0.1:8013") as client:
        assert login(client).status_code == 200
        yield client, repo


@pytest.mark.parametrize(
    "path",
    [
        "/api/overview",
        "/api/sales/daily",
        "/api/indicators/filters",
        "/api/resources/employees",
        "/api/resources/products",
        "/api/resources/invoices",
        "/api/resources/outgoing",
        "/api/resources/transfers",
        "/api/resources/writeoffs",
        "/api/resources/charts",
        "/api/resources/cash-shifts",
        "/api/resources/balances",
        "/api/resources/events",
        "/api/status",
        "/api/purchase-prices",
        "/api/assistant/status",
        "/api/balance-products",
        "/api/topology",
    ],
)
def test_hidden_menu_is_denied_by_server(restricted, path):
    client, _ = restricted
    assert client.get(path).status_code == 403


@pytest.mark.parametrize("path", ["/api/management/accounts", "/api/management/venues"])
def test_management_needs_explicit_admin_not_owner(restricted, path):
    client, repo = restricted
    repo.sections = list(SECTIONS)
    assert client.get(path).status_code == 403


def test_deposits_grant_can_be_revoked_without_new_login(restricted):
    client, repo = restricted
    repo.sections = ["sales"]
    assert client.get("/api/deposits/permissions").status_code == 403
    assert client.get("/api/deposits/creation-venues").status_code == 403


def test_disabled_user_cannot_use_management_or_data(restricted):
    client, repo = restricted
    repo.admin = True
    repo.active = False
    for path in ["/api/me", "/api/deposits", "/api/management/accounts", "/api/management/venues"]:
        assert client.get(path).status_code == 403


def test_admin_mutation_checks_origin_before_writing(restricted):
    client, repo = restricted
    repo.admin = True
    response = client.post(
        "/api/management/venues/00000000-0000-4000-8000-000000000001",
        headers={"Origin": "https://attacker.invalid"},
        json={"name": "A"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "fields",
    [
        dict(sections=["unknown"]),
        dict(sections=["sales"]),
        dict(sections=["deposits"], deposits_create=True),
        dict(sections=[], deposits_all=True),
        dict(sections=["deposits", "deposits"]),
        dict(is_portal_admin=True),
        dict(role="owner"),
    ],
)
def test_invalid_or_privileged_account_fields_rejected(fields):
    with pytest.raises(ValidationError):
        EditAccount.model_validate(
            {"display_name": "Test", "sections": [], "revision": 1, **fields}
        )


def test_password_and_provider_key_not_represented_in_models():
    account = NewAccount(
        request_id="00000000-0000-4000-8000-000000000001",
        email="test@example.invalid",
        password="private-password-42",
        display_name="Test",
        sections=[],
    )
    terminal = Terminal(name="Terminal", api_key="private-terminal-key")
    assert "private-password-42" not in repr(account)
    assert "private-terminal-key" not in repr(terminal)


def test_inactive_terminal_cannot_be_selected_for_payments():
    with pytest.raises(ValidationError):
        Terminal(name="Test", active=False, make_default=True)
