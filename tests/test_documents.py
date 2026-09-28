"""The BFF never widens a menu grant or retries an uncertain document write."""

import asyncio
from dataclasses import replace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.portal import create_portal
from app.web.documents import DocumentsClient
from app.web.settings import WebSettings
from tests.test_portal import FakeRepository, login, provider


class DocumentRepository(FakeRepository):
    sections = ["transfers"]
    admin = False

    def scope(self, *args):
        scope = super().scope(*args)
        return replace(
            scope, user={**scope.user, "sections": self.sections, "is_portal_admin": self.admin}
        )


@pytest.fixture
def documents():
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer verified"
        if request.url.path.endswith("/export"):
            return httpx.Response(
                200,
                content=b"number;status\nDJ000001;Created",
                headers={"Content-Type": "text/csv"},
            )
        if request.url.path.endswith("/1/confirm"):
            raise httpx.ReadTimeout("provider response lost")
        return httpx.Response(200, json={"rows": []})

    repo = DocumentRepository()
    app = create_portal(
        Settings(_env_file=None),
        WebSettings(_env_file=None, anon_key="test", documents_enabled=True),
        repository=repo,
        auth_transport=httpx.MockTransport(provider),
        documents_transport=httpx.MockTransport(upstream),
    )
    with TestClient(app, base_url="http://127.0.0.1:8013") as client:
        yield client, repo, calls


def test_anonymous_cannot_contact_document_service(documents):
    client, _, calls = documents
    assert client.get("/api/documents/waybill").status_code == 401
    assert calls == []


def test_menu_rights_apply_to_fixed_document_types(documents):
    client, repo, calls = documents
    login(client)
    assert client.get("/api/documents/waybill").status_code == 200
    assert calls[-1].url.path == "/api/portal-documents/waybill"
    assert client.get("/api/documents/writeoff").status_code == 403
    repo.sections = []
    assert client.get("/api/documents/waybill").status_code == 403
    assert len(calls) == 1


def test_readonly_access_does_not_bypass_origin_on_mutations(documents):
    client, _, calls = documents
    login(client)
    response = client.post(
        "/api/documents/waybill", json={}, headers={"Origin": "https://attacker.invalid"}
    )
    assert response.status_code == 403
    assert calls == []


def test_uncertain_post_is_not_retried(documents):
    client, _, calls = documents
    login(client)
    response = client.post(
        "/api/documents/waybill/1/confirm",
        json={"request_id": "00000000-0000-4000-8000-000000000001", "version": 1},
        headers={"Origin": "http://127.0.0.1:8013"},
    )
    assert response.status_code == 503
    assert len(calls) == 1
    assert "provider response" not in response.text


def test_staff_directory_requires_explicit_management_role(documents):
    client, repo, calls = documents
    login(client)
    assert client.get("/api/documents/admin/staff").status_code == 403
    assert calls == []
    repo.admin = True
    repo.sections = []
    assert client.get("/api/documents/admin/staff").status_code == 200


def test_export_checks_menu_and_returns_csv(documents):
    client, _, _ = documents
    login(client)
    response = client.get("/api/documents/waybill/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")


def test_arbitrary_upstream_path_is_not_forwarded(documents):
    client, _, calls = documents
    login(client)
    assert client.get("/api/documents/auth/users").status_code in {403, 404, 422}
    assert calls == []


@pytest.mark.parametrize("code", [{"unexpected": "value"}, ["version"]])
def test_malformed_upstream_error_code_still_returns_safe_error(code):
    async def scenario():
        service = DocumentsClient(
            "https://documents.invalid",
            True,
            httpx.MockTransport(
                lambda request: httpx.Response(
                    409, json={"code": code, "detail": "private upstream data"}
                )
            ),
        )
        try:
            with pytest.raises(HTTPException) as error:
                await service.call("test-token", "POST", "waybill/1/confirm")
            assert error.value.status_code == 409
            assert "private" not in error.value.detail
        finally:
            await service.client.aclose()

    asyncio.run(scenario())
