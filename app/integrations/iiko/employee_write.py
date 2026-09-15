"""iikoServer employee GET and partial POST; no full-replacement PUT or retries.

Contract: ru.iiko.help/articles/#!api-documentations/rabota-s-dannymi-sotrudnikovv
Confirmed against the deployed Chain application.wadl on 2026-09-15.
"""

import asyncio
from urllib.parse import urlencode
from uuid import UUID

import httpx

from app.integrations.iiko.client import IikoClient
from app.integrations.iiko.errors import IikoError


class EmployeeGateway:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.client = IikoClient(settings, transport)
        self.token = None

    async def __aenter__(self):
        try:
            self.token = await self.client.authenticate()
        except BaseException:
            await self.client.aclose()
            raise
        return self

    async def __aexit__(self, *args):
        try:
            if self.token:
                await self.client.logout(self.token)
        finally:
            await self.client.aclose()

    async def get(self, employee_id: UUID):
        return await self.request(employee_id)

    async def save(self, employee_id: UUID, fields: dict):
        return await self.request(employee_id, fields)

    async def request(self, employee_id: UUID, fields=None):
        method = "GET" if fields is None else "POST"
        base = str(self.settings.iiko_base_url).rstrip("/")
        url = f"{base}/employees/byId/{UUID(str(employee_id))}"
        headers = {"Cookie": f"key={self.token}", "Accept": "application/xml"}
        content = None
        if fields is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
            content = urlencode(fields, doseq=True).encode()
        try:
            async with (
                asyncio.timeout(30),
                self.client._http.stream(
                    method, url, headers=headers, content=content, timeout=30
                ) as response,
            ):
                if response.status_code == 404 and method == "GET":
                    return None
                if response.status_code not in {200, 201}:
                    raise IikoError(
                        "employee_write_rejected",
                        f"iiko отклонил запрос сотрудника (HTTP {response.status_code}). "
                        "Проверьте права учётной записи iiko и значения полей.",
                        upstream_status_code=response.status_code,
                        outcome_unknown=response.status_code >= 500,
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1024 * 1024:
                        raise IikoError(
                            "employee_response_large",
                            "Ответ iiko слишком большой.",
                            outcome_unknown=method == "POST",
                        )
                return bytes(body)
        except (httpx.RequestError, TimeoutError):
            raise IikoError(
                "employee_connection_failed",
                "Не удалось получить ответ iiko.",
                outcome_unknown=method == "POST",
            ) from None
