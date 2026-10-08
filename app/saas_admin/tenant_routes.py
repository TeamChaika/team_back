"""Tenant API namespace; owner auth is never accepted here."""

from fastapi import Depends, Request, Response
from pydantic import ConfigDict, Field, SecretStr

from .auth_validation import CredentialsModel, csrf_matches
from .repository import Problem

TENANT_BASE = "/api/saas-tenant"
TENANT_COOKIE = "saas_tenant_session"


class TenantLogin(CredentialsModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    username: str = Field(min_length=1, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=1024)


class PasswordChange(CredentialsModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    current_password: SecretStr = Field(min_length=1, max_length=1024)
    new_password: SecretStr = Field(min_length=8, max_length=1024)


def mount_tenant_routes(app, repo, mode="local"):
    base = TENANT_BASE + "/{slug}"

    def cookie(response, token):
        response.set_cookie(
            TENANT_COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=mode == "production",
            max_age=8 * 3600,
            path=TENANT_BASE,
        )

    def session(slug: str, request: Request):
        value = repo.tenant_session(request.cookies.get(TENANT_COOKIE, ""), slug)
        if request.method not in ("GET", "HEAD") and not csrf_matches(
            request.headers.get("x-csrf-token", ""), value["csrf_token"]
        ):
            raise Problem(403, "csrf_failed", "Обновите страницу и повторите действие")
        return value

    tenant_dependency = Depends(session)

    @app.post(base + "/auth/login")
    def login(slug: str, body: TenantLogin, request: Request, response: Response):
        token, value = repo.tenant_login(
            slug,
            body.username,
            body.password.get_secret_value(),
            request.client.host if request.client else "unknown",
            request.cookies.get(TENANT_COOKIE, ""),
        )
        cookie(response, token)
        return value

    @app.get(base + "/auth/me")
    def me(value=tenant_dependency):
        return value

    @app.post(base + "/auth/logout", status_code=204)
    def logout(request: Request, value=tenant_dependency):
        repo.tenant_logout(request.cookies.get(TENANT_COOKIE, ""))
        response = Response(status_code=204)
        response.delete_cookie(
            TENANT_COOKIE,
            path=TENANT_BASE,
            httponly=True,
            samesite="strict",
            secure=mode == "production",
        )
        return response

    @app.post(base + "/auth/password")
    def password(
        slug: str,
        body: PasswordChange,
        request: Request,
        response: Response,
        value=tenant_dependency,
    ):
        token, value = repo.tenant_password(
            request.cookies.get(TENANT_COOKIE, ""),
            slug,
            body.current_password.get_secret_value(),
            body.new_password.get_secret_value(),
            request.headers.get("x-csrf-token", ""),
            request.client.host if request.client else "unknown",
        )
        cookie(response, token)
        return value

    @app.get(base + "/workspace")
    def workspace(slug: str, request: Request):
        value = repo.tenant_workspace(request.cookies.get(TENANT_COOKIE, ""), slug)
        return {**value, "mode": mode}
