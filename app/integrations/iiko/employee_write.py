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


async def rejection(response: httpx.Response) -> IikoError:
    status = response.status_code
    if status == 409:
        # Only recognize a verified fixed response. Never expose arbitrary upstream
        # text: employee errors can include credentials or personal data.
        body = bytearray()
        try:
            async with asyncio.timeout(2):
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 4096:
                        break
                    body.extend(chunk)
                else:
                    if bytes(body).decode("utf-8", errors="replace").strip() == (
                        "Указанный ПИН-код уже существует."
                    ):
                        return IikoError(
                            "employee_pin_conflict",
                            "ПИН-код сотрудника уже используется в iiko. "
                            "Укажите другой PIN-код и сохраните изменения ещё раз.",
                            status_code=409,
                            upstream_status_code=409,
                        )
        except (httpx.RequestError, TimeoutError):
            pass  # The received 409 already confirms rejection, even without a body.
        return IikoError(
            "employee_write_rejected",
            "iiko отклонил изменения сотрудника из-за конфликта данных (HTTP 409). "
            "Проверьте значения полей в карточке iiko.",
            status_code=409,
            upstream_status_code=409,
        )
    return IikoError(
        "employee_write_rejected",
        f"iiko отклонил запрос сотрудника (HTTP {status}). "
        "Проверьте права учётной записи iiko и значения полей.",
        upstream_status_code=status,
        outcome_unknown=status >= 500,
    )


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
                    raise await rejection(response)
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
