"""Unauthenticated recovery requires the one-use Telegram credential, never an email."""

import logging

import httpx
import psycopg
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.concurrency import run_in_threadpool

from app.documents import password_recovery as recovery
from app.documents.service import DocumentService
from app.web.auth import ACCESS_COOKIE, REFRESH_COOKIE, LoginLimiter

logger = logging.getLogger(__name__)


class ResetPassword(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    token: SecretStr = Field(min_length=43, max_length=43)
    new_password: SecretStr = Field(min_length=8, max_length=128)


def reset(service, web, repo, payload, *, transport=None):
    key = web.auth_admin_key.get_secret_value()
    if not key:
        raise HTTPException(503, "Восстановление пароля сейчас недоступно.")
    ticket = recovery.claim(service, payload.token.get_secret_value())
    # Persist consumption before sending the password. Neither an ambiguous response nor
    # a lost DB commit may authorize replaying the same bearer credential.
    with service.database.connection() as db:
        recovery.locked_identity(db, ticket)
        try:
            with httpx.Client(
                timeout=httpx.Timeout(15, connect=3),
                trust_env=False,
                follow_redirects=False,
                transport=transport,
            ) as client:
                response = client.put(
                    web.supabase_url.rstrip("/")
                    + "/auth/v1/admin/users/"
                    + str(ticket["portal_id"]),
                    headers={"apikey": key, "Authorization": "Bearer " + key},
                    json={"password": payload.new_password.get_secret_value()},
                )
            confirmed = response.status_code == 200 and response.json().get("id") == str(
                ticket["portal_id"]
            )
        except (httpx.HTTPError, ValueError, AttributeError):
            confirmed = False
        if not confirmed:
            logger.warning("telegram_password_recovery_unconfirmed")
            raise HTTPException(
                503,
                "Результат смены пароля не подтверждён. Попробуйте войти с новым паролем; "
                "при необходимости запросите новую ссылку через 10 минут.",
            )
        # Supabase admin password update atomically revokes all sessions. Portal session
        # validation calls GET /user for every request, so revoked sessions are rejected.
        try:
            repo.complete_password_change(ticket["portal_id"])
        except (psycopg.Error, HTTPException):
            logger.warning("telegram_password_recovery_completion_failed")
            raise HTTPException(
                503,
                "Новый пароль сохранён. Войдите с ним; если появится обязательная смена, "
                "завершите её в профиле.",
            ) from None
    return {"status": "ok"}


def create_recovery_router(repo, web, *, transport=None):
    router = APIRouter(prefix="/api/auth/recovery", tags=["auth"])
    attempts, tokens = LoginLimiter(10), LoginLimiter(5)

    def native(request):
        service = request.app.state.documents
        if not isinstance(service, DocumentService) or not web.auth_admin_key.get_secret_value():
            raise HTTPException(503, "Восстановление пароля сейчас недоступно.")
        return service

    @router.get("/telegram")
    async def telegram(request: Request):
        attempts.check(request.client.host if request.client else "unknown")
        return await run_in_threadpool(recovery.start_url, native(request))

    @router.post("/reset")
    async def password(payload: ResetPassword, request: Request, response: Response):
        request.app.state.auth.check_origin(request)
        attempts.check(request.client.host if request.client else "unknown")
        tokens.check(recovery.digest(payload.token.get_secret_value()))
        result = await run_in_threadpool(
            reset, native(request), web, repo, payload, transport=transport
        )
        for name in (ACCESS_COOKIE, REFRESH_COOKIE):
            response.delete_cookie(
                name, path="/", secure=web.secure_cookie, httponly=True, samesite="strict"
            )
        return result

    return router
