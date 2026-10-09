"""Strict platform authorization and target-origin exchange endpoints."""

from uuid import UUID

from fastapi import Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .repository import Problem
from .tenant_routes import TENANT_BASE, TENANT_COOKIE


class SSOAuthorize(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_id: UUID
    state: str = Field(pattern=r"^[A-Za-z0-9_-]{43,128}$")
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{43,128}$")
    challenge: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class SSOExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    state: str = Field(pattern=r"^[A-Za-z0-9_-]{43,128}$")
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{43,128}$")
    verifier: str = Field(pattern=r"^[A-Za-z0-9_-]{43,128}$")


def mount_platform_sso(app, repo, owner_dependency, mode="production"):
    @app.post("/api/saas-admin/sso/authorize")
    def authorize(body: SSOAuthorize, request: Request, owner=owner_dependency):
        if not hasattr(repo, "authorize_platform"):
            raise Problem(503, "sso_unavailable", "Центральный вход ещё не настроен")
        return repo.authorize_platform(
            request.cookies.get("saas_owner_session", ""),
            str(body.company_id),
            body.state,
            body.nonce,
            body.challenge,
        )

    @app.post(TENANT_BASE + "/{slug}/auth/sso/exchange")
    def exchange(slug: str, body: SSOExchange, request: Request, response: Response):
        company = getattr(request.state, "saas_company", None)
        if not company or company["slug"] != slug:
            raise Problem(403, "invalid_sso_target", "Вход доступен только на домене компании")
        token, session = repo.exchange_platform(
            body.code,
            body.verifier,
            body.state,
            body.nonce,
            slug,
            request.headers.get("origin", ""),
            "https://" + request.headers.get("host", ""),
        )
        response.delete_cookie(TENANT_COOKIE, path=TENANT_BASE)
        response.set_cookie(
            TENANT_COOKIE,
            token,
            httponly=True,
            secure=mode == "production",
            samesite="strict",
            max_age=8 * 3600,
            path="/",
        )
        return session
