"""Validate every session through Supabase; permissions come only from our database."""

import asyncio
import logging
import time
from collections import defaultdict, deque
from threading import Lock
from uuid import UUID

import httpx
from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.web.settings import WebSettings

ACCESS_COOKIE = "chaika_access"
REFRESH_COOKIE = "chaika_refresh"
AUTH_REQUEST_TIMEOUT = 15
logger = logging.getLogger(__name__)


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=256)


class LoginLimiter:
    def __init__(self, maximum: int):
        self.maximum = maximum
        self.attempts = defaultdict(deque)
        self.lock = Lock()

    def check(self, key: str):
        with self.lock:
            now = time.monotonic()
            for stale in [k for k, v in self.attempts.items() if not v or v[-1] < now - 300]:
                del self.attempts[stale]
            history = self.attempts[key]
            while history and history[0] < now - 300:
                history.popleft()
            if len(history) >= self.maximum:
                raise HTTPException(429, "Слишком много попыток входа. Повторите через 5 минут.")
            history.append(now)


class Auth:
    def __init__(self, settings: WebSettings, transport=None):
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.supabase_url.rstrip("/") + "/auth/v1/",
            headers={"apikey": settings.anon_key.get_secret_value()},
            timeout=httpx.Timeout(AUTH_REQUEST_TIMEOUT, connect=3, pool=3),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    async def call(self, method: str, path: str, **kwargs):
        # Never log a response body, URL query, email, password or bearer token.
        operation = path.partition("?")[0]
        operation = operation if operation in {"token", "user", "logout"} else "other"
        try:
            async with asyncio.timeout(AUTH_REQUEST_TIMEOUT):
                response = await self._request(method, path, operation, **kwargs)
        except TimeoutError:
            logger.warning("supabase_auth_failed operation=%s reason=deadline", operation)
            raise HTTPException(503, "Сервис входа временно недоступен.") from None
        if response.status_code >= 500:
            raise HTTPException(503, "Сервис входа временно недоступен.")
        if response.status_code == 429:
            raise HTTPException(429, "Слишком много запросов к сервису входа.")
        if response.status_code >= 400:
            raise HTTPException(401, "Не удалось войти. Проверьте почту и пароль.")
        if 300 <= response.status_code < 400:
            raise HTTPException(503, "Сервис входа временно недоступен.")
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            raise HTTPException(503, "Некорректный ответ сервиса входа.") from None
        if not isinstance(payload, dict):
            raise HTTPException(503, "Некорректный ответ сервиса входа.")
        if path.startswith("token?") and not all(
            isinstance(payload.get(key), str) and 0 < len(payload[key]) <= 8192
            for key in ("access_token", "refresh_token")
        ):
            raise HTTPException(503, "Некорректный ответ сервиса входа.")
        return payload

    async def _request(self, method: str, path: str, operation: str, **kwargs):
        for attempt in range(2):
            try:
                response = await self.client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                # A connect failure means no request was sent. After a write/read failure
                # only GET is safe: retrying token rotation could invalidate a session.
                safe = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)) or (
                    method == "GET"
                    and isinstance(
                        exc,
                        (
                            httpx.ReadError,
                            httpx.ReadTimeout,
                            httpx.WriteError,
                            httpx.WriteTimeout,
                            httpx.RemoteProtocolError,
                        ),
                    )
                )
                retry = safe and attempt == 0
                logger.warning(
                    "supabase_auth_transport operation=%s reason=%s attempt=%d retry=%s",
                    operation,
                    type(exc).__name__,
                    attempt + 1,
                    retry,
                )
                if not retry:
                    raise HTTPException(503, "Сервис входа временно недоступен.") from None
            else:
                retry = method == "GET" and response.status_code in {502, 503, 504} and attempt == 0
                if response.status_code >= 500 or 300 <= response.status_code < 400:
                    logger.warning(
                        "supabase_auth_upstream operation=%s status=%d attempt=%d retry=%s",
                        operation,
                        response.status_code,
                        attempt + 1,
                        retry,
                    )
                if not retry:
                    return response
            await asyncio.sleep(0.2)
        raise HTTPException(503, "Сервис входа временно недоступен.")

    async def user(self, token: str | None) -> UUID:
        if not token or len(token) > 8192:
            raise HTTPException(401, "Войдите в систему.")
        payload = await self.call("GET", "user", headers={"Authorization": "Bearer " + token})
        try:
            return UUID(payload["id"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(401, "Сессия недействительна.") from None

    def cookies(self, response: Response, payload: dict):
        for name, value, age in [
            (
                ACCESS_COOKIE,
                payload["access_token"],
                min(int(payload.get("expires_in", 3600)), 86400),
            ),
            (REFRESH_COOKIE, payload["refresh_token"], 7 * 86400),
        ]:
            response.set_cookie(
                name,
                value,
                httponly=True,
                secure=self.settings.secure_cookie,
                samesite="strict",
                max_age=age,
                path="/",
            )

    def check_origin(self, request: Request):
        if request.headers.get("origin") != self.settings.origin:
            raise HTTPException(403, "Недопустимый источник запроса.")
