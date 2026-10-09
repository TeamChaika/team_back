"""Tenant API namespace; owner auth is never accepted here."""

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse
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
        # Remove the former narrower cookie so upgraded browsers send one identity.
        response.delete_cookie(TENANT_COOKIE, path=TENANT_BASE)
        response.set_cookie(
            TENANT_COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=mode == "production",
            max_age=8 * 3600,
            path="/",
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
        response.delete_cookie(TENANT_COOKIE, path=TENANT_BASE)
        response.delete_cookie(
            TENANT_COOKIE,
            path="/",
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
        accounts = getattr(app.state, "company_accounts", None)
        company_id = str(value["company"]["id"])
        if accounts is not None and company_id in accounts.targets:
            result = accounts.password(
                company_id,
                request.cookies.get(TENANT_COOKIE, ""),
                request.headers.get("x-csrf-token", ""),
                body.current_password.get_secret_value(),
                body.new_password.get_secret_value(),
            )
            current = repo.tenant_session(result["token"], slug)
            outgoing = (
                response
                if result["completed"]
                else JSONResponse(
                    {
                        **current,
                        "detail": "Пароль сохранён. Обновление доступа требует повторной проверки.",
                    },
                    status_code=503,
                )
            )
            cookie(outgoing, result["token"])
            return current if result["completed"] else outgoing
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
        registry = getattr(app.state, "runtime_registry", None)
        if registry is not None:
            company = repo.get(str(value["company"]["id"]))
            value = {
                **value,
                "setup_available": bool(
                    getattr(registry, "resolve_setup", lambda _: None)(company)
                ),
                "full_dashboard_ready": bool(registry.resolve(company)),
                "full_dashboard_available": bool(
                    getattr(registry, "resolve_existing", registry.resolve)(company)
                ),
            }
        return {**value, "mode": mode}
