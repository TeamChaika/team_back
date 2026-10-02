"""Short login and telephone aliases preserve exact accounts and access checks."""

import json
from contextlib import contextmanager
from unittest.mock import Mock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.portal import create_portal
from app.web.auth import Login, login_candidates
from app.web.repository import Repository
from app.web.settings import WebSettings
from tests.test_portal import FakeRepository, provider


@pytest.mark.parametrize(
    "value,expected",
    [
        ("  GastroDvor  ", ("gastrodvor@chaika.team",)),
        ("1", ("1@chaika.team",)),
        ("AV@CHAIKA.TEAM", ("av@chaika.team",)),
        ("Person+tag@Other.Example", ("person+tag@other.example",)),
        ("+79781234567@chaika.team", ("+79781234567@chaika.team",)),
        ("89781234567@chaika.team", ("89781234567@chaika.team",)),
    ],
)
def test_short_names_and_explicit_emails(value, expected):
    payload = Login(email=value, password=" secret ")
    assert login_candidates(payload.email) == expected
    assert payload.password.get_secret_value() == " secret "


@pytest.mark.parametrize("value", ["+7 (978) 123-45-67", "79781234567", "8 978 123 45 67"])
def test_phone_format_variations_have_same_candidates(value):
    assert login_candidates(Login(email=value, password="secret").email) == (
        "79781234567@chaika.team",
        "+79781234567@chaika.team",
        "89781234567@chaika.team",
    )


@pytest.mark.parametrize("value", ["", "  ", "name other", "name@@chaika.team", "a" * 250])
def test_invalid_login_is_rejected(value):
    with pytest.raises(ValidationError):
        Login(email=value, password="secret")


@pytest.fixture
def login_client():
    calls = []

    def auth_provider(request):
        calls.append(request)
        return provider(request)

    repo = FakeRepository()
    repo.resolve_login_email = Mock(return_value="+79781234567@chaika.team")
    app = create_portal(
        Settings(_env_file=None),
        WebSettings(_env_file=None, anon_key="test"),
        auth_transport=httpx.MockTransport(auth_provider),
        repository=repo,
    )
    with TestClient(app, base_url="http://127.0.0.1:8013") as client:
        yield client, repo, calls


def sign_in(client, login):
    return client.post(
        "/api/auth/login",
        headers={"Origin": "http://127.0.0.1:8013"},
        json={"email": login, "password": "unchanged secret"},
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", "1@chaika.team"),
        ("gastrodvor", "gastrodvor@chaika.team"),
        ("+79781234567@chaika.team", "+79781234567@chaika.team"),
    ],
)
def test_names_and_full_emails_skip_alias_lookup(login_client, value, expected):
    client, repo, calls = login_client
    assert sign_in(client, value).status_code == 200
    repo.resolve_login_email.assert_not_called()
    assert json.loads(calls[0].content) == {"email": expected, "password": "unchanged secret"}


def test_phone_logs_in_once_with_existing_account_email(login_client):
    client, repo, calls = login_client
    assert sign_in(client, "79781234567").status_code == 200
    repo.resolve_login_email.assert_called_once_with(login_candidates("79781234567"))
    tokens = [r for r in calls if r.url.path.endswith("/token")]
    assert len(tokens) == 1
    assert json.loads(tokens[0].content)["email"] == "+79781234567@chaika.team"


def test_ambiguous_phone_does_not_try_passwords_or_create_session(login_client):
    client, repo, calls = login_client
    repo.resolve_login_email.side_effect = HTTPException(401, "Введите полный логин")
    response = sign_in(client, "+7 (978) 123-45-67")
    assert response.status_code == 401
    assert not calls
    assert not response.headers.get("set-cookie")


def test_phone_does_not_bypass_disabled_account(login_client):
    client, repo, calls = login_client
    repo.active = False
    response = sign_in(client, "+7 (978) 123-45-67")
    assert response.status_code == 403
    assert not response.headers.get("set-cookie")


@pytest.mark.parametrize(
    "matches,expected",
    [
        ([], "79781234567@chaika.team"),
        ([{"email": "+79781234567@chaika.team"}], "+79781234567@chaika.team"),
        ([{"email": "79781234567@chaika.team"}, {"email": "+79781234567@chaika.team"}], None),
    ],
)
def test_repository_resolves_only_unique_identity(matches, expected):
    db = Mock()
    db.execute.return_value.fetchall.return_value = matches

    @contextmanager
    def connection():
        yield db

    repo = object.__new__(Repository)
    repo.connection = connection
    candidates = login_candidates("79781234567")
    if expected:
        assert repo.resolve_login_email(candidates) == expected
    else:
        with pytest.raises(HTTPException) as exc:
            repo.resolve_login_email(candidates)
        assert exc.value.status_code == 401
    assert db.execute.call_args.args[1] == (list(candidates),)
