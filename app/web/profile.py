"""Authenticated self-service; no admin key or caller-supplied account identifier."""

import logging
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from starlette.concurrency import run_in_threadpool

from app.documents import telegram_link
from app.documents.service import DocumentService
from app.web.auth import ACCESS_COOKIE, LoginLimiter
from app.web.repository import Scope

logger = logging.getLogger(__name__)


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    current_password: SecretStr = Field(min_length=1, max_length=256)
    new_password: SecretStr = Field(min_length=12, max_length=128)

    @model_validator(mode="after")
    def different(self):
        if self.current_password.get_secret_value() == self.new_password.get_secret_value():
            raise ValueError("Choose a different password")
        return self


async def change_password(auth, caller_id, token, payload):
    identity = await auth.call("GET", "user", headers={"Authorization": "Bearer " + token})
    if identity.get("id") != str(caller_id) or not isinstance(identity.get("email"), str):
        raise HTTPException(403, "Не удалось подтвердить учётную запись.")
    try:
        session = await auth.call(
            "POST",
            "token?grant_type=password",
            json={
                "email": identity["email"],
                "password": payload.current_password.get_secret_value(),
            },
        )
    except HTTPException as error:
        if error.status_code == 401:
            raise HTTPException(422, "Текущий пароль указан неверно.") from None
        raise
    success = False
    try:
        if await auth.user(session["access_token"]) != UUID(str(caller_id)):
            raise HTTPException(403, "Не удалось подтвердить учётную запись.")
        try:
            updated = await auth.call(
                "PUT",
                "user",
                headers={
                    "Authorization": "Bearer " + session["access_token"],
                },
                json={"password": payload.new_password.get_secret_value()},
            )
        except HTTPException as error:
            if error.status_code == 401:
                raise HTTPException(
                    422, "Сервис входа отклонил новый пароль. Выберите другой."
                ) from None
            raise
        if updated.get("id") != str(caller_id):
            raise HTTPException(503, "Не удалось подтвердить смену пароля.")
        success = True
        return session
    finally:
        # A failed attempt must not leave an extra reauthentication session behind.
        if not success:
            try:
                await auth.call(
                    "POST",
                    "logout?scope=local",
                    headers={
                        "Authorization": "Bearer " + session["access_token"],
                    },
                )
            except HTTPException:
                logger.warning("profile_password_cleanup_failed")


def create_profile_router(access, repo):
    router = APIRouter(prefix="/api/profile", tags=["profile"])
    User = Annotated[Scope, Depends(access)]
    passwords, links = LoginLimiter(5), LoginLimiter(10)

    def native(request):
        service = request.app.state.documents
        if not isinstance(service, DocumentService):
            raise HTTPException(503, "Подключение Telegram сейчас недоступно.")
        return service

    @router.post("/password")
    async def password(payload: PasswordChange, request: Request, response: Response, scope: User):
        auth = request.app.state.auth
        auth.check_origin(request)
        passwords.check(str(scope.user["id"]))
        session = await change_password(
            auth, scope.user["id"], request.cookies[ACCESS_COOKIE], payload
        )
        try:
            await run_in_threadpool(repo.complete_password_change, scope.user["id"])
        except psycopg.Error:
            # Auth has already saved the password. Preserve the new session but do not
            # claim that the database gate was cleared after an unconfirmed commit.
            logger.warning("profile_password_completion_failed")
            partial = JSONResponse(
                {
                    "detail": "Новый пароль сохранён, но обновление доступа не подтверждено. "
                    "Обновите страницу. Если форма осталась, укажите новый пароль как текущий."
                },
                status_code=503,
            )
            auth.cookies(partial, session)
            return partial
        auth.cookies(response, session)
        return {"status": "ok"}

    @router.get("/telegram")
    async def telegram_status(request: Request, scope: User):
        service = request.app.state.documents
        if not isinstance(service, DocumentService):
            return {"available": False, "linked": False, "telegram_id": None}
        return await run_in_threadpool(telegram_link.status, service, scope.user["id"])

    @router.post("/telegram/link")
    async def telegram_issue(request: Request, scope: User):
        request.app.state.auth.check_origin(request)
        links.check(str(scope.user["id"]))
        return await run_in_threadpool(telegram_link.issue, native(request), scope.user["id"])

    @router.post("/telegram/unlink")
    async def telegram_unlink(request: Request, scope: User):
        request.app.state.auth.check_origin(request)
        links.check(str(scope.user["id"]))
        return await run_in_threadpool(telegram_link.unlink, native(request), scope.user["id"])

    return router
