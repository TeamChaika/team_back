"""Synchronous, server-only GoTrue transport. Never expose upstream bodies."""

import time
from urllib.parse import urlsplit

import httpx

from .repository import Problem


class SupabaseAuthClient:
    def __init__(self, url, anon_key, service_key, *, transport=None):
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Invalid Supabase Auth URL")
        if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Supabase Auth requires HTTPS outside loopback")
        self.url = url.rstrip("/")
        self.anon_key, self.service_key = anon_key, service_key
        self.transport = transport

    def _request(self, method, path, payload=None, token=None, admin=False, ambiguous=False):
        key = self.service_key if admin else self.anon_key
        headers = {"apikey": key, "Authorization": "Bearer " + (token or key)}
        try:
            with httpx.Client(
                timeout=15, follow_redirects=False, transport=self.transport
            ) as client:
                response = client.request(method, self.url + path, headers=headers, json=payload)
        except httpx.HTTPError:
            raise Problem(
                503,
                "auth_ambiguous" if ambiguous else "auth_unavailable",
                "Сервис входа временно недоступен",
            ) from None
        if response.status_code >= 500 or response.status_code in (301, 302, 307, 308):
            raise Problem(
                503,
                "auth_ambiguous" if ambiguous else "auth_unavailable",
                "Сервис входа временно недоступен",
            )
        if response.status_code == 429:
            raise Problem(429, "rate_limited", "Слишком много попыток. Повторите позже")
        if not 200 <= response.status_code < 300:
            raise Problem(
                401 if not admin else 409,
                "invalid_credentials" if not admin else "auth_rejected",
                "Не удалось подтвердить учётные данные",
            )
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except ValueError:
            raise Problem(
                503,
                "auth_ambiguous" if ambiguous else "auth_unavailable",
                "Сервис входа вернул некорректный ответ",
            ) from None

    @staticmethod
    def _tokens(data):
        try:
            access, refresh = data["access_token"], data["refresh_token"]
            user = data["user"]["id"]
            if not all(isinstance(x, str) and x for x in (access, refresh, user)):
                raise ValueError()
            return {
                "access_token": access,
                "refresh_token": refresh,
                "user_id": user,
                "expires_at": time.time() + float(data["expires_in"]),
            }
        except (KeyError, ValueError, TypeError):
            raise Problem(503, "auth_unavailable", "Некорректный ответ сервиса входа") from None

    def close(self):
        """Requests own bounded clients; no persistent resources to close."""

    def login(self, email, password):
        return self._tokens(
            self._request(
                "POST", "/token?grant_type=password", {"email": email, "password": password}
            )
        )

    def refresh(self, refresh_token):
        return self._tokens(
            self._request(
                "POST",
                "/token?grant_type=refresh_token",
                {"refresh_token": refresh_token},
                ambiguous=True,
            )
        )

    def user(self, access_token):
        return self._request("GET", "/user", token=access_token)

    def create_user(self, email, password, marker):
        return self._request(
            "POST",
            "/admin/users",
            {
                "email": email,
                "password": password,
                "email_confirm": True,
                "app_metadata": {"restcontrol_request_id": marker},
            },
            admin=True,
            ambiguous=True,
        )

    def password(self, access_token, password):
        return self._request(
            "PUT", "/user", {"password": password}, token=access_token, ambiguous=True
        )

    def reset_password(self, user_id, password):
        return self._request(
            "PUT",
            "/admin/users/" + str(user_id),
            {"password": password},
            admin=True,
            ambiguous=True,
        )
